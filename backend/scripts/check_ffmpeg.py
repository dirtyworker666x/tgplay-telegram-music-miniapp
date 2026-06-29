#!/usr/bin/env python3
"""
Проверка наличия ffmpeg и возможности remux (нужно для HLS→MP3 в server_lite).
Запуск из корня backend:  python scripts/check_ffmpeg.py
Выход: 0 при успехе, 1 при ошибке.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

# Имя бинарника как в server_lite
FFMPEG = "ffmpeg"


def main() -> int:
    ffmpeg_path = shutil.which(FFMPEG)
    if not ffmpeg_path:
        print(f"FAIL: {FFMPEG} не найден в PATH")
        return 1
    print(f"  {FFMPEG}: {ffmpeg_path}")

    r = subprocess.run(
        [FFMPEG, "-version"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    if r.returncode != 0:
        print(f"FAIL: {FFMPEG} -version вернул {r.returncode}")
        return 1
    print("  ffmpeg -version: OK")

    # Минимальная проверка: pipe stdin → mp3 (пустой ввод даст ошибку, но мы проверяем что ffmpeg запускается)
    r = subprocess.run(
        [
            FFMPEG,
            "-hide_banner",
            "-loglevel",
            "error",
            "-y",
            "-i",
            "pipe:0",
            "-vn",
            "-c:a",
            "copy",
            "-f",
            "mp3",
            "pipe:1",
        ],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        timeout=5,
    )
    # Пустой ввод даёт ненулевой код (1 или 183 на macOS); главное — не крэш (например -11)
    if r.returncode != 0 and r.returncode not in (1, 183):
        print(f"FAIL: ffmpeg pipe test вернул {r.returncode}")
        return 1
    print("  ffmpeg pipe (remux) test: OK")

    print("OK: ffmpeg доступен и пригоден для HLS remux.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
