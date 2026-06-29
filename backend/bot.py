"""
TGPlay Telegram Bot — один экземпляр (fcntl flock), /start → одно сообщение.
"""
from __future__ import annotations
import asyncio, os, signal, sys
from pathlib import Path
import aiohttp

try:
    import fcntl
except ImportError:
    fcntl = None  # Windows

from secrets_env import load_secrets_env

load_secrets_env()

from telegram_welcome import WELCOME_MESSAGE, WEBAPP_URL_CANONICAL, BOT_ABOUT_TEXT, BOT_DESCRIPTION, BOT_NAME, MENU_BUTTON_TEXT

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
LOCK_FILE = Path(__file__).parent / "bot.lock"
_lock_fd = None

if not BOT_TOKEN:
    print("❌  BOT_TOKEN не указан в backend/.env!")
    sys.exit(1)

API = f"https://api.telegram.org/bot{BOT_TOKEN}"


async def tg_request(session: aiohttp.ClientSession, method: str, **kwargs) -> dict:
    """Вызов Telegram Bot API."""
    async with session.post(f"{API}/{method}", json=kwargs) as resp:
        data = await resp.json()
    if not data.get("ok"):
        print(f"⚠️  TG API {method}: {data.get('description', data)}")
    return data


async def set_menu_button(session: aiohttp.ClientSession):
    """Устанавливает кнопку PLAY в меню бота — всегда tgplay.fun."""
    await tg_request(
        session,
        "setChatMenuButton",
        menu_button={
            "type": "web_app",
            "text": "PLAY",  # всегда PLAY
            "web_app": {"url": WEBAPP_URL_CANONICAL},
        },
    )
    print(f"✅ Menu button set → {WEBAPP_URL_CANONICAL}")


async def set_bot_commands(session: aiohttp.ClientSession):
    """Устанавливает команды бота."""
    await tg_request(
        session,
        "setMyCommands",
        commands=[
            {"command": "start", "description": "Запустить музыкальный плеер"},
            {"command": "playlist", "description": "Мой плейлист"},
        ],
    )
    print("✅ Bot commands set")


async def set_bot_name(session: aiohttp.ClientSession):
    """Имя бота для поиска в Telegram (до 64 символов)."""
    name = (BOT_NAME or "").strip()[:64]
    if not name:
        return
    for payload in [{"name": name}, {"name": name, "language_code": "ru"}]:
        r = await tg_request(session, "setMyName", **payload)
        if not r.get("ok"):
            print(f"⚠️  setMyName: {r.get('description', r)}")
    print(f"✅ Имя бота установлено: {name!r}")


async def set_bot_description(session: aiohttp.ClientSession):
    """About (short) + Description (full) — чтобы не слетало и индексировалось в поиске."""
    about_text = (BOT_ABOUT_TEXT or "")[:120]
    if not about_text:
        return
    for lang_code, label in [("", "default"), ("ru", "ru")]:
        kw = {"short_description": about_text, "language_code": lang_code} if lang_code else {"short_description": about_text}
        r1 = await tg_request(session, "setMyShortDescription", **kw)
        kw2 = {"description": BOT_DESCRIPTION or "", "language_code": lang_code} if lang_code else {"description": BOT_DESCRIPTION or ""}
        r2 = await tg_request(session, "setMyDescription", **kw2)
        if not r1.get("ok"):
            print(f"⚠️  setMyShortDescription({label}): {r1.get('description', r1)}")
        if not r2.get("ok"):
            print(f"⚠️  setMyDescription({label}): {r2.get('description', r2)}")
    print("✅ Описание бота установлено (About + поиск)")


_processed_updates: set[int] = set()
_MAX_PROCESSED = 500


async def handle_update(session: aiohttp.ClientSession, update: dict):
    """Обрабатывает входящее обновление."""
    update_id = update.get("update_id")
    if update_id in _processed_updates:
        return
    _processed_updates.add(update_id)
    if len(_processed_updates) > _MAX_PROCESSED:
        _processed_updates.clear()

    message = update.get("message")
    if not message:
        return

    chat_id = message.get("chat", {}).get("id")
    if not chat_id:
        return

    text = (message.get("text") or "").strip()
    if text.startswith("/start") or text.startswith("/playlist"):
        await tg_request(
            session,
            "sendMessage",
            chat_id=chat_id,
            text=WELCOME_MESSAGE,
            reply_markup={
                "inline_keyboard": [
                    [{"text": "PLAY", "web_app": {"url": WEBAPP_URL_CANONICAL}}],
                ],
            },
        )


async def poll_updates(session: aiohttp.ClientSession):
    """Long polling для получения обновлений."""
    offset = 0
    print("🔄 Polling for updates...")

    while True:
        try:
            data = await tg_request(
                session,
                "getUpdates",
                offset=offset,
                timeout=30,
                allowed_updates=["message"],
            )
            updates = data.get("result", [])
            for update in updates:
                offset = update["update_id"] + 1
                await handle_update(session, update)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"⚠️  Polling error: {e}")
            await asyncio.sleep(3)


async def main():
    print(f"🤖 TGPlay Bot starting...")
    print(f"🌐 WebApp URL: {WEBAPP_URL_CANONICAL}")

    async with aiohttp.ClientSession() as session:
        # Проверяем бота
        me = await tg_request(session, "getMe")
        if me.get("ok"):
            bot = me["result"]
            print(f"✅ Bot: @{bot.get('username', '?')} ({bot.get('first_name', '?')})")
        else:
            print("❌ Не удалось подключиться к боту!")
            return

        # Меню, имя, команды и описание — для поиска и профиля в Telegram
        await set_menu_button(session)
        await set_bot_name(session)
        await set_bot_commands(session)
        await set_bot_description(session)

        use_poll = os.getenv("TG_BOT_POLLING", "").strip() == "1"
        if not use_poll:
            print("━" * 50)
            print("✅ Меню и команды выставлены. Webhook НЕ снимаем (его обслуживает server_lite).")
            print("   Локальный long polling: TG_BOT_POLLING=1 python bot.py")
            print("━" * 50)
            return

        # Только для отдельного режима polling: снять webhook, иначе getUpdates пустой
        await tg_request(session, "deleteWebhook", drop_pending_updates=True)

        print("━" * 50)
        print("▶️ Polling: напиши /start в Telegram (webhook отключён).")
        print("━" * 50)

        await poll_updates(session)


def _acquire_lock() -> bool:
    """Только один экземпляр: fcntl.flock (Linux/macOS). Возвращает True если lock взят."""
    global _lock_fd
    if fcntl is None:
        return True
    try:
        _lock_fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_RDWR, 0o600)
        fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except (OSError, BlockingIOError) as e:
        if _lock_fd is not None:
            try:
                os.close(_lock_fd)
            except OSError:
                pass
            _lock_fd = None
        return False


def _release_lock() -> None:
    global _lock_fd
    if _lock_fd is not None and fcntl is not None:
        try:
            fcntl.flock(_lock_fd, fcntl.LOCK_UN)
            os.close(_lock_fd)
        except OSError:
            pass
        _lock_fd = None


def _on_signal(signum, frame):
    _release_lock()
    sys.exit(0)


if __name__ == "__main__":
    if sys.platform != "win32":
        signal.signal(signal.SIGTERM, _on_signal)
    if not _acquire_lock():
        print("❌ Уже запущен другой экземпляр бота. Останови его: pkill -f 'python.*bot.py'")
        sys.exit(1)
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n👋 Bot stopped")
    finally:
        _release_lock()