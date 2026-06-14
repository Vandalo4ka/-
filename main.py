import os
import json
import base64
import uuid
import re
import time
import threading
import urllib.parse
import urllib.request
from datetime import datetime
from zoneinfo import ZoneInfo

import gspread
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from google.oauth2.service_account import Credentials
from pydantic import BaseModel


SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
TELEGRAM_BOT_USERNAME = os.getenv("TELEGRAM_BOT_USERNAME")
TIMEZONE = os.getenv("TIMEZONE", "Europe/Madrid")
ADMIN_KEY = os.getenv("ADMIN_KEY", "").strip()

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
CACHE_TTL_SECONDS = 60
REMINDER_CHECK_SECONDS = 600

DEFAULT_TIME_SLOTS = ["09:00", "10:00", "11:00", "12:00", "13:00", "14:00", "15:00", "16:00", "17:00", "18:00"]
RU_MONTHS = ["январь", "февраль", "март", "апрель", "май", "июнь", "июль", "август", "сентябрь", "октябрь", "ноябрь", "декабрь"]
RU_WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]

DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")
TIME_LINE_RE = re.compile(r"^(\d{1,2}:\d{2})\s*(.*)$")

_cache = {"services": {"time": 0, "data": None}, "dates": {"time": 0, "data": None}, "slots": {}}
_reminder_thread_started = False

app = FastAPI(title="Manicure Booking API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


class BookingRequest(BaseModel):
    client_name: str
    notes: str | None = ""
    service_id: str
    date: str
    time: str


class CancelBookingRequest(BaseModel):
    booking_id: str


class AdminSlotUpdateRequest(BaseModel):
    date: str
    time: str


class AdminDayUpdateRequest(BaseModel):
    date: str


class AdminMoveBookingRequest(BaseModel):
    booking_id: str
    new_date: str
    new_time: str


class AdminChangeFreeTimeRequest(BaseModel):
    date: str
    old_time: str
    new_time: str


def check_admin_key(key: str | None = None):
    expected = ADMIN_KEY.strip()
    if not expected:
        return
    provided = (key or '').strip()
    if provided != expected:
        raise HTTPException(status_code=403, detail="Invalid admin key")


def status_type_from_text(status_text: str) -> str:
    value = str(status_text or '').strip().casefold()
    if value in ['', 'free', 'свободно']:
        return 'free'
    if value == 'blocked':
        return 'blocked'
    return 'booked'


def parse_booking_preview(status_text: str):
    raw = str(status_text or '').strip()
    parts = [p.strip() for p in raw.split('—')]
    if len(parts) >= 3:
        return {'client_name': parts[0], 'service_name': parts[1], 'price': parts[2]}
    if len(parts) == 2:
        return {'client_name': parts[0], 'service_name': parts[1], 'price': ''}
    return {'client_name': '', 'service_name': raw, 'price': ''}


def find_active_booking_by_date_time(bookings_ws, date_text: str, time_text: str):
    target_date = normalize_date(date_text)
    target_time = normalize_time(time_text)
    for row_number, row in enumerate(bookings_ws.get_all_records(), start=2):
        if normalize_date(str(row.get('date', ''))) != target_date:
            continue
        if normalize_time(str(row.get('time', ''))) != target_time:
            continue
        status = str(row.get('status', '')).strip().casefold()
        if status in ['cancelled', 'canceled', 'отменено']:
            continue
        return row_number, row
    return None, None


def update_calendar_time_status(date_text: str, time_text: str, new_status: str, require_current_type: str | None = None):
    worksheet = get_calendar_worksheet_for_date(date_text)
    row_index, col_index, cell_text = find_calendar_cell_by_date(worksheet, date_text)
    if not row_index or not col_index:
        raise HTTPException(status_code=404, detail='Date not found in schedule')

    line_index, current_status = find_time_in_cell(cell_text, time_text)
    if line_index is None:
        raise HTTPException(status_code=404, detail='Time not found in schedule')

    current_type = status_type_from_text(current_status)
    if require_current_type and current_type != require_current_type:
        raise HTTPException(status_code=409, detail=f'Time is currently {current_type}')

    worksheet.update_cell(row_index, col_index, replace_time_line(cell_text, time_text, new_status))
    clear_cache()
    return {'date': normalize_date(date_text), 'time': normalize_time(time_text), 'from': current_type, 'to': status_type_from_text(new_status), 'raw_status': new_status}


def list_upcoming_dates(limit: int = 21):
    try:
        dates = get_dates()
        return dates[:limit]
    except Exception:
        return []


def readable_error(exc: Exception) -> str:
    message = str(exc)
    return f"{type(exc).__name__}: {message}" if message else f"{type(exc).__name__}: {repr(exc)}"


def cache_is_valid(cache_item: dict) -> bool:
    return cache_item.get("data") is not None and time.time() - cache_item.get("time", 0) < CACHE_TTL_SECONDS


def set_cache(key: str, data):
    _cache[key]["data"] = data
    _cache[key]["time"] = time.time()


def clear_cache():
    _cache["services"] = {"time": 0, "data": None}
    _cache["dates"] = {"time": 0, "data": None}
    _cache["slots"] = {}


def load_credentials_info():
    credentials_b64 = os.getenv("GOOGLE_CREDENTIALS_B64")
    credentials_json = os.getenv("GOOGLE_CREDENTIALS_JSON")

    if credentials_b64:
        try:
            decoded = base64.b64decode(credentials_b64).decode("utf-8")
            return json.loads(decoded)
        except Exception as exc:
            raise RuntimeError(f"Invalid GOOGLE_CREDENTIALS_B64: {readable_error(exc)}")

    if credentials_json:
        try:
            return json.loads(credentials_json)
        except Exception as exc:
            raise RuntimeError(f"Invalid GOOGLE_CREDENTIALS_JSON: {readable_error(exc)}")

    raise RuntimeError("No credentials found")


def get_credentials():
    try:
        return Credentials.from_service_account_info(load_credentials_info(), scopes=SCOPES)
    except Exception as exc:
        raise RuntimeError(f"Cannot create Google credentials: {readable_error(exc)}")


def get_spreadsheet():
    try:
        if not SPREADSHEET_ID:
            raise RuntimeError("SPREADSHEET_ID is missing")
        client = gspread.authorize(get_credentials())
        return client.open_by_key(SPREADSHEET_ID)
    except Exception as exc:
        raise RuntimeError(f"Cannot open spreadsheet: {readable_error(exc)}")


def worksheet_records(sheet_name: str):
    try:
        return get_spreadsheet().worksheet(sheet_name).get_all_records()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=readable_error(exc))


