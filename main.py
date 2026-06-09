import os
import json
import base64
import uuid
import traceback
import time
from datetime import datetime

import gspread
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from google.oauth2.service_account import Credentials
from pydantic import BaseModel


SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")

# Google Sheets scope is enough for reading/writing by spreadsheet ID.
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]

# Cache time in seconds.
# 300 = 5 minutes.
CACHE_TTL_SECONDS = 300

_cache = {
    "services": {"time": 0, "data": None},
    "dates": {"time": 0, "data": None},
    "slots": {}
}


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
            raise RuntimeError(f"Invalid GOOGLE_CREDENTIALS_JSON: {readable_error(exc)}")

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


@app.get("/")
def index():
    return FileResponse("index.html")


@app.get("/health")
def health():
    return {
        "status": "ok",
        "spreadsheet_id_present": bool(os.getenv("SPREADSHEET_ID")),
        "credentials_b64_present": bool(os.getenv("GOOGLE_CREDENTIALS_B64")),
        "credentials_json_present": bool(os.getenv("GOOGLE_CREDENTIALS_JSON")),
        "cache_ttl_seconds": CACHE_TTL_SECONDS,
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


@app.get("/api/services")
def get_services():
    if cache_is_valid(_cache["services"]):
        return _cache["services"]["data"]

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

    set_cache("services", services)
    return services


@app.get("/api/dates")
def get_dates():
    if cache_is_valid(_cache["dates"]):
        return _cache["dates"]["data"]

    records = worksheet_records("slots")
    dates = sorted({
        str(row.get("date", "")).strip()
        for row in records
        if str(row.get("status", "")).strip().lower() == "free"
    })

    set_cache("dates", dates)
    return dates


@app.get("/api/slots")
def get_slots(date: str = Query(...)):
    cached = _cache["slots"].get(date)
    if cached and cache_is_valid(cached):
        return cached["data"]

    records = worksheet_records("slots")

    slots = []
    for row in records:
        row_date = str(row.get("date", "")).strip()
        status = str(row.get("status", "")).strip().lower()
        row_time = str(row.get("time", "") or row.get("time_start", "")).strip()

        if row_date == date and status == "free":
            slots.append({
                "slot_id": str(row.get("slot_id", "")).strip(),
                "date": row_date,
                "time": row_time,
                "status": status,
            })

    slots = sorted(slots, key=lambda item: item["time"])
    _cache["slots"][date] = {"time": time.time(), "data": slots}
    return slots


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
            row_time = str(row.get("time", "") or row.get("time_start", "")).strip()
            same_date = str(row.get("date", "")).strip() == request.date
            same_time = row_time == request.time
            is_free = str(row.get("status", "")).strip().lower() == "free"

            if same_date and same_time and is_free:
                slot_row_number = index
                break

        if not slot_row_number:
            clear_cache()
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

        headers = slots_ws.row_values(1)
        status_col = None
        for i, header in enumerate(headers, start=1):
            if str(header).strip() == "status":
                status_col = i
                break

        if not status_col:
            raise RuntimeError("Column status not found in slots sheet")

        slots_ws.update_cell(slot_row_number, status_col, "booked")

        # Important: after a booking, clear cache so the booked slot disappears immediately.
        clear_cache()

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
        raise HTTPException(status_code=500, detail=readable_error(exc))
