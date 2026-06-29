#!/bin/bash
cd "$(dirname "$0")/../backend"
source venv/bin/activate
echo "🎵 TGPlay Lite API (без MongoDB)"
echo "Бэкенд запускается на http://127.0.0.1:8000"
echo "Документация API: http://127.0.0.1:8000/docs"
echo ""
python3 server_lite.py