def normalize_date(value):
    return str(value).strip()


def normalize_time(value):
    value = str(value).strip()
    if not value:
        return ""
    if len(value) >= 5 and value[2] == ":":
        return value[:5]
    if len(value) == 4 and value[1] == ":":
        return "0" + value
    return value


def normalize_price(value):
    if value is None:
        return ""
    raw = str(value).strip().replace("€", "").replace(" ", "")
    if raw == "":
        return ""

    if isinstance(value, (int, float)):
        number = float(value)
        if number >= 100 and number % 100 == 0:
            number = number / 100
        return str(int(number)) if number.is_integer() else str(number).rstrip("0").rstrip(".")

    normalized = raw.replace(",", ".")
    try:
        number = float(normalized)
        if "," not in raw and "." not in raw and number >= 100 and number % 100 == 0:
            number = number / 100
        return str(int(number)) if number.is_integer() else str(number).rstrip("0").rstrip(".")
    except Exception:
        return raw


def parse_date(date_text: str) -> datetime:
    try:
        return datetime.strptime(normalize_date(date_text), "%Y-%m-%d")
    except ValueError:
        raise RuntimeError("Date must be in YYYY-MM-DD format")


def current_local_datetime():
    return datetime.now(ZoneInfo(TIMEZONE))


def is_past_date(date_text: str) -> bool:
    date_only = parse_date(date_text).date()
    today = current_local_datetime().date()
    return date_only < today


def is_past_or_current_time_for_today(date_text: str, time_text: str) -> bool:
    date_only = parse_date(date_text).date()
    now = current_local_datetime()

    if date_only != now.date():
        return False

    try:
        slot_time = datetime.strptime(normalize_time(time_text), "%H:%M").time()
    except ValueError:
        return True

    # Do not offer times that have already started or are exactly now.
    return slot_time <= now.time()


def is_weekend(date_text: str) -> bool:
    return parse_date(date_text).weekday() >= 5


def month_sheet_name(year: int, month: int) -> str:
    return f"{RU_MONTHS[month - 1]} {year}"


def sheet_name_for_date(date_text: str) -> str:
    dt = parse_date(date_text)
    return month_sheet_name(dt.year, dt.month)


def is_ru_month_sheet_name(name: str) -> bool:
    parts = str(name).strip().casefold().split()
    return len(parts) == 2 and parts[0] in RU_MONTHS and parts[1].isdigit() and len(parts[1]) == 4


def month_sort_key(name: str):
    parts = str(name).strip().casefold().split()
    return int(parts[1]), RU_MONTHS.index(parts[0]) + 1


def add_months(year: int, month: int, offset: int):
    month_index = (year * 12 + (month - 1)) + offset
    return month_index // 12, month_index % 12 + 1


def days_in_month(year: int, month: int) -> int:
    if month == 12:
        next_month = datetime(year + 1, 1, 1)
    else:
        next_month = datetime(year, month + 1, 1)
    this_month = datetime(year, month, 1)
    return (next_month - this_month).days


def build_day_cell(dt: datetime) -> str:
    lines = [f"{dt.strftime('%Y-%m-%d')} {RU_WEEKDAYS[dt.weekday()]}"]
    lines.extend([f"{slot} free" for slot in DEFAULT_TIME_SLOTS])
    return "\n".join(lines)


def create_calendar_month_if_missing(spreadsheet, year: int, month: int):
    sheet_name = month_sheet_name(year, month)
    try:
        return spreadsheet.worksheet(sheet_name)
    except Exception:
        pass

    worksheet = spreadsheet.add_worksheet(title=sheet_name, rows=8, cols=5)
    values = [["Пн", "Вт", "Ср", "Чт", "Пт"]]
    weeks = []
    current_week = ["", "", "", "", ""]

    for day in range(1, days_in_month(year, month) + 1):
        dt = datetime(year, month, day)
        if dt.weekday() >= 5:
            continue

        col = dt.weekday()
        if col == 0 and any(current_week):
            weeks.append(current_week)
            current_week = ["", "", "", "", ""]

        current_week[col] = build_day_cell(dt)

    if any(current_week):
        weeks.append(current_week)

    values.extend(weeks)
    worksheet.update("A1", values)

    try:
        worksheet.freeze(rows=1)
        worksheet.format("A1:E1", {
            "backgroundColor": {"red": 0.93, "green": 0.93, "blue": 0.93},
            "horizontalAlignment": "CENTER",
            "verticalAlignment": "MIDDLE",
            "textFormat": {"bold": True},
        })
        worksheet.format(f"A2:E{len(values)}", {
            "horizontalAlignment": "LEFT",
            "verticalAlignment": "TOP",
            "wrapStrategy": "WRAP",
        })
    except Exception:
        pass

    return worksheet


def ensure_calendar_months_ahead(months_ahead: int = 3):
    spreadsheet = get_spreadsheet()
    today = datetime.now(ZoneInfo(TIMEZONE))
    for offset in range(months_ahead):
        year, month = add_months(today.year, today.month, offset)
        create_calendar_month_if_missing(spreadsheet, year, month)


