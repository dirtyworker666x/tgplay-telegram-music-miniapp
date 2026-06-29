#!/usr/bin/env python3
"""
Проверка скриптов аналитики на тестовой БД и временном каталоге:
- migrate_analytics.py (init + опционально migrate-old)
- backfill_aggregates.py
- backfill_downloads_from_cache.py

Запуск из корня backend:
  python scripts/check_analytics_sanity.py
Выход: 0 при успехе, 1 при ошибке.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parent.parent
SCRIPTS = BACKEND_ROOT / "scripts"


def run(cmd: list[str], env: dict | None = None, cwd: Path | None = None) -> bool:
    env = env or os.environ
    cwd = cwd or BACKEND_ROOT
    r = subprocess.run(cmd, env={**os.environ, **(env or {})}, cwd=str(cwd), capture_output=True, text=True)
    if r.returncode != 0:
        print(f"  stderr: {r.stderr}")
        print(f"  stdout: {r.stdout}")
    return r.returncode == 0


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="analytics_sanity_") as tmp:
        tmp_path = Path(tmp)
        db_path = tmp_path / "analytics.db"
        cache_dir = tmp_path / "mp3_cache"
        cache_dir.mkdir()
        env = {**os.environ, "ANALYTICS_DB": str(db_path)}

        # 1) Миграция: только init (в тестовой БД нет старой таблицы events)
        print("1. migrate_analytics.py --db ...")
        if not run(
            [sys.executable, str(SCRIPTS / "migrate_analytics.py"), "--db", str(db_path)],
            env=env,
            cwd=BACKEND_ROOT,
        ):
            print("FAIL: migrate_analytics.py")
            return 1

        # 2) Пересчёт агрегатов за короткий период
        print("2. backfill_aggregates.py --db ... --from 2025-02-01")
        if not run(
            [
                sys.executable,
                str(SCRIPTS / "backfill_aggregates.py"),
                "--db",
                str(db_path),
                "--from",
                "2025-02-01",
            ],
            env=env,
            cwd=BACKEND_ROOT,
        ):
            print("FAIL: backfill_aggregates.py")
            return 1

        # 3) Два фейковых .mp3 в временном каталоге
        (cache_dir / "test_track_1.mp3").write_bytes(b"\xff\xfb\x90\x00")  # минимальный заголовок
        (cache_dir / "test_track_2.mp3").write_bytes(b"\xff\xfb\x90\x00")
        print("3. backfill_downloads_from_cache.py --cache-dir ... --db ...")
        if not run(
            [
                sys.executable,
                str(SCRIPTS / "backfill_downloads_from_cache.py"),
                "--cache-dir",
                str(cache_dir),
                "--db",
                str(db_path),
            ],
            env=env,
            cwd=BACKEND_ROOT,
        ):
            print("FAIL: backfill_downloads_from_cache.py")
            return 1

    print("OK: все проверки аналитики прошли.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
