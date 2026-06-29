# TGPlay — стабильный деплой без падающих туннелей.
# Один образ: фронт + бэкенд. Запуск на VPS или Railway/Render даёт постоянный URL.

# ─── Stage 1: сборка фронта ─────────────────────────────────────
FROM node:20-alpine AS frontend
WORKDIR /app
COPY package*.json ./
RUN npm ci --omit=dev
COPY . .
RUN npm run build

# ─── Stage 2: бэкенд + статика ──────────────────────────────────
FROM python:3.12-slim
WORKDIR /app

# ffmpeg для HLS→MP3
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY backend/ ./backend/
# Статика из stage 1 в корень проекта (server_lite ищет ../dist)
COPY --from=frontend /app/dist /app/dist

ENV PORT=8000
EXPOSE 8000

# WORKERS и PORT задаются при запуске (Railway/VPS). По умолчанию 4 воркера.
ENV WORKERS=4
CMD ["sh", "-c", "cd /app/backend && python -m uvicorn server_lite:app --host 0.0.0.0 --port ${PORT:-8000} --workers ${WORKERS:-4} --limit-concurrency 500 --timeout-keep-alive 120"]
