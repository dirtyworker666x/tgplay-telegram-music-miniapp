#!/usr/bin/env bash
# Запуск только бэкенда (фронт + API + webhook для бота). Без туннеля.
# Для работы через свой сервер: на сервере запускай этот скрипт или systemd unit.
# В .env укажи WEBAPP_URL=https://tgplay.fun
set -e
cd "$(dirname "$0")/.."
PORT="${APP_PORT:-8787}"

echo "🛑 Останавливаю старые процессы..."
pkill -9 -f "server_lite.py" 2>/dev/null || true
sleep 2

# Освобождаем порт (до 10 попыток)
for i in 1 2 3 4 5 6 7 8 9 10; do
  pids=$(lsof -ti:$PORT 2>/dev/null || true)
  [[ -z "$pids" ]] && break
  echo "$pids" | xargs kill -9 2>/dev/null || true
  sleep 1
done
if lsof -i:$PORT >/dev/null 2>&1; then
  echo "❌ Порт $PORT занят. Освободи: lsof -ti:$PORT | xargs kill -9"
  exit 1
fi
echo "✅ Порт $PORT свободен"

echo "📦 Сборка фронтенда..."
npm run build
echo "✅ Фронтенд собран"

echo "▶️ Запуск бэкенда на http://0.0.0.0:$PORT"
cd backend
if [[ -d venv ]]; then
  source venv/bin/activate
else
  echo "❌ Создай venv: cd backend && python3 -m venv venv && ./venv/bin/pip install -r requirements.txt"
  exit 1
fi
exec python server_lite.py
