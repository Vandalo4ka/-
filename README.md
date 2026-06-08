# Manicure Booking — Backend

## Структура
```
backend/
├── main.py              # FastAPI сервер
├── requirements.txt     # Зависимости
├── Procfile             # Для Railway/Render
├── .env.example         # Шаблон переменных
├── credentials/
│   └── google_credentials.json   # Сюда кладёшь ключ от Google
└── static/
    └── index.html       # Фронтенд (форма записи)
```

## Запуск локально

1. Скопируй `.env.example` → `.env` и заполни:
```
GOOGLE_CREDENTIALS_PATH=credentials/google_credentials.json
SPREADSHEET_ID=твой_id_таблицы
```

2. Положи `google_credentials.json` в папку `credentials/`

3. Установи зависимости:
```bash
pip install -r requirements.txt
```

4. Запусти:
```bash
python main.py
```

Открой http://localhost:8000 — увидишь форму записи.

## Деплой на Railway

1. Зарегистрируйся на railway.app
2. New Project → Deploy from GitHub repo
3. Добавь переменные окружения в настройках:
   - SPREADSHEET_ID
   - GOOGLE_CREDENTIALS_PATH=credentials/google_credentials.json
4. Загрузи `google_credentials.json` через Volume или вставь содержимое как переменную GOOGLE_CREDENTIALS_JSON

## Структура Google Sheets

### Лист `services`
| service_id | name | duration_min | price | is_active |
|---|---|---|---|---|
| SVC-01 | Маникюр + покрытие | 120 | 2500 | TRUE |

### Лист `slots`
| slot_id | date | time_start | time_end | status | booked_by | booking_id |
|---|---|---|---|---|---|---|
| S-20250614-1000 | 2025-06-14 | 10:00 | 12:00 | free | | |

### Лист `bookings`
| booking_id | client_name | slot_id | service | price | booking_status | reminder_24h_sent | reminder_2h_sent | created_at |
|---|---|---|---|---|---|---|---|---|
