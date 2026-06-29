#!/usr/bin/env bash
# Быстрый запуск — минимум ожиданий. Использование: ./scripts/quick-start.sh
set -e
cd "$(dirname "$0")/.."
PORT=8787

echo "🛑 Останавливаю процессы..."
pkill -9 -f "python.*bot.py" 2>/dev/null || true
pkill -9 -f "bot.py" 2>/dev/null || true
pkill -9 -f "server_lite" 2>/dev/null || true
pkill -9 -f "tunnel-watchdog" 2>/dev/null || true
pkill -9 -f "ssh.*localhost" 2>/dev/null || true
pkill -9 -f "ssh.*nokey" 2>/dev/null || true
lsof -ti:$PORT | xargs kill -9 2>/dev/null || true
rm -f backend/bot.lock .tunnel-watchdog.lock /tmp/tgplay_tunnel_restart
sleep 2

# Освободить порт
lsof -ti:$PORT | xargs kill -9 2>/dev/null || true
sleep 1

echo "▶️ Запуск бэкенда..."
(cd backend && source venv/bin/activate && APP_PORT=$PORT python server_lite.py) &
sleep 2
echo "▶️ Запуск туннеля и бота..."
exec ./tunnel-watchdog.sh