def get_calendar_worksheets():
    ensure_calendar_months_ahead(3)
    worksheets = [ws for ws in get_spreadsheet().worksheets() if is_ru_month_sheet_name(ws.title)]
    return sorted(worksheets, key=lambda ws: month_sort_key(ws.title))


def get_calendar_worksheet_for_date(date_text: str):
    dt = parse_date(date_text)
    if dt.weekday() >= 5:
        raise RuntimeError("Weekend dates are not available")
    spreadsheet = get_spreadsheet()
    sheet_name = sheet_name_for_date(date_text)
    try:
        return spreadsheet.worksheet(sheet_name)
    except Exception:
        return create_calendar_month_if_missing(spreadsheet, dt.year, dt.month)


def extract_date_from_cell(cell_text: str):
    match = DATE_RE.search(str(cell_text))
    return match.group(0) if match else ""


def parse_day_cell(cell_text: str):
    lines = str(cell_text or "").splitlines()
    date_text = extract_date_from_cell(cell_text)
    time_lines = []
    for line in lines:
        match = TIME_LINE_RE.match(line.strip())
        if match:
            time_lines.append({
                "time": normalize_time(match.group(1)),
                "status": match.group(2).strip(),
                "raw": line.strip(),
            })
    return date_text, time_lines


def is_time_free(status_text: str):
    return str(status_text or "").strip().casefold() in ["", "free", "свободно"]


def find_calendar_cell_by_date(worksheet, date_text: str):
    values = worksheet.get_all_values()
    for row_index, row in enumerate(values, start=1):
        for col_index, cell in enumerate(row, start=1):
            if extract_date_from_cell(cell) == normalize_date(date_text):
                return row_index, col_index, str(cell)
    return None, None, ""


def find_time_in_cell(cell_text: str, time_text: str):
    lines = str(cell_text or "").splitlines()
    target_time = normalize_time(time_text)
    for index, line in enumerate(lines):
        match = TIME_LINE_RE.match(line.strip())
        if match and normalize_time(match.group(1)) == target_time:
            return index, match.group(2).strip()
    return None, ""


def replace_time_line(cell_text: str, time_text: str, new_status: str):
    lines = str(cell_text or "").splitlines()
    target_time = normalize_time(time_text)

    for index, line in enumerate(lines):
        match = TIME_LINE_RE.match(line.strip())
        if match and normalize_time(match.group(1)) == target_time:
            lines[index] = f"{target_time} {new_status}".rstrip()
            return "\n".join(lines)

    lines.append(f"{target_time} {new_status}".rstrip())
    return "\n".join(lines)


def send_telegram_message(chat_id: str, text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode("utf-8")
        request = urllib.request.Request(url, data=payload, method="POST")
        with urllib.request.urlopen(request, timeout=8) as response:
            response.read()
        return True
    except Exception:
        return False


def send_master_notification(text: str) -> bool:
    return send_telegram_message(TELEGRAM_CHAT_ID, text) if TELEGRAM_CHAT_ID else False


def make_telegram_reminder_url(booking_id: str) -> str | None:
    if not TELEGRAM_BOT_USERNAME:
        return None
    username = TELEGRAM_BOT_USERNAME.lstrip("@").strip()
    return f"https://t.me/{username}?start={booking_id}" if username else None


def get_header_map(worksheet):
    headers = worksheet.row_values(1)
    return {str(header).strip(): index for index, header in enumerate(headers, start=1)}


def update_columns_by_header(worksheet, row_number: int, updates: dict):
    header_map = get_header_map(worksheet)
    for column_name, value in updates.items():
        col = header_map.get(column_name)
        if col:
            worksheet.update_cell(row_number, col, value)


def get_or_create_clients_worksheet(spreadsheet):
    headers = ["created_at", "booking_id", "client_name", "service_name", "date", "time", "price", "notes", "status"]
    try:
        worksheet = spreadsheet.worksheet("clients")
    except Exception:
        worksheet = spreadsheet.add_worksheet(title="clients", rows=1000, cols=len(headers))
        worksheet.update("A1", [headers])

    if not worksheet.row_values(1):
        worksheet.update("A1", [headers])
    return worksheet


def append_client_visit(spreadsheet, created_at, booking_id, client_name, service_name, date_text, time_text, price, notes):
    clients_ws = get_or_create_clients_worksheet(spreadsheet)
    clients_ws.append_row([created_at, booking_id, client_name, service_name, date_text, time_text, price, notes or "", "new"])


def update_client_visit_status(spreadsheet, booking_id: str, status: str):
    try:
        clients_ws = get_or_create_clients_worksheet(spreadsheet)
        for row_number, row in enumerate(clients_ws.get_all_records(), start=2):
            if str(row.get("booking_id", "")).strip() == str(booking_id).strip():
                update_columns_by_header(clients_ws, row_number, {"status": status})
                return
    except Exception:
        pass


def find_booking_by_id(bookings_ws, booking_id: str):
    for row_number, row in enumerate(bookings_ws.get_all_records(), start=2):
        if str(row.get("booking_id", "")).strip() == str(booking_id).strip():
            return row_number, row
    return None, None


def free_calendar_cell_for_booking(date_text: str, time_text: str):
    worksheet = get_calendar_worksheet_for_date(date_text)
    row_index, col_index, cell_text = find_calendar_cell_by_date(worksheet, date_text)
    if row_index and col_index:
        worksheet.update_cell(row_index, col_index, replace_time_line(cell_text, time_text, "free"))


def parse_appointment_datetime(date_text: str, time_text: str):
    tz = ZoneInfo(TIMEZONE)
    raw = f"{date_text.strip()} {time_text.strip()}"
    for fmt in ["%Y-%m-%d %H:%M", "%d.%m.%Y %H:%M", "%d/%m/%Y %H:%M"]:
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=tz)
        except ValueError:
            pass
    return None


