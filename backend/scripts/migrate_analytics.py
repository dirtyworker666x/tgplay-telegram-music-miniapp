#!/usr/bin/env python3
"""
Миграция аналитики: создаёт новые таблицы (если ещё нет) и при желании
переносит данные из старой таблицы events в новую схему (events_user_activity,
events_button_clicks, events_track_usage, events_errors).

Запуск из корня backend:
  python scripts/migrate_analytics.py [--migrate-old]
Без флага --migrate-old только инициализирует БД (init_db).
С флагом — дополнительно копирует старые события в новые таблицы.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.chdir(Path(__file__).resolve().parent.parent)

import analytics_db


def migrate_old_events() -> None:
    conn = analytics_db._get_conn()
    try:
        conn.execute("CREATE TABLE IF NOT EXISTS _migrated_events (old_id INTEGER PRIMARY KEY)")
        conn.commit()
        cur = conn.execute(
            "SELECT id, ts, event_type, payload, user_hash FROM events ORDER BY id"
        )
        rows = cur.fetchall()
        cur = conn.execute("SELECT old_id FROM _migrated_events")
        migrated_ids = {r[0] for r in cur.fetchall()}
        to_migrate = [r for r in rows if r[0] not in migrated_ids]
        if not to_migrate:
            print("Нет новых записей в events для миграции (все уже перенесены).")
            return
        for row in to_migrate:
            eid, ts, event_type, payload_json, user_hash = row
            ts_utc = int(ts) if isinstance(ts, (int, float)) else int(float(ts))
            try:
                payload = json.loads(payload_json) if payload_json else {}
            except Exception:
                payload = {}
            if event_type in ("app_open", "search"):
                conn.execute(
                    """
                    INSERT INTO events_user_activity (
                        ts_utc, telegram_user_id, username, event_type, event_source, extra_json
                    )
                    VALUES (?, NULL, NULL, ?, 'miniapp', ?)
                    """,
                    (ts_utc, "open_app" if event_type == "app_open" else event_type, json.dumps({"user_hash": user_hash or "", "migrated_id": eid})),
                )
            elif event_type in ("button_add_playlist", "button_add_send", "button_remove"):
                conn.execute(
                    """
                    INSERT INTO events_button_clicks (
                        ts_utc, telegram_user_id, username, button_id, context, extra_json
                    )
                    VALUES (?, NULL, NULL, ?, NULL, ?)
                    """,
                    (ts_utc, event_type, json.dumps({"user_hash": user_hash or "", "migrated_id": eid})),
                )
            elif event_type == "track_play":
                conn.execute(
                    """
                    INSERT INTO events_track_usage (
                        ts_utc, telegram_user_id, username, track_id, action, from_cache, extra_json
                    )
                    VALUES (?, NULL, NULL, ?, 'play', 0, ?)
                    """,
                    (ts_utc, payload.get("track_id") or "", json.dumps({"user_hash": user_hash or "", "migrated_id": eid})),
                )
            elif event_type == "track_finish":
                conn.execute(
                    """
                    INSERT INTO events_track_usage (
                        ts_utc, telegram_user_id, username, track_id, action, from_cache, extra_json
                    )
                    VALUES (?, NULL, NULL, ?, 'complete', 0, ?)
                    """,
                    (ts_utc, payload.get("track_id") or "", json.dumps({"user_hash": user_hash or "", "migrated_id": eid})),
                )
            elif event_type == "error":
                conn.execute(
                    """
                    INSERT INTO events_errors (
                        ts_utc, telegram_user_id, username, error_key, message, extra_json
                    )
                    VALUES (?, NULL, NULL, ?, ?, ?)
                    """,
                    (ts_utc, payload.get("place") or "frontend_error", payload.get("message") or "", json.dumps({"user_hash": user_hash or "", "migrated_id": eid})),
                )
            conn.execute("INSERT OR IGNORE INTO _migrated_events (old_id) VALUES (?)", (eid,))
        conn.commit()
        print(f"Перенесено записей из events: {len(to_migrate)}")
    finally:
        conn.close()


def main() -> None:
    ap = argparse.ArgumentParser(description="Миграция аналитики TGPlay")
    ap.add_argument("--migrate-old", action="store_true", help="Перенести старые события из events в новую схему")
    ap.add_argument("--db", type=str, default="", help="Путь к analytics.db (по умолчанию env ANALYTICS_DB или backend/analytics.db)")
    args = ap.parse_args()
    if args.db:
        os.environ["ANALYTICS_DB"] = args.db
        analytics_db.DB_PATH = Path(args.db)
    analytics_db.init_db()
    print("Таблицы аналитики инициализированы.")
    if args.migrate_old:
        migrate_old_events()
    print("Готово.")


if __name__ == "__main__":
    main()
