#!/usr/bin/env python3
"""
Генерация фейковых событий аналитики за последние N дней для проверки
агрегатов и дашборда. Не перезаписывает существующие данные — только добавляет.

Запуск из корня backend:
  python scripts/seed_fake_analytics.py [--days 7]
"""
from __future__ import annotations

import argparse
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from datetime import datetime, timezone, timedelta

# импорт после смены пути
import analytics_db


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7, help="Количество дней в прошлое для генерации")
    args = ap.parse_args()
    analytics_db.init_db()

    now = datetime.now(timezone.utc)
    users = [1000 + i for i in range(20)]
    usernames = [f"user_{u}" for u in users]

    events_created = 0
    for day_offset in range(args.days):
        base_ts = int((now - timedelta(days=day_offset)).replace(hour=12, minute=0, second=0, microsecond=0).timestamp())
        for _ in range(random.randint(5, 25)):
            uid = random.choice(users)
            uname = usernames[users.index(uid)]
            ts = base_ts + random.randint(-3600, 3600)
            # записываем через сырые INSERT, т.к. логи принимают "сейчас"
            conn = analytics_db._get_conn()
            try:
                conn.execute(
                    "INSERT INTO events_user_activity (ts_utc, telegram_user_id, username, event_type, event_source) VALUES (?, ?, ?, 'open_app', 'miniapp')",
                    (ts, uid, uname),
                )
                events_created += 1
                if random.random() < 0.7:
                    conn.execute(
                        "INSERT INTO events_track_usage (ts_utc, telegram_user_id, username, track_id, action, from_cache) VALUES (?, ?, ?, ?, 'play', 0)",
                        (ts, uid, uname, f"track_{random.randint(1,50)}"),
                    )
                    events_created += 1
                if random.random() < 0.3:
                    conn.execute(
                        "INSERT INTO events_button_clicks (ts_utc, telegram_user_id, username, button_id, context) VALUES (?, ?, ?, 'button_add_send', 'main')",
                        (ts, uid, uname),
                    )
                    events_created += 1
                conn.commit()
            finally:
                conn.close()

    print(f"Добавлено фейковых событий: {events_created}")

    # пересчёт агрегатов за последние дни
    for day_offset in range(1, min(args.days + 1, 8)):
        d = (now - timedelta(days=day_offset)).strftime("%Y-%m-%d")
        analytics_db.recompute_daily_aggregate(d)
        print(f"  daily aggregate: {d}")
    analytics_db.recompute_monthly_aggregate(now.strftime("%Y-%m"))
    print("  monthly aggregate: ", now.strftime("%Y-%m"))
    print("Готово. Открой дашборд и проверь данные.")


if __name__ == "__main__":
    main()