def process_due_reminders():
    if not TELEGRAM_BOT_TOKEN:
        return

    spreadsheet = get_spreadsheet()
    bookings_ws = spreadsheet.worksheet("bookings")
    rows = bookings_ws.get_all_records()
    now = datetime.now(ZoneInfo(TIMEZONE))

    for row_number, row in enumerate(rows, start=2):
        reminder_requested = str(row.get("reminder_requested", "")).strip().casefold()
        reminder_sent = str(row.get("reminder_sent", "")).strip().casefold()
        chat_id = str(row.get("telegram_chat_id", "")).strip()
        status = str(row.get("status", "")).strip().casefold()

        if reminder_requested not in ["yes", "true", "1", "да"]:
            continue
        if reminder_sent in ["yes", "true", "1", "да", "cancelled"]:
            continue
        if not chat_id:
            continue
        if status in ["cancelled", "canceled", "отменено"]:
            continue

        appointment_dt = parse_appointment_datetime(str(row.get("date", "")), str(row.get("time", "")))
        if not appointment_dt:
            continue

        seconds_left = (appointment_dt - now).total_seconds()
        if 0 < seconds_left <= 24 * 60 * 60:
            text = (
                "Напоминание о записи\n\n"
                f"Услуга: {row.get('service_name', '')}\n"
                f"Дата: {row.get('date', '')}\n"
                f"Время: {row.get('time', '')}\n\n"
                "Ждём вас!"
            )
            if send_telegram_message(chat_id, text):
                update_columns_by_header(bookings_ws, row_number, {"reminder_sent": "yes"})


def reminder_loop():
    while True:
        try:
            process_due_reminders()
        except Exception:
            pass
        time.sleep(REMINDER_CHECK_SECONDS)


@app.on_event("startup")
def startup_event():
    global _reminder_thread_started
    if not _reminder_thread_started:
        threading.Thread(target=reminder_loop, daemon=True).start()
        _reminder_thread_started = True


@app.get("/")
def index():
    return FileResponse("index.html")


@app.get("/admin")
def admin_page():
    return FileResponse("admin.html")



@app.get("/cancel")
def cancel_page(booking_id: str = Query(...)):
    safe_booking_id = str(booking_id).strip()
    html = f"""
    <!DOCTYPE html>
    <html lang="ru">
    <head>
      <meta charset="UTF-8">
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
      <title>Отмена записи</title>
      <style>
        body {{ margin: 0; min-height: 100vh; background: #f3ede5; color: #6b4b43; font-family: Arial, sans-serif; display: flex; align-items: center; justify-content: center; padding: 20px; }}
        .card {{ width: 100%; max-width: 520px; background: #fff; border-radius: 22px; padding: 30px; box-shadow: 0 18px 40px rgba(92, 62, 50, 0.10); text-align: center; }}
        h1 {{ font-family: Georgia, "Times New Roman", serif; font-weight: 400; margin-top: 0; }}
        .summary {{ background: #fbf1f3; border: 1px solid #eadfda; border-radius: 16px; padding: 16px; margin: 20px 0; line-height: 1.6; }}
        button {{ width: 100%; border: none; border-radius: 14px; padding: 16px; font-size: 16px; font-weight: 700; cursor: pointer; background: #c98091; color: white; }}
        .message {{ margin-top: 18px; line-height: 1.6; }}
      </style>
    </head>
    <body>
      <div class="card">
        <h1>Отмена записи</h1>
        <div class="summary">Номер записи:<br><b>{safe_booking_id}</b></div>
        <button id="cancelBtn">Отменить запись</button>
        <div class="message" id="message"></div>
      </div>
      <script>
        const bookingId = {json.dumps(safe_booking_id)};
        document.getElementById("cancelBtn").onclick = async () => {{
          if (!confirm("Точно отменить запись? Это время снова станет доступным для записи.")) return;
          const btn = document.getElementById("cancelBtn");
          const message = document.getElementById("message");
          btn.disabled = true;
          btn.textContent = "Отменяем...";
          try {{
            const response = await fetch("/api/bookings/cancel", {{
              method: "POST",
              headers: {{ "Content-Type": "application/json" }},
              body: JSON.stringify({{ booking_id: bookingId }})
            }});
            const data = await response.json();
            if (!response.ok) throw new Error(data.detail || "Ошибка сервера");
            btn.style.display = "none";
            message.innerHTML = "Запись отменена.<br>Это время снова доступно для записи.";
          }} catch (error) {{
            btn.disabled = false;
            btn.textContent = "Отменить запись";
            message.textContent = "Не удалось отменить запись. " + error.message;
          }}
        }};
      </script>
    </body>
    </html>
    """
    return HTMLResponse(html)


@app.get("/health")
def health():
    return {
        "status": "ok",
        "spreadsheet_id_present": bool(os.getenv("SPREADSHEET_ID")),
        "credentials_b64_present": bool(os.getenv("GOOGLE_CREDENTIALS_B64")),
        "credentials_json_present": bool(os.getenv("GOOGLE_CREDENTIALS_JSON")),
        "telegram_bot_token_present": bool(os.getenv("TELEGRAM_BOT_TOKEN")),
        "telegram_chat_id_present": bool(os.getenv("TELEGRAM_CHAT_ID")),
        "telegram_bot_username_present": bool(os.getenv("TELEGRAM_BOT_USERNAME")),
        "timezone": TIMEZONE,
        "cache_ttl_seconds": CACHE_TTL_SECONDS,
        "schedule_mode": "calendar_grid",
        "past_dates_hidden": True,
        "past_times_today_hidden": True,
        "admin_key_present": bool(ADMIN_KEY),
    }


@app.get("/debug/schedule")
def debug_schedule():
    try:
        worksheets = get_calendar_worksheets()
        return {"calendar_sheets": [ws.title for ws in worksheets], "count": len(worksheets), "expected_format": "Пн-Вт-Ср-Чт-Пт calendar grid"}
    except Exception as exc:
        raise HTTPException(status_code=500, detail=readable_error(exc))


