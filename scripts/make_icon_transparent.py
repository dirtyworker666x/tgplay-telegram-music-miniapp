#!/usr/bin/env python3
"""Делает голубой фон на иконке прозрачным, белая фигура остаётся без изменений."""
import sys
from pathlib import Path

try:
    from PIL import Image
except ImportError:
    print("Установите Pillow: pip install Pillow")
    sys.exit(1)

def main():
    root = Path(__file__).resolve().parent.parent
    src = root / "public" / "icon-original.png"
    out = root / "public" / "icon.png"
    if not src.exists():
        print(f"Не найден файл: {src}")
        sys.exit(1)

    img = Image.open(src).convert("RGBA")
    data = img.getdata()
    new_data = []
    # Всё голубое/синее — в прозрачное. Белая фигура не трогаем.
    for item in data:
        r, g, b, a = item
        # Голубой фон: синий доминирует, не белый (белый = r,g,b все высокие)
        is_white = r > 200 and g > 200 and b > 200
        is_blue = not is_white and b > 100 and (b >= r or b >= g) and (r + g + b) < 600
        if is_blue:
            new_data.append((r, g, b, 0))
        else:
            new_data.append(item)
    img.putdata(new_data)
    img.save(out, "PNG")
    print(f"Сохранено: {out}")

if __name__ == "__main__":
    main()
