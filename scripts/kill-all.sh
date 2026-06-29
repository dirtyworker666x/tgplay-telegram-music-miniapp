#!/usr/bin/env bash
# Полная остановка всех процессов TGPlay. Запускай перед restart-all.sh
# Использование: ./scripts/kill-all.sh
set -e
cd "$(dirname "$0")/.."

echo "🛑 Убиваю все процессы TGPlay..."
pkill -9 -f "python.*bot.py" 2>/dev/null || true
pkill -9 -f "bot.py" 2>/dev/null || true
pkill -9 -f "server_lite.py" 2>/dev/null || true
pkill -9 -f "tunnel-watchdog" 2>/dev/null || true
pkill -9 -f "ssh.*localhost" 2>/dev/null || true
pkill -9 -f "ssh.*nokey" 2>/dev/null || true
pkill -9 -f "cloudflared.*tunnel" 2>/dev/null || true
lsof -ti:8787 | xargs kill -9 2>/dev/null || true

rm -f backend/bot.lock .tunnel-watchdog.lock /tmp/tgplay_tunnel_restart

echo "✅ Все процессы остановлены."
echo ""
# Проверка
if pgrep -f "bot.py" >/dev/null 2>&1; then
  echo "⚠️  Внимание: процессы bot.py всё ещё найдены!"
  pgrep -fl "bot.py" 2>/dev/null || true
else
  echo "✓ Подтверждено: bot.py не запущен."
fi
echo ""
echo "⚠️  Если Conflict продолжается — бот может быть на другом ПК или в другом терминале."
echo "   Один BOT_TOKEN = одно подключение к Telegram."
echo ""
