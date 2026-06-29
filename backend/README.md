# Backend TGPlay

Продакшен-API: **`server_lite.py`** (FastAPI, один процесс).

- Поиск и метаданные: SoundCloud (`sc_client_simple.py`), legacy VK/YouTube
- Плейлисты: JSON в `user_data/`
- Аналитика: SQLite (`analytics_db.py`)
- Кэш: Redis (опционально)

Запуск: `python3 server_lite.py` (порт 8000). Секреты — `backend/.env` (см. `.env.example`).

Общее описание проекта и стек: **[../README.md](../README.md)**
