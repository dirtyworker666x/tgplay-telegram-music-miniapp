"""
Pytest conftest: set env and mocks so server_lite can be imported without real tokens/ffmpeg.
Run from repo root: pytest backend/tests/ -v
Or from backend: pytest tests/ -v (with PYTHONPATH=.)
"""
import os
import sys
from pathlib import Path

# Set env before any server_lite import (TokenPool and BOT_TOKEN check at load)
backend_dir = Path(__file__).resolve().parent.parent
os.environ.setdefault("BOT_TOKEN", "123456:test_bot_token_for_pytest")
os.environ.setdefault("VK_TOKEN", "test_vk_token_for_pytest")
os.environ.setdefault("ANALYTICS_ADMIN_KEY", "test_admin_key_pytest_789")
os.environ.setdefault("TELEGRAM_OAUTH_CLIENT_ID", "123456789")
os.environ.setdefault("TGPLAY_WEB_SESSION_SECRET", "pytest_web_session_secret_key_min_32_chars_ok")
# Иначе resolve/download в TestClient бьют в почасовой лимит по IP и дают 429
os.environ.setdefault("RATE_LIMIT_DISABLED", "1")

# Ensure ffmpeg check passes when ffmpeg is not installed (e.g. CI)
import shutil
_original_which = shutil.which
def _mock_which(cmd):
    if cmd == "ffmpeg":
        return "/usr/bin/ffmpeg"
    return _original_which(cmd)
shutil.which = _mock_which

# Add backend to path and import app (after env and ffmpeg mock)
if str(backend_dir) not in sys.path:
    sys.path.insert(0, str(backend_dir))

from fastapi.testclient import TestClient

# Import server_lite last so it sees env and mocked which
import server_lite  # noqa: E402

def get_app():
    return server_lite.app

def pytest_configure(config):
    config.addinivalue_line("markers", "asyncio: mark test as async (pytest-asyncio)")


def pytest_sessionfinish(session, exitstatus):
    # Close aiohttp sessions created by server_lite.get_session() during tests
    try:
        import asyncio

        async def _close():
            if getattr(server_lite, "_http_session", None) is not None and not server_lite._http_session.closed:
                await server_lite._http_session.close()
            if getattr(server_lite, "_tg_upload_session", None) is not None and not server_lite._tg_upload_session.closed:
                await server_lite._tg_upload_session.close()
            for s in getattr(server_lite, "_proxy_sessions", {}).values():
                try:
                    if s is not None and not s.closed:
                        await s.close()
                except Exception:
                    pass

        asyncio.run(_close())
    except Exception:
        pass