@app.get("/debug/cache")
def debug_cache():
    return {"ttl_seconds": CACHE_TTL_SECONDS, "services_cached": cache_is_valid(_cache["services"]), "dates_cached": cache_is_valid(_cache["dates"]), "slots_cached_dates": list(_cache["slots"].keys())}


@app.post("/debug/cache/clear")
def debug_cache_clear():
    clear_cache()
    return {"status": "cache cleared"}


@app.post("/debug/reminders/check")
def debug_reminders_check():
    process_due_reminders()
    return {"status": "checked"}


@app.post("/telegram/webhook")
async def telegram_webhook(request: Request):
    update = await request.json()
    message = update.get("message") or {}
    chat = message.get("chat") or {}
    text = str(message.get("text") or "").strip()
    chat_id = str(chat.get("id") or "")

    if not chat_id or not text.startswith("/start"):
        return {"ok": True}

    parts = text.split(maxsplit=1)
    booking_id = parts[1].strip() if len(parts) > 1 else ""

    if not booking_id:
        send_telegram_message(chat_id, "Чтобы подключить напоминание, нажмите кнопку на странице записи.")
        return {"ok": True}

    try:
        spreadsheet = get_spreadsheet()
        bookings_ws = spreadsheet.worksheet("bookings")
        for row_number, row in enumerate(bookings_ws.get_all_records(), start=2):
            if str(row.get("booking_id", "")).strip() == booking_id:
                update_columns_by_header(bookings_ws, row_number, {"telegram_chat_id": chat_id, "reminder_requested": "yes", "reminder_sent": "no"})
                send_telegram_message(chat_id, f"Напоминание подключено.\n\nЗапись: {row.get('service_name', '')}\nДата: {row.get('date', '')}\nВремя: {row.get('time', '')}\n\nМы напомним вам за сутки.")
                return {"ok": True}
        send_telegram_message(chat_id, "Запись не найдена. Попробуйте открыть кнопку напоминания ещё раз.")
        return {"ok": True}
    except Exception:
        send_telegram_message(chat_id, "Не удалось подключить напоминание. Попробуйте позже.")
        return {"ok": True}



def parse_admin_calendar_day_items(cell_text: str):
    """
    Parses day cell lines such as:
    2026-06-15 Пн
    09:30 Анна
    маникюр
    16:30 Olga
    маникюр/педикюр
    18:30 free

    Returns time items where non-time lines after a booked time are service/details.
    """
    lines = [str(line or "").strip() for line in str(cell_text or "").splitlines()]
    lines = [line for line in lines if line]

    items = []
    current = None

    for line in lines:
        match = TIME_LINE_RE.match(line)

        if match:
            if current:
                items.append(current)

            time_text = normalize_time(match.group(1))
            status_text = match.group(2).strip()

            current = {
                "time": time_text,
                "status": status_text,
                "details": [],
            }
            continue

        if current and not DATE_RE.search(line):
            current["details"].append(line)

    if current:
        items.append(current)

    return items


def split_admin_booking_status(status_text: str, details: list[str] | None = None):
    raw = str(status_text or "").strip()
    details = [str(x or "").strip() for x in (details or []) if str(x or "").strip()]

    parts = [part.strip() for part in raw.split("—") if part.strip()]

    if len(parts) >= 3:
        return {
            "client_name": parts[0],
            "service_name": " — ".join(parts[1:-1]),
            "price": parts[-1],
        }

    if len(parts) == 2:
        return {
            "client_name": parts[0],
            "service_name": parts[1],
            "price": "",
        }

    if details:
        return {
            "client_name": raw,
            "service_name": " ".join(details),
            "price": "",
        }

    return {
        "client_name": raw,
        "service_name": "",
        "price": "",
    }



@app.get("/api/admin/dates")
def admin_dates(key: str | None = Query(default=None)):
    check_admin_key(key)
    return list_upcoming_dates(31)


@app.get("/api/admin/schedule")
def admin_schedule(date: str = Query(...), key: str | None = Query(default=None)):
    check_admin_key(key)
    try:
        worksheet = get_calendar_worksheet_for_date(date)
        row_index, col_index, cell_text = find_calendar_cell_by_date(worksheet, date)
        if not row_index or not col_index:
            raise HTTPException(status_code=404, detail='Date not found')

        parsed_items = parse_admin_calendar_day_items(cell_text)
        spreadsheet = get_spreadsheet()
        bookings_ws = spreadsheet.worksheet('bookings')
        items = []

        for item in sorted(parsed_items, key=lambda x: normalize_time(x["time"])):
            slot_type = status_type_from_text(item["status"])
            payload = {
                'time': normalize_time(item["time"]),
                'type': slot_type,
                'raw_status': item["status"],
                'date': normalize_date(date),
            }

            if slot_type == 'booked':
                _, booking = find_active_booking_by_date_time(bookings_ws, date, item["time"])
                preview = split_admin_booking_status(item["status"], item.get("details") or [])
                payload.update(preview)

                # bookings sheet has priority if present, because it is the most structured source.
                if booking:
                    booking_client = str(booking.get('client_name', '')).strip()
                    booking_service = str(booking.get('service_name', '')).strip()
                    if booking_client:
                        payload['client_name'] = booking_client
                    if booking_service:
                        payload['service_name'] = booking_service
                    payload.update({
                        'booking_id': str(booking.get('booking_id', '')).strip(),
                        'notes': str(booking.get('notes', '')).strip(),
                        'status': str(booking.get('status', '')).strip(),
                    })

            items.append(payload)

        return {'date': normalize_date(date), 'items': items}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=readable_error(exc))


