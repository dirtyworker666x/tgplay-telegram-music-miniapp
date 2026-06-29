import hashlib
import json
import os
import secrets
from typing import Any, Dict, Optional

import aiohttp

VK_API_URL = "https://api.vk.com/method"
VK_USER_AGENT = os.getenv(
    "VK_USER_AGENT",
    "KateMobileAndroid/56 lite-460 (Android 4.4.2; SDK 19; x86; unknown Android SDK built for x86; en)",
)
VK_VERSION = os.getenv("VK_API_VERSION", "5.131")
VK_X_VK_ANDROID_CLIENT = (os.getenv("VK_X_VK_ANDROID_CLIENT") or "new").strip()

_worker_random_device_id: Optional[str] = None


def _device_id_for_token(token: str) -> str:
    global _worker_random_device_id
    env_did = (os.getenv("VK_DEVICE_ID") or "").strip()
    if env_did:
        return env_did[:64]
    if os.getenv("VK_DEVICE_ID_RANDOM", "").strip() == "1":
        if _worker_random_device_id is None:
            _worker_random_device_id = secrets.token_hex(16)
        return _worker_random_device_id
    return hashlib.sha256((token or "vk").encode()).hexdigest()[:32]


def _api_headers() -> Dict[str, str]:
    h: Dict[str, str] = {"User-Agent": VK_USER_AGENT.strip() or VK_USER_AGENT}
    if VK_X_VK_ANDROID_CLIENT:
        h["X-VK-Android-Client"] = VK_X_VK_ANDROID_CLIENT
    return h


def _merge_audio_search_and_execute(method: str, params: Dict[str, Any], token: str) -> Dict[str, Any]:
    out = dict(params)
    did = _device_id_for_token(token)
    if method in ("audio.search", "audio.getById"):
        out["https"] = 1
        out.setdefault("device_id", did)
    elif method == "execute":
        c = out.get("code")
        if isinstance(c, str):
            c2 = c
            if "API.audio.search(" in c2 and '"https":1' not in c2:
                needle = 'API.audio.search({"q":'
                repl = f'API.audio.search({{"https":1,"device_id":"{did}","q":'
                if needle in c2:
                    c2 = c2.replace(needle, repl)
            if "API.audio.getById({" in c2 and 'API.audio.getById({"https":1' not in c2:
                needle = 'API.audio.getById({"audios":'
                repl = f'API.audio.getById({{"https":1,"device_id":"{did}","audios":'
                if needle in c2:
                    c2 = c2.replace(needle, repl)
            out["code"] = c2
    return out


class VKClient:
    """Minimal VK API client for worker: one access token (+optional proxy)."""

    def __init__(self) -> None:
        self._token = (os.getenv("VK_TOKEN") or "").strip()
        if not self._token:
            raise RuntimeError("VK_TOKEN is required for worker")
        self._proxy_url = (os.getenv("VK_PROXY_URL") or "").strip() or None
        timeout = aiohttp.ClientTimeout(
            total=max(5, min(60, int(os.getenv("VK_WORKER_VK_TIMEOUT", "15"))))
        )
        self._session: Optional[aiohttp.ClientSession] = None
        self._timeout = timeout

    async def get_session(self) -> aiohttp.ClientSession:
        if self._session and not self._session.closed:
            return self._session
        if self._proxy_url:
            try:
                from aiohttp_socks import ProxyConnector

                connector = ProxyConnector.from_url(self._proxy_url)
                self._session = aiohttp.ClientSession(
                    connector=connector, timeout=self._timeout
                )
            except Exception as e:
                # Fallback to direct connection on proxy errors
                print(f"⚠️ Worker proxy error {self._proxy_url}: {e}, using direct")
                self._session = aiohttp.ClientSession(timeout=self._timeout)
        else:
            self._session = aiohttp.ClientSession(timeout=self._timeout)
        return self._session

    async def close(self) -> None:
        if self._session and not self._session.closed:
            await self._session.close()

    async def call(self, method: str, params: Dict[str, Any], post: bool = False) -> Dict[str, Any]:
        """Call VK API once with this worker's token.

        Returns parsed JSON: either {"response": ...} or {"error": {...}}.
        For HTTP / network errors, synthesizes {"error": {"error_code": -1, ...}}.
        """
        session = await self.get_session()
        url = f"{VK_API_URL}/{method}"
        headers = _api_headers()
        merged = _merge_audio_search_and_execute(method, params, self._token)
        body = {**merged, "access_token": self._token, "v": VK_VERSION}

        try:
            if post:
                async with session.post(url, data=body, headers=headers) as resp:
                    text = await resp.text()
            else:
                async with session.get(url, params=body, headers=headers) as resp:
                    text = await resp.text()
        except Exception as e:
            msg = str(e)[:200]
            return {"error": {"error_code": -1, "error_msg": f"network error: {msg}"}}

        try:
            data = json.loads(text)
        except Exception:
            # Treat non-JSON / 5xx as error -1
            return {"error": {"error_code": -1, "error_msg": f"invalid json or http body: {text[:200]}"}}

        # If VK returned non-200 with no error object, synthesize -1
        if not isinstance(data, dict):
            return {"error": {"error_code": -1, "error_msg": "non-dict VK response"}}

        return data
