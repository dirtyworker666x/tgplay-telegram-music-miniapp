#!/usr/bin/env bash
# TGPlay — установка по домену tgplay.fun: бэкенд :8000 + nginx :80. Без туннеля.
# Запуск из корня проекта: sudo bash scripts/setup-server-ubuntu.sh
# Требует: backend/.env с BOT_TOKEN, VK_TOKEN. WEBAPP_URL=https://tgplay.fun.
set -e

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Запусти скрипт с sudo: sudo bash scripts/setup-server-ubuntu.sh"
  exit 1
fi

REAL_USER="${SUDO_USER:-root}"
PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJECT_ROOT"

echo "📁 Проект: $PROJECT_ROOT"
echo "👤 Пользователь: $REAL_USER"
echo ""

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq curl ca-certificates

# Node.js 18
if ! command -v node &>/dev/null || [[ "$(node -v 2>/dev/null | cut -d. -f1 | tr -d v)" -lt 18 ]]; then
  echo "▶️ Node.js 18..."
  curl -fsSL https://deb.nodesource.com/setup_18.x | bash -
  apt-get install -y -qq nodejs
fi
echo "✅ Node $(node -v)"

# Python3, ffmpeg, nginx
echo "▶️ Python3, ffmpeg, nginx..."
apt-get install -y -qq python3 python3-venv python3-pip ffmpeg nginx

# Backend venv
echo "▶️ Backend..."
if [[ ! -d "$PROJECT_ROOT/backend/venv" ]]; then
  sudo -u "$REAL_USER" python3 -m venv "$PROJECT_ROOT/backend/venv"
fi
sudo -u "$REAL_USER" "$PROJECT_ROOT/backend/venv/bin/pip" install -q -r "$PROJECT_ROOT/backend/requirements.txt"

# Frontend build
echo "▶️ Сборка фронта..."
sudo -u "$REAL_USER" bash -c "cd '$PROJECT_ROOT' && npm run build"
echo "✅ Фронт собран"

# .env
if [[ ! -f "$PROJECT_ROOT/backend/.env" ]]; then
  cp "$PROJECT_ROOT/backend/.env.example" "$PROJECT_ROOT/backend/.env"
  chown "$REAL_USER:$REAL_USER" "$PROJECT_ROOT/backend/.env"
  echo "⚠️ Заполни BOT_TOKEN и VK_TOKEN в backend/.env"
fi
grep -q '^WEBAPP_URL=' "$PROJECT_ROOT/backend/.env" 2>/dev/null && sed -i 's|^WEBAPP_URL=.*|WEBAPP_URL=https://tgplay.fun|' "$PROJECT_ROOT/backend/.env" || echo "WEBAPP_URL=https://tgplay.fun" >> "$PROJECT_ROOT/backend/.env"
echo "✅ WEBAPP_URL=https://tgplay.fun"

# Systemd: только бэкенд :8000
echo "▶️ Сервис бэкенда..."
cat > /etc/systemd/system/tgplay-backend.service << EOF
[Unit]
Description=TGPlay Backend (FastAPI)
After=network.target

[Service]
Type=simple
User=$REAL_USER
WorkingDirectory=$PROJECT_ROOT/backend
ExecStart=$PROJECT_ROOT/backend/venv/bin/python server_lite.py
Restart=always
RestartSec=5
Environment=PATH=$PROJECT_ROOT/backend/venv/bin:/usr/bin
Environment=APP_PORT=8000

[Install]
WantedBy=multi-user.target
EOF

# Nginx → 8000
echo "▶️ Nginx..."
if [[ -f "$PROJECT_ROOT/deploy/nginx-tgplay.conf" ]]; then
  cp "$PROJECT_ROOT/deploy/nginx-tgplay.conf" /etc/nginx/sites-available/tgplay
  ln -sf /etc/nginx/sites-available/tgplay /etc/nginx/sites-enabled/tgplay
  rm -f /etc/nginx/sites-enabled/default
  nginx -t && systemctl reload nginx
  echo "✅ Nginx проксирует на 127.0.0.1:8000"
fi

systemctl daemon-reload
systemctl enable tgplay-backend.service
systemctl start tgplay-backend.service
sleep 4
systemctl is-active -q tgplay-backend.service || { echo "❌ Бэкенд не поднялся"; exit 1; }
echo "✅ Бэкенд запущен (порт 8000)"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "✅ Готово. Домен: https://tgplay.fun (без туннеля)"
echo "   systemctl status tgplay-backend nginx"
echo "   curl -s http://127.0.0.1:8000/api/status"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