@app.post("/api/admin/block")
def admin_block(request: AdminSlotUpdateRequest, key: str | None = Query(default=None)):
    check_admin_key(key)
    try:
        return update_calendar_time_status(request.date, request.time, 'blocked', require_current_type='free')
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=readable_error(exc))


@app.post("/api/admin/unblock")
def admin_unblock(request: AdminSlotUpdateRequest, key: str | None = Query(default=None)):
    check_admin_key(key)
    try:
        return update_calendar_time_status(request.date, request.time, 'free', require_current_type='blocked')
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=readable_error(exc))


@app.post("/api/admin/close-day")
def admin_close_day(request: AdminDayUpdateRequest, key: str | None = Query(default=None)):
    check_admin_key(key)
    try:
        worksheet = get_calendar_worksheet_for_date(request.date)
        row_index, col_index, cell_text = find_calendar_cell_by_date(worksheet, request.date)
        if not row_index or not col_index:
            raise HTTPException(status_code=404, detail='Date not found')
        _, time_lines = parse_day_cell(cell_text)
        updated = cell_text
        for item in time_lines:
            if status_type_from_text(item['status']) == 'free':
                updated = replace_time_line(updated, item['time'], 'blocked')
        worksheet.update_cell(row_index, col_index, updated)
        clear_cache()
        return {'status': 'ok', 'date': normalize_date(request.date), 'action': 'closed'}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=readable_error(exc))


@app.post("/api/admin/open-day")
def admin_open_day(request: AdminDayUpdateRequest, key: str | None = Query(default=None)):
    check_admin_key(key)
    try:
        worksheet = get_calendar_worksheet_for_date(request.date)
        row_index, col_index, cell_text = find_calendar_cell_by_date(worksheet, request.date)
        if not row_index or not col_index:
            raise HTTPException(status_code=404, detail='Date not found')
        _, time_lines = parse_day_cell(cell_text)
        updated = cell_text
        for item in time_lines:
            if status_type_from_text(item['status']) == 'blocked':
                updated = replace_time_line(updated, item['time'], 'free')
        worksheet.update_cell(row_index, col_index, updated)
        clear_cache()
        return {'status': 'ok', 'date': normalize_date(request.date), 'action': 'opened'}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=readable_error(exc))


@app.post("/api/admin/change-free-time")
def admin_change_free_time(request: AdminChangeFreeTimeRequest, key: str | None = Query(default=None)):
    check_admin_key(key)

    try:
        date_text = normalize_date(request.date)
        old_time = normalize_time(request.old_time)
        new_time = normalize_time(request.new_time)

        if not new_time:
            raise HTTPException(status_code=400, detail="New time is required")

        if is_past_date(date_text) or is_past_or_current_time_for_today(date_text, new_time):
            raise HTTPException(status_code=409, detail="This time is no longer available")

        worksheet = get_calendar_worksheet_for_date(date_text)
        row_index, col_index, cell_text = find_calendar_cell_by_date(worksheet, date_text)

        if not row_index or not col_index:
            raise HTTPException(status_code=404, detail="Date not found in schedule")

        old_index, old_status = find_time_in_cell(cell_text, old_time)

        if old_index is None:
            raise HTTPException(status_code=404, detail="Old time not found")

        old_type = status_type_from_text(old_status)

        # Allow changing only free or blocked slots, not occupied bookings.
        if old_type == "booked":
            raise HTTPException(status_code=409, detail="Booked slot time cannot be changed here")

        existing_index, existing_status = find_time_in_cell(cell_text, new_time)

        if existing_index is not None and existing_index != old_index:
            raise HTTPException(status_code=409, detail="This time already exists")

        lines = str(cell_text or "").splitlines()
        lines[old_index] = f"{new_time} {old_status}".rstrip()

        # Keep date header first, then sort time lines by time.
        header_lines = []
        time_lines = []

        for line in lines:
            match = TIME_LINE_RE.match(str(line).strip())
            if match:
                time_lines.append(str(line).strip())
            else:
                header_lines.append(str(line))

        time_lines = sorted(
            time_lines,
            key=lambda line: normalize_time(TIME_LINE_RE.match(line).group(1))
        )

        new_cell_text = "\n".join(header_lines + time_lines)
        worksheet.update_cell(row_index, col_index, new_cell_text)

        clear_cache()

        return {
            "status": "changed",
            "date": date_text,
            "old_time": old_time,
            "new_time": new_time,
            "slot_type": old_type,
        }

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=readable_error(exc))


