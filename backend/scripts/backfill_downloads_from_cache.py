#!/usr/bin/env python3
"""
Backfill download_to_bot events in analytics from existing mp3_cache files.

Использует mtime файлов в mp3_cache как приблизительное время первого скачивания
и создаёт записи в events_track_usage (action = 'download_to_bot') там, где их ещё нет.

Запуск из корня backend:
  python scripts/backfill_downloads_from_cache.py
  python scripts/backfill_downloads_from_cache.py --cache-dir /tmp/mp3_cache --db /tmp/analytics.db
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from datetime import datetime, timezone

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import analytics_db  # type: ignore


def main() -> None:
    ap = argparse.ArgumentParser(description="Backfill download_to_bot from mp3_cache")
    ap.add_argument("--cache-dir", type=str, default="", help="Каталог с .mp3 (по умолчанию backend/mp3_cache)")
    ap.add_argument("--db", type=str, default="", help="Путь к analytics.db (по умолчанию env ANALYTICS_DB или backend/analytics.db)")
    args = ap.parse_args()

    base_dir = Path(__file__).resolve().parent.parent
    if args.db:
        os.environ["ANALYTICS_DB"] = args.db
        # перезагрузить путь в модуле (уже импортирован)
        analytics_db.DB_PATH = Path(args.db)
    if args.cache_dir:
        cache_dir = Path(args.cache_dir)
    else:
        cache_dir = base_dir / "mp3_cache"

    analytics_db.init_db()
    if not cache_dir.is_dir():
        print(f"mp3_cache not found at {cache_dir}")
        return

    conn = analytics_db._get_conn()  # type: ignore[attr-defined]
    try:
        inserted = 0
        scanned = 0
        for path in cache_dir.glob("*.mp3"):
            scanned += 1
            track_id = path.stem
            try:
                st = path.stat()
            except OSError:
                continue
            ts_utc = int(st.st_mtime)
            # пропускаем будущее/некорректное время
            if ts_utc <= 0:
                continue
            # уже есть download_to_bot по этому треку?
            cur = conn.execute(
                "SELECT 1 FROM events_track_usage WHERE track_id = ? AND action = 'download_to_bot' LIMIT 1",
                (track_id,),
            )
            if cur.fetchone():
                continue
            conn.execute(
                """
                INSERT INTO events_track_usage (
                    ts_utc, telegram_user_id, username, track_id, action,
                    duration_sec, from_cache, region, extra_json
                )
                VALUES (?, NULL, NULL, ?, 'download_to_bot', NULL, 1, NULL, ?)
                """,
                (ts_utc, track_id, "{}"),
            )
            inserted += 1
        conn.commit()
        print(f"Scanned files: {scanned}, inserted download_to_bot events: {inserted}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
