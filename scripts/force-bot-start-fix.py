#!/usr/bin/env python3
"""
Однократный скрипт: принудительно выставить в Telegram боте
- кнопку меню PLAY → tgplay.fun,
- описание бота (About) — текст из telegram_welcome.BOT_ABOUT_TEXT.
Webhook НЕ трогаем — иначе бот перестанет реагировать на /start (обработка на сервере).

Запуск из корня проекта: python3 scripts/force-bot-start-fix.py
Требует backend/.env с BOT_TOKEN.
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ENV = ROOT / "backend" / ".env"
BASE = "https://api.telegram.org/bot"
sys.path.insert(0, str(ROOT / "backend"))
from telegram_welcome import WEBAPP_URL_CANONICAL, BOT_NAME, BOT_ABOUT_TEXT, BOT_DESCRIPTION
URL_CANONICAL = WEBAPP_URL_CANONICAL


def load_token():
    if not ENV.exists():
        print(f"❌ Нет файла {ENV}")
        sys.exit(1)
    token = None
    for line in ENV.read_text().splitlines():
        line = line.strip()
        if line.startswith("BOT_TOKEN="):
            token = line.split("=", 1)[1].strip().strip('"\'')
            break
    if not token:
        print("❌ BOT_TOKEN не найден в backend/.env")
        sys.exit(1)
    return token


def api(token: str, method: str, **kwargs) -> dict:
    url = f"{BASE}{token}/{method}"
    data = json.dumps(kwargs).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def main():
    token = load_token()
    print("1. Ставлю имя бота (для поиска):", BOT_NAME)
    j = api(token, "setMyName", name=BOT_NAME[:64])
    if j.get("ok"):
        print("   ✅ Имя установлено")
    else:
        print("   ⚠️", j.get("description", j))
    time.sleep(1)

    print("2. Ставлю кнопку меню: PLAY →", URL_CANONICAL)
    j = api(
        token,
        "setChatMenuButton",
        menu_button={
            "type": "web_app",
            "text": "PLAY",  # см. telegram_welcome.MENU_BUTTON_TEXT
            "web_app": {"url": URL_CANONICAL},
        },
    )
    if j.get("ok"):
        print("   ✅ Меню: PLAY, URL = tgplay.fun")
    else:
        print("   ⚠️", j.get("description", j))
    time.sleep(1.5)

    print("3. Выставляю About + Description (default + ru)…")
    about_text = BOT_ABOUT_TEXT[:120]
    for lang_label, kw in [("default", {}), ("ru", {"language_code": "ru"})]:
        j = api(token, "setMyShortDescription", short_description=about_text, **kw)
        if j.get("ok"):
            print(f"   ✅ About ({lang_label})")
        else:
            print(f"   ⚠️ setMyShortDescription({lang_label}):", j.get("description", j))
        j = api(token, "setMyDescription", description=BOT_DESCRIPTION, **kw)
        if j.get("ok"):
            print(f"   ✅ description ({lang_label})")
        else:
            print(f"   ⚠️ setMyDescription({lang_label}):", j.get("description", j))
        time.sleep(1)

    print("4. Проверяю webhook (бот должен получать /start через сервер)…")
    j = api(token, "getWebhookInfo")
    wh = j.get("result", {}).get("url") or ""
    if wh == f"{URL_CANONICAL}/api/telegram-webhook":
        print("   ✅ Webhook уже на сервере")
    else:
        j2 = api(token, "setWebhook", url=f"{URL_CANONICAL}/api/telegram-webhook")
        if j2.get("ok"):
            print("   ✅ Webhook установлен → сервер")
        else:
            print("   ⚠️ setWebhook:", j2.get("description", j2))
    print("\n✅ Готово. About установлен, кнопка PLAY. /start обрабатывается сервером.")


if __name__ == "__main__":
    main()