@app.post("/api/admin/move-booking")
def admin_move_booking(request: AdminMoveBookingRequest, key: str | None = Query(default=None)):
    check_admin_key(key)

    try:
        booking_id = str(request.booking_id).strip()
        new_date = normalize_date(request.new_date)
        new_time = normalize_time(request.new_time)

        if not booking_id:
            raise HTTPException(status_code=400, detail="Booking ID is required")

        if is_weekend(new_date):
            raise HTTPException(status_code=409, detail="Weekend dates are not available")

        if is_past_date(new_date) or is_past_or_current_time_for_today(new_date, new_time):
            raise HTTPException(status_code=409, detail="This time is no longer available")

        spreadsheet = get_spreadsheet()
        bookings_ws = spreadsheet.worksheet("bookings")
        row_number, booking = find_booking_by_id(bookings_ws, booking_id)

        if not row_number:
            raise HTTPException(status_code=404, detail="Booking not found")

        current_status = str(booking.get("status", "")).strip().casefold()
        if current_status in ["cancelled", "canceled", "отменено"]:
            raise HTTPException(status_code=409, detail="This booking is already cancelled")

        old_date = normalize_date(str(booking.get("date", "")))
        old_time = normalize_time(str(booking.get("time", "")))

        if old_date == new_date and old_time == new_time:
            return {
                "status": "unchanged",
                "booking_id": booking_id,
                "date": new_date,
                "time": new_time,
            }

        # Check that the new time is free.
        new_calendar_ws = get_calendar_worksheet_for_date(new_date)
        new_row_index, new_col_index, new_cell_text = find_calendar_cell_by_date(new_calendar_ws, new_date)

        if not new_row_index or not new_col_index:
            raise HTTPException(status_code=404, detail="New date not found in schedule")

        new_time_line_index, new_current_status = find_time_in_cell(new_cell_text, new_time)

        if new_time_line_index is None:
            raise HTTPException(status_code=404, detail="New time not found in schedule")

        if not is_time_free(new_current_status):
            raise HTTPException(status_code=409, detail="New time is not free")

        client_name = str(booking.get("client_name", "")).strip()
        service_name = str(booking.get("service_name", "")).strip()

        # Price may not be stored in bookings, so try to get it from clients first.
        price = ""
        try:
            clients_ws = get_or_create_clients_worksheet(spreadsheet)
            for client_row in clients_ws.get_all_records():
                if str(client_row.get("booking_id", "")).strip() == booking_id:
                    price = normalize_price(client_row.get("price", ""))
                    break
        except Exception:
            price = ""

        booked_text = f"{client_name} — {service_name}"
        if price:
            booked_text += f" — {price} €"

        # Free old calendar slot.
        try:
            old_calendar_ws = get_calendar_worksheet_for_date(old_date)
            old_row_index, old_col_index, old_cell_text = find_calendar_cell_by_date(old_calendar_ws, old_date)
            if old_row_index and old_col_index:
                old_updated_cell = replace_time_line(old_cell_text, old_time, "free")
                old_calendar_ws.update_cell(old_row_index, old_col_index, old_updated_cell)
        except Exception:
            pass

        # Book new calendar slot.
        new_updated_cell = replace_time_line(new_cell_text, new_time, booked_text)
        new_calendar_ws.update_cell(new_row_index, new_col_index, new_updated_cell)

        # Update bookings.
        update_columns_by_header(bookings_ws, row_number, {
            "date": new_date,
            "time": new_time,
        })

        # Update clients sheet.
        try:
            clients_ws = get_or_create_clients_worksheet(spreadsheet)
            for client_row_number, client_row in enumerate(clients_ws.get_all_records(), start=2):
                if str(client_row.get("booking_id", "")).strip() == booking_id:
                    update_columns_by_header(clients_ws, client_row_number, {
                        "date": new_date,
                        "time": new_time,
                    })
                    break
        except Exception:
            pass

        clear_cache()

        message = (
            "Запись перенесена\\n\\n"
            f"Клиент: {client_name}\\n"
            f"Услуга: {service_name}\\n"
            f"Было: {old_date}, {old_time}\\n"
            f"Стало: {new_date}, {new_time}\\n"
            f"Номер записи: {booking_id}"
        )
        send_master_notification(message)

        client_chat_id = str(booking.get("telegram_chat_id", "")).strip()
        if client_chat_id:
            send_telegram_message(
                client_chat_id,
                "Ваша запись перенесена.\\n\\n"
                f"Услуга: {service_name}\\n"
                f"Новая дата: {new_date}\\n"
                f"Новое время: {new_time}"
            )

        return {
            "status": "moved",
            "booking_id": booking_id,
            "old_date": old_date,
            "old_time": old_time,
            "new_date": new_date,
            "new_time": new_time,
        }

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=readable_error(exc))


@app.post("/api/admin/cancel-slot")
def admin_cancel_slot(request: AdminSlotUpdateRequest, key: str | None = Query(default=None)):
    check_admin_key(key)
    try:
        spreadsheet = get_spreadsheet()
        bookings_ws = spreadsheet.worksheet('bookings')
        row_number, booking = find_active_booking_by_date_time(bookings_ws, request.date, request.time)
        if not row_number:
            raise HTTPException(status_code=404, detail='Active booking not found')
        cancel_booking(CancelBookingRequest(booking_id=str(booking.get('booking_id', '')).strip()))
        return {'status': 'cancelled', 'booking_id': str(booking.get('booking_id', '')).strip()}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=readable_error(exc))


@app.get("/api/services")
def get_services():
    if cache_is_valid(_cache["services"]):
        return _cache["services"]["data"]

    services = []
    for row in worksheet_records("services"):
        is_active = str(row.get("is_active", "")).strip().casefold()
        if is_active in ["true", "1", "yes", "да"]:
            services.append({
                "service_id": str(row.get("service_id", "")).strip(),
                "name": str(row.get("name", "")).strip(),
                "duration_min": str(row.get("duration_min", "")).strip(),
                "price": normalize_price(row.get("price", "")),
                "is_active": is_active,
            })

    set_cache("services", services)
    return services


@app.get("/api/dates")
def get_dates():
    if cache_is_valid(_cache["dates"]):
        return _cache["dates"]["data"]

    try:
        available_dates = []
        for worksheet in get_calendar_worksheets():
            values = worksheet.get_all_values()
            for row in values[1:]:
                for cell in row[:5]:
                    date_text, time_lines = parse_day_cell(cell)
                    if not date_text or is_weekend(date_text) or is_past_date(date_text):
                        continue

                    has_available_future_time = any(
                        is_time_free(item["status"])
                        and not is_past_or_current_time_for_today(date_text, item["time"])
                        for item in time_lines
                    )

                    if has_available_future_time:
                        available_dates.append(date_text)
        available_dates = sorted(set(available_dates))
        set_cache("dates", available_dates)
        return available_dates
    except Exception as exc:
        raise HTTPException(status_code=500, detail=readable_error(exc))


