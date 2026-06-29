#!/bin/bash
# Один раз: установка зависимостей, туннеля и .env
set -e
cd "$(dirname "$0")/.."

echo "=== 1. Фронт (Node) ==="
npm install
echo ""

echo "=== 2. Бэкенд (Python) ==="
cd backend
if [ ! -d "venv" ]; then
  python3 -m venv venv
  echo "Создано виртуальное окружение backend/venv"
fi
source venv/bin/activate
pip install -r requirements.txt
cd ..
echo ""

echo "=== 3. Туннель (cloudflared) ==="
if ! command -v cloudflared &>/dev/null; then
  if command -v brew &>/dev/null; then
    echo "Устанавливаю cloudflared..."
    brew install cloudflared
  else
    echo "⚠ cloudflared не найден. Установи: brew install cloudflared (или будет localhost.run)"
  fi
else
  echo "cloudflared уже установлен"
fi
echo ""

echo "=== 4. .env ==="
if [ ! -f backend/.env ]; then
  cp backend/.env.example backend/.env
  echo "Создан backend/.env — вставь BOT_TOKEN и VK_TOKEN из START_HERE.md"
else
  echo "backend/.env уже есть"
fi
echo ""

echo "Готово. Дальше:"
echo "  1. Вставь токены в backend/.env (BOT_TOKEN, VK_TOKEN)"
echo "  2. npm run start  — всё поднимется"
