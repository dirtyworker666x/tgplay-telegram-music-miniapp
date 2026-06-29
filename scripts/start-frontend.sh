#!/bin/bash
cd "$(dirname "$0")/.."
echo "Фронт запускается на http://127.0.0.1:5173"
echo "Открой в браузере: http://127.0.0.1:5173"
echo ""
npm run dev
