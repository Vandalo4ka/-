import os
import json
import base64
import uuid
import traceback
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

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
CACHE_TTL_SECONDS = 300
REMINDER_CHECK_SECONDS = 600

_cache = {
    "services": {"time": 0, "data": None},
    "dates": {"time": 0, "data": None},
    "slots": {}
}

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


def readable_error(exc: Exception) -> str:
    message = str(exc)
    if message:
        return f"{type(exc).__name__}: {message}"
    return f"{type(exc).__name__}: {repr(exc)}"


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
            raise RuntimeError(f"Invalid GOOGLE_CREDENTIALS_JSON: {exc}")

    raise RuntimeError("No credentials found")


def get_credentials():
    try:
        credentials_info = load_credentials_info()
        return Credentials.from_service_account_info(credentials_info, scopes=SCOPES)
    except Exception as exc:
        raise RuntimeError(f"Cannot create Google credentials: {readable_error(exc)}")


def get_spreadsheet():
    try:
        if not SPREADSHEET_ID:
            raise RuntimeError("SPREADSHEET_ID is missing")

        credentials = get_credentials()
        client = gspread.authorize(credentials)
        return client.open_by_key(SPREADSHEET_ID)
    except Exception as exc:
        raise RuntimeError(f"Cannot open spreadsheet: {readable_error(exc)}")


def worksheet_records(sheet_name: str):
    try:
        spreadsheet = get_spreadsheet()
        worksheet = spreadsheet.worksheet(sheet_name)
        return worksheet.get_all_records()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=readable_error(exc))


def get_header_map(worksheet):
    headers = worksheet.row_values(1)
    return {str(header).strip(): index for index, header in enumerate(headers, start=1)}


def update_booking_columns(worksheet, row_number: int, updates: dict):
    header_map = get_header_map(worksheet)
    for column_name, value in updates.items():
        col = header_map.get(column_name)
        if col:
            worksheet.update_cell(row_number, col, value)


def normalize_date(value):
    return str(value).strip()


def normalize_time(value):
    value = str(value).strip()
    if not value:
        return ""

    # Google Sheets sometimes returns times as "10:00:00".
    if len(value) >= 5 and value[2] == ":":
        return value[:5]

    return value


def get_schedule_worksheet():
    spreadsheet = get_spreadsheet()
    return spreadsheet.worksheet("schedule")


def get_schedule_matrix():
    worksheet = get_schedule_worksheet()
    values = worksheet.get_all_values()

    if not values or len(values) < 2:
        raise RuntimeError("Schedule sheet is empty. It must have dates in column A and times in row 1.")

    return worksheet, values


def find_schedule_position(values, date_text: str, time_text: str):
    """
    schedule format:
    date | 10:00 | 13:00 | 15:00
    2025-06-14 | free | booked | blocked
    2025-06-15 | free | free | blocked
    """
    target_date = normalize_date(date_text)
    target_time = normalize_time(time_text)

    headers = values[0]
    time_col = None

    for col_index, header in enumerate(headers, start=1):
        if normalize_time(header) == target_time:
            time_col = col_index
            break

    if not time_col:
        return None, None

    date_row = None

    for row_index, row in enumerate(values[1:], start=2):
        if not row:
            continue

        row_date = normalize_date(row[0])
        if row_date == target_date:
            date_row = row_index
            break

    if not date_row:
        return None, None

    return date_row, time_col


