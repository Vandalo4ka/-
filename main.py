import os
import json
import base64
import uuid
from datetime import datetime

import gspread
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from google.oauth2.service_account import Credentials
from pydantic import BaseModel


SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")

SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

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
    phone: str
    notes: str | None = ""
    service_id: str
    date: str
    time: str


def get_credentials():
    """
    Railway supports both:
    1. GOOGLE_CREDENTIALS_B64 — recommended
    2. GOOGLE_CREDENTIALS_JSON — must be one-line JSON
    """
    credentials_b64 = os.getenv("GOOGLE_CREDENTIALS_B64")
    credentials_json = os.getenv("GOOGLE_CREDENTIALS_JSON")

    if credentials_b64:
        try:
            decoded = base64.b64decode(credentials_b64).decode("utf-8")
            credentials_info = json.loads(decoded)
            return Credentials.from_service_account_info(credentials_info, scopes=SCOPES)
        except Exception as exc:
            raise RuntimeError(f"Invalid GOOGLE_CREDENTIALS_B64: {exc}")

    if credentials_json:
        try:
            credentials_info = json.loads(credentials_json)
            return Credentials.from_service_account_info(credentials_info, scopes=SCOPES)
        except Exception as exc:
            raise RuntimeError(f"Invalid GOOGLE_CREDENTIALS_JSON: {exc}")

    raise RuntimeError("No credentials found")


def get_spreadsheet():
    if not SPREADSHEET_ID:
        raise RuntimeError("SPREADSHEET_ID is missing")

    credentials = get_credentials()
    client = gspread.authorize(credentials)
    return client.open_by_key(SPREADSHEET_ID)


def worksheet_records(sheet_name: str):
    try:
        spreadsheet = get_spreadsheet()
        worksheet = spreadsheet.worksheet(sheet_name)
        return worksheet.get_all_records()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/")
def index():
    return FileResponse("index.html")


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/services")
def get_services():
    records = worksheet_records("services")

    services = []
    for row in records:
        is_active = str(row.get("is_active", "")).strip().lower()
        if is_active in ["true", "1", "yes", "да"]:
            services.append({
                "service_id": str(row.get("service_id", "")).strip(),
                "name": str(row.get("name", "")).strip(),
                "duration_min": str(row.get("duration_min", "")).strip(),
                "price": str(row.get("price", "")).strip(),
                "is_active": is_active,
            })

    return services


@app.get("/api/dates")
def get_dates():
    records = worksheet_records("slots")
    dates = sorted({
        str(row.get("date", "")).strip()
        for row in records
        if str(row.get("status", "")).strip().lower() == "free"
    })
    return dates


@app.get("/api/slots")
def get_slots(date: str = Query(...)):
    records = worksheet_records("slots")

    slots = []
    for row in records:
        row_date = str(row.get("date", "")).strip()
        status = str(row.get("status", "")).strip().lower()

        if row_date == date and status == "free":
            slots.append({
                "slot_id": str(row.get("slot_id", "")).strip(),
                "date": row_date,
                "time": str(row.get("time", "")).strip(),
                "status": status,
            })

    return sorted(slots, key=lambda item: item["time"])


@app.post("/api/bookings")
def create_booking(request: BookingRequest):
    try:
        spreadsheet = get_spreadsheet()

        services_ws = spreadsheet.worksheet("services")
        slots_ws = spreadsheet.worksheet("slots")
        bookings_ws = spreadsheet.worksheet("bookings")

        services = services_ws.get_all_records()
        service = next(
            (
                row for row in services
                if str(row.get("service_id", "")).strip() == request.service_id
                and str(row.get("is_active", "")).strip().lower() in ["true", "1", "yes", "да"]
            ),
            None
        )

        if not service:
            raise HTTPException(status_code=404, detail="Service not found")

        slots = slots_ws.get_all_records()
        slot_row_number = None

        for index, row in enumerate(slots, start=2):
            same_date = str(row.get("date", "")).strip() == request.date
            same_time = str(row.get("time", "")).strip() == request.time
            is_free = str(row.get("status", "")).strip().lower() == "free"

            if same_date and same_time and is_free:
                slot_row_number = index
                break

        if not slot_row_number:
            raise HTTPException(status_code=409, detail="This time is no longer available")

        booking_id = "BK-" + uuid.uuid4().hex[:8].upper()
        created_at = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        bookings_ws.append_row([
            booking_id,
            created_at,
            request.client_name,
            request.phone,
            request.service_id,
            service.get("name", ""),
            request.date,
            request.time,
            request.notes or "",
            "new",
        ])

        status_col = None
        headers = slots_ws.row_values(1)
        for i, header in enumerate(headers, start=1):
            if header == "status":
                status_col = i
                break

        if not status_col:
            raise RuntimeError("Column status not found in slots sheet")

        slots_ws.update_cell(slot_row_number, status_col, "booked")

        return {
            "booking_id": booking_id,
            "service_name": service.get("name", ""),
            "date": request.date,
            "time": request.time,
            "status": "new",
        }

    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