@app.get("/api/slots")
def get_slots(date: str = Query(...)):
    cached = _cache["slots"].get(date)
    if cached and cache_is_valid(cached):
        return cached["data"]

    try:
        if is_weekend(date):
            return []
        worksheet = get_calendar_worksheet_for_date(date)
        row_index, col_index, cell_text = find_calendar_cell_by_date(worksheet, date)
        if not row_index or not col_index:
            return []
        _, time_lines = parse_day_cell(cell_text)
        slots = [
            {
                "slot_id": f"{date}-{item['time']}",
                "date": normalize_date(date),
                "time": item["time"],
                "status": "free",
            }
            for item in time_lines
            if is_time_free(item["status"])
            and not is_past_or_current_time_for_today(date, item["time"])
        ]
        slots = sorted(slots, key=lambda item: item["time"])
        _cache["slots"][date] = {"time": time.time(), "data": slots}
        return slots
    except Exception as exc:
        raise HTTPException(status_code=500, detail=readable_error(exc))


@app.get("/api/bookings/search")
def search_bookings(client_name: str = Query(...)):
    try:
        query = str(client_name).strip().casefold()
        if len(query) < 2:
            raise HTTPException(status_code=400, detail="Введите минимум 2 символа имени")

        rows = get_spreadsheet().worksheet("bookings").get_all_records()
        results = []
        for row in rows:
            row_name = str(row.get("client_name", "")).strip()
            row_status = str(row.get("status", "")).strip().casefold()
            if row_status in ["cancelled", "canceled", "отменено"]:
                continue
            if query not in row_name.casefold():
                continue
            results.append({"booking_id": str(row.get("booking_id", "")).strip(), "client_name": row_name, "service_name": str(row.get("service_name", "")).strip(), "date": str(row.get("date", "")).strip(), "time": str(row.get("time", "")).strip(), "notes": str(row.get("notes", "")).strip(), "status": str(row.get("status", "")).strip()})
        return results
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=readable_error(exc))


@app.post("/api/bookings/cancel")
def cancel_booking(request: CancelBookingRequest):
    try:
        booking_id = str(request.booking_id).strip()
        if not booking_id:
            raise HTTPException(status_code=400, detail="Booking ID is required")

        spreadsheet = get_spreadsheet()
        bookings_ws = spreadsheet.worksheet("bookings")
        row_number, booking = find_booking_by_id(bookings_ws, booking_id)

        if not row_number:
            raise HTTPException(status_code=404, detail="Booking not found")

        current_status = str(booking.get("status", "")).strip().casefold()
        if current_status in ["cancelled", "canceled", "отменено"]:
            return {"status": "already_cancelled", "booking_id": booking_id, "message": "Запись уже отменена"}

        update_columns_by_header(bookings_ws, row_number, {"status": "cancelled", "reminder_sent": "cancelled"})
        free_calendar_cell_for_booking(str(booking.get("date", "")), str(booking.get("time", "")))
        update_client_visit_status(spreadsheet, booking_id, "cancelled")
        clear_cache()

        message = f"Запись отменена\n\nКлиент: {booking.get('client_name', '')}\nУслуга: {booking.get('service_name', '')}\nДата: {booking.get('date', '')}\nВремя: {booking.get('time', '')}\nНомер записи: {booking_id}"
        send_master_notification(message)

        client_chat_id = str(booking.get("telegram_chat_id", "")).strip()
        if client_chat_id:
            send_telegram_message(client_chat_id, f"Ваша запись отменена.\n\nУслуга: {booking.get('service_name', '')}\nДата: {booking.get('date', '')}\nВремя: {booking.get('time', '')}")

        return {"status": "cancelled", "booking_id": booking_id, "message": "Запись отменена"}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=readable_error(exc))


@app.post("/api/bookings")
def create_booking(request: BookingRequest):
    try:
        if is_weekend(request.date):
            raise HTTPException(status_code=409, detail="Weekend dates are not available")

        if is_past_date(request.date) or is_past_or_current_time_for_today(request.date, request.time):
            raise HTTPException(status_code=409, detail="This time is no longer available")

        spreadsheet = get_spreadsheet()
        services_ws = spreadsheet.worksheet("services")
        bookings_ws = spreadsheet.worksheet("bookings")
        calendar_ws = get_calendar_worksheet_for_date(request.date)

        services = services_ws.get_all_records()
        service = next((row for row in services if str(row.get("service_id", "")).strip() == request.service_id and str(row.get("is_active", "")).strip().casefold() in ["true", "1", "yes", "да"]), None)
        if not service:
            raise HTTPException(status_code=404, detail="Service not found")

        row_index, col_index, cell_text = find_calendar_cell_by_date(calendar_ws, request.date)
        if not row_index or not col_index:
            clear_cache()
            raise HTTPException(status_code=409, detail="This date is not available")

        time_line_index, current_status = find_time_in_cell(cell_text, request.time)
        if time_line_index is None or not is_time_free(current_status):
            clear_cache()
            raise HTTPException(status_code=409, detail="This time is no longer available")

        booking_id = "BK-" + uuid.uuid4().hex[:8].upper()
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        service_name = str(service.get("name", ""))
        service_price = normalize_price(service.get("price", ""))

        bookings_ws.append_row([booking_id, created_at, request.client_name, request.service_id, service_name, request.date, request.time, request.notes or "", "new", "", "no", "no"])

        booked_text = f"{request.client_name} — {service_name} — {service_price} €"
        calendar_ws.update_cell(row_index, col_index, replace_time_line(cell_text, request.time, booked_text))

        append_client_visit(spreadsheet, created_at, booking_id, request.client_name, service_name, request.date, request.time, service_price, request.notes or "")
        clear_cache()

        message = f"Новая запись\n\nКлиент: {request.client_name}\nУслуга: {service_name}\nДата: {request.date}\nВремя: {request.time}\nКомментарий: {request.notes or '-'}\nНомер записи: {booking_id}"
        send_master_notification(message)

        return {"booking_id": booking_id, "service_name": service_name, "date": request.date, "time": request.time, "status": "new", "telegram_reminder_url": make_telegram_reminder_url(booking_id), "cancel_url": f"/cancel?booking_id={urllib.parse.quote(booking_id)}"}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=readable_error(exc))
