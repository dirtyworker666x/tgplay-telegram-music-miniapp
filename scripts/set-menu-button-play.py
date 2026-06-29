#!/usr/bin/env python3
"""
Проверка и принудительная установка кнопки меню бота на «PLAY».
Показывает, что сейчас видит Telegram (getChatMenuButton), затем ставит PLAY.

Запуск из корня проекта: python3 scripts/set-menu-button-play.py
Требует backend/.env с BOT_TOKEN.
"""
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "backend"))
from telegram_welcome import WEBAPP_URL_CANONICAL, MENU_BUTTON_TEXT

ENV = ROOT / "backend" / ".env"
BASE = "https://api.telegram.org/bot"


def load_token():
    if not ENV.exists():
        print(f"❌ Нет файла {ENV}")
        sys.exit(1)
    for line in ENV.read_text().splitlines():
        line = line.strip()
        if line.startswith("BOT_TOKEN="):
            return line.split("=", 1)[1].strip().strip('"\'')
    print("❌ BOT_TOKEN не найден в backend/.env")
    sys.exit(1)


def api_get(token: str, method: str) -> dict:
    import urllib.request
    url = f"{BASE}{token}/{method}"
    with urllib.request.urlopen(url, timeout=15) as r:
        return json.loads(r.read().decode())


def api_post(token: str, method: str, **kwargs) -> dict:
    import urllib.request
    url = f"{BASE}{token}/{method}"
    data = json.dumps(kwargs).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=15) as r:
        return json.loads(r.read().decode())


def main():
    token = load_token()
    print(f"Текст кнопки из telegram_welcome: {MENU_BUTTON_TEXT!r}")
    print(f"URL: {WEBAPP_URL_CANONICAL}\n")

    print("1. Текущая кнопка (getChatMenuButton):")
    j = api_get(token, "getChatMenuButton")
    if not j.get("ok"):
        print("   Ошибка:", j.get("description", j))
        sys.exit(1)
    result = j.get("result", {})
    print("   ", json.dumps(result, ensure_ascii=False, indent=2))
    current_text = result.get("text") if result.get("type") == "web_app" else None
    if current_text == MENU_BUTTON_TEXT:
        print(f"   Уже установлено {MENU_BUTTON_TEXT!r}. Повторно выставляю для надёжности.\n")

    payload = {
        "type": "web_app",
        "text": MENU_BUTTON_TEXT,
        "web_app": {"url": WEBAPP_URL_CANONICAL},
    }

    print("2. Устанавливаю setChatMenuButton (application/json):")
    j = api_post(token, "setChatMenuButton", menu_button=payload)
    if j.get("ok"):
        print("   ✅ OK")
    else:
        print("   Ошибка:", j.get("description", j))
        print("3. Пробую form с menu_button как JSON-строка:")
        import urllib.request
        import urllib.parse
        data = urllib.parse.urlencode({"menu_button": json.dumps(payload)}).encode()
        req = urllib.request.Request(
            f"{BASE}{token}/setChatMenuButton",
            data=data,
            method="POST",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            j2 = json.loads(r.read().decode())
        if j2.get("ok"):
            print("   ✅ OK (form)")
        else:
            print("   Ошибка:", j2.get("description", j2))
            sys.exit(1)

    print("4. Проверка после установки (getChatMenuButton):")
    j = api_get(token, "getChatMenuButton")
    if j.get("ok"):
        res = j.get("result", {})
        print("   ", json.dumps(res, ensure_ascii=False, indent=2))
        if res.get("type") == "web_app" and res.get("text") == MENU_BUTTON_TEXT:
            print(f"\n✅ Кнопка в API: {MENU_BUTTON_TEXT!r}. В клиенте может показываться «ОТКРЫТЬ» в списке чатов — это ограничение Telegram.")
    else:
        print("   Ошибка:", j.get("description", j))


if __name__ == "__main__":
    main()
