#!/usr/bin/env python3
"""
Пересчёт суточных и месячных агрегатов за период (для отображения полной статистики с даты старта).

Запуск из корня backend:
  python scripts/backfill_aggregates.py --from 2025-02-09
"""
from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import analytics_db


def main() -> None:
    ap = argparse.ArgumentParser(description="Пересчёт агрегатов аналитики за период")
    ap.add_argument("--from", dest="from_date", default="2025-02-09", help="Начальная дата YYYY-MM-DD (UTC)")
    ap.add_argument("--db", type=str, default="", help="Путь к analytics.db (по умолчанию env ANALYTICS_DB или backend/analytics.db)")
    args = ap.parse_args()
    from_str = getattr(args, "from_date", "2025-02-09")
    if args.db:
        import os
        os.environ["ANALYTICS_DB"] = args.db
        analytics_db.DB_PATH = Path(args.db)
    analytics_db.init_db()

    try:
        start = datetime.strptime(from_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    except ValueError:
        print("Неверный формат даты. Используй YYYY-MM-DD.")
        sys.exit(1)

    now = datetime.now(timezone.utc)
    end = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    if start > end:
        print("Начальная дата в будущем или сегодня. Укажи дату до вчера.")
        sys.exit(1)

    current = start
    days_done = 0
    while current <= end:
        d = current.strftime("%Y-%m-%d")
        analytics_db.recompute_daily_aggregate(d)
        days_done += 1
        if days_done <= 5 or days_done % 10 == 0:
            print(f"  daily: {d}")
        current += timedelta(days=1)

    months_done = set()
    current = start
    while current <= end:
        m = current.strftime("%Y-%m")
        if m not in months_done:
            analytics_db.recompute_monthly_aggregate(m)
            months_done.add(m)
            print(f"  monthly: {m}")
        current = (current.replace(day=1) + timedelta(days=32)).replace(day=1)

    print(f"Готово: пересчитано дней {days_done}, месяцев {len(months_done)}.")


if __name__ == "__main__":
    main()