def send_telegram_message(chat_id: str, text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not chat_id:
        return False

    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = urllib.parse.urlencode({
            "chat_id": chat_id,
            "text": text
        }).encode("utf-8")

        request = urllib.request.Request(url, data=payload, method="POST")
        with urllib.request.urlopen(request, timeout=8) as response:
            response.read()

        return True
    except Exception:
        return False


def send_master_notification(text: str) -> bool:
    if not TELEGRAM_CHAT_ID:
        return False
    return send_telegram_message(TELEGRAM_CHAT_ID, text)


def make_telegram_reminder_url(booking_id: str) -> str | None:
    if not TELEGRAM_BOT_USERNAME:
        return None

    username = TELEGRAM_BOT_USERNAME.lstrip("@").strip()
    if not username:
        return None

    return f"https://t.me/{username}?start={booking_id}"


def parse_appointment_datetime(date_text: str, time_text: str):
    tz = ZoneInfo(TIMEZONE)
    raw = f"{date_text.strip()} {time_text.strip()}"

    formats = [
        "%Y-%m-%d %H:%M",
        "%d.%m.%Y %H:%M",
        "%d/%m/%Y %H:%M",
    ]

    for fmt in formats:
        try:
            return datetime.strptime(raw, fmt).replace(tzinfo=tz)
        except ValueError:
            pass

    return None


def reminder_loop():
    while True:
        try:
            process_due_reminders()
        except Exception:
            pass

        time.sleep(REMINDER_CHECK_SECONDS)


def process_due_reminders():
    if not TELEGRAM_BOT_TOKEN:
        return

    spreadsheet = get_spreadsheet()
    bookings_ws = spreadsheet.worksheet("bookings")
    rows = bookings_ws.get_all_records()
    tz = ZoneInfo(TIMEZONE)
    now = datetime.now(tz)

    for row_number, row in enumerate(rows, start=2):
        reminder_requested = str(row.get("reminder_requested", "")).strip().casefold()
        reminder_sent = str(row.get("reminder_sent", "")).strip().casefold()
        chat_id = str(row.get("telegram_chat_id", "")).strip()
        status = str(row.get("status", "")).strip().casefold()

        if reminder_requested not in ["yes", "true", "1", "да"]:
            continue

        if reminder_sent in ["yes", "true", "1", "да"]:
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
                update_booking_columns(bookings_ws, row_number, {"reminder_sent": "yes"})


@app.on_event("startup")
def startup_event():
    global _reminder_thread_started

    if not _reminder_thread_started:
        thread = threading.Thread(target=reminder_loop, daemon=True)
        thread.start()
        _reminder_thread_started = True


@app.get("/")
def index():
    return FileResponse("index.html")


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
        body {{
          margin: 0;
          min-height: 100vh;
          background: #f3ede5;
          color: #6b4b43;
          font-family: Arial, sans-serif;
          display: flex;
          align-items: center;
          justify-content: center;
          padding: 20px;
        }}
        .card {{
          width: 100%;
          max-width: 520px;
          background: #fff;
          border-radius: 22px;
          padding: 30px;
          box-shadow: 0 18px 40px rgba(92, 62, 50, 0.10);
          text-align: center;
        }}
        h1 {{
          font-family: Georgia, "Times New Roman", serif;
          font-weight: 400;
          margin-top: 0;
        }}
        .summary {{
          background: #fbf1f3;
          border: 1px solid #eadfda;
          border-radius: 16px;
          padding: 16px;
          margin: 20px 0;
          line-height: 1.6;
        }}
        button {{
          width: 100%;
          border: none;
          border-radius: 14px;
          padding: 16px;
          font-size: 16px;
          font-weight: 700;
          cursor: pointer;
          background: #c98091;
          color: white;
        }}
        button:hover {{
          background: #b86d7f;
        }}
        .message {{
          margin-top: 18px;
          line-height: 1.6;
        }}
        a {{
          color: #b86d7f;
        }}
      </style>
    </head>
    <body>
      <div class="card">
        <h1>Отмена записи</h1>
        <div class="summary">
          Номер записи:<br>
          <b>{safe_booking_id}</b>
        </div>

        <button id="cancelBtn">Отменить запись</button>
        <div class="message" id="message"></div>
      </div>

      <script>
        const bookingId = {json.dumps(safe_booking_id)};

        document.getElementById("cancelBtn").onclick = async () => {{
          const confirmed = confirm("Точно отменить запись? Это время снова станет доступным для записи.");

          if (!confirmed) {{
            return;
          }}

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

            if (!response.ok) {{
              throw new Error(data.detail || "Ошибка сервера");
            }}

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
        "schedule_mode": "dates_down_times_across",
    }


@app.get("/debug/credentials")
def debug_credentials():
    try:
        info = load_credentials_info()
        return {
            "type": info.get("type"),
            "project_id": info.get("project_id"),
            "client_email": info.get("client_email"),
            "has_private_key": bool(info.get("private_key")),
            "spreadsheet_id": SPREADSHEET_ID,
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=readable_error(exc))


@app.get("/debug/sheets")
def debug_sheets():
    try:
        spreadsheet = get_spreadsheet()
        return {
            "title": spreadsheet.title,
            "worksheets": [ws.title for ws in spreadsheet.worksheets()],
        }
    except Exception as exc:
        return {
            "error": readable_error(exc),
            "traceback": traceback.format_exc().splitlines()[-8:],
        }


@app.get("/debug/schedule")
def debug_schedule():
    try:
        worksheet, values = get_schedule_matrix()
        return {
            "sheet": worksheet.title,
            "headers": values[0],
            "rows": len(values) - 1,
            "expected_format": "date in column A, times in row 1",
        }
    except Exception as exc:
        raise HTTPException(status_code=500, detail=readable_error(exc))


@app.get("/debug/cache")
def debug_cache():
    return {
        "ttl_seconds": CACHE_TTL_SECONDS,
        "services_cached": cache_is_valid(_cache["services"]),
        "dates_cached": cache_is_valid(_cache["dates"]),
        "slots_cached_dates": list(_cache["slots"].keys()),
    }


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
        rows = bookings_ws.get_all_records()

        for row_number, row in enumerate(rows, start=2):
            current_booking_id = str(row.get("booking_id", "")).strip()

            if current_booking_id == booking_id:
                update_booking_columns(bookings_ws, row_number, {
                    "telegram_chat_id": chat_id,
                    "reminder_requested": "yes",
                    "reminder_sent": "no",
                })

                send_telegram_message(
                    chat_id,
                    "Напоминание подключено.\n\n"
                    f"Запись: {row.get('service_name', '')}\n"
                    f"Дата: {row.get('date', '')}\n"
                    f"Время: {row.get('time', '')}\n\n"
                    "Мы напомним вам за сутки."
                )
                return {"ok": True}

        send_telegram_message(chat_id, "Запись не найдена. Попробуйте открыть кнопку напоминания ещё раз.")
        return {"ok": True}

    except Exception:
        send_telegram_message(chat_id, "Не удалось подключить напоминание. Попробуйте позже.")
        return {"ok": True}



def find_booking_by_id(bookings_ws, booking_id: str):
    rows = bookings_ws.get_all_records()

    for row_number, row in enumerate(rows, start=2):
        current_booking_id = str(row.get("booking_id", "")).strip()
        if current_booking_id == str(booking_id).strip():
            return row_number, row

    return None, None


def free_schedule_cell_for_booking(schedule_ws, date_text: str, time_text: str):
    _, schedule_values = get_schedule_matrix()
    schedule_row, schedule_col = find_schedule_position(schedule_values, date_text, time_text)

    if schedule_row and schedule_col:
        schedule_ws.update_cell(schedule_row, schedule_col, "free")


@app.get("/api/services")
def get_services():
    if cache_is_valid(_cache["services"]):
        return _cache["services"]["data"]

    records = worksheet_records("services")

    services = []
    for row in records:
        is_active = str(row.get("is_active", "")).strip().casefold()
        if is_active in ["true", "1", "yes", "да"]:
            services.append({
                "service_id": str(row.get("service_id", "")).strip(),
                "name": str(row.get("name", "")).strip(),
                "duration_min": str(row.get("duration_min", "")).strip(),
                "price": str(row.get("price", "")).strip(),
                "is_active": is_active,
            })

    set_cache("services", services)
    return services


@app.get("/api/dates")
def get_dates():
    if cache_is_valid(_cache["dates"]):
        return _cache["dates"]["data"]

    try:
        worksheet, values = get_schedule_matrix()
        available_dates = []

        for row in values[1:]:
            if not row:
                continue

            date_text = normalize_date(row[0])
            if not date_text:
                continue

            has_free_slot = False

            for cell in row[1:]:
                status = str(cell).strip().casefold()
                if status in ["free", ""]:
                    has_free_slot = True
                    break

            if has_free_slot:
                available_dates.append(date_text)

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
        worksheet, values = get_schedule_matrix()
        headers = values[0]

        target_row = None

        for row in values[1:]:
            if not row:
                continue

            if normalize_date(row[0]) == normalize_date(date):
                target_row = row
                break

        if not target_row:
            return []

        slots = []

        for col_index, header in enumerate(headers[1:], start=2):
            time_text = normalize_time(header)
            if not time_text:
                continue

            status = ""
            if len(target_row) >= col_index:
                status = str(target_row[col_index - 1]).strip().casefold()

            if status in ["free", ""]:
                slots.append({
                    "slot_id": f"{date}-{time_text}",
                    "date": normalize_date(date),
                    "time": time_text,
                    "status": "free",
                })

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

        spreadsheet = get_spreadsheet()
        bookings_ws = spreadsheet.worksheet("bookings")
        rows = bookings_ws.get_all_records()

        results = []

        for row in rows:
            row_name = str(row.get("client_name", "")).strip()
            row_status = str(row.get("status", "")).strip().casefold()

            if row_status in ["cancelled", "canceled", "отменено"]:
                continue

            if query not in row_name.casefold():
                continue

            results.append({
                "booking_id": str(row.get("booking_id", "")).strip(),
                "client_name": row_name,
                "service_name": str(row.get("service_name", "")).strip(),
                "date": str(row.get("date", "")).strip(),
                "time": str(row.get("time", "")).strip(),
                "notes": str(row.get("notes", "")).strip(),
                "status": str(row.get("status", "")).strip(),
            })

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
        schedule_ws = spreadsheet.worksheet("schedule")

        row_number, booking = find_booking_by_id(bookings_ws, booking_id)

        if not row_number:
            raise HTTPException(status_code=404, detail="Booking not found")

        current_status = str(booking.get("status", "")).strip().casefold()

        if current_status in ["cancelled", "canceled", "отменено"]:
            return {
                "status": "already_cancelled",
                "booking_id": booking_id,
                "message": "Запись уже отменена",
            }

        update_booking_columns(bookings_ws, row_number, {
            "status": "cancelled",
            "reminder_sent": "cancelled",
        })

        free_schedule_cell_for_booking(
            schedule_ws,
            str(booking.get("date", "")),
            str(booking.get("time", "")),
        )

        clear_cache()

        message = (
            "Запись отменена\\n\\n"
            f"Клиент: {booking.get('client_name', '')}\\n"
            f"Услуга: {booking.get('service_name', '')}\\n"
            f"Дата: {booking.get('date', '')}\\n"
            f"Время: {booking.get('time', '')}\\n"
            f"Номер записи: {booking_id}"
        )
        send_master_notification(message)

        client_chat_id = str(booking.get("telegram_chat_id", "")).strip()
        if client_chat_id:
            send_telegram_message(
                client_chat_id,
                "Ваша запись отменена.\\n\\n"
                f"Услуга: {booking.get('service_name', '')}\\n"
                f"Дата: {booking.get('date', '')}\\n"
                f"Время: {booking.get('time', '')}"
            )

        return {
            "status": "cancelled",
            "booking_id": booking_id,
            "message": "Запись отменена",
        }

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=readable_error(exc))


@app.post("/api/bookings")
def create_booking(request: BookingRequest):
    try:
        spreadsheet = get_spreadsheet()

        services_ws = spreadsheet.worksheet("services")
        bookings_ws = spreadsheet.worksheet("bookings")
        schedule_ws, schedule_values = get_schedule_matrix()

        services = services_ws.get_all_records()
        service = next(
            (
                row for row in services
                if str(row.get("service_id", "")).strip() == request.service_id
                and str(row.get("is_active", "")).strip().casefold() in ["true", "1", "yes", "да"]
            ),
            None
        )

        if not service:
            raise HTTPException(status_code=404, detail="Service not found")

        schedule_row, schedule_col = find_schedule_position(
            schedule_values,
            request.date,
            request.time,
        )

        if not schedule_row or not schedule_col:
            clear_cache()
            raise HTTPException(status_code=409, detail="This time is no longer available")

        current_status = schedule_ws.cell(schedule_row, schedule_col).value
        current_status = str(current_status or "").strip().casefold()

        if current_status not in ["free", ""]:
            clear_cache()
            raise HTTPException(status_code=409, detail="This time is no longer available")

        booking_id = "BK-" + uuid.uuid4().hex[:8].upper()
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        bookings_ws.append_row([
            booking_id,
            created_at,
            request.client_name,
            request.service_id,
            service.get("name", ""),
            request.date,
            request.time,
            request.notes or "",
            "new",
            "",
            "no",
            "no",
        ])

        # After booking, the calendar cell changes automatically from free to booked.
        schedule_ws.update_cell(schedule_row, schedule_col, "booked")

        clear_cache()

        message = (
            "Новая запись\n\n"
            f"Клиент: {request.client_name}\n"
            f"Услуга: {service.get('name', '')}\n"
            f"Дата: {request.date}\n"
            f"Время: {request.time}\n"
            f"Комментарий: {request.notes or '-'}\n"
            f"Номер записи: {booking_id}"
        )
        send_master_notification(message)

        return {
            "booking_id": booking_id,
            "service_name": service.get("name", ""),
            "date": request.date,
            "time": request.time,
            "status": "new",
            "telegram_reminder_url": make_telegram_reminder_url(booking_id),
            "cancel_url": f"/cancel?booking_id={urllib.parse.quote(booking_id)}",
        }

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=readable_error(exc))
