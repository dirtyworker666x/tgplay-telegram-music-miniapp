#!/usr/bin/env python3
"""
Повторно заполнить user_bot_audio_delivered из events_track_usage (download_to_bot)
и из events_button_clicks (button_download + extra.track_id).

По умолчанию используется backend/analytics.db или путь из env ANALYTICS_DB.

Пример на VPS (из корня репозитория):
  python3 scripts/backfill-user-bot-audio-delivered.py
  python3 scripts/backfill-user-bot-audio-delivered.py --force   # сбросить флаг и слить снова
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
BACKEND = REPO_ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))


def main() -> int:
    ap = argparse.ArgumentParser(description="Backfill user_bot_audio_delivered from analytics SQLite")
    ap.add_argument(
        "--force",
        action="store_true",
        help="Игнорировать analytics_meta (повторить слияние)",
    )
    args = ap.parse_args()

    import analytics_db

    db_path = os.environ.get("ANALYTICS_DB", "").strip() or str(analytics_db.DB_PATH)
    print(f"DB: {db_path}", flush=True)
    r = analytics_db.backfill_user_bot_audio_delivered_from_history(force=args.force)
    if r.get("skipped"):
        print("Пропуск: backfill уже выполнялся (используйте --force для повтора).", flush=True)
        return 0
    print(
        f"Готово: sqlite_changes={r.get('sqlite_changes')}, "
        f"track_usage={r.get('changes_from_track_usage')}, "
        f"button_clicks={r.get('changes_from_button_clicks')}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
