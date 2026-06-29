#!/usr/bin/env bash
# Обновить локальную копию Telegram WebApp API (раз в несколько месяцев или после проблем с клиентом).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
curl -fsSL "https://telegram.org/js/telegram-web-app.js" -o "$ROOT/public/telegram-web-app.js"
echo "OK $(wc -c <"$ROOT/public/telegram-web-app.js") bytes -> public/telegram-web-app.js"
