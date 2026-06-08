from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from pydantic import BaseModel
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime
import os

load_dotenv()

# ─── Google Sheets ─────────────────────────────────────────────────────────────

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

SPREADSHEET_ID = os.getenv("SPREADSHEET_ID", "")
CREDENTIALS_PATH = os.getenv("GOOGLE_CREDENTIALS_PATH", "credentials/google_credentials.json")
CREDENTIALS_JSON = os.getenv("GOOGLE_CREDENTIALS_JSON", "")  # для Railway

_spreadsheet = None

def get_sheet(name: str) -> gspread.Worksheet:
    global _spreadsheet
    if _spreadsheet is None:
        if CREDENTIALS_JSON:
            import json
            info = json.loads(CREDENTIALS_JSON)
            creds = Credentials.from_service_account_info(info, scopes=SCOPES)
        else:
            creds = Credentials.from_service_account_file(CREDENTIALS_PATH, scopes=SCOPES)
        client = gspread.authorize(creds)
        _spreadsheet = client.open_by_key(SPREADSHEET_ID)
    return _spreadsheet.worksheet(name)


# ─── FastAPI ───────────────────────────────────────────────────────────────────

app = FastAPI(title="Manicure Booking API")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ─── Модели ────────────────────────────────────────────────────────────────────

class BookingRequest(BaseModel):
    client_name: str
    service_id: str
    service_name: str
    price: int
    slot_id: str
    date: str
    time_start: str
    time_end: str


# ─── Эндпоинты ─────────────────────────────────────────────────────────────────

@app.get("/api/services")
def get_services():
    """Возвращает активные услуги из прайса."""
    try:
        sheet = get_sheet("services")
        records = sheet.get_all_records()
        return [s for s in records if str(s.get("is_active", "")).upper() == "TRUE"]
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/dates")
def get_dates():
    """Возвращает даты где есть хотя бы один свободный слот."""
    try:
        sheet = get_sheet("slots")
        records = sheet.get_all_records()
        today = datetime.now().strftime("%Y-%m-%d")
        dates = sorted(set(
            r["date"] for r in records
            if r.get("status") == "free" and str(r.get("date", "")) >= today
        ))
        return {"dates": dates}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.get("/api/slots/{date}")
def get_slots(date: str):
    """Возвращает свободные слоты на конкретную дату."""
    try:
        sheet = get_sheet("slots")
        records = sheet.get_all_records()
        slots = [
            r for r in records
            if r.get("date") == date and r.get("status") == "free"
        ]
        return {"slots": slots}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/book")
def create_booking(data: BookingRequest):
    """Создаёт запись: блокирует слот и добавляет строку в bookings."""
    try:
        slots_sheet = get_sheet("slots")
        records = slots_sheet.get_all_records()

        # Проверяем что слот ещё свободен
        slot_row = None
        for i, r in enumerate(records, start=2):
            if r.get("slot_id") == data.slot_id:
                if r.get("status") != "free":
                    raise HTTPException(status_code=409, detail="Слот уже занят")
                slot_row = i
                break

        if not slot_row:
            raise HTTPException(status_code=404, detail="Слот не найден")

        # Генерируем booking_id
        bookings_sheet = get_sheet("bookings")
        count = len(bookings_sheet.get_all_records())
        booking_id = f"B-{(count + 1):04d}"
        now = datetime.now().strftime("%Y-%m-%d %H:%M")

        # Блокируем слот
        slots_sheet.update_cell(slot_row, 5, "booked")       # status
        slots_sheet.update_cell(slot_row, 6, data.client_name)  # booked_by
        slots_sheet.update_cell(slot_row, 7, booking_id)     # booking_id

        # Добавляем запись
        bookings_sheet.append_row([
            booking_id,
            data.client_name,
            data.slot_id,
            data.service_name,
            data.price,
            "confirmed",   # booking_status
            "FALSE",       # reminder_24h_sent
            "FALSE",       # reminder_2h_sent
            now,           # created_at
        ], value_input_option="USER_ENTERED")

        return {
            "success": True,
            "booking_id": booking_id,
        }

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ─── Отдаём фронтенд ───────────────────────────────────────────────────────────

# Ищем index.html — сначала в static/, потом в корне
if os.path.exists("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

@app.get("/")
def serve_index():
    if os.path.exists("static/index.html"):
        return FileResponse("static/index.html")
    elif os.path.exists("index.html"):
        return FileResponse("index.html")
    return {"error": "index.html not found"}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
