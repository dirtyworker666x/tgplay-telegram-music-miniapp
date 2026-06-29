#!/usr/bin/env bash
# Замер времени ответа API: поиск, resolve, health.
# Использование: BASE_URL=https://tgplay.fun ./scripts/measure-api-latency.sh
# или: ./scripts/measure-api-latency.sh https://localhost:8000
# Не блокирует unit-тесты; запускать вручную или отдельным CI job.

set -e
BASE="${BASE_URL:-${1:-https://tgplay.fun}}"
SEARCH_Q="${SEARCH_QUERY:-beatles}"
echo "Base URL: $BASE"
echo "---"

measure() {
  local name="$1"
  local url="$2"
  local t
  t=$(curl -s -o /dev/null -w '%{time_total}' "$url" 2>/dev/null || echo "0")
  echo "${name}: ${t}s"
}

measure "GET /api/health" "$BASE/api/health"
measure "GET /api/music/search?q=$SEARCH_Q&limit=5" "$BASE/api/music/search?q=$SEARCH_Q&limit=5"

# Resolve: берём первый track id из поиска
SEARCH_RESP=$(curl -s "$BASE/api/music/search?q=$SEARCH_Q&limit=1" 2>/dev/null || echo '{"items":[]}')
TRACK_ID=$(echo "$SEARCH_RESP" | python3 -c "
import json, sys
try:
    d = json.load(sys.stdin)
    items = d.get('items') or []
    print(items[0].get('id', '') if items else '')
except Exception:
    print('')
" 2>/dev/null || true)

if [ -n "$TRACK_ID" ]; then
  measure "GET /api/music/resolve/$TRACK_ID" "$BASE/api/music/resolve/$TRACK_ID"
else
  echo "Resolve: skip (no track id from search)"
fi

echo "--- done"
