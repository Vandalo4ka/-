# Manicure Booking

Мини-сайт записи к мастеру маникюра на Railway + Google Sheets.

## Файлы

- `main.py` — backend API.
- `index.html` — страница записи.
- `requirements.txt` — зависимости Python.
- `Procfile` — команда запуска Railway.
- `runtime.txt` — фиксирует Python 3.11.10.

## Railway Variables

Нужно добавить:

```env
SPREADSHEET_ID=ID_таблицы
GOOGLE_CREDENTIALS_B64=base64_строка_из_google_credentials.json
```

## Google Sheets

В таблице должны быть 3 листа:

### services

```text
service_id | name | duration_min | price | is_active
SVC-01 | Маникюр + покрытие | 120 | 2500 | true
```

### slots

```text
slot_id | date | time | status
SL-01 | 2026-06-10 | 10:00 | free
SL-02 | 2026-06-10 | 12:00 | free
```

### bookings

```text
booking_id | created_at | client_name | phone | service_id | service_name | date | time | notes | status
```

Сервисному аккаунту Google нужно дать доступ к таблице как редактору.
