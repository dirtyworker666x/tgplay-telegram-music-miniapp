"""
TGPlay Lite API — поиск VK + ffmpeg-стриминг HLS→MP3 + Telegram auth + плейлисты.
Запускай:  python3 server_lite.py

Оптимизации:
- Единая aiohttp сессия (connection pool)
- Параллельные VK-запросы через asyncio.gather
- Быстрый ffmpeg пресет для низкой задержки
- Кеширование VK audio URL (до 24 ч), поиск — 7 дней (общий кэш для всех)
- Потоковая отдача MP3 — клиент играет через 1-2 сек
- Security headers
"""
from __future__ import annotations
import asyncio, hashlib, hmac, html, io, json, logging, os, random, re, secrets, shutil, struct, time, unicodedata, uuid, zlib
from collections import OrderedDict, defaultdict, deque
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional, List, Dict, Any, Tuple, Set, AbstractSet
from urllib.parse import parse_qs, quote, unquote, urlencode
import base64
import redis.asyncio as aioredis

from secrets_env import load_secrets_env
from yt_downloader import (
    search_youtube_tracks,
    download_youtube_audio,
    get_cached_path,
    extract_video_id,
    fetch_youtube_track_meta_sync,
    youtube_radio_tracks_from_video_id,
    youtube_related_artist_names,
)
from sc_client_simple import (
    SoundCloudClient,
    build_soundcloud_track_id,
    is_soundcloud_track_id,
    parse_soundcloud_track_id,
    _translit_ru_to_lat,
    _tokens,
)

# Секреты должны лежать вне репозитория (например ~/.tgplay/secrets.env или /root/.tgplay/secrets.env).
# Этот загрузчик не перезаписывает уже заданные переменные окружения.
load_secrets_env()

logger = logging.getLogger("tgplay")


def _agent_debug_log(hypothesis_id: str, location: str, message: str, data: Dict[str, Any]) -> None:
    # Опциональная NDJSON-диагностика; по умолчанию выкл. (синхронный диск на вебхуке блокирует asyncio).
    if not (os.getenv("TGPLAY_AGENT_DEBUG") or "").strip():
        return
    try:
        _raw = (os.getenv("TGPLAY_AGENT_DEBUG_LOG") or "").strip()
        _p = Path(_raw) if _raw else Path(__file__).resolve().parent / "logs" / "agent-debug.ndjson"
        _p.parent.mkdir(parents=True, exist_ok=True)
        _line = (
            json.dumps(
                {
                    "hypothesisId": hypothesis_id,
                    "location": location,
                    "message": message,
                    "data": data,
                    "timestamp": int(time.time() * 1000),
                },
                ensure_ascii=False,
            )
            + "\n"
        )
        with open(_p, "a", encoding="utf-8") as _f:
            _f.write(_line)
    except Exception:
        pass


from telegram_welcome import WELCOME_MESSAGE, WEBAPP_URL_CANONICAL, BOT_ABOUT_TEXT, BOT_DESCRIPTION, BOT_NAME

BOT_USERNAME = "tgplayxbot"

# Дефолт — Kate Mobile: токены Kate дают заглушку с User-Agent официального клиента (VKAndroidApp/*).
# Официальный user token — задайте VK_USER_AGENT под свой клиент (см. vodka2/vk-audio-token SupportedClients).
VK_USER_AGENT = os.getenv(
    "VK_USER_AGENT",
    "KateMobileAndroid/56 lite-460 (Android 4.4.2; SDK 19; x86; unknown Android SDK built for x86; en)",
)
# Как у Android-клиента ВК; отключить: VK_X_VK_ANDROID_CLIENT=
VK_X_VK_ANDROID_CLIENT = (os.getenv("VK_X_VK_ANDROID_CLIENT") or "new").strip()
_vk_process_random_device_id: Optional[str] = None

# Ротация Kate-style UA (тот же lite-460, разные девайсы) — без суффикса tgplay.
_DEFAULT_VK_ANDROID_UAS = (
    "KateMobileAndroid/56 lite-460 (Android 4.4.2; SDK 19; x86; unknown Android SDK built for x86; en)",
    "KateMobileAndroid/56 lite-460 (Android 11; SDK 30; arm64-v8a; samsung SM-G998B; ru)",
    "KateMobileAndroid/56 lite-460 (Android 10; SDK 29; arm64-v8a; Xiaomi Redmi Note 8 Pro; ru)",
    "KateMobileAndroid/56 lite-460 (Android 12; SDK 31; arm64-v8a; Google Pixel 6; ru)",
)


def _vk_user_agents_list_from_env() -> List[str]:
    """Список User-Agent на токен.

    Раньше VK_USER_AGENTS резали по запятой — у Kate/VKAndroidApp внутри скобок тоже запятые, список ломался.

    Приоритет:
      1) VK_USER_AGENTS_JSON — JSON-массив строк, например ["KateMobile...","VKAndroidApp/..."]
      2) VK_USER_AGENTS с разделителем ||| между полными строками
      3) VK_USER_AGENTS как JSON-массив в одной строке
      4) устаревшее: запятая (только если в UA нет запятых)
    """
    j = (os.getenv("VK_USER_AGENTS_JSON") or "").strip()
    if j:
        try:
            arr = json.loads(j)
            if isinstance(arr, list):
                out = [str(x).strip() for x in arr if str(x).strip()]
                if out:
                    return out
        except json.JSONDecodeError:
            print("⚠️ VK_USER_AGENTS_JSON: невалидный JSON, пропуск")
    raw = (os.getenv("VK_USER_AGENTS") or "").strip()
    if not raw:
        return []
    if "|||" in raw:
        return [p.strip() for p in raw.split("|||") if p.strip()]
    if raw.startswith("["):
        try:
            arr = json.loads(raw)
            if isinstance(arr, list):
                return [str(x).strip() for x in arr if str(x).strip()]
        except json.JSONDecodeError:
            pass
    return [p.strip() for p in raw.split(",") if p.strip()]


def _vk_pick_user_agent_for_token(ua_list: List[str], ua_idx: int) -> str:
    """Один явный UA в списке — применяем ко всем токенам; иначе по индексу или ротация Kate."""
    if len(ua_list) == 1 and ua_list[0].strip():
        return ua_list[0].strip()
    if ua_idx < len(ua_list) and ua_list[ua_idx].strip():
        return ua_list[ua_idx].strip()
    return _DEFAULT_VK_ANDROID_UAS[ua_idx % len(_DEFAULT_VK_ANDROID_UAS)]


def _vk_device_id_for_state(state: "_TokenState") -> str:
    """device_id для audio.search: из VK_DEVICE_ID, случайный при VK_DEVICE_ID_RANDOM=1, иначе стабильный от токена."""
    global _vk_process_random_device_id
    env_did = (os.getenv("VK_DEVICE_ID") or "").strip()
    if env_did:
        return env_did[:64]
    if os.getenv("VK_DEVICE_ID_RANDOM", "").strip() == "1":
        if _vk_process_random_device_id is None:
            _vk_process_random_device_id = secrets.token_hex(16)
        return _vk_process_random_device_id
    raw = (state.token or state.worker_url or "vk").encode()
    return hashlib.sha256(raw).hexdigest()[:32]


def _vk_audio_search_extra_params(state: "_TokenState") -> Dict[str, Any]:
    return {"https": 1, "device_id": _vk_device_id_for_state(state)}


def _vk_execute_inject_audio_search_extras(code: str, state: "_TokenState") -> str:
    """Вставляет https и device_id в каждый API.audio.search внутри VKScript execute."""
    did = _vk_device_id_for_state(state)
    needle = 'API.audio.search({"q":'
    repl = f'API.audio.search({{"https":1,"device_id":"{did}","q":'
    if needle not in code:
        return code
    return code.replace(needle, repl)


def _vk_execute_inject_audio_getbyid_extras(code: str, state: "_TokenState") -> str:
    """https + device_id в API.audio.getById внутри execute (как у мобильных клиентов)."""
    did = _vk_device_id_for_state(state)
    needle = 'API.audio.getById({"audios":'
    repl = f'API.audio.getById({{"https":1,"device_id":"{did}","audios":'
    if needle not in code or 'API.audio.getById({"https":1' in code:
        return code
    return code.replace(needle, repl)


def _vk_expand_vk_method_params(method: str, params: Dict, state: "_TokenState") -> Dict:
    out = dict(params)
    if method in ("audio.search", "audio.getById"):
        out.update(_vk_audio_search_extra_params(state))
    elif method == "execute":
        c = out.get("code")
        if isinstance(c, str):
            c2 = c
            if "API.audio.search(" in c2 and '"https":1' not in c2:
                c2 = _vk_execute_inject_audio_search_extras(c2, state)
            if "API.audio.getById({" in c2 and 'API.audio.getById({"https":1' not in c2:
                c2 = _vk_execute_inject_audio_getbyid_extras(c2, state)
            out["code"] = c2
    return out


def _vk_api_client_headers(user_agent: str) -> Dict[str, str]:
    ua = (user_agent or "").strip() or VK_USER_AGENT
    h: Dict[str, str] = {"User-Agent": ua}
    if VK_X_VK_ANDROID_CLIENT:
        h["X-VK-Android-Client"] = VK_X_VK_ANDROID_CLIENT
    return h


BOT_TOKEN = os.getenv("BOT_TOKEN", "")
SOUNDCLOUD_CLIENT_ID = (os.getenv("SOUNDCLOUD_CLIENT_ID") or "").strip()
SOUNDCLOUD_CLIENT_SECRET = (os.getenv("SOUNDCLOUD_CLIENT_SECRET") or "").strip()
SOUNDCLOUD_TOKEN_CACHE_SECONDS = max(300, min(3600, int(os.getenv("SOUNDCLOUD_TOKEN_CACHE_SECONDS", "3600"))))
# Telegram Login (OIDC): client_id = Bot ID из BotFather; client_secret — из BotFather Web Login (для /token, не для проверки id_token)
TELEGRAM_OAUTH_CLIENT_ID = (os.getenv("TELEGRAM_OAUTH_CLIENT_ID") or "8575565887").strip()
TELEGRAM_OAUTH_CLIENT_SECRET = (os.getenv("TELEGRAM_OAUTH_CLIENT_SECRET") or "").strip()
TGPLAY_WEB_SESSION_SECRET = (os.getenv("TGPLAY_WEB_SESSION_SECRET") or "").strip()
TELEGRAM_OIDC_ISSUER = "https://oauth.telegram.org"
TELEGRAM_OIDC_JWKS_URL = "https://oauth.telegram.org/.well-known/jwks.json"
WEB_SESSION_JWT_ALG = "HS256"
WEB_SESSION_EXPIRE_DAYS = max(1, min(90, int(os.getenv("TGPLAY_WEB_SESSION_DAYS", "30"))))
PORT = int(os.getenv("APP_PORT", "8000"))
LIMIT_CONCURRENCY = max(1, int(os.getenv("LIMIT_CONCURRENCY", "10000")))
REDIS_URL = os.getenv("REDIS_URL", "").strip()
TELEGRAM_WEBHOOK_SECRET = (os.getenv("TELEGRAM_WEBHOOK_SECRET") or "").strip()
SEARCH_PRESOLVE_TOP_N = max(0, min(50, int(os.getenv("SEARCH_PRESOLVE_TOP_N", "10"))))
VK_WORKER_TIMEOUT = max(5, min(60, int(os.getenv("VK_WORKER_TIMEOUT", "15"))))
VK_TOKEN_ACQUIRE_TIMEOUT = max(3, min(30, int(os.getenv("VK_TOKEN_ACQUIRE_TIMEOUT", "8"))))
USE_REDIS_WORKERS = os.getenv("VK_USE_REDIS_WORKERS", "").strip() == "1"
_TRACK_INFO_CACHE_TTL_HOURS = max(1, min(24, int(os.getenv("TRACK_INFO_CACHE_TTL_HOURS", "2"))))
# Лимит Telegram Bot API; треки больше не качаем/не держим в памяти (защита от тормозов)
TG_MAX_FILE_BYTES = 50 * 1024 * 1024

if not BOT_TOKEN:
    print("❌  BOT_TOKEN не указан в backend/.env!")
    exit(1)

SC_CLIENT = SoundCloudClient(
    SOUNDCLOUD_CLIENT_ID,
    SOUNDCLOUD_CLIENT_SECRET,
    token_cache_seconds=SOUNDCLOUD_TOKEN_CACHE_SECONDS,
)


# ─── VK Token Pool: round-robin, per-token rate limit, health tracking ───

# Параметры Token Bucket как у воркеров (claim_head.lua): capacity=3, refill 3/сек
_BUCKET_CAPACITY = 3
_BUCKET_REFILL_PER_SEC = 3.0


class _TokenState:
    __slots__ = ("token", "proxy", "worker_url", "user_agent", "requests", "requests_hour", "errors",
                 "cooldown_until", "throttle_until", "last_error", "last_error_code", "total_requests",
                 "total_errors", "captchas_solved", "captchas_failed", "bucket_tokens", "last_refill_ts")

    def __init__(
        self,
        token: str,
        proxy: Optional[str] = None,
        worker_url: Optional[str] = None,
        user_agent: Optional[str] = None,
    ):
        self.token = token or ""
        self.proxy = proxy
        self.worker_url = worker_url
        # Для каждого ключа может быть свой User-Agent; по умолчанию общий VK_USER_AGENT.
        self.user_agent = (user_agent or VK_USER_AGENT).strip()
        self.requests: List[float] = []      # sliding window 1s (как у воркеров rate:window)
        self.requests_hour: List[float] = [] # timestamps of VK calls in last hour (per-token/worker limit)
        self.errors: List[float] = []         # timestamps of recent errors
        self.cooldown_until: float = 0.0      # unix ts until which token is in cooldown
        self.throttle_until: float = 0.0      # optional: unix ts until token is in throttle (when VK_THROTTLE_DURATION > 0)
        self.last_error: str = ""
        self.last_error_code: int = 0
        self.total_requests: int = 0
        self.total_errors: int = 0
        self.captchas_solved: int = 0
        self.captchas_failed: int = 0
        self.bucket_tokens: float = float(_BUCKET_CAPACITY)   # Token Bucket (как у воркеров rate:bucket)
        self.last_refill_ts: float = 0.0

    @property
    def healthy(self) -> bool:
        return time.time() >= self.cooldown_until

    @property
    def req_per_sec(self) -> float:
        now = time.time()
        self.requests = [t for t in self.requests if now - t < 1.0]
        return len(self.requests)

    def _trim_hour(self, now: float, window: float = 3600.0) -> None:
        self.requests_hour = [t for t in self.requests_hour if now - t < window]

    def under_hourly_limit(self, now: float, limit: int, window: float = 3600.0) -> bool:
        """Не превышен ли почасовой лимит (чтобы один воркер не получил бан от VK)."""
        self._trim_hour(now, window)
        return len(self.requests_hour) < limit

    def record_hourly_request(self, limit: int, window: float = 3600.0) -> None:
        now = time.time()
        self.requests_hour.append(now)
        self._trim_hour(now, window)

    def _refill_bucket(self, now: float) -> None:
        """Token Bucket: пополнение как у воркеров (refill_per_ms = 3/1000)."""
        if self.last_refill_ts <= 0:
            self.last_refill_ts = now
            return
        delta = now - self.last_refill_ts
        self.bucket_tokens = min(_BUCKET_CAPACITY, self.bucket_tokens + delta * _BUCKET_REFILL_PER_SEC)
        self.last_refill_ts = now

    def available(self, now: float, max_rps: int, hourly_limit: int = 0) -> bool:
        """Как у воркеров: Token Bucket (сглаживание) + Sliding Window (строгий анти-burst). Оба должны пройти."""
        if now < self.cooldown_until or now < self.throttle_until:
            return False
        if hourly_limit > 0 and not self.under_hourly_limit(now, hourly_limit):
            return False
        self._refill_bucket(now)
        self.requests = [t for t in self.requests if now - t < 1.0]
        window_ok = len(self.requests) < max_rps
        bucket_ok = self.bucket_tokens >= 1.0
        return window_ok and bucket_ok

    def record_request(self, max_rps: int, throttle_duration: float) -> None:
        now = time.time()
        self._refill_bucket(now)
        self.bucket_tokens -= 1.0
        if self.bucket_tokens < 0:
            self.bucket_tokens = 0.0
        self.requests.append(now)
        self.requests = [t for t in self.requests if now - t < 1.0]
        self.total_requests += 1
        if throttle_duration > 0 and len(self.requests) >= max_rps:
            self.throttle_until = now + throttle_duration

    def record_error(self, code: int, msg: str, cooldown_sec: float = 300):
        now = time.time()
        self.errors.append(now)
        self.total_errors += 1
        self.last_error = msg
        self.last_error_code = code
        # Токен уходит в cooldown только для ошибок перегрева/капчи и HTTP 5xx (временные сбои VK).
        if code in (14, 29, 6, 9, -1):  # captcha, rate limit, too many requests, flood control, HTTP 5xx/502
            self.cooldown_until = now + cooldown_sec
            label = self._label()
            print(f"🛑 Token {label} cooldown {cooldown_sec}s (error {code})")

    def _label(self) -> str:
        """Для логов и админки: суффикс токена или worker:N."""
        if self.worker_url:
            try:
                return f"worker:{self.worker_url.rstrip('/').split('/')[-1] or 'vk'}"
            except Exception:
                return "worker:?"
        return f"...{self.token[-6:]}" if len(self.token) >= 6 else "...??????"


class TokenPool:
    """Round-robin VK token manager: как у воркеров — Token Bucket (сглаживание) + Sliding Window (анти-burst), 3 req/s на ключ. FIFO при нехватке."""

    def __init__(self):
        # Макс. запросов в секунду на один ключ (лимит VK 3). Ограничение только скользящим окном 1 с.
        rps_raw = float(os.getenv("VK_MAX_RPS_PER_TOKEN", "3"))
        self.max_rps_per_token = max(1, min(3, int(round(rps_raw))))
        # Throttle после достижения RPS: 0 = не использовать, только round-robin + окно (та же логика, что у воркеров по IP).
        self.throttle_duration = max(0.0, min(5.0, float(os.getenv("VK_THROTTLE_DURATION", "0"))))
        # Почасовой лимит на токен/воркер: 0 = выключен (достаточно round-robin + 3 req/s на ключ).
        self.per_token_hour_limit = max(0, min(2000, int(os.getenv("RATE_VK_PER_TOKEN_PER_HOUR", "0"))))
        self._states: List[_TokenState] = []
        self._waiters: deque = deque()  # FIFO queue of asyncio.Future, each gets one _TokenState when a token is free

        tokens_raw = os.getenv("VK_TOKENS", "") or os.getenv("VK_TOKEN", "")
        ua_list = _vk_user_agents_list_from_env()
        ua_idx = 0

        for entry in (tokens_raw or "").split(","):
            entry = entry.strip()
            if not entry:
                continue
            # Kate/VKAndroidApp: см. _vk_user_agents_list_from_env (JSON или |||, не запятая внутри UA).
            ua_value = _vk_pick_user_agent_for_token(ua_list, ua_idx)
            ua_idx += 1
            if ":" in entry and "://" in entry:
                parts = entry.split(":", 1)
                if parts[1].startswith("socks") or parts[1].startswith("http"):
                    self._states.append(_TokenState(parts[0], parts[1], None, ua_value))
                else:
                    self._states.append(_TokenState(entry, None, None, ua_value))
            else:
                self._states.append(_TokenState(entry, None, None, ua_value))

        worker_urls_raw = (os.getenv("VK_WORKER_URLS") or "").strip()
        for entry in worker_urls_raw.split(","):
            url = entry.strip()
            if url:
                self._states.append(_TokenState("", worker_url=url))

        if not self._states:
            # VK-токен давно не обязателен: основной путь — SoundCloud, а старые VK-треки
            # резолвятся через YouTube-фолбэк. Без токенов VK API просто отключён (быстрый отказ
            # в _vk_api_call), процесс при этом стартует штатно.
            print("ℹ️  VK токены/воркеры не заданы — VK API отключён (SoundCloud + YouTube-фолбэк).")

        self._idx = 0
        self._lock = None  # lazy init (needs event loop)
        n_workers = sum(1 for s in self._states if s.worker_url)
        hour_note = f", {self.per_token_hour_limit}/час на токен/воркер" if self.per_token_hour_limit > 0 else ""
        throttle_note = f", throttle {self.throttle_duration}s" if self.throttle_duration > 0 else ""
        print(f"🔑 VK Token Pool: {len(self._states)} токенов/воркеров загружено (до {self.max_rps_per_token} запросов/с на ключ{throttle_note}{hour_note})" + (f" ({n_workers} воркеров)" if n_workers else ""))

    def _get_lock(self) -> asyncio.Lock:
        if self._lock is None:
            self._lock = asyncio.Lock()
        return self._lock

    def _first_available_state(self, now: float) -> Optional[_TokenState]:
        """First free token in round-robin order (healthy, not throttled, under RPS)."""
        n = len(self._states)
        for _ in range(n):
            state = self._states[self._idx]
            self._idx = (self._idx + 1) % n
            if state.available(now, self.max_rps_per_token, self.per_token_hour_limit):
                return state
        return None

    async def acquire(self) -> _TokenState:
        """Get next free token (under RPS, not throttled). If all busy, wait in FIFO queue until one frees."""
        loop = asyncio.get_event_loop()
        while True:
            async with self._get_lock():
                now = time.time()
                state = self._first_available_state(now)
                if state is not None:
                    state.record_request(self.max_rps_per_token, self.throttle_duration)  # int 1–3
                    return state
                # No free token: join FIFO queue and wait for dispatcher to assign one
                fut: asyncio.Future = loop.create_future()
                self._waiters.append(fut)
            try:
                return await fut
            except asyncio.CancelledError:
                async with self._get_lock():
                    if fut in self._waiters:
                        self._waiters.remove(fut)
                raise

    def report_error(self, state: _TokenState, code: int, msg: str, cooldown_sec: Optional[int] = None):
        # Базовые кулдауны (если не передан cooldown_sec, напр. из retry_after):
        # 14 (captcha)      → 1 час
        # 29 (rate limit)   → 5 минут
        # 6  (too many req) → 5 минут
        # 9  (flood control)→ min(retry_after от VK, 3600) или 5 мин
        # прочие ошибки     → 1 минута
        if cooldown_sec is not None and cooldown_sec > 0:
            cooldown = min(int(cooldown_sec), 3600)
        elif code == 14:
            cooldown = 3600
        elif code in (29, 6, 9):
            cooldown = 300
        else:
            cooldown = 60
        state.record_error(code, msg, cooldown)

    def record_token_hourly(self, state: _TokenState) -> None:
        """Учесть один запрос к VK для почасового лимита (если включён)."""
        if self.per_token_hour_limit > 0:
            state.record_hourly_request(self.per_token_hour_limit)

    def report_success(self, state: _TokenState):
        pass  # token stays healthy

    @property
    def count(self) -> int:
        return len(self._states)

    @property
    def healthy_count(self) -> int:
        return sum(1 for s in self._states if s.healthy)

    @property
    def has_workers(self) -> bool:
        return any(s.worker_url for s in self._states)

    def stats(self) -> List[Dict]:
        now = time.time()
        result = []
        for i, s in enumerate(self._states):
            s._refill_bucket(now)
            recent_errs = [t for t in s.errors if now - t < 300]
            result.append({
                "index": i,
                "token_suffix": s._label(),
                "proxy": "worker" if s.worker_url else (s.proxy or "direct"),
                "healthy": s.healthy,
                "cooldown_remaining": max(0, int(s.cooldown_until - now)),
                "throttle_remaining": max(0, int(s.throttle_until - now)),
                "bucket_tokens": round(s.bucket_tokens, 2),
                "rps": round(s.req_per_sec, 1),
                "total_requests": s.total_requests,
                "total_errors": s.total_errors,
                "errors_5min": len(recent_errs),
                "last_error": s.last_error,
                "last_error_code": s.last_error_code,
                "captchas_solved": s.captchas_solved,
                "captchas_failed": s.captchas_failed,
            })
        return result

    async def _dispatcher_loop(self) -> None:
        """Background: when there are waiters and a free token, assign first free token to first waiter (FIFO)."""
        while True:
            async with self._get_lock():
                now = time.time()
                if not self._waiters:
                    sleep_for = 0.15
                else:
                    state = self._first_available_state(now)
                    if state is not None:
                        fut = self._waiters.popleft()
                        state.record_request(self.max_rps_per_token, self.throttle_duration)  # int 1–3
                        try:
                            fut.set_result(state)
                        except asyncio.InvalidStateError:
                            pass
                        continue
                    # Waiters but no free token: sleep until earliest throttle (or cooldown) ends
                    earliest = min((s.throttle_until for s in self._states), default=0.0)
                    sleep_for = min(0.15, max(0.0, earliest - now))
            await asyncio.sleep(max(0.05, sleep_for))


_token_pool = TokenPool()

# ─── Автоввод капчи (rucaptcha.com / 2captcha.com) ──────────────
_rucaptcha_key = os.getenv("RUCAPTCHA_KEY", "").strip()
if _rucaptcha_key:
    print(f"🔓 Автокапча: rucaptcha.com ключ загружен")
else:
    print(f"⚠️  RUCAPTCHA_KEY не задан — капчи будут вызывать cooldown токенов")

# ─── Redis: глобальный кэш библиотеки треков/поиска ───────────────
_redis: Optional[aioredis.Redis] = None


async def get_redis() -> Optional[aioredis.Redis]:
    """Ленивая инициализация Redis. Если REDIS_URL не задан или Redis недоступен — возвращает None."""
    global _redis
    if not REDIS_URL:
        return None
    if _redis is not None:
        return _redis
    try:
        _redis = aioredis.from_url(
            REDIS_URL,
            encoding="utf-8",
            decode_responses=True,
            health_check_interval=30,
        )
        # Лёгкий ping, чтобы поймать неверный URL сразу
        await _redis.ping()
        print(f"🧠 Redis cache connected: {REDIS_URL}")
        return _redis
    except Exception as e:
        print(f"⚠️  Redis disabled ({REDIS_URL}): {e}")
        _redis = None
        return None


async def _load_cache_metrics_from_redis() -> None:
    """Восстановить накопленные метрики кэша из Redis при старте сервера (чтобы не терять их при рестарте)."""
    redis = await get_redis()
    if not redis:
        return
    try:
        data = await redis.hgetall(_CACHE_METRICS_REDIS_KEY)
        if not data:
            return
        for k, v in data.items():
            if k not in _cache_metrics:
                continue
            try:
                if isinstance(_cache_metrics[k], float):
                    _cache_metrics[k] = float(v)
                else:
                    _cache_metrics[k] = int(v)
            except Exception:
                continue
        print("✅ Cache metrics restored from Redis")
    except Exception as e:
        print(f"⚠️  Redis cache metrics restore error: {e}")


async def _cache_metrics_flush_loop() -> None:
    """Периодически сохранять метрики кэша в Redis (каждую минуту), чтобы переживать рестарты."""
    while True:
        try:
            await asyncio.sleep(60)
            redis = await get_redis()
            if not redis:
                continue
            mapping = {k: str(v) for k, v in _cache_metrics.items()}
            await redis.hset(_CACHE_METRICS_REDIS_KEY, mapping=mapping)
        except asyncio.CancelledError:
            break
        except Exception as e:
            print(f"⚠️  Redis cache metrics flush error: {e}")
            await asyncio.sleep(60)


def _worker_token_id(state: "_TokenState") -> str:
    """Deterministic token identifier shared with Redis workers.

    For now we derive it from token string (or worker_url if token пустой),
    keeping only a short SHA256 hex prefix. Workers should use the same logic
    when WORKER_TOKEN_ID не задан явно.
    """
    base = state.worker_url or state.token
    if not base:
        base = "token:unknown"
    h = hashlib.sha256(base.encode("utf-8")).hexdigest()
    return h[:16]


async def _redis_get_json(key: str) -> Optional[Any]:
    """Утилита: получить JSON-объект из Redis или None."""
    redis = await get_redis()
    if not redis:
        return None
    try:
        raw = await redis.get(key)
        if not raw:
            return None
        return json.loads(raw)
    except Exception as e:
        print(f"⚠️ Redis get_json error for {key}: {e}")
        return None


async def _redis_set_json(key: str, value: Any, ex: Optional[int] = None) -> None:
    """Утилита: записать JSON-объект в Redis (best-effort)."""
    redis = await get_redis()
    if not redis:
        return
    try:
        data = json.dumps(value, ensure_ascii=False)
        if ex:
            await redis.set(key, data, ex=ex)
        else:
            await redis.set(key, data)
    except Exception as e:
        print(f"⚠️ Redis set_json error for {key}: {e}")


# Версионирование кэша: при смене структуры ответа меняем CACHE_VERSION — старые ключи перестают читаться
CACHE_VERSION = (os.getenv("CACHE_VERSION", "v1")).strip() or "v1"
_NEGATIVE_CACHE_TTL = max(300, min(1800, int(os.getenv("NEGATIVE_CACHE_TTL_SEC", "600"))))  # 5–30 мин

# Рекомендации VK: общий кэш по seed + лимит живых вызовов/мин (защита ключей при тысячах онлайн)
_RECOMMENDATIONS_CACHE_TTL_SEC = max(300, int(os.getenv("RECOMMENDATIONS_CACHE_TTL_SEC", "86400")))
_RECOMMENDATIONS_LOCK_SEC = max(5, min(120, int(os.getenv("RECOMMENDATIONS_LOCK_SEC", "30"))))
_RECOMMENDATIONS_VK_MAX_PER_MINUTE = max(0, int(os.getenv("RECOMMENDATIONS_VK_MAX_PER_MINUTE", "60")))
_RECOMMENDATIONS_POPULAR_TTL_SEC = max(300, int(os.getenv("RECOMMENDATIONS_POPULAR_CACHE_TTL_SEC", "3600")))
_RECOMMENDATIONS_NEGATIVE_TTL_SEC = max(60, min(3600, int(os.getenv("RECOMMENDATIONS_NEGATIVE_CACHE_TTL_SEC", "600"))))
_RECOMMENDATIONS_VK_LOCAL_CONCURRENCY = max(1, min(64, int(os.getenv("RECOMMENDATIONS_VK_LOCAL_CONCURRENCY", "6"))))
_RECOMMENDATIONS_VK_FETCH_COUNT = max(20, min(100, int(os.getenv("RECOMMENDATIONS_VK_FETCH_COUNT", "80"))))
_REC_PERSONAL_MAX_SEEDS = max(3, min(16, int(os.getenv("RECOMMENDATIONS_PERSONAL_MAX_SEEDS", "12"))))
# «Моя волна»: больше опорных треков из избранного для VK-рекомендаций
_REC_PERSONAL_MAX_SEEDS_WAVE = max(6, min(24, int(os.getenv("RECOMMENDATIONS_PERSONAL_MAX_SEEDS_WAVE", "20"))))
# Опорные треки и «настроение» — из последних добавленных в избранное (хвост списка на диске)
_REC_PERSONAL_SEED_RECENT_WINDOW = max(24, min(160, int(os.getenv("REC_PERSONAL_SEED_RECENT_WINDOW", "56"))))
_REC_PERSONAL_SEED_RECENT_WINDOW_WAVE = max(32, min(200, int(os.getenv("REC_PERSONAL_SEED_RECENT_WINDOW_WAVE", "88"))))
_REC_ARTIST_PROFILE_RECENT = max(12, min(120, int(os.getenv("REC_ARTIST_PROFILE_RECENT", "48"))))
# Персональные рекомендации: не повторять треки из последних N выдач; пул кандидатов = limit * mult (тот же VK-кэш по seed)
# Несколько полных выдач по ~100 id; иначе хвост старой порции выпадает из Redis и треки снова попадают в пул.
_REC_PERSONAL_EXCLUDE_RECENT_CAP = max(80, min(800, int(os.getenv("REC_PERSONAL_EXCLUDE_RECENT_CAP", "420"))))
_REC_PERSONAL_BLEND_POOL_MULT = max(3, min(12, int(os.getenv("REC_PERSONAL_BLEND_POOL_MULT", "6"))))
_REC_PERSONAL_BLEND_POOL_MULT_WAVE = max(4, min(10, int(os.getenv("REC_PERSONAL_BLEND_POOL_MULT_WAVE", "5"))))
_REC_PERSONAL_RECENT_IDS_TTL_SEC = max(86400, int(os.getenv("REC_PERSONAL_RECENT_IDS_TTL_SEC", str(30 * 86400))))
# Долговременный профиль вкуса в Redis (жанры/год/язык), с экспоненциальным затуханием.
_REC_TASTE_PROFILE_TTL_SEC = max(86400, int(os.getenv("REC_TASTE_PROFILE_TTL_SEC", str(180 * 86400))))
_REC_TASTE_HALFLIFE_DAYS = max(7, min(365, int(os.getenv("REC_TASTE_HALFLIFE_DAYS", "35"))))
_REC_TASTE_REBUILD_DAYS = max(14, min(365, int(os.getenv("REC_TASTE_REBUILD_DAYS", "180"))))
REC_TRACE = os.getenv("REC_TRACE", "").strip() == "1"
REC_TRACE_USER_IDS = {int(x) for x in (os.getenv("REC_TRACE_USER_IDS", "").split(",")) if x.strip().isdigit()}
REC_TRACE_TOP_K = max(10, min(120, int(os.getenv("REC_TRACE_TOP_K", "30"))))
REC_WAVE_INTERLEAVE_FAVORITES = os.getenv("REC_WAVE_INTERLEAVE_FAVORITES", "").strip() == "1"
# Персональные рекомендации: якорь — последние N треков в плейлистах и последние M поисков (жанровый allowlist).
# Сколько уникальных исполнителей взять с хвоста плейлистов (не «10 треков»); env REC_STRICT_ANCHOR_TRACKS — для совместимости.
_REC_STRICT_ANCHOR_ARTISTS = max(1, min(30, int(os.getenv("REC_STRICT_ANCHOR_TRACKS", "10"))))
_REC_STRICT_ANCHOR_SEARCHES = max(1, min(12, int(os.getenv("REC_STRICT_ANCHOR_SEARCHES", "5"))))
# Доля слотов якоря: избранное / кастомные плейлисты / поиск (остаток — в поиск; сумма обычно 100).
_REC_ANCHOR_PCT_MAIN = max(0, min(100, int(os.getenv("REC_ANCHOR_PCT_MAIN", "60"))))
_REC_ANCHOR_PCT_CUSTOM = max(0, min(100, int(os.getenv("REC_ANCHOR_PCT_CUSTOM", "30"))))
# Нейтральный q для audio.search+genre_id (не названия жанров в тексте — иначе матчится по заголовкам).
_REC_GENRE_SEARCH_NEUTRAL_Q = (os.getenv("REC_GENRE_SEARCH_NEUTRAL_Q", "a") or "a").strip()[:80]
# Не отбрасывать треки без genre_id в мете при жанровом фильтре (иначе после VK остаётся очень мало позиций).
_REC_GENRE_FILTER_KEEP_UNKNOWN = os.getenv("REC_GENRE_FILTER_KEEP_UNKNOWN", "1").strip() != "0"
# Персональные реки без VK: YTM (ytmusicapi) по якорям из избранного / поиска / plays.
_REC_YT_PERSONAL_MAX_QUERIES = max(2, min(24, int(os.getenv("REC_YT_PERSONAL_MAX_QUERIES", "12"))))
_REC_YT_PERSONAL_PER_QUERY = max(4, min(25, int(os.getenv("REC_YT_PERSONAL_PER_QUERY", "12"))))
_REC_YT_PERSONAL_CONCURRENCY = max(1, min(8, int(os.getenv("REC_YT_PERSONAL_CONCURRENCY", "4"))))
_REC_YT_COLDSTART_QUERY = (os.getenv("REC_YT_COLDSTART_QUERY", "trending music") or "trending music").strip()[:160]
_REC_YT_SEED_FALLBACK_QUERY = (os.getenv("REC_YT_SEED_FALLBACK_QUERY", "popular songs") or "popular songs").strip()[:160]


def _rec_env_flag(name: str, *, default: bool) -> bool:
    """Пустой env → default; 0/false/off → False; 1/true/on → True."""
    v = os.getenv(name)
    if v is None or not str(v).strip():
        return default
    s = str(v).strip().lower()
    if s in ("0", "false", "no", "off"):
        return False
    return s in ("1", "true", "yes", "on")


# 1 — не вызывать VK в гостевых реках / _rec_do_vk_or_popular (редко нужно).
_REC_SKIP_VK = _rec_env_flag("REC_SKIP_VK", default=False)
# Персональные реки (/recommendations/personal) без VK по умолчанию: только YTM. Вернуть VK: REC_PERSONAL_SKIP_VK=0.
_REC_PERSONAL_SKIP_VK = _rec_env_flag("REC_PERSONAL_SKIP_VK", default=True)
# Метаданные треков в Redis (подборки, resolve): дольше in-memory TRACK_INFO — «библиотека» для рекомендаций
_TRACK_META_REDIS_TTL_SEC = max(3600, int(os.getenv("TRACK_META_REDIS_TTL_SEC", str(14 * 86400))))


def _cache_ns(kind: str, *parts: str) -> str:
    """Единый namespace для ключей кэша: v1:kind:part1:part2."""
    return f"{CACHE_VERSION}:{kind}:{':'.join(parts)}"


def _track_meta_redis_key(track_id: str) -> str:
    if is_soundcloud_track_id(track_id):
        return _cache_ns("track", track_id, "meta_sc3")
    return _cache_ns("track", track_id, "meta")


async def _redis_get_track_meta(track_id: str) -> Optional[Dict]:
    return await _redis_get_json(_track_meta_redis_key(track_id))


async def _redis_set_track_meta(track_id: str, meta: Dict) -> None:
    await _redis_set_json(_track_meta_redis_key(track_id), meta, ex=_TRACK_META_REDIS_TTL_SEC)


def _rec_meta_fields_from_cached_meta(meta: Optional[Dict]) -> Tuple[Optional[int], Optional[int], Optional[str]]:
    if not meta or not isinstance(meta, dict):
        return None, None, None
    gid_raw = meta.get("genre_id")
    gid: Optional[int] = None
    if isinstance(gid_raw, int):
        gid = gid_raw
    elif isinstance(gid_raw, str) and gid_raw.strip().isdigit():
        gid = int(gid_raw.strip())
    ry_raw = meta.get("release_year")
    ry: Optional[int] = ry_raw if isinstance(ry_raw, int) else None
    lb = _rec_lang_bucket_track(meta) if (meta.get("title") or meta.get("artist")) else None
    return gid, ry, lb


async def _redis_cache_tracks_meta_batch(items: Optional[List[Dict]]) -> None:
    """Сохранить полные dict'и треков (как из _parse_tracks) в Redis для последующих подборок и resolve."""
    if not items:
        return
    coros = []
    for t in items:
        if not isinstance(t, dict):
            continue
        tid = str(t.get("id") or "").strip()
        if not tid or not _valid_track_id(tid):
            continue
        coros.append(_redis_set_track_meta(tid, t))
    if coros:
        await asyncio.gather(*coros, return_exceptions=True)


async def _redis_get_track_source(track_id: str) -> Optional[Dict]:
    return await _redis_get_json(_cache_ns("track", track_id, "source"))


# Ссылки на стриминг VK по опыту и обсуждениям живут до ~24 ч; кэш на максимум под это
_TRACK_SOURCE_REDIS_TTL = 90 * 86400  # сезон (~3 мес) — кэш URL воспроизведённых треков

async def _redis_set_track_source(track_id: str, source: Dict, ttl: int = 0) -> None:
    ex = ttl if ttl > 0 else _TRACK_SOURCE_REDIS_TTL
    await _redis_set_json(_cache_ns("track", track_id, "source"), source, ex=ex)
    # Сбрасываем негативный кэш при успешной записи
    redis = await get_redis()
    if redis:
        try:
            await redis.delete(_cache_ns("track", track_id, "negative"))
        except Exception:
            pass


# ─── YouTube: CDN URL для прокси (L1 RAM + Redis + singleflight + to_thread) ───
_YT_DIRECT_MEM_MAX = max(100, min(20_000, int(os.getenv("YT_DIRECT_MEM_MAX", "4000"))))
_YT_DIRECT_MEM_TTL_SEC = max(120, min(86_400, int(os.getenv("YT_DIRECT_MEM_TTL_SEC", "7200"))))
_yt_direct_mem: "OrderedDict[str, Tuple[str, float]]" = OrderedDict()
_yt_direct_inflight: Dict[str, "asyncio.Task[str]"] = {}
_yt_direct_inflight_lock = asyncio.Lock()


def _yt_direct_mem_get(video_id: str) -> Optional[str]:
    ent = _yt_direct_mem.get(video_id)
    if not ent:
        return None
    url, ts = ent
    if time.time() - ts > _YT_DIRECT_MEM_TTL_SEC:
        try:
            del _yt_direct_mem[video_id]
        except KeyError:
            pass
        return None
    _yt_direct_mem.move_to_end(video_id)
    return url


def _yt_direct_mem_set(video_id: str, url: str) -> None:
    _yt_direct_mem[video_id] = (url, time.time())
    _yt_direct_mem.move_to_end(video_id)
    while len(_yt_direct_mem) > _YT_DIRECT_MEM_MAX:
        _yt_direct_mem.popitem(last=False)


def _extract_youtube_direct_url_sync(video_id: str) -> str:
    """Синхронный yt-dlp — вызывать только через asyncio.to_thread (не блокировать event loop)."""
    import yt_dlp

    raw = (video_id or "").strip()
    vid = raw if re.match(r"^[\w-]{11}$", raw) else extract_video_id(raw)
    if not vid:
        raise ValueError("invalid YouTube video id")
    # YouTube (2026): формат 140 (audio-only m4a) часто отдаётся без прямого URL
    # (SABR-эксперимент) → yt-dlp падает "Requested format is not available".
    # Раньше это глоталось ignoreerrors=True → возвращался None → 500.
    # Берём цепочку: audio-only m4a → любой bestaudio → прогрессивный 18 (mp4 a+v) → best.
    # ignoreerrors НЕ ставим, чтобы реальные ошибки не превращались в "no info".
    # js_runtimes не задаём (node на сервере нет; yt-dlp сам подберёт доступный рантайм).
    ydl_opts = {
        "format": "bestaudio[ext=m4a]/bestaudio/18/best",
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "nocheckcertificate": True,
        "socket_timeout": 20,
        "retries": 2,
        "fragment_retries": 1,
        "noplaylist": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(vid, download=False)
        if not info:
            raise RuntimeError("yt-dlp: no info")
        if "entries" in info:
            entries = info.get("entries") or []
            info = entries[0] if entries else None
        if not info:
            raise RuntimeError("yt-dlp: no info")
        url = info.get("url")
        if not url:
            # info без верхнеуровневого url: выбираем формат с прямым URL и аудио,
            # предпочитая audio-only и обычный http (не m3u8/dash).
            best: Optional[Tuple[int, str]] = None
            for f in (info.get("formats") or []):
                fu = f.get("url")
                if not fu or f.get("acodec") in (None, "none"):
                    continue
                proto = str(f.get("protocol") or "")
                if "m3u8" in proto or "dash" in proto:
                    continue
                score = 2 if f.get("vcodec") in (None, "none") else 1
                if best is None or score > best[0]:
                    best = (score, fu)
            if best:
                url = best[1]
        if not url:
            raise RuntimeError("yt-dlp: no url")
        return str(url)


async def _get_or_set_direct_url(video_id: str) -> str:
    """
    Прямой URL аудио с YouTube (формат 140 AAC). Redis + быстрый RAM-кэш.
    Извлечение через yt-dlp в thread pool + singleflight (не дублируем работу).
    """
    mem = _yt_direct_mem_get(video_id)
    if mem:
        return mem
    src = await _redis_get_track_source(video_id)
    if src and "direct_url" in src:
        u = str(src["direct_url"])
        _yt_direct_mem_set(video_id, u)
        return u

    async with _yt_direct_inflight_lock:
        if video_id not in _yt_direct_inflight:

            async def _run() -> str:
                try:
                    url = await asyncio.to_thread(_extract_youtube_direct_url_sync, video_id)
                    await _redis_set_track_source(video_id, {"direct_url": url}, ttl=86400)
                    _yt_direct_mem_set(video_id, url)
                    return url
                finally:
                    async with _yt_direct_inflight_lock:
                        _yt_direct_inflight.pop(video_id, None)

            _yt_direct_inflight[video_id] = asyncio.create_task(_run())
        task = _yt_direct_inflight[video_id]
    return await task


_YOUTUBE_WARM_CONCURRENCY = max(1, min(8, int(os.getenv("YOUTUBE_WARM_CONCURRENCY", "3"))))


async def _warm_youtube_direct_for_ids(track_ids: List[str]) -> None:
    """
    Фон: для YouTube-треков заранее кладёт direct CDN URL в Redis (yt-dlp один раз на video_id).
    Ускоряет первый байт на /api/music/youtube-direct/* когда пользователь жмёт Play.
    """
    ordered: List[str] = []
    seen: Set[str] = set()
    for tid in track_ids:
        if not tid:
            continue
        vid = extract_video_id(tid)
        if not vid and re.search(r"(?:youtube\.com|youtu\.be)", tid):
            m = re.search(r"(?:v=|youtu\.be/|shorts/)([\w-]{11})", tid)
            vid = m.group(1) if m else None
        if not vid and re.match(r"^[\w-]{11}$", tid):
            vid = tid
        if vid and vid not in seen:
            seen.add(vid)
            ordered.append(vid)
    if not ordered:
        return
    ordered = ordered[:SEARCH_PRESOLVE_TOP_N]
    sem = asyncio.Semaphore(_YOUTUBE_WARM_CONCURRENCY)

    async def _one(v: str) -> None:
        async with sem:
            try:
                await asyncio.sleep(float(os.getenv("YOUTUBE_WARM_STAGGER_SEC", "0.08")))
                await _get_or_set_direct_url(v)
            except Exception:
                pass

    await asyncio.gather(*(_one(v) for v in ordered))


# ─── YouTube: полный M4A на диске (повторные визиты ≈ как VK — мгновенно с Range) ───
_YT_DISK_CACHE_MAX_FILES = max(50, min(5000, int(os.getenv("YT_DISK_CACHE_MAX_FILES", "500"))))
_yt_disk_fill_inflight: Set[str] = set()
_yt_disk_fill_lock = asyncio.Lock()


def _yt_disk_cache_path(vid: str) -> Path:
    v = (vid or "").strip()
    if not re.match(r"^[\w-]{11}$", v):
        raise ValueError("invalid YouTube id for disk cache")
    return CACHE_DIR / f"yt_{v}.m4a"


def _yt_disk_cache_file_ready(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size >= 8192
    except OSError:
        return False


def _cleanup_yt_m4a_disk_cache() -> None:
    try:
        files = [f for f in CACHE_DIR.glob("yt_*.m4a") if f.is_file()]
    except OSError:
        return
    if len(files) <= _YT_DISK_CACHE_MAX_FILES:
        return
    files.sort(key=lambda f: f.stat().st_mtime)
    while len(files) > _YT_DISK_CACHE_MAX_FILES:
        try:
            files.pop(0).unlink(missing_ok=True)
        except OSError:
            break


def _yt_cached_m4a_file_response(file_path: str, file_size: int, request: Request, vid: str) -> Response | FileResponse:
    import re as _re
    range_header = request.headers.get("range") if request else None
    base_h: Dict[str, str] = {
        "Accept-Ranges": "bytes",
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "public, max-age=86400",
    }
    if range_header:
        m = _re.search(r"bytes=(\d+)-(\d*)", range_header)
        if m:
            start = int(m.group(1))
            end = int(m.group(2)) if m.group(2) else file_size - 1
            end = min(end, file_size - 1)
            if start >= file_size or start < 0:
                return Response(
                    status_code=416,
                    headers={**base_h, "Content-Range": f"bytes */{file_size}"},
                )
            length = end - start + 1
            with open(file_path, "rb") as f:
                f.seek(start)
                data = f.read(length)
            return Response(
                content=data,
                status_code=206,
                media_type="audio/mp4",
                headers={
                    **base_h,
                    "Content-Range": f"bytes {start}-{end}/{file_size}",
                    "Content-Length": str(length),
                },
            )
    return FileResponse(
        path=file_path,
        media_type="audio/mp4",
        filename=f"{vid}.m4a",
        headers={**base_h, "Content-Length": str(file_size)},
    )


async def _download_stream_url_to_m4a_file(stream_url: str, dest: Path) -> None:
    tmp = dest.with_suffix(".m4a.part")
    tmp.unlink(missing_ok=True)
    session = await _get_youtube_session()
    try:
        async with session.get(stream_url) as resp:
            if resp.status not in (200, 206):
                return
            with open(tmp, "wb") as out:
                async for chunk in resp.content.iter_chunked(512 * 1024):
                    out.write(chunk)
        if tmp.stat().st_size < 4096:
            tmp.unlink(missing_ok=True)
            return
        os.replace(tmp, dest)
        _cleanup_yt_m4a_disk_cache()
    except Exception as e:
        tmp.unlink(missing_ok=True)
        print(f"⚠️ yt disk cache download: {e}")


async def _youtube_disk_cache_fill_job(vid: str, stream_url: str) -> None:
    async with _yt_disk_fill_lock:
        if vid in _yt_disk_fill_inflight:
            return
        try:
            dest = _yt_disk_cache_path(vid)
        except ValueError:
            return
        if _yt_disk_cache_file_ready(dest):
            return
        _yt_disk_fill_inflight.add(vid)
    try:
        try:
            dest = _yt_disk_cache_path(vid)
        except ValueError:
            return
        await _download_stream_url_to_m4a_file(stream_url, dest)
    finally:
        async with _yt_disk_fill_lock:
            _yt_disk_fill_inflight.discard(vid)


def _schedule_youtube_disk_cache_fill(vid: str, stream_url: str) -> None:
    _safe_ensure_future(_youtube_disk_cache_fill_job(vid, stream_url))


async def _redis_get_track_negative(track_id: str) -> bool:
    """Есть ли запись «трек недоступен» (удалён, geo, приватный)."""
    redis = await get_redis()
    if not redis:
        return False
    try:
        v = await redis.get(_cache_ns("track", track_id, "negative"))
        return v is not None
    except Exception:
        return False


async def _redis_set_track_negative(track_id: str) -> None:
    """Записать негативный результат: трек недоступен (TTL 5–30 мин)."""
    redis = await get_redis()
    if not redis:
        return
    try:
        await redis.set(_cache_ns("track", track_id, "negative"), "1", ex=_NEGATIVE_CACHE_TTL)
    except Exception as e:
        print(f"⚠️ Redis set negative error: {e}")


async def _redis_delete_track_negative(track_id: str) -> None:
    """Снять negative по канону owner_audio (ложный negative после старого батча без retry)."""
    tid = (track_id or "").strip()
    if _valid_track_id(tid):
        tid = _vk_canonical_track_id(tid)
    redis = await get_redis()
    if not redis:
        return
    try:
        await redis.delete(_cache_ns("track", tid, "negative"))
    except Exception as e:
        logger.debug("redis delete track negative failed: %s", e)


async def _redis_delete_track_source(track_id: str) -> None:
    """Удалить кэш URL трека — следующий resolve получит свежую ссылку из VK."""
    tid = (track_id or "").strip()
    if _valid_track_id(tid):
        tid = _vk_canonical_track_id(tid)
    _url_cache_fallback.pop(tid, None)
    redis = await get_redis()
    if not redis:
        return
    try:
        await redis.delete(_cache_ns("track", tid, "source"))
        await redis.delete(_cache_ns("track", tid, "negative"))
    except Exception as e:
        print(f"⚠️ Redis delete track source error: {e}")


# In-memory fallback для URL при недоступности Redis. LRU: при переполнении вытесняем давно не использованные.
_url_cache_fallback: OrderedDict = OrderedDict()  # track_id -> (url, ts)
_URL_CACHE_FALLBACK_TTL = 3600  # 1 ч
_URL_CACHE_FALLBACK_MAX = 10000


def _url_cache_fallback_get(track_id: str) -> Optional[str]:
    if track_id not in _url_cache_fallback:
        return None
    entry = _url_cache_fallback[track_id]
    if time.time() - entry[1] >= _URL_CACHE_FALLBACK_TTL:
        _url_cache_fallback.pop(track_id, None)
        return None
    _url_cache_fallback.move_to_end(track_id)
    return entry[0]


def _url_cache_fallback_set(track_id: str, url: str) -> None:
    now = time.time()
    _url_cache_fallback[track_id] = (url, now)
    _url_cache_fallback.move_to_end(track_id)
    while len(_url_cache_fallback) > _URL_CACHE_FALLBACK_MAX:
        _url_cache_fallback.popitem(last=False)


# ─── Папка для хранения плейлистов ──────────────────────────────
DATA_DIR = Path(__file__).parent / "user_data"
DATA_DIR.mkdir(exist_ok=True)

# ─── Кеш VK audio URL только в Redis (30 дней), in-memory слой убран ─

# ─── Кеш сгенерированных карточек треков (ускорение повторной загрузки в историю) ─
_card_cache: Dict[str, tuple] = {}  # track_id -> (png_bytes, timestamp)
_CARD_CACHE_TTL = 3600
_CARD_CACHE_MAX = 300

# ─── Кеш поиска (Redis + память): TTL из env, LRU при переполнении ─
_search_cache: OrderedDict = OrderedDict()  # key -> (tracks, ts)
# Жёсткий TTL ключа в Redis (сек): по умолчанию 24 ч; было 7 сут — долго для устаревшей разметки (обложки и т.д.).
_SEARCH_CACHE_TTL = max(
    60,
    min(30 * 86400, int((os.getenv("SEARCH_CACHE_TTL_SEC") or "86400").strip() or "86400")),
)
_SEARCH_CACHE_MAX = max(
    100,
    min(200_000, int((os.getenv("SEARCH_CACHE_MAX_ENTRIES") or "50000").strip() or "50000")),
)
# SWR: после SOFT отдаём кэш, но фоном обновляем; 0 = выключить фоновое обновление.
_SEARCH_CACHE_SOFT_TTL = max(
    0,
    min(
        _SEARCH_CACHE_TTL,
        int((os.getenv("SEARCH_CACHE_SOFT_TTL_SEC") or "3600").strip() or "3600"),
    ),
)

# Singleflight для поиска и для resolve (request coalescing)
_search_singleflight: Dict[str, asyncio.Future] = {}
_search_singleflight_lock = asyncio.Lock()
_source_singleflight: Dict[str, asyncio.Future] = {}
_source_singleflight_lock = asyncio.Lock()
_meta_singleflight: Dict[str, asyncio.Future] = {}
_meta_singleflight_lock = asyncio.Lock()
_rec_singleflight: Dict[str, asyncio.Future] = {}
_rec_singleflight_lock = asyncio.Lock()
_rec_memory_cache: "OrderedDict[str, Tuple[List[Dict], float, str]]" = OrderedDict()
_REC_MEMORY_MAX = 2000
_rec_vk_sem = asyncio.Semaphore(_RECOMMENDATIONS_VK_LOCAL_CONCURRENCY)
_rec_seed_async_locks: Dict[str, asyncio.Lock] = {}
_rec_seed_async_locks_guard = asyncio.Lock()

# Метрики кэша (per-process, но с периодическим сохранением в Redis, чтобы не терять при рестартах)
_CACHE_METRICS_REDIS_KEY = "tgplay:cache:metrics"
_cache_metrics = {
    "search_hit": 0,
    "search_miss": 0,
    "search_age_sum": 0.0,
    "search_age_count": 0,
    "source_hit": 0,
    "source_miss": 0,
    "meta_hit": 0,
    "meta_miss": 0,
    "negative_hit": 0,
}

# ─── Кеш метаданных треков (hot), LRU ─
_track_info_cache: OrderedDict = OrderedDict()  # track_id -> (meta, ts)
_TRACK_INFO_TTL = _TRACK_INFO_CACHE_TTL_HOURS * 3600
_TRACK_INFO_MAX = 20000

# ─── VK API request helper with token pool ─
_vk_global_semaphore: Optional[asyncio.Semaphore] = None

def _get_vk_semaphore() -> asyncio.Semaphore:
    global _vk_global_semaphore
    if _vk_global_semaphore is None:
        _vk_global_semaphore = asyncio.Semaphore(max(5, _token_pool.count * 3))
    return _vk_global_semaphore


_proxy_sessions: Dict[str, aiohttp.ClientSession] = {}

async def _get_proxy_session(proxy_url: str) -> aiohttp.ClientSession:
    """Get or create an aiohttp session for the given proxy URL."""
    if proxy_url in _proxy_sessions and not _proxy_sessions[proxy_url].closed:
        return _proxy_sessions[proxy_url]
    try:
        from aiohttp_socks import ProxyConnector
    except ImportError:
        raise RuntimeError("aiohttp-socks required for proxy support: pip install aiohttp-socks")
    connector = ProxyConnector.from_url(proxy_url)
    timeout = aiohttp.ClientTimeout(total=15, connect=5)
    session = aiohttp.ClientSession(connector=connector, timeout=timeout)
    _proxy_sessions[proxy_url] = session
    return session


async def _get_vk_session(state: _TokenState) -> aiohttp.ClientSession:
    """Return proxy session if token has a proxy, otherwise default session."""
    if state.proxy:
        try:
            return await _get_proxy_session(state.proxy)
        except Exception as e:
            print(f"⚠️ Proxy session error for {state.proxy}: {e}, falling back to direct")
    return await get_session()


async def _vk_check_single_token(state: _TokenState) -> Dict:
    """Один лёгкий запрос к VK с данным токеном/воркером. Для проверки: всё ещё ошибка 9/502 или уже ок.
    Возвращает: {"ok": True} или {"error_code": 9, "error_msg": "..."} или {"http_error": 502} или {"network_error": "..."}.
    """
    if state.worker_url:
        session = await get_session()
        search_p = {"q": "a", "count": 1}
        search_p.update(_vk_audio_search_extra_params(state))
        body = {"method": "audio.search", "params": search_p, "post": False}
        wh = {**_vk_api_client_headers(getattr(state, "user_agent", VK_USER_AGENT)), "Content-Type": "application/json"}
        try:
            async with session.post(state.worker_url, json=body, headers=wh, timeout=aiohttp.ClientTimeout(total=VK_WORKER_TIMEOUT)) as resp:
                if resp.status >= 500:
                    return {"http_error": resp.status}
                try:
                    data = await resp.json()
                except Exception:
                    return {"http_error": resp.status, "body_not_json": True}
        except Exception as e:
            err = str(e).lower()
            return {"network_error": str(e)[:120], "is_502": "502" in err or "gateway" in err}
        if "error" in data:
            err = data["error"]
            return {"error_code": int(err.get("error_code", 0)), "error_msg": (err.get("error_msg") or "")[:120]}
        return {"ok": True}

    session = await _get_vk_session(state)
    url = "https://api.vk.com/method/audio.search"
    search_p = {"q": "a", "count": 1}
    search_p.update(_vk_audio_search_extra_params(state))
    params = {**search_p, "access_token": state.token, "v": "5.131"}
    headers = _vk_api_client_headers(getattr(state, "user_agent", VK_USER_AGENT))
    try:
        async with session.get(url, params=params, headers=headers) as resp:
            if resp.status >= 500:
                return {"http_error": resp.status}
            try:
                data = await resp.json()
            except Exception:
                return {"http_error": resp.status, "body_not_json": True}
    except Exception as e:
        err = str(e).lower()
        return {"network_error": str(e)[:120], "is_502": "502" in err or "gateway" in err}
    if "error" in data:
        err = data["error"]
        return {"error_code": int(err.get("error_code", 0)), "error_msg": (err.get("error_msg") or "")[:120]}
    return {"ok": True}


async def _vk_api_call(method: str, params: Dict, *, post: bool = False) -> Dict:
    """Universal VK API caller with token rotation and retry.

    При USE_REDIS_WORKERS=1 реальные вызовы VK выполняют воркеры, а бэкенд
    кладёт задания в Redis и ждёт результат. В противном случае используется
    старая схема: прямые запросы к VK (или HTTP-воркерам по worker_url).
    """
    url = f"https://api.vk.com/method/{method}"

    # Без VK-токенов VK API отключён: мгновенный отказ, чтобы вызывающий код ушёл в
    # YouTube-фолбэк (старые VK-треки), а не ждал таймаут acquire().
    if _token_pool.count == 0:
        return {"error": {"error_code": -1, "error_msg": "VK disabled (no tokens)"}}

    for attempt in range(max(2, min(3, _token_pool.count))):
        try:
            state = await asyncio.wait_for(_token_pool.acquire(), timeout=VK_TOKEN_ACQUIRE_TIMEOUT)
        except asyncio.TimeoutError:
            # All tokens are throttled/cooldown: fail fast so UI doesn't spin for minutes.
            return {
                "error": {
                    "error_code": -1,
                    "error_msg": f"VK token pool busy (acquire timeout {VK_TOKEN_ACQUIRE_TIMEOUT}s)",
                }
            }
        ua = getattr(state, "user_agent", VK_USER_AGENT)
        headers = _vk_api_client_headers(ua)
        effective_params = _vk_expand_vk_method_params(method, params, state)
        label = state._label()

        if USE_REDIS_WORKERS:
            # enqueue job for this token_id and wait for result from worker
            redis = await get_redis()
            if not redis:
                # Fallback: если Redis недоступен — старый прямой вызов (лучше медленно, чем совсем никак)
                session = await _get_vk_session(state)
                call_params = {**effective_params, "access_token": state.token, "v": "5.131"}
                async with _get_vk_semaphore():
                    try:
                        if post:
                            async with session.post(url, data=call_params, headers=headers) as resp:
                                data = await resp.json()
                        else:
                            async with session.get(url, params=call_params, headers=headers) as resp:
                                data = await resp.json()
                    except Exception as e:
                        _token_pool.report_error(state, 0, str(e)[:100])
                        print(f"⚠️ VK {method} network error ({label}): {e}")
                        continue
            else:
                token_id = _worker_token_id(state)
                job_id = uuid.uuid4().hex
                queue_key = f"vk:q:{token_id}"
                resq_key = f"vk:resq:{job_id}"
                res_key = f"vk:res:{job_id}"
                job = {"job_id": job_id, "method": method, "params": dict(effective_params), "post": bool(post)}
                try:
                    await redis.rpush(queue_key, json.dumps(job, ensure_ascii=False))
                    # ждём сигнал от воркера (или таймаут)
                    timeout = VK_WORKER_TIMEOUT
                    blpop_res = await redis.blpop(resq_key, timeout=timeout)
                    if not blpop_res:
                        data = {"error": {"error_code": -1, "error_msg": "worker timeout"}}
                    else:
                        raw = await redis.get(res_key)
                        if not raw:
                            data = {"error": {"error_code": -1, "error_msg": "worker result missing"}}
                        else:
                            try:
                                data = json.loads(raw)
                            except Exception:
                                data = {"error": {"error_code": -1, "error_msg": "worker result not json"}}
                except Exception as e:
                    _token_pool.report_error(state, 0, str(e)[:100])
                    print(f"⚠️ VK {method} redis-worker error ({label}): {e}")
                    continue
        else:
            if state.worker_url:
                # Старый HTTP-воркер: POST method + params (без access_token/v), воркер сам дергает VK со своего IP.
                session = await get_session()
                body = {"method": method, "params": dict(effective_params), "post": post}
                async with _get_vk_semaphore():
                    try:
                        async with session.post(state.worker_url, json=body, headers={**headers, "Content-Type": "application/json"}, timeout=aiohttp.ClientTimeout(total=VK_WORKER_TIMEOUT)) as resp:
                            try:
                                data = await resp.json()
                            except Exception:
                                data = {"error": {"error_code": -1, "error_msg": "worker response not json"}}
                            if resp.status >= 500 and "error" not in data:
                                data = {"error": {"error_code": -1, "error_msg": f"worker HTTP {resp.status}"}}
                    except Exception as e:
                        _token_pool.report_error(state, 0, str(e)[:100])
                        print(f"⚠️ VK {method} worker error ({label}): {e}")
                        continue
            else:
                session = await _get_vk_session(state)
                call_params = {**effective_params, "access_token": state.token, "v": "5.131"}

                async with _get_vk_semaphore():
                    try:
                        if post:
                            async with session.post(url, data=call_params, headers=headers) as resp:
                                data = await resp.json()
                                if resp.status >= 500:
                                    data = {"error": {"error_code": -1, "error_msg": f"VK HTTP {resp.status}"}}
                        else:
                            async with session.get(url, params=call_params, headers=headers) as resp:
                                data = await resp.json()
                                if resp.status >= 500:
                                    data = {"error": {"error_code": -1, "error_msg": f"VK HTTP {resp.status}"}}
                    except Exception as e:
                        _token_pool.report_error(state, 0, str(e)[:100])
                        print(f"⚠️ VK {method} network error ({label}): {e}")
                        continue

        _token_pool.record_token_hourly(state)
        if "error" in data:
            try:
                err_code = int(data["error"].get("error_code", 0))
            except (TypeError, ValueError):
                err_code = 0
            err_msg = data["error"].get("error_msg", "")[:100]

            # Только ошибка 14 = капча. Ошибка 9 (flood) и др. логируем как cooldown_start, не как капчу.
            if err_code == 14:
                captcha_sid = data["error"].get("captcha_sid")
                captcha_img = data["error"].get("captcha_img")
                if not state.worker_url:
                    try:
                        import analytics_db
                        analytics_db.init_db()
                        token_id = hashlib.sha256(state.token.encode()).hexdigest()[:16]
                        analytics_db.log_captcha_event(
                            token_id=token_id,
                            event_type="captcha_shown",
                            cooldown_seconds=3600,
                            rucaptcha_used=bool(_rucaptcha_key and captcha_sid and captcha_img),
                            result=None,
                            extra={"vk_error": err_code},
                        )
                    except Exception:
                        pass
                    if _rucaptcha_key and captcha_sid and captcha_img:
                        call_params = {**effective_params, "access_token": state.token, "v": "5.131"}
                        state.record_error(14, "captcha — healing in bg", cooldown_sec=3600)
                        asyncio.create_task(_heal_token_captcha(
                            state, method, call_params, captcha_sid, captcha_img
                        ))
                        print(f"🧩 VK captcha on {label}: bg heal started, switching token")
                    else:
                        _token_pool.report_error(state, 14, err_msg)
                        print(f"🛑 VK captcha on {label}: no rucaptcha key, cooldown 1h")
                else:
                    _token_pool.report_error(state, 14, err_msg)
                    print(f"🛑 VK captcha on {label}: worker, cooldown 1h")
                continue

            retry_after = None
            if err_code == 9:
                try:
                    retry_after = int(data["error"].get("retry_after", 0))
                except (TypeError, ValueError):
                    pass
            if err_code in (29, 6, 9):
                try:
                    import analytics_db
                    analytics_db.init_db()
                    token_id = hashlib.sha256((state.worker_url or state.token).encode()).hexdigest()[:16]
                    analytics_db.log_captcha_event(
                        token_id=token_id,
                        event_type="cooldown_start",
                        cooldown_seconds=retry_after if retry_after and err_code == 9 else 300,
                        rucaptcha_used=False,
                        result=None,
                        extra={"vk_error": int(err_code), "retry_after": retry_after},
                    )
                except Exception:
                    pass
            _token_pool.report_error(state, err_code, err_msg, cooldown_sec=retry_after if err_code == 9 and retry_after else (60 if err_code == -1 else None))
            print(f"❌ VK {method} error {err_code} ({label}): {err_msg}" + (f" (retry_after={retry_after}s)" if retry_after else ""))
            if err_code in (29, 6, 9, -1):  # rate limit, too many requests, flood control, HTTP 5xx — пробуем другой токен
                continue
            return data  # non-retryable error

        _token_pool.report_success(state)
        return data

    return {"error": {"error_code": -1, "error_msg": "All tokens exhausted"}}


async def _solve_captcha(captcha_img_url: str) -> Optional[str]:
    """Send captcha to rucaptcha.com and poll for result. Returns solved text or None."""
    if not _rucaptcha_key:
        return None
    session = await get_session()
    try:
        async with session.post("https://rucaptcha.com/in.php", data={
            "key": _rucaptcha_key,
            "method": "link",
            "body": captcha_img_url,
            "json": "1",
        }) as resp:
            result = await resp.json()
        if result.get("status") != 1:
            print(f"⚠️ rucaptcha submit error: {result}")
            return None
        captcha_id = result["request"]
    except Exception as e:
        print(f"⚠️ rucaptcha submit exception: {e}")
        return None

    for _ in range(10):  # max 30 sec (10 * 3s)
        await asyncio.sleep(3)
        try:
            async with session.get("https://rucaptcha.com/res.php", params={
                "key": _rucaptcha_key,
                "action": "get",
                "id": captcha_id,
                "json": "1",
            }) as resp:
                result = await resp.json()
            if result.get("status") == 1:
                return result["request"]
            if result.get("request") != "CAPCHA_NOT_READY":
                print(f"⚠️ rucaptcha poll error: {result}")
                return None
        except Exception as e:
            print(f"⚠️ rucaptcha poll exception: {e}")
            return None
    print(f"⚠️ rucaptcha timeout (30s)")
    return None


async def _heal_token_captcha(
    state: _TokenState,
    method: str,
    original_params: Dict,
    captcha_sid: str,
    captcha_img: str,
):
    """Background task: solve captcha via rucaptcha, confirm with VK.
    ВАЖНО: токен НЕ возвращаем мгновенно — он останется в своём часовом кулдауне.
    Задача healing — очистить капчу на стороне VK, пока токен отдыхает."""
    t0 = time.time()
    try:
        solved = await _solve_captcha(captcha_img)
        if not solved:
            state.captchas_failed += 1
            state.cooldown_until = max(state.cooldown_until, time.time() + 3600)
            try:
                import analytics_db
                analytics_db.init_db()
                token_id = hashlib.sha256(state.token.encode()).hexdigest()[:16]
                analytics_db.log_captcha_event(token_id=token_id, event_type="captcha_heal", rucaptcha_used=True, result="fail", extra={"reason": "solve_returned_none"})
            except Exception:
                pass
            print(f"❌ Captcha heal failed for {state._label()}: solve returned None, cooldown extended")
            return

        confirm_params = {
            **original_params,
            "captcha_sid": captcha_sid,
            "captcha_key": solved,
        }
        session = await _get_vk_session(state)
        headers = _vk_api_client_headers(getattr(state, "user_agent", "") or VK_USER_AGENT)
        url = f"https://api.vk.com/method/{method}"
        async with session.post(url, data=confirm_params, headers=headers) as resp:
            data = await resp.json()

        if "error" in data:
            err_code = data["error"].get("error_code", 0)
            state.captchas_failed += 1
            try:
                import analytics_db
                analytics_db.init_db()
                token_id = hashlib.sha256(state.token.encode()).hexdigest()[:16]
                analytics_db.log_captcha_event(token_id=token_id, event_type="captcha_heal", rucaptcha_used=True, result="fail", extra={"vk_error": err_code})
            except Exception:
                pass
            if err_code == 14:
                state.cooldown_until = max(state.cooldown_until, time.time() + 7200)
                print(f"❌ Captcha heal: VK returned another captcha for {state._label()}, cooldown 2h")
            else:
                state.cooldown_until = max(state.cooldown_until, time.time() + 3600)
                print(f"❌ Captcha heal: VK error {err_code} for {state._label()}, cooldown extended")
            return

        state.captchas_solved += 1
        try:
            import analytics_db
            analytics_db.init_db()
            token_id = hashlib.sha256(state.token.encode()).hexdigest()[:16]
            analytics_db.log_captcha_event(token_id=token_id, event_type="captcha_heal", rucaptcha_used=True, result="success", extra={})
        except Exception:
            pass
        elapsed = time.time() - t0
        print(f"✅ Captcha healed for {state._label()} in {elapsed:.1f}s — token will return after cooldown")

    except Exception as e:
        state.captchas_failed += 1
        state.cooldown_until = max(state.cooldown_until, time.time() + 3600)
        try:
            import analytics_db
            analytics_db.init_db()
            token_id = hashlib.sha256(state.token.encode()).hexdigest()[:16]
            analytics_db.log_captcha_event(token_id=token_id, event_type="captcha_heal", rucaptcha_used=True, result="fail", extra={"exception": str(e)[:200]})
        except Exception:
            pass
        print(f"❌ Captcha heal exception for {state._label()}: {e}, cooldown extended")


# ─── Ожидающие шеринга по KeyboardButtonRequestUsers: (sender_id, request_id) -> (track_id, ts) ─
_pending_share: Dict[tuple, tuple] = {}
_PENDING_SHARE_TTL = 300
_PENDING_SHARE_MAX = 500

# ─── Транслитерация EN↔RU для fallback-поиска ───────────────────
_EN2RU = {
    "a": "а", "b": "б", "c": "ц", "d": "д", "e": "е", "f": "ф",
    "g": "г", "h": "х", "i": "и", "j": "дж", "k": "к", "l": "л",
    "m": "м", "n": "н", "o": "о", "p": "п", "q": "к", "r": "р",
    "s": "с", "t": "т", "u": "у", "v": "в", "w": "в", "x": "кс",
    "y": "й", "z": "з",
    "sh": "ш", "ch": "ч", "zh": "ж", "th": "т", "ph": "ф",
    "ya": "я", "yu": "ю", "yo": "ё", "ye": "е", "ey": "ей",
    "oo": "у", "ee": "и", "ts": "ц", "ck": "к",
}

_RU2EN = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "yo",
    "ж": "zh", "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m",
    "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
    "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
}


def _transliterate_to_russian(text: str) -> str:
    result = text.lower()
    for lat, cyr in sorted(_EN2RU.items(), key=lambda x: -len(x[0])):
        result = result.replace(lat, cyr)
    return result


def _transliterate_to_latin(text: str) -> str:
    result_parts: List[str] = []
    for ch in text.lower():
        result_parts.append(_RU2EN.get(ch, ch))
    return "".join(result_parts)


def _has_cyrillic(text: str) -> bool:
    return bool(re.search(r"[а-яёА-ЯЁ]", text))


def _has_latin(text: str) -> bool:
    return bool(re.search(r"[a-zA-Z]", text))

# Формат VK track_id: owner_id (опционально минус) + _ + audio_id;
# опционально третий сегмент — access_key (нужен части аудио для audio.getById).
_VK_TRACK_ID_RE = re.compile(r"^(-?\d+)_(\d+)(?:_([a-zA-Z0-9_-]+))?$")


def _valid_track_id(track_id: str) -> bool:
    s = (track_id or "").strip()
    return bool(s and (_VK_TRACK_ID_RE.match(s) or is_soundcloud_track_id(s)))


_YT_VIDEO_ID_RE = re.compile(r"^[\w-]{11}$")


def _is_youtube_video_id(s: str) -> bool:
    return bool(s and _YT_VIDEO_ID_RE.match(s.strip()))


def _vk_canonical_track_id(track_id: str) -> str:
    """Ключ Redis/негативного кэша: всегда owner_id_audio_id без access_key."""
    m = _VK_TRACK_ID_RE.match((track_id or "").strip())
    if m:
        return f"{m.group(1)}_{m.group(2)}"
    sid = parse_soundcloud_track_id(track_id or "")
    if sid is not None:
        return build_soundcloud_track_id(sid)
    return (track_id or "").strip()


def _startapp_track_token(canon: str) -> str:
    """Токен для startapp=tr_*: SC без «:» — иначе ссылка в Telegram обрезается."""
    sid = parse_soundcloud_track_id(canon)
    if sid is not None:
        return f"sc_{sid}"
    return canon


def _canonical_share_track_id(raw: str) -> Optional[str]:
    """VK/SC id как есть; YouTube — 11-симв. video id (для tr_* и карточек)."""
    s = (raw or "").strip()
    if not s:
        return None
    sid = parse_soundcloud_track_id(s)
    if sid is not None:
        return build_soundcloud_track_id(sid)
    if _valid_track_id(s):
        return s
    if _is_youtube_video_id(s):
        return s
    vid = extract_video_id(s)
    return vid if vid else None


def _valid_playlist_library_track_id(track_id: str) -> bool:
    """Избранное и кастомные плейлисты: VK/SC id, YouTube video id или watch-URL."""
    s = (track_id or "").strip()
    if not s:
        return False
    if _valid_track_id(s) or _is_youtube_video_id(s):
        return True
    canon = _canonical_share_track_id(s)
    return bool(canon and (_valid_track_id(canon) or _is_youtube_video_id(canon)))


def _playlist_library_track_id_stored(raw_id: str) -> str:
    """Единый ключ в JSON плейлиста: VK без access_key; YouTube — 11 символов."""
    s = (raw_id or "").strip()
    c = _canonical_share_track_id(s)
    if c:
        return c[:50]
    return s[:50]


def _rec_norm_library_track_id(tid: str) -> str:
    """Ключ для дедупа избранное ↔ выдача YT (URL vs 11-симв.)."""
    s = (tid or "").strip()
    if not s:
        return ""
    c = _canonical_share_track_id(s)
    return (c or s).strip()


FFMPEG = shutil.which("ffmpeg")
if not FFMPEG:
    print("❌  ffmpeg не найден! brew install ffmpeg")
    exit(1)

import aiohttp
from fastapi import FastAPI, Query, Path as Param, Header, HTTPException, Request, BackgroundTasks, File, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, Response, RedirectResponse, FileResponse

# ─── Единая HTTP-сессия ──────────────────────────────────────────
_http_session: Optional[aiohttp.ClientSession] = None

def _menu_button_payload() -> dict:
    return {
        "type": "web_app",
        "text": "PLAY",  # всегда PLAY; MENU_BUTTON_TEXT в telegram_welcome для единообразия
        "web_app": {"url": WEBAPP_URL_CANONICAL},
    }


async def _set_telegram_menu_button() -> None:
    """При старте выставляем кнопку меню бота (текст PLAY → WEBAPP_URL_CANONICAL). Пробуем JSON и form."""
    if not BOT_TOKEN:
        return
    payload = _menu_button_payload()
    try:
        session = await get_session()
        # 1) Стандартный вызов: application/json
        async with session.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/setChatMenuButton",
            json={"menu_button": payload},
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get("ok"):
                    print(f"✅ Menu button set → PLAY → {WEBAPP_URL_CANONICAL}")
                    return
                err = data.get("description", data)
                print(f"⚠️  setChatMenuButton (json): {err}")
        # 2) Часть документации требует «JSON-serialized» строку в form
        async with session.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/setChatMenuButton",
            data={"menu_button": json.dumps(payload)},
        ) as resp:
            if resp.status == 200:
                data = await resp.json()
                if data.get("ok"):
                    print(f"✅ Menu button set (form) → PLAY")
                    return
    except Exception as e:
        print(f"⚠️  setChatMenuButton: {e}")


async def _set_telegram_bot_description() -> None:
    """Выставляем описание бота (About) через API — чтобы не слетало и индексировалось в поиске Telegram."""
    if not BOT_TOKEN:
        return
    about_text = (BOT_ABOUT_TEXT or "")[:120]
    if not about_text:
        return
    try:
        session = await get_session()
        for lang_label, short_payload, desc_payload in [
            ("default", {"short_description": about_text}, {"description": BOT_DESCRIPTION or ""}),
            ("ru", {"short_description": about_text, "language_code": "ru"}, {"description": BOT_DESCRIPTION or "", "language_code": "ru"}),
        ]:
            async with session.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/setMyShortDescription", json=short_payload
            ) as resp:
                data = await resp.json() if resp.status == 200 else {}
                if not data.get("ok"):
                    print(f"⚠️  setMyShortDescription({lang_label}): {data.get('description', data)}")
            async with session.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/setMyDescription", json=desc_payload
            ) as resp:
                data = await resp.json() if resp.status == 200 else {}
                if not data.get("ok"):
                    print(f"⚠️  setMyDescription({lang_label}): {data.get('description', data)}")
        print("✅ Описание бота установлено (About + поиск Telegram)")
    except Exception as e:
        print(f"⚠️  set bot description: {e}")


async def _set_telegram_webhook_to_our_server() -> None:
    """Ставим webhook с безопасным fallback.
    
    Перед переключением проверяем, что новый URL отвечает нашим сервером (GET /api/health
    возвращает 200). Это гарантирует, что при недоступности нового домена (DNS ещё не
    обновился, SSL не выдан) старый webhook сохраняется.

    Важно: getWebhookInfo.last_error_message «Connection timed out» значит, что **серверы
    Telegram** не достучались до webhook URL (firewall, маршрутизация, перегрузка VPS).

    Порядок кандидатов: **tgplay.fun**, канонический WEBAPP_URL, **WEBAPP_URL** из env,
    **TELEGRAM_WEBHOOK_BASE_URL** (без устаревшего запасного домена — меньше лишних
    health-check при старте и нет несовпадения TLS/SNI).
    """
    if not BOT_TOKEN:
        return
    candidates: list[str] = []
    for u in (
        "https://tgplay.fun",
        (WEBAPP_URL_CANONICAL or "").strip().rstrip("/"),
        (os.getenv("WEBAPP_URL") or "").strip().rstrip("/"),
        (os.getenv("TELEGRAM_WEBHOOK_BASE_URL") or "").strip().rstrip("/"),
    ):
        if u and u not in candidates:
            candidates.append(u)
    try:
        session = await get_session()
        # 1. Узнаём текущий webhook — не трогаем, пока не убедимся в новом
        async with session.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getWebhookInfo") as resp:
            info = (await resp.json()) if resp.status == 200 else {}
        result_wh = info.get("result", {}) or {}
        current_url = (result_wh.get("url") or "").rstrip("/")
        last_err = (result_wh.get("last_error_message") or "").lower()
        delivery_broken = "timeout" in last_err or "timed out" in last_err or "connection" in last_err

        # Публичный /api/health с самого VPS иногда флапает (тот же класс проблем, что и у Telegram).
        # Несколько попыток — чтобы при старте не срывать setWebhook из‑за одного таймаута.
        wh_retries = max(1, min(15, int((os.getenv("TELEGRAM_WEBHOOK_HEALTH_RETRIES") or "5").strip() or "5")))
        wh_delay = max(0.5, min(30.0, float((os.getenv("TELEGRAM_WEBHOOK_HEALTH_RETRY_DELAY_SEC") or "2.0").strip() or "2.0")))

        async def _probe_health_200(base: str) -> bool:
            health_url = f"{base}/api/health"
            try:
                async with session.get(
                    health_url,
                    timeout=aiohttp.ClientTimeout(total=8),
                    allow_redirects=False,
                ) as hr:
                    return hr.status == 200
            except Exception:
                return False

        healthy: list[str] = []
        for base in candidates:
            ok = False
            for attempt in range(wh_retries):
                if await _probe_health_200(base):
                    ok = True
                    break
                if attempt + 1 < wh_retries:
                    await asyncio.sleep(wh_delay)
            if ok:
                healthy.append(base)

        chosen: Optional[str] = None
        if healthy:
            if delivery_broken and current_url:
                # При ошибках доставки пробуем любой другой доступный base URL, если он есть.
                for base in healthy:
                    if base.rstrip("/") != current_url.rstrip("/"):
                        chosen = base
                        break
            if chosen is None:
                chosen = healthy[0]

        if not chosen:
            print(
                f"⚠️  Webhook не переключён: ни один URL не ответил /api/health 200: {candidates}. "
                f"Текущий webhook: {current_url!r}."
            )
            return

        webhook_url = f"{chosen}/api/telegram-webhook"
        same_url = current_url == webhook_url.rstrip("/")
        if same_url and not delivery_broken:
            print(f"✅ Webhook уже установлен → {webhook_url}")
            return
        if same_url and delivery_broken:
            print(
                f"⚠️  Webhook URL совпадает с выбранным, но Telegram сообщает ошибку доставки "
                f"({result_wh.get('last_error_message')!r}). Пробуем setWebhook повторно "
                f"(тот же URL сбрасывает очередь ошибок в части случаев)."
            )

        # 2. Выбранный домен доступен — переключаем (или другой базовый URL при таймаутах с стороны TG)
        webhook_payload: Dict[str, Any] = {"url": webhook_url, "allowed_updates": ["message", "inline_query"]}
        if delivery_broken or not same_url:
            # Сбросить зависшие апдейты в очереди Telegram при смене URL или при известных сбоях доставки
            webhook_payload["drop_pending_updates"] = True
        if TELEGRAM_WEBHOOK_SECRET:
            webhook_payload["secret_token"] = TELEGRAM_WEBHOOK_SECRET
        async with session.post(
            f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
            json=webhook_payload,
        ) as resp:
            data = (await resp.json()) if resp.status == 200 else {}

        if data.get("ok"):
            print(f"✅ Webhook переключён → {webhook_url}")
            # #region agent log
            _agent_debug_log(
                "H5",
                "server_lite:_set_telegram_webhook_to_our_server",
                "setWebhook_ok",
                {
                    "webhook_url": webhook_url,
                    "chosen_base": chosen,
                    "delivery_broken": delivery_broken,
                    "same_url": same_url,
                    "healthy_count": len(healthy),
                },
            )
            # #endregion
        else:
            err = data.get("description", data)
            print(f"⚠️  setWebhook не удался ({err}), webhook остался: {current_url!r}")
    except Exception as e:
        print(f"⚠️  setWebhook: {e}")


async def _set_telegram_bot_name() -> None:
    """Имя бота для поиска в Telegram (до 64 символов)."""
    if not BOT_TOKEN or not (BOT_NAME or "").strip():
        return
    name = (BOT_NAME or "").strip()[:64]
    if not name:
        return
    try:
        session = await get_session()
        for payload in [{"name": name}, {"name": name, "language_code": "ru"}]:
            async with session.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/setMyName", json=payload
            ) as resp:
                data = await resp.json() if resp.status == 200 else {}
                if not data.get("ok"):
                    print(f"⚠️  setMyName: {data.get('description', data)}")
        print(f"✅ Имя бота установлено: {name!r}")
    except Exception as e:
        print(f"⚠️  setMyName: {e}")


_server_start_time: float = 0.0
_geoip_cache: Dict[str, tuple[Optional[str], Optional[str]]] = {}
_init_data_validation_failures: int = 0  # счётчик неуспешных валидаций initData (для мониторинга)


async def _lifespan_deferred_heavy_init() -> None:
    """Webhook / меню / имя бота + backfill — после того как Uvicorn уже принимает запросы.

    Раньше это выполнялось в startup до yield: setWebhook дергает публичный GET /api/health,
    а воркер ещё не слушает → nginx отдаёт 502, до 15×retry×sleep — «сайт мёртв» на деплое."""
    try:
        await _set_telegram_webhook_to_our_server()
        await _set_telegram_menu_button()
        await _set_telegram_bot_name()
        await _set_telegram_bot_description()
    except Exception as e:
        print(f"⚠️  Telegram setup: {e}")
    try:
        import analytics_db

        analytics_db.init_db()
        try:
            bf = analytics_db.backfill_user_bot_audio_delivered_from_history()
            if bf.get("skipped"):
                pass
            elif bf.get("ran"):
                print(
                    f"✅ Статусы «в чате с ботом»: восстановление из аналитики "
                    f"({bf.get('sqlite_changes', 0)} операций записи в user_bot_audio_delivered)"
                )
        except Exception as bf_err:
            print(f"⚠️  backfill user_bot_audio_delivered: {bf_err}")
    except Exception as e:
        print(f"⚠️  deferred analytics backfill: {e}")


@asynccontextmanager
async def _lifespan(app: FastAPI):
    global _server_start_time
    _server_start_time = time.time()
    try:
        import analytics_db

        analytics_db.init_db()
        print("✅ Аналитика: SQLite готова")
    except Exception as e:
        print(f"⚠️  Аналитика: {e}")
    if (os.getenv("ANALYTICS_ADMIN_KEY") or "").strip() == "":
        print("⚠️  ANALYTICS_ADMIN_KEY не задан — используется ключ по умолчанию (ссылка из деплоя).")

    # Восстановить накопленные метрики кэша из Redis (если есть)
    try:
        await _load_cache_metrics_from_redis()
    except Exception as e:
        print(f"⚠️  Cache metrics restore on startup failed: {e}")

    asyncio.create_task(_token_pool._dispatcher_loop())

    async def _daily_aggregator_loop() -> None:
        while True:
            try:
                now = datetime.now(timezone.utc)
                next_run = now.replace(hour=0, minute=1, second=0, microsecond=0)
                if next_run <= now:
                    next_run += timedelta(days=1)
                delay = (next_run - now).total_seconds()
                if delay > 0:
                    await asyncio.sleep(delay)
                run_time = datetime.now(timezone.utc)
                import analytics_db
                analytics_db.init_db()
                for i in range(1, 4):
                    d = (run_time + timedelta(days=-i)).strftime("%Y-%m-%d")
                    analytics_db.recompute_daily_aggregate(d)
                analytics_db.recompute_monthly_aggregate(run_time.strftime("%Y-%m"))
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"⚠️  Аналитика агрегатор: {e}")
                await asyncio.sleep(3600)

    _aggregator_task: Optional[asyncio.Task] = None
    _cache_metrics_task: Optional[asyncio.Task] = None
    try:
        _aggregator_task = asyncio.create_task(_daily_aggregator_loop())
    except Exception as e:
        print(f"⚠️  Запуск агрегатора: {e}")
        _aggregator_task = None

    async def _warm_cache_loop() -> None:
        await asyncio.sleep(3600)
        while True:
            try:
                await _warm_search_cache()
                await asyncio.sleep(3600 * 24)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"⚠️  Прогрев кэша поиска: {e}")
                await asyncio.sleep(3600)

    try:
        asyncio.create_task(_warm_cache_loop())
    except Exception as e:
        print(f"⚠️  Запуск warm cache: {e}")

    # Периодический flush метрик кэша в Redis
    try:
        _cache_metrics_task = asyncio.create_task(_cache_metrics_flush_loop())
    except Exception as e:
        print(f"⚠️  Запуск cache metrics flush: {e}")
        _cache_metrics_task = None

    _safe_ensure_future(_lifespan_deferred_heavy_init())

    yield
    if _aggregator_task is not None and not _aggregator_task.done():
        _aggregator_task.cancel()
        try:
            await _aggregator_task
        except asyncio.CancelledError:
            pass
    if _cache_metrics_task is not None and not _cache_metrics_task.done():
        _cache_metrics_task.cancel()
        try:
            await _cache_metrics_task
        except asyncio.CancelledError:
            pass
    global _http_session, _tg_upload_session
    if _http_session and not _http_session.closed:
        await _http_session.close()
    if _tg_upload_session and not _tg_upload_session.closed:
        await _tg_upload_session.close()
    for ps in _proxy_sessions.values():
        if not ps.closed:
            await ps.close()

app = FastAPI(
    title="TGPlay Lite API",
    version="0.20.1",
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
    lifespan=_lifespan,
)

async def get_session() -> aiohttp.ClientSession:
    global _http_session
    if _http_session is None or _http_session.closed:
        timeout = aiohttp.ClientTimeout(total=45, connect=12, sock_read=35)
        connector = aiohttp.TCPConnector(
            limit=100,
            limit_per_host=30,
            ttl_dns_cache=300,
            enable_cleanup_closed=True,
            force_close=False,
            keepalive_timeout=60,
        )
        _http_session = aiohttp.ClientSession(timeout=timeout, connector=connector)
    return _http_session

_tg_upload_session: Optional[aiohttp.ClientSession] = None

async def _get_tg_upload_session() -> aiohttp.ClientSession:
    """Отдельная сессия для upload файлов в Telegram — большой таймаут, DNS-кэш, keepalive."""
    global _tg_upload_session
    if _tg_upload_session is None or _tg_upload_session.closed:
        timeout = aiohttp.ClientTimeout(total=180, connect=15, sock_read=90)
        connector = aiohttp.TCPConnector(
            limit=10,
            limit_per_host=5,
            ttl_dns_cache=600,
            enable_cleanup_closed=True,
            force_close=False,
            keepalive_timeout=120,
        )
        _tg_upload_session = aiohttp.ClientSession(timeout=timeout, connector=connector)
    return _tg_upload_session


# ─── CORS (явные origins: Mini App + Telegram; * с credentials запрещён спецификацией) ─


def _cors_allow_origins() -> list[str]:
    """Несколько публичных доменов: Telegram / PWA (ярлык с рабочего стола часто на tgplay.fun)."""
    seen: set[str] = set()
    out: list[str] = []

    def add(url: str) -> None:
        u = (url or "").strip().rstrip("/")
        if not u.startswith("http://") and not u.startswith("https://"):
            return
        if u not in seen:
            seen.add(u)
            out.append(u)

    env_main = (os.getenv("WEBAPP_URL") or "").strip().rstrip("/")
    if env_main:
        add(env_main)
    add(WEBAPP_URL_CANONICAL)
    add("https://tgplay.fun")
    add("https://www.tgplay.fun")
    for part in (os.getenv("WEBAPP_CORS_ORIGINS") or "").split(","):
        add(part.strip())
    add("https://web.telegram.org")
    add("https://t.me")
    return out


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_allow_origins(),
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type", "Accept"],
    expose_headers=["Content-Length", "Content-Type", "Content-Range", "Accept-Ranges"],
)

# ─── Rate limiting (in-memory, по IP и глобально, плюс live search) ─────────
# У нас один исходящий IP к VK и несколько ключей/воркеров. VK даёт ошибку 9 при >1000 запросов/час
# с одного IP (для VK «пользователь» = наш сервер = 1 IP). Поэтому лимит общий на весь сервер,
# а для поиска считаем только живые походы к VK (кэш Redis/in-memory не учитывается).
_rate_limit_store: Dict[str, List[float]] = {}  # универсальное окно событий по ключу (используем и для live search)
_rate_limit_hourly: Dict[str, List[float]] = {}  # per client IP, окно 1 ч (VK resolve/download)
_rate_limit_vk_global: List[float] = []  # все запросы к VK с сервера за последний час (один IP у VK)
_live_search_hourly: Dict[str, List[float]] = {}  # per user live-search события за час
_RATE_WINDOW = max(1.0, float(os.getenv("RATE_WINDOW_SEC", "60")))  # окно для live-поиска
_RATE_SEARCH = max(1, int(os.getenv("RATE_SEARCH", "8")))  # базовый минимум для live-поиска (используется как нижняя граница)
_LIVE_SEARCH_HOUR_WINDOW = 3600.0
_H_PER_KEY_PER_HOUR = max(100.0, float(os.getenv("RATE_VK_PER_KEY_PER_HOUR", "1000")))
_RATE_LOGIN = max(1, int(os.getenv("RATE_LOGIN", "15")))
_RATE_AUTH_TELEGRAM_OAUTH = max(1, int(os.getenv("RATE_AUTH_TELEGRAM_OAUTH", "20")))
_RATE_ANALYTICS = max(1, int(os.getenv("RATE_ANALYTICS", "120")))
# Глобальный почасовой лимит без воркеров: 0 = выключен.
_RATE_VK_GLOBAL_PER_HOUR = max(0, min(2000, int(os.getenv("RATE_VK_GLOBAL_PER_HOUR", "0"))))
# Без воркеров: глобальный RPS с сервера к VK. При N токенах разумно N×3 (round-robin даёт до 3 req/s на ключ).
# Дефолт 3 при одном токене; при 7 токенах задай RATE_VK_GLOBAL_RPS=21 в .env.
_RATE_VK_GLOBAL_RPS = max(1, min(100, int(os.getenv("RATE_VK_GLOBAL_RPS", "3"))))
# Throttle при достижении глобального RPS: 0 = выключен (токены в пуле сами дросселируют через token bucket).
_SERVER_VK_THROTTLE_DURATION = max(0.0, min(5.0, float(os.getenv("VK_THROTTLE_DURATION", "0"))))
_server_vk_throttle_until: float = 0.0
# Лимит по клиенту в час (resolve/download). Расчёт: 7 токенов × 3 req/s × 3600 = 75 600 VK/ч;
# при hit кэша ~90% допустимо 75600/0.1 = 756k запросов к бэку/ч; при 400 юзерах → до 1890/юзера. Ставим 500 (запас).
_RATE_VK_PER_IP_PER_HOUR = max(20, min(2000, int(os.getenv("RATE_VK_PER_IP_PER_HOUR", "500"))))
_RATE_VK_HOUR_WINDOW = 3600.0
# Дневной лимит: бесплатные пользователи. 0 = выключен.
_RATE_VK_PER_USER_PER_DAY = max(0, min(1000, int(os.getenv("RATE_VK_PER_USER_PER_DAY", "50"))))
# Дневной лимит для подписчиков (наша платная подписка; пока — список SUBSCRIBER_TG_IDS). 0 = как у бесплатных.
_RATE_VK_PER_USER_SUB_PER_DAY = max(0, min(1000, int(os.getenv("RATE_VK_PER_USER_SUB_PER_DAY", "100"))))
_SUBSCRIBER_TG_IDS: set = set()  # tg id тех, кто купил нашу подписку (пока подписка не реализована — ручной список)
_subscriber_ids_raw = (os.getenv("SUBSCRIBER_TG_IDS") or "").strip()
if _subscriber_ids_raw:
    for s in _subscriber_ids_raw.split(","):
        s = s.strip()
        if s.isdigit():
            _SUBSCRIBER_TG_IDS.add(s)
# Юзернеймы без дневного лимита (через запятую, без @). Пример: DAILY_LIMIT_EXEMPT_USERNAMES=povariwe,otheruser
_DAILY_LIMIT_EXEMPT_USERNAMES: set = set()
_exempt_usernames_raw = (os.getenv("DAILY_LIMIT_EXEMPT_USERNAMES") or "").strip()
if _exempt_usernames_raw:
    for u in _exempt_usernames_raw.split(","):
        u = u.strip().lstrip("@").lower()
        if u:
            _DAILY_LIMIT_EXEMPT_USERNAMES.add(u)
# Telegram user_id, для которых дневной лимит поиска отключён навсегда (доп. к списку по юзернейму)
_DAILY_LIMIT_EXEMPT_USER_IDS: set = {72627317}  # katttttya
_RATE_VK_DAY_WINDOW = 86400.0  # 24 ч
_rate_limit_vk_daily: Dict[str, List[float]] = {}
# Обход rate limit только для нагрузочного теста: не включать на проде с реальными пользователями.
_RATE_LIMIT_DISABLED = os.getenv("RATE_LIMIT_DISABLED", "").strip() == "1"
_LOAD_TEST_SECRET = (os.getenv("LOAD_TEST_SECRET") or "").strip()


def _request_ip(request: Request) -> str:
    """Реальный IP клиента: читаем X-Real-IP / X-Forwarded-For (проставляются nginx).
    Если заголовки отсутствуют (прямой запрос без прокси) — берём transport IP.
    Возвращает первый IP из X-Forwarded-For, чтобы не попасть на IP CDN/прокси в конце цепочки.
    """
    xff = request.headers.get("x-forwarded-for") or request.headers.get("X-Forwarded-For")
    if xff:
        return xff.split(",")[0].strip() or "0.0.0.0"
    xri = request.headers.get("x-real-ip") or request.headers.get("X-Real-IP")
    if xri:
        return xri.strip() or "0.0.0.0"
    return (request.client.host if request.client else "") or "0.0.0.0"


_tg_oidc_jwks_client: Any = None


def _get_tg_oidc_jwks_client():
    """PyJWKClient для проверки id_token Telegram Login (OIDC)."""
    global _tg_oidc_jwks_client
    if _tg_oidc_jwks_client is None:
        import jwt
        from jwt import PyJWKClient

        _tg_oidc_jwks_client = PyJWKClient(TELEGRAM_OIDC_JWKS_URL, cache_keys=True, max_cached_keys=16)
    return _tg_oidc_jwks_client


def verify_telegram_oidc_id_token(raw_token: str) -> Dict[str, Any]:
    """Проверка подписи и клеймов OIDC id_token от oauth.telegram.org."""
    import jwt

    if not TELEGRAM_OAUTH_CLIENT_ID:
        raise ValueError("TELEGRAM_OAUTH_CLIENT_ID is not configured")
    client = _get_tg_oidc_jwks_client()
    signing_key = client.get_signing_key_from_jwt(raw_token)
    return jwt.decode(
        raw_token,
        signing_key.key,
        algorithms=["RS256"],
        audience=TELEGRAM_OAUTH_CLIENT_ID,
        issuer=TELEGRAM_OIDC_ISSUER,
        options={"require": ["exp", "iat", "sub", "aud", "iss"]},
    )


def _merge_user_from_oidc_callback(claims: Dict[str, Any], extra: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Единый dict user как у initData: id, first_name, username."""
    uid = claims.get("id")
    if uid is None:
        try:
            uid = int(str(claims.get("sub", "")).strip())
        except (TypeError, ValueError):
            uid = None
    if uid is None and extra and extra.get("id") is not None:
        try:
            uid = int(extra["id"])
        except (TypeError, ValueError):
            uid = None
    if uid is None:
        raise ValueError("id_token missing user id")

    first_name = ""
    username = None
    name = (claims.get("name") or "").strip()
    if name:
        first_name = name.split()[0] if name.split() else name
    pu = claims.get("preferred_username")
    if isinstance(pu, str) and pu.strip():
        username = pu.strip()

    if extra and isinstance(extra, dict):
        if not first_name and isinstance(extra.get("first_name"), str) and extra["first_name"].strip():
            first_name = extra["first_name"].strip()
        if username is None and isinstance(extra.get("username"), str) and extra["username"].strip():
            username = extra["username"].strip()
        if not first_name and isinstance(extra.get("last_name"), str) and extra["last_name"].strip():
            first_name = extra["last_name"].strip()

    if not first_name:
        first_name = "User"

    return {"id": int(uid), "first_name": first_name[:200], "username": username}


def issue_web_session_jwt(user: Dict[str, Any]) -> Tuple[str, int]:
    """HS256 JWT для Authorization: Bearer (веб/PWA без Mini App initData)."""
    import jwt

    if not TGPLAY_WEB_SESSION_SECRET:
        raise ValueError("TGPLAY_WEB_SESSION_SECRET is not configured")
    now = int(time.time())
    exp_ts = now + WEB_SESSION_EXPIRE_DAYS * 86400
    un = user.get("username")
    payload = {
        "tgplay": 1,
        "v": 1,
        "uid": int(user["id"]),
        "fn": str(user.get("first_name") or "User")[:200],
        "un": (str(un).strip()[:200] if isinstance(un, str) and un.strip() else None),
        "iat": now,
        "exp": exp_ts,
    }
    token = jwt.encode(payload, TGPLAY_WEB_SESSION_SECRET, algorithm=WEB_SESSION_JWT_ALG)
    if isinstance(token, bytes):
        token = token.decode("ascii")
    return token, exp_ts - now


def verify_web_session_jwt(raw_token: str) -> Optional[Dict[str, Any]]:
    import jwt

    if not TGPLAY_WEB_SESSION_SECRET or not raw_token:
        return None
    try:
        payload = jwt.decode(
            raw_token,
            TGPLAY_WEB_SESSION_SECRET,
            algorithms=[WEB_SESSION_JWT_ALG],
            options={"require": ["exp", "iat", "uid"]},
        )
    except jwt.InvalidTokenError:
        return None
    if payload.get("tgplay") != 1:
        return None
    try:
        uid = int(payload["uid"])
    except (TypeError, ValueError):
        return None
    un = payload.get("un")
    return {
        "id": uid,
        "first_name": str(payload.get("fn") or "User")[:200],
        "username": str(un).strip() if isinstance(un, str) and str(un).strip() else None,
    }


def _telegram_web_login_response_dict(
    raw_id_token: str,
    extra: Optional[Dict[str, Any]] = None,
    expected_nonce: Optional[str] = None,
) -> Dict[str, Any]:
    """Общий JSON для /api/auth/telegram и /api/auth/telegram/code после валидного id_token."""
    try:
        claims = verify_telegram_oidc_id_token(raw_id_token)
    except ValueError as e:
        raise HTTPException(503, str(e)) from e
    except Exception as e:
        print(f"⚠️ id_token verify: {e}")
        raise HTTPException(401, "Invalid id_token") from e
    # nonce в доке Telegram опционален: если id_token не содержит claim nonce — не режем вход.
    # Если claim есть — обязан совпасть с тем, что клиент отправил (replay / подмена окна).
    if expected_nonce is not None and str(expected_nonce).strip():
        exp = str(expected_nonce).strip()
        if len(exp) > 200:
            raise HTTPException(400, "Invalid nonce length")
        cn = claims.get("nonce")
        if cn is not None and str(cn) != exp:
            raise HTTPException(401, "Invalid id_token: nonce mismatch")
    try:
        user = _merge_user_from_oidc_callback(claims, extra)
    except ValueError:
        raise HTTPException(401, "Invalid id_token: missing user id") from None
    _register_bot_subscriber_from_telegram_user(user, "auth_oidc_telegram", force=True)
    try:
        access_token, expires_in = issue_web_session_jwt(user)
    except ValueError as e:
        raise HTTPException(503, str(e)) from e
    safe_user = {
        "id": user["id"],
        "first_name": user.get("first_name", ""),
        "username": user.get("username"),
    }
    return {
        "access_token": access_token,
        "token_type": "Bearer",
        "expires_in": expires_in,
        "user": safe_user,
    }


def _user_from_authorization_header(auth: Optional[str]) -> Optional[Dict]:
    """initData (tma) или сессионный JWT (Bearer) → user dict как у validate_init_data."""
    if not auth:
        return None
    parts = auth.split(" ", 1)
    if len(parts) != 2:
        return None
    scheme, value = parts[0].lower(), parts[1].strip()
    if not value:
        return None
    if scheme == "tma":
        return validate_init_data(value, BOT_TOKEN)
    if scheme == "bearer":
        return verify_web_session_jwt(value)
    return None


def _telegram_user_from_auth_header(auth: Optional[str]) -> Optional[Dict]:
    """Совместимость: валидный user из Authorization, иначе None."""
    return _user_from_authorization_header(auth)


def _tg_user_id_from_request(request: Request) -> Optional[int]:
    """Telegram user_id из initData; из request.state (RegisterTelegramUserMiddleware), без второго HMAC на /api/*."""
    if hasattr(request.state, "tgplay_telegram_user"):
        user = request.state.tgplay_telegram_user
        if not user:
            return None
        uid = user.get("id") if isinstance(user, dict) else None
        try:
            return int(uid) if uid is not None else None
        except (TypeError, ValueError):
            return None
    user = _telegram_user_from_auth_header(request.headers.get("Authorization"))
    if not user:
        return None
    uid = user.get("id")
    try:
        return int(uid) if uid is not None else None
    except (TypeError, ValueError):
        return None


_last_bot_sub_upsert: Dict[int, float] = {}
_BOT_SUB_THROTTLE_SEC = 120.0  # не пишем в SQLite на каждый поиск/resolve — только раз в N сек на uid


def _register_bot_subscriber_from_telegram_user(
    user: Optional[Dict], source: str, *, force: bool = False
) -> None:
    """Сохранить numeric user id для рассылок (идемпотентно). force=True — без троттлинга (webhook, /api/me/register)."""
    if not user or user.get("id") is None:
        return
    try:
        import analytics_db

        uid = int(user["id"])
        now = time.time()
        if not force:
            prev = _last_bot_sub_upsert.get(uid, 0.0)
            if now - prev < _BOT_SUB_THROTTLE_SEC:
                return
            _last_bot_sub_upsert[uid] = now
            if len(_last_bot_sub_upsert) > 25000:
                _last_bot_sub_upsert.clear()

        analytics_db.init_db()
        un = user.get("username")
        un = un.strip() if isinstance(un, str) and un.strip() else None
        analytics_db.upsert_bot_subscriber(uid, un, source[:48])
    except Exception as e:
        print(f"⚠️ bot_subscriber ({source}): {e}")


def _rate_limit_key(path: str, request: Request) -> str:
    """Ключ для лимита: по Telegram user_id при наличии, иначе по IP (для гостей/ботов)."""
    uid = _tg_user_id_from_request(request)
    if uid is not None:
        return f"{path}:tg:{uid}"
    ip = _request_ip(request)
    return f"{path}:ip:{ip}"

def _is_vk_consuming_path(path: str) -> bool:
    if path == "/api/music/search" or path == "/api/music/resolve-batch":
        return True
    if path.startswith("/api/music/resolve/") or path.startswith("/api/music/download/"):
        return True
    return False

def _check_rate_limit(request: Request, limit: int) -> bool:
    path = request.url.path
    key = _rate_limit_key(path, request)
    now = time.time()
    if key not in _rate_limit_store:
        _rate_limit_store[key] = []
    times = _rate_limit_store[key]
    times[:] = [t for t in times if now - t < _RATE_WINDOW]
    if len(times) >= limit:
        return False
    times.append(now)
    # Периодическая очистка старых ключей
    if len(_rate_limit_store) > 5000:
        cutoff = now - _RATE_WINDOW
        for k in list(_rate_limit_store):
            _rate_limit_store[k] = [t for t in _rate_limit_store[k] if t > cutoff]
            if not _rate_limit_store[k]:
                del _rate_limit_store[k]
    return True


def _estimate_live_search_users(window: float) -> int:
    """Оценка числа активных пользователей live-search: уникальные ключи /api/music/search:... за последнее окно."""
    now = time.time()
    count = 0
    for k, times in list(_rate_limit_store.items()):
        if not k.startswith("/api/music/search:"):
            continue
        # times уже могут содержать события от других лимитов, но для поиска храним только live-search
        times_recent = [t for t in times if now - t < window]
        if not times_recent:
            _rate_limit_store.pop(k, None)
            continue
        _rate_limit_store[k] = times_recent
        # активный пользователь — если был live-search в пределах окна
        if now - times_recent[-1] < window:
            count += 1
    return max(count, 1)


def _live_search_budget_per_user() -> int:
    """Динамический лимит живых поисков к VK на пользователя за окно (_RATE_WINDOW), с учётом онлайна и hit-rate кэша."""
    window = _RATE_WINDOW
    U = _estimate_live_search_users(window)
    # Оценка hit-rate поиска по глобальным метрикам
    search_total = _cache_metrics["search_hit"] + _cache_metrics["search_miss"]
    if search_total > 0:
        p_hit = _cache_metrics["search_hit"] / search_total
        p_hit = max(0.0, min(0.99, p_hit))
    else:
        p_hit = 0.5  # до первых запросов считаем 50/50
    live_fraction = max(0.01, 1.0 - p_hit)
    # Безопасный лимит VK: 3 RPS на ключ, 70% от максимума
    R_safe = max(1.0, 3.0 * _token_pool.count * 0.7)
    q_target = R_safe / (U * live_fraction)
    # Ограничения: от 0.2 до 2 живых поисков/с на пользователя
    q_min = 0.2
    q_max = 2.0
    q_user = max(q_min, min(q_max, q_target))
    S_live = max(1, int(q_user * window))
    return S_live


def _live_search_hour_budget_per_user() -> int:
    """Динамический часовой бюджет живых поисков к VK на пользователя.

    Используем как мягкий слой: если пользователь стабильно выше своего бюджета,
    увеличиваем для него кулдауны, но не режем жёстко сразу.
    """
    # Окно по живому поиску (те же активные пользователи, что и для 60-секундного окна)
    window_short = _RATE_WINDOW
    U = _estimate_live_search_users(window_short)
    # Оценка hit-rate поиска по глобальным метрикам
    search_total = _cache_metrics["search_hit"] + _cache_metrics["search_miss"]
    if search_total > 0:
        p_hit = _cache_metrics["search_hit"] / search_total
        p_hit = max(0.0, min(0.99, p_hit))
    else:
        p_hit = 0.5
    live_fraction = max(0.01, 1.0 - p_hit)
    # Часовой безопасный лимит VK: H_per_key_per_hour * N_keys * 0.7
    H_safe = _H_PER_KEY_PER_HOUR * max(1, _token_pool.count) * 0.7
    # Целевой бюджет на пользователя: сколько живых запросов/час он может сделать
    h_target = H_safe / (U * live_fraction)
    # Ограничения: от 50 до 5000 живых поисков/час на пользователя
    h_min = 50.0
    h_max = 5000.0
    h_user = max(h_min, min(h_max, h_target))
    return max(1, int(h_user))


def _check_live_search_limit(request: Request) -> Tuple[bool, int]:
    """Лимит только на живые походы к VK для /api/music/search.

    Считаем динамически доступный бюджет S_live на окно и применяем только если кэш не сработал.
    Возвращает (True, 0) если запрос можно пускать к VK, иначе (False, retry_after_sec).
    """
    key = _rate_limit_key("/api/music/search", request)
    now = time.time()
    window = _RATE_WINDOW
    S_live = _live_search_budget_per_user()
    # 60-секундное окно: события живого поиска для пользователя
    times = _rate_limit_store.get(key, [])
    times = [t for t in times if now - t < window]
    # Часовое окно для live-поиска этого пользователя
    h_budget = _live_search_hour_budget_per_user()
    hour_times = _live_search_hourly.get(key, [])
    hour_times = [t for t in hour_times if now - t < _LIVE_SEARCH_HOUR_WINDOW]

    # Превышен ли мягкий часовой бюджет
    over_hour = len(hour_times) >= h_budget

    if len(times) >= S_live:
        oldest = min(times) if times else now
        retry_user = max(1.0, window - (now - oldest))
        # Если пользователь ещё и превысил часовой бюджет, делаем кулдаун чуть длиннее
        if over_hour:
            retry_user *= 2.0
        retry_after = int(min(window, retry_user))
        return False, retry_after

    # Разрешаем запрос: учитываем его и в коротком, и в часовом окне
    times.append(now)
    _rate_limit_store[key] = times
    hour_times.append(now)
    _live_search_hourly[key] = hour_times
    return True, 0

def _vk_global_rps_under_limit() -> bool:
    """Как у воркеров: 3 запроса/с, затем кулдаун 1.5 с (при воркерах не вызывается)."""
    global _server_vk_throttle_until
    now = time.time()
    if now < _server_vk_throttle_until:
        return False
    recent_1s = [t for t in _rate_limit_vk_global if now - t < 1.0]
    return len(recent_1s) < _RATE_VK_GLOBAL_RPS

def _vk_global_under_limit() -> bool:
    """Есть ли ещё место в глобальном почасовом лимите (0 = не проверяем)."""
    if _RATE_VK_GLOBAL_PER_HOUR <= 0:
        return True
    now = time.time()
    recent = [t for t in _rate_limit_vk_global if now - t < _RATE_VK_HOUR_WINDOW]
    return len(recent) < _RATE_VK_GLOBAL_PER_HOUR

def _vk_global_record() -> None:
    global _server_vk_throttle_until
    now = time.time()
    _rate_limit_vk_global[:] = [t for t in _rate_limit_vk_global if now - t < _RATE_VK_HOUR_WINDOW]
    _rate_limit_vk_global.append(now)
    recent_1s = [t for t in _rate_limit_vk_global if now - t < 1.0]
    if len(recent_1s) >= _RATE_VK_GLOBAL_RPS:
        _server_vk_throttle_until = now + _SERVER_VK_THROTTLE_DURATION

def _check_rate_limit_vk_hourly(request: Request) -> bool:
    """Не больше RATE_VK_PER_IP_PER_HOUR в час с одного клиента (Telegram user_id, если есть; иначе IP); при проходе записывает."""
    uid = _tg_user_id_from_request(request)
    if uid is not None:
        key = f"vk_hourly:tg:{uid}"
    else:
        ip = _request_ip(request)
        key = f"vk_hourly:ip:{ip}"
    now = time.time()
    if key not in _rate_limit_hourly:
        _rate_limit_hourly[key] = []
    times = _rate_limit_hourly[key]
    times[:] = [t for t in times if now - t < _RATE_VK_HOUR_WINDOW]
    if len(times) >= _RATE_VK_PER_IP_PER_HOUR:
        return False
    times.append(now)
    if len(_rate_limit_hourly) > 10000:
        cutoff = now - _RATE_VK_HOUR_WINDOW
        for k in list(_rate_limit_hourly):
            _rate_limit_hourly[k] = [t for t in _rate_limit_hourly[k] if t > cutoff]
            if not _rate_limit_hourly[k]:
                del _rate_limit_hourly[k]
    return True


def _get_vk_daily_limit_key(request: Request) -> str:
    """Ключ для дневного лимита: telegram_user_id если есть авторизация, иначе IP.

    Привязываем к дате UTC (YYYY-MM-DD), чтобы лимит жёстко сбрасывался в 00:00 UTC для всех пользователей.
    """
    today_utc = datetime.utcnow().strftime("%Y-%m-%d")
    auth = request.headers.get("Authorization")
    if auth:
        try:
            user = _user_from_authorization_header(auth)
            if user and user.get("id") is not None:
                return f"vkdaily:{today_utc}:tg:{user['id']}"
        except Exception:
            pass
    return f"vkdaily:{today_utc}:ip:{_request_ip(request)}"


def _is_vk_subscriber(request: Request) -> bool:
    """Подписчик = купил нашу подписку (пока только id в SUBSCRIBER_TG_IDS). Telegram Premium не учитывается."""
    auth = request.headers.get("Authorization")
    if not auth:
        return False
    try:
        user = _user_from_authorization_header(auth)
        if not user:
            return False
        uid = user.get("id")
        if uid is not None and str(uid) in _SUBSCRIBER_TG_IDS:
            return True
    except Exception:
        pass
    return False


def _get_vk_daily_limit_for_request(request: Request) -> int:
    """Дневной лимит VK-запросов для этого пользователя: исключённые по user_id или username — без лимита, подписчик — выше, иначе 50. 0 = проверка выключена."""
    auth = request.headers.get("Authorization")
    if auth:
        try:
            user = _user_from_authorization_header(auth)
            if user:
                uid = user.get("id")
                if uid is not None and str(uid) in _DAILY_LIMIT_EXEMPT_USER_IDS:
                    return 0
                if _DAILY_LIMIT_EXEMPT_USERNAMES:
                    uname = (user.get("username") or "").strip().lower()
                    if uname and uname in _DAILY_LIMIT_EXEMPT_USERNAMES:
                        return 0
        except Exception:
            pass
    if _RATE_VK_PER_USER_PER_DAY <= 0 and _RATE_VK_PER_USER_SUB_PER_DAY <= 0:
        return 0
    if _is_vk_subscriber(request) and _RATE_VK_PER_USER_SUB_PER_DAY > 0:
        return _RATE_VK_PER_USER_SUB_PER_DAY
    return _RATE_VK_PER_USER_PER_DAY if _RATE_VK_PER_USER_PER_DAY > 0 else 0


def _check_rate_limit_vk_daily(request: Request) -> bool:
    """Не больше дневного лимита с одного пользователя (подписчик — выше лимит). 0 = отключено."""
    limit = _get_vk_daily_limit_for_request(request)
    if limit <= 0:
        return True
    key = _get_vk_daily_limit_key(request)
    now = time.time()
    if key not in _rate_limit_vk_daily:
        _rate_limit_vk_daily[key] = []
    times = _rate_limit_vk_daily[key]
    times[:] = [t for t in times if now - t < _RATE_VK_DAY_WINDOW]
    if len(times) >= limit:
        return False
    times.append(now)
    if len(_rate_limit_vk_daily) > 50000:
        cutoff = now - _RATE_VK_DAY_WINDOW
        for k in list(_rate_limit_vk_daily):
            _rate_limit_vk_daily[k] = [t for t in _rate_limit_vk_daily[k] if t > cutoff]
            if not _rate_limit_vk_daily[k]:
                del _rate_limit_vk_daily[k]
    return True


# ─── GZip (меньше трафика для VPN/медленных сетей) ─────────────────
from starlette.middleware.gzip import GZipMiddleware
app.add_middleware(GZipMiddleware, minimum_size=500)

# ─── Security middleware ─────────────────────────────────────────
from starlette.middleware.base import BaseHTTPMiddleware

class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        skip_search_limit = _RATE_LIMIT_DISABLED or (
            _LOAD_TEST_SECRET and request.headers.get("X-Load-Test") == _LOAD_TEST_SECRET
        )
        # Лимит запросов к VK: при одном IP — RPS и почасовой лимит; при воркерах (N IP) глобальные лимиты не применяем.
        if _is_vk_consuming_path(path) and not skip_search_limit:
            if not _token_pool.has_workers:
                if not _vk_global_rps_under_limit():
                    # Глобальный лимит исходящих запросов к VK на сервер без воркеров:
                    # отдаём мягкий 429 с retry_after_sec, чтобы фронт показал тот же
                    # бэкофф и один раз автоматически повторил запрос.
                    now = time.time()
                    retry_after = _SERVER_VK_THROTTLE_DURATION
                    remaining = max(0.0, _server_vk_throttle_until - now)
                    if remaining > 0:
                        retry_after = max(retry_after, remaining)
                    payload = {
                        "detail": "Too Many Requests (server rate limit)",
                        "retry_after_sec": max(1.0, min(30.0, float(retry_after))),
                    }
                    return Response(
                        status_code=429,
                        content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                        media_type="application/json",
                    )
                if _RATE_VK_GLOBAL_PER_HOUR > 0 and not _vk_global_under_limit():
                    return Response(
                        status_code=429,
                        content=b'{"detail":"Too Many Requests (server hourly limit)"}',
                        media_type="application/json",
                    )
                _vk_global_record()
            # Почасовой лимит по клиенту: только resolve/download (не search). При превышении — 429,
            # фронт пробует download как fallback, тот тоже 429 → воспроизведение не стартует (play_failed).
            if path != "/api/music/search":
                if not _check_rate_limit_vk_hourly(request):
                    return Response(
                        status_code=429,
                        content=b'{"detail":"Too Many Requests (hourly limit per client)"}',
                        media_type="application/json",
                    )
            # Дневной лимит: только поиск (/api/music/search), и только «первая страница» (offset=0).
            # «Подгрузить ещё» (offset>0) в лимит не входят — иначе 1 поиск + 10 подгрузок = 11 запросов в квоту.
            if path == "/api/music/search":
                offset_s = request.query_params.get("offset", "0").strip()
                try:
                    search_offset = int(offset_s)
                except ValueError:
                    search_offset = 0
                if search_offset == 0:
                    vk_daily_limit = _get_vk_daily_limit_for_request(request)
                    if vk_daily_limit > 0 and not _check_rate_limit_vk_daily(request):
                        return Response(
                            status_code=429,
                            content=json.dumps(
                                {"detail": f"Превышен дневной лимит поисковых запросов ({vk_daily_limit} в день)."},
                                ensure_ascii=False,
                            ).encode("utf-8"),
                            media_type="application/json",
                        )
        elif path == "/api/auth/login":
            if not _check_rate_limit(request, _RATE_LOGIN):
                return Response(status_code=429, content=b'{"detail":"Too Many Requests"}', media_type="application/json")
        elif path == "/api/auth/telegram":
            if not _check_rate_limit(request, _RATE_AUTH_TELEGRAM_OAUTH):
                return Response(status_code=429, content=b'{"detail":"Too Many Requests"}', media_type="application/json")
        elif path == "/api/auth/telegram/code":
            if not _check_rate_limit(request, _RATE_AUTH_TELEGRAM_OAUTH):
                return Response(status_code=429, content=b'{"detail":"Too Many Requests"}', media_type="application/json")
        elif path == "/api/analytics/event":
            if not _check_rate_limit(request, _RATE_ANALYTICS):
                return Response(status_code=429, content=b'{"detail":"Too Many Requests"}', media_type="application/json")
        elif path == "/api/me/register":
            if not _check_rate_limit(request, _RATE_ANALYTICS):
                return Response(status_code=429, content=b'{"detail":"Too Many Requests"}', media_type="application/json")
        elif path == "/api/me/dislike":
            if not _check_rate_limit(request, _RATE_ANALYTICS):
                return Response(status_code=429, content=b'{"detail":"Too Many Requests"}', media_type="application/json")
        return await call_next(request)

app.add_middleware(RateLimitMiddleware)


# Vite пишет content-hash в имени файла в base64url-алфавите (буквы обоих регистров, цифры, '-'/'_'),
# а НЕ в hex. Раньше шаблон требовал hex → хэш вида index-BAvI202H.css не матчился и ассеты отдавались
# с no-cache (повторные визиты заново качали весь бандл). Берём весь base64url-алфавит.
_HASHED_ASSET_RE = re.compile(r"/assets/[^/]+-[A-Za-z0-9_-]{8,}\.(js|css|woff2?|ttf|eot|png|svg|jpg|jpeg|webp|ico)$")


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        # Не ставим X-Frame-Options — Mini App открывается в iframe Telegram
        response.headers["Content-Security-Policy"] = (
            "frame-ancestors 'self' https://web.telegram.org https://t.me"
        )
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        # HSTS: браузер больше не ходит по HTTP после первого посещения (31536000 = 1 год)
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"

        path = request.url.path
        if _HASHED_ASSET_RE.match(path):
            # Vite генерирует content-hash в имени — файл неизменен, кэшируем на год
            response.headers["Cache-Control"] = "public, max-age=31536000, immutable"
            # Starlette MutableHeaders не поддерживает .pop(); удаляем через del с проверкой наличия.
            if "Pragma" in response.headers:
                del response.headers["Pragma"]
            if "Expires" in response.headers:
                del response.headers["Expires"]
        elif path.startswith("/assets/"):
            # Не-хэшированные ресурсы (редко): переспросить, но использовать кэш при 304
            response.headers["Cache-Control"] = "no-cache"
        return response

app.add_middleware(SecurityHeadersMiddleware)


class RegisterTelegramUserMiddleware(BaseHTTPMiddleware):
    """
    Любой /api/* с валидным Authorization: tma <initData> или Bearer <web_session_jwt> → user id (рассылки).
    Кладёт user в request.state — без повторной проверки на каждый лимит.
    """

    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith("/api/") and request.method != "OPTIONS":
            auth = request.headers.get("Authorization")
            user = _telegram_user_from_auth_header(auth) if auth else None
            request.state.tgplay_telegram_user = user
            _register_bot_subscriber_from_telegram_user(user, "api_initdata_header")
        return await call_next(request)


app.add_middleware(RegisterTelegramUserMiddleware)


# ─── Telegram WebApp Auth ────────────────────────────────────────

def validate_init_data(init_data: str, bot_token: str) -> Optional[Dict]:
    global _init_data_validation_failures
    try:
        parsed = parse_qs(init_data, keep_blank_values=True)
        check_hash = parsed.get("hash", [None])[0]
        if not check_hash:
            _init_data_validation_failures += 1
            return None

        pairs = []
        for key, values in parsed.items():
            if key == "hash":
                continue
            pairs.append(f"{key}={values[0]}")
        pairs.sort()
        data_check_string = "\n".join(pairs)

        secret_key = hmac.new(b"WebAppData", bot_token.encode(), hashlib.sha256).digest()
        calculated = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        if not hmac.compare_digest(calculated, check_hash):
            _init_data_validation_failures += 1
            return None

        auth_date = int(parsed.get("auth_date", ["0"])[0])
        if time.time() - auth_date > 7 * 86400:
            _init_data_validation_failures += 1
            return None

        user_raw = parsed.get("user", [None])[0]
        if not user_raw:
            _init_data_validation_failures += 1
            return None
        user = json.loads(unquote(user_raw))
        return user
    except Exception as e:
        _init_data_validation_failures += 1
        print(f"⚠️ initData validation error: {e}")
        return None


def get_user_from_header(authorization: Optional[str]) -> Dict:
    if not authorization:
        raise HTTPException(401, "Missing Authorization header")
    user = _user_from_authorization_header(authorization)
    if not user:
        raise HTTPException(401, "Invalid or expired Telegram session")
    return user


# ─── User playlist storage ──────────────────────────────────────
# Системный плейлист «Избранное» = user_data/{user_id}.json (в лимит не входит).
# Кастомные плейлисты (до MAX_FREE_PLAYLISTS) = user_data/{user_id}_playlists.json.
# Публичные шары (для deep link) = user_data/shares.json (без telegram id).

MAX_FREE_PLAYLISTS = 5  # лимит созданных плейлистов; для будущей монетизации

def _playlist_path(user_id: int) -> Path:
    safe_id = int(user_id)
    return DATA_DIR / f"{safe_id}.json"

def load_playlist(user_id: int) -> List[Dict]:
    p = _playlist_path(user_id)
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text("utf-8"))
    except Exception as e:
        logger.error("load_playlist failed: user_id=%s: %s", user_id, e, exc_info=True)
        return []

def save_playlist(user_id: int, tracks: List[Dict]):
    p = _playlist_path(user_id)
    old_ids: set[str] = set()
    if p.exists():
        try:
            for t in json.loads(p.read_text("utf-8")):
                raw = str(t.get("id") or "").strip()
                if _valid_playlist_library_track_id(raw):
                    old_ids.add(_playlist_library_track_id_stored(raw))
        except Exception:
            pass
    new_ids: set[str] = set()
    for t in tracks:
        raw = str(t.get("id") or "").strip()
        if _valid_playlist_library_track_id(raw):
            new_ids.add(_playlist_library_track_id_stored(raw))
    removed = list(old_ids - new_ids)
    p.write_text(json.dumps(tracks, ensure_ascii=False, indent=2), "utf-8")
    if removed:
        try:
            import analytics_db

            analytics_db.init_db()
            analytics_db.record_removed_library_track_ids(int(user_id), removed)
        except Exception as e:
            logger.debug("record_removed_library_track_ids uid=%s: %s", user_id, e)
    _sync_user_library_index(int(user_id))


def _custom_playlists_path(user_id: int) -> Path:
    return DATA_DIR / f"{int(user_id)}_playlists.json"

def load_custom_playlists(user_id: int) -> List[Dict]:
    p = _custom_playlists_path(user_id)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text("utf-8"))
        return data.get("playlists", [])
    except Exception as e:
        logger.error("load_custom_playlists failed: user_id=%s: %s", user_id, e, exc_info=True)
        return []

def save_custom_playlists(user_id: int, playlists: List[Dict]):
    p = _custom_playlists_path(user_id)
    p.write_text(json.dumps({"playlists": playlists}, ensure_ascii=False, indent=2), "utf-8")
    _sync_user_library_index(int(user_id))


def _library_track_ids_flat(user_id: int) -> List[str]:
    """Все track_id из избранного и кастомных плейлистов (для индекса коллаборатива)."""
    seen: set[str] = set()
    out: List[str] = []
    for t in load_playlist(user_id):
        raw = str(t.get("id") or "").strip()
        if not _valid_playlist_library_track_id(raw):
            continue
        eff = _playlist_library_track_id_stored(raw)
        if eff in seen:
            continue
        seen.add(eff)
        out.append(eff)
    for pl in load_custom_playlists(user_id):
        for raw in pl.get("track_ids") or []:
            raw_s = str(raw).strip()
            if not _valid_playlist_library_track_id(raw_s):
                continue
            eff = _playlist_library_track_id_stored(raw_s)
            if eff in seen:
                continue
            seen.add(eff)
            out.append(eff)
    return out


def _sync_user_library_index(user_id: int) -> None:
    try:
        import analytics_db

        analytics_db.init_db()
        analytics_db.replace_user_library_tracks(int(user_id), _library_track_ids_flat(user_id))
    except Exception as e:
        logger.debug("user library index sync failed uid=%s: %s", user_id, e)


async def _rec_playlist_split_for_recs(
    uid: int,
) -> Tuple[List[Dict], List[str], List[Dict], List[str], List[Dict]]:
    """
    Избранное и кастомные плейлисты раздельно + merged.
    main_tracks, main_ids_ordered, custom_tracks, custom_ids_ordered, merged.
    """
    main = [dict(x) for x in load_playlist(uid)]
    main_ids = _favorite_track_ids_ordered(main)
    seen_ids: set[str] = set(main_ids)
    custom_pl = load_custom_playlists(uid)
    extra_ids: List[str] = []
    custom_meta_by_id: Dict[str, Dict[str, Any]] = {}
    for pl in custom_pl:
        pl_meta = pl.get("track_meta") or {}
        for tid in pl.get("track_ids") or []:
            raw = str(tid).strip()
            if not _valid_playlist_library_track_id(raw):
                continue
            eff = _playlist_library_track_id_stored(raw)
            if eff in seen_ids:
                continue
            seen_ids.add(eff)
            extra_ids.append(eff)
            row = pl_meta.get(eff)
            if isinstance(row, dict):
                custom_meta_by_id[eff] = row
    custom_tracks: List[Dict] = []
    vk_batch_ids = [tid for tid in extra_ids if _valid_track_id(tid)]
    vk_seen: set[str] = set()
    if vk_batch_ids and not _REC_PERSONAL_SKIP_VK:
        chunks = [vk_batch_ids[i : i + 80] for i in range(0, min(len(vk_batch_ids), 400), 80)]

        async def _chunk_meta(c: List[str]):
            try:
                return await _vk_batch_get_by_id(c)
            except Exception:
                return []

        raws = await asyncio.gather(*[_chunk_meta(c) for c in chunks])
        for raw in raws:
            for t in _parse_tracks(raw):
                tid = str(t.get("id") or "").strip()
                if tid and _valid_track_id(tid):
                    custom_tracks.append(t)
                    vk_seen.add(tid)
    for eff in extra_ids:
        if _is_youtube_video_id(eff) and eff not in vk_seen:
            m = custom_meta_by_id.get(eff) or {}
            stub: Dict[str, Any] = {
                "id": eff,
                "title": str(m.get("title") or ""),
                "artist": str(m.get("artist") or ""),
                "duration": int(m.get("duration") or 0),
                "cover_url": m.get("cover_url"),
            }
            if m.get("vk_legacy") is not None:
                stub["vk_legacy"] = bool(m.get("vk_legacy"))
            _rec_ensure_track_genre(stub)
            custom_tracks.append(stub)
    merged = main + custom_tracks
    return main, main_ids, custom_tracks, extra_ids, merged


async def _rec_all_playlist_tracks_for_recs(uid: int) -> List[Dict]:
    """Избранное + треки из кастомных плейлистов — единая база для жанров и рекомендаций."""
    *_, merged = await _rec_playlist_split_for_recs(uid)
    return merged


SHARES_FILE = DATA_DIR / "shares.json"

def load_shares() -> Dict[str, Dict]:
    if not SHARES_FILE.exists():
        return {}
    try:
        return json.loads(SHARES_FILE.read_text("utf-8"))
    except Exception as e:
        logger.error("load_shares failed: %s", e, exc_info=True)
        return {}

def save_shares(shares: Dict[str, Dict]):
    SHARES_FILE.write_text(json.dumps(shares, ensure_ascii=False, indent=2), "utf-8")


from pydantic import BaseModel

class TrackPayload(BaseModel):
    id: str
    title: str
    artist: str
    duration: int = 0
    cover_url: Optional[str] = None
    # False — трек из текущей выдачи (поиск/подборки): resolve с YouTube-fallback. True/None в JSON избранного — только VK как раньше.
    vk_legacy: Optional[bool] = None


# ─── VK helpers (оптимизированные) ───────────────────────────────

# Частые опечатки в поиске (кириллица) — исправляем для VK и релевантности
_TYPO_FIXES = [
    ("болше", "больше"),
    ("болтьше", "больше"),
    ("хочет", "хочу"),
    ("видет", "видеть"),
    ("себа", "себя"),
    ("никода", "никогда"),
    ("нехочу", "не хочу"),
]


def _fix_common_typos(text: str) -> str:
    """Исправляет типичные опечатки в запросе."""
    result = text.strip()
    for wrong, right in _TYPO_FIXES:
        result = re.sub(re.escape(wrong), right, result, flags=re.IGNORECASE)
    return result


def _build_search_queries(query: str) -> List[str]:
    """Формирует список поисковых запросов. Для смешанного запроса первым идёт полная фраза (VK лучше находит трек)."""
    words = query.split()
    queries = []

    if " - " in query:
        artist_part = query.split(" - ", 1)[0].strip()
        if len(artist_part) >= 3:
            queries.append(artist_part)
        queries.append(query)
    elif _has_latin(query) and _has_cyrillic(query):
        # Сначала полная фраза — VK по ней часто находит нужный трек
        queries.append(query)
        lat_words = [w for w in words if _has_latin(w) and not _has_cyrillic(w)]
        cyr_words = [w for w in words if _has_cyrillic(w)]
        if lat_words:
            queries.append(" ".join(lat_words))
        if cyr_words and len(cyr_words) >= 2:
            cyr_part = " ".join(cyr_words[:6])
            if cyr_part.strip().lower() not in [q.strip().lower() for q in queries]:
                queries.append(cyr_part)
    elif _has_latin(query) and not _has_cyrillic(query):
        queries.append(query)
        converted = [_transliterate_to_russian(w) if _has_latin(w) else w for w in words]
        candidate = " ".join(converted)
        if candidate != query.lower():
            queries.append(candidate)
    else:
        # Чисто кириллический запрос.
        queries.append(query)
        # Если запрос длинный (3+ слов) — добавляем хвост как отдельный запрос,
        # чтобы лучше находить редкие треки по названию.
        if len(words) >= 3:
            tail = " ".join(words[-3:]).strip()
            if tail and tail.lower() not in [q.strip().lower() for q in queries]:
                queries.append(tail)
        # Добавляем латинскую транслитерацию артиста/фразы (для Band of Moscow, Banda Moskvy и т.п.).
        latin = _transliterate_to_latin(query)
        if latin and latin.lower() not in [q.strip().lower() for q in queries]:
            queries.append(latin)

    seen = set()
    unique = []
    for q in queries:
        k = q.strip().lower()
        if k and k not in seen:
            seen.add(k)
            unique.append(q)
    return unique[:3]


async def _vk_execute_search(queries: List[str], limit: int) -> List[Dict]:
    """Комбинированный execute-поиск: до 3 audio.search внутри одного execute.

    Берём несколько вариантов запроса (queries), для каждого вызываем
    audio.search внутри VKScript, затем объединяем результаты (без дублей).
    Далее поверх этого списка уже работает наше ранжирование _relevance_score.
    """
    # Запрашиваем чуть больше, чем limit, чтобы было из чего ранжировать.
    count = min(max(limit, 50), 100)

    seen_q: set[str] = set()
    unique: List[str] = []
    for q in queries:
        k = q.strip().lower()
        if k and k not in seen_q:
            seen_q.add(k)
            unique.append(q)
    unique = unique[:3]

    if not unique:
        return []

    code_parts: List[str] = []
    var_names: List[str] = []
    for i, q in enumerate(unique):
        esc = _vk_escape(q)
        v = f"v{i}"
        code_parts.append(
            f'var {v}=API.audio.search({{"q":"{esc}","count":{count},"sort":0,"auto_complete":0,"search_own":0}});'
        )
        var_names.append(v)

    ret_obj = ",".join(f'"{v}":{v}.items' for v in var_names)
    code_parts.append(f"return {{{ret_obj}}};")
    vkscript = "".join(code_parts)

    data = await _vk_api_call("execute", {"code": vkscript}, post=True)
    if "error" in data:
        # fallback: один raw-поиск по первой фразе
        return await _vk_search_raw_fallback(unique[0], limit)

    response = data.get("response", {})
    if not isinstance(response, dict):
        return []

    all_items: List[Dict] = []
    seen_ids: set[str] = set()
    for v in var_names:
        items = response.get(v)
        if not isinstance(items, list):
            continue
        for item in items:
            try:
                tid = f"{item['owner_id']}_{item['id']}"
            except Exception:
                continue
            if tid in seen_ids:
                continue
            seen_ids.add(tid)
            all_items.append(item)
    return all_items


def _vk_escape(s: str) -> str:
    """Экранирование строки для VKScript."""
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").replace("\r", "")


async def _vk_search_raw_fallback(query: str, limit: int, offset: int = 0, sort: int = 0) -> List[Dict]:
    """Fallback: обычный audio.search (с offset для пагинации). sort: 0 дата, 1 длительность, 2 популярность (VK)."""
    data = await _vk_api_call("audio.search", {
        "q": query,
        "count": min(limit, 300),
        "offset": max(0, offset),
        "sort": sort,
        "auto_complete": 0,
        "search_own": 0,
    })
    if "error" in data:
        return []
    return data.get("response", {}).get("items", [])


# Справочник VK audio.genre_id → короткая метка для группировки рекомендаций
_VK_AUDIO_GENRE_LABELS: Dict[int, str] = {
    1: "Рок",
    2: "Поп",
    3: "Рэп и хип-хоп",
    4: "Лёгкая музыка",
    5: "Электроника",
    6: "Инструментал",
    7: "Метал",
    8: "Дабстеп",
    10: "Drum & bass",
    11: "Транс",
    12: "Шансон",
    13: "Этника",
    14: "Акустика и вокал",
    15: "Регги",
    16: "Классика",
    17: "Инди-поп",
    18: "Другое",
    19: "Речь",
    21: "Альтернатива",
    22: "Электропоп и диско",
    1001: "Джаз и блюз",
}

# Штраф показа в рекомендациях по жанру: без id, «Другое» (18) и неизвестные VK id — только по исполнителю.
_REC_GENRE_WEAK_FOR_SHOW_PENALTY: AbstractSet[int] = frozenset({18})


def _rec_genre_id_strong_for_show_penalty(gi: Optional[int]) -> bool:
    if gi is None:
        return False
    if gi in _REC_GENRE_WEAK_FOR_SHOW_PENALTY:
        return False
    return gi in _VK_AUDIO_GENRE_LABELS


def _rec_show_penalty_hides_track(rng: random.Random, penalty: int) -> bool:
    if penalty <= 0:
        return False
    if penalty >= 100:
        return True
    return rng.random() * 100.0 < float(penalty)


async def _vk_audio_search_by_genre_raw(genre_id: int, count: int, offset: int = 0) -> List[Dict]:
    """
    audio.search с фильтром genre_id (метаданные VK). Поле q — нейтральный токен,
    не подписи жанров из _VK_AUDIO_GENRE_LABELS (иначе поиск матчится по словам в названиях треков).
    """
    base = {
        "q": _REC_GENRE_SEARCH_NEUTRAL_Q,
        "count": min(100, max(1, count)),
        "offset": max(0, offset),
        "sort": 2,
        "auto_complete": 0,
        "search_own": 0,
        "genre_id": genre_id,
    }
    data = await _vk_api_call("audio.search", base)
    if "error" in data:
        return []
    return data.get("response", {}).get("items", []) or []


_REC_JUNK_SUBSTRINGS = (
    "nightcore", "slowed", "reverb", "8d audio", "bass boost", "bass boosted",
    "tiktok", "tik tok", "phonk edit", "sped up", "speed up",
    "аудиокниг", "подкаст", "podcast", "karaoke", "караоке",
    "soundcloud", "dj remix", "bootleg", "cover by", " кавер", "кавер ",
    "минусовк", "типограф", "read by", "full album", "voice message",
    "звуковое сообщ", "_asmr", " asmr",
    "mashup", "megamix", "type beat", "prod.", "prod by", "видео",
    "перезалив", "reupload", "re-upload", "музыка для", "для сна",
    "challenge", "челлендж", "freestyle", "radio edit", "radio-edit",
)
# Рекомендации: отсечь бесконечные миксы
_REC_MAX_TRACK_DURATION_SEC = max(300, min(3600, int(os.getenv("REC_MAX_TRACK_DURATION_SEC", "900"))))


def _rec_genre_label_from_item(item: Dict) -> Tuple[Optional[int], str]:
    gid_raw = item.get("genre_id")
    gid: Optional[int] = None
    if isinstance(gid_raw, int):
        gid = gid_raw
    elif isinstance(gid_raw, str) and gid_raw.strip().isdigit():
        gid = int(gid_raw.strip())
    if gid is not None and gid in _VK_AUDIO_GENRE_LABELS:
        return gid, _VK_AUDIO_GENRE_LABELS[gid]
    return gid, "Другое"


def _rec_ensure_track_genre(t: Dict) -> None:
    if t.get("genre_label"):
        return
    t["genre_label"] = "Другое"
    t.setdefault("genre_id", None)


def _rec_track_quality_ok(t: Dict) -> bool:
    dur = int(t.get("duration") or 0)
    if dur > 0 and dur < 40:
        return False
    if dur > _REC_MAX_TRACK_DURATION_SEC:
        return False
    title = _normalize_for_match(str(t.get("title") or ""))
    artist = _normalize_for_match(str(t.get("artist") or ""))
    if len(artist) < 2 or len(title) < 2:
        return False
    blob = f"{title} {artist}"
    for junk in _REC_JUNK_SUBSTRINGS:
        if junk in blob:
            return False
    return True


def _rec_favorite_artist_profile(favorites: List[Dict]) -> Tuple[set, set]:
    """Нормализованные строки артистов + множество значимых слов из избранного (для фильтра рекомендаций)."""
    strings: set = set()
    tokens: set = set()
    for t in favorites:
        a = str(t.get("artist") or "").strip()
        if len(a) < 2:
            continue
        ns = _normalize_for_match(a)
        if len(ns) >= 3:
            strings.add(ns)
        for w in _meaningful_words(a):
            if len(w) >= 3:
                tokens.add(w)
    return strings, tokens


def _rec_track_matches_favorite_artists(t: Dict, artist_strings: set, artist_tokens: set) -> bool:
    """Трек «в зоне» артистов из избранного (подстроки / общие слова в имени исполнителя)."""
    if not artist_strings and not artist_tokens:
        return True
    raw_a = str(t.get("artist") or "").strip()
    ta = _normalize_for_match(raw_a)
    if len(ta) < 2:
        return False
    for s in artist_strings:
        if len(s) >= 4 and (s in ta or ta in s):
            return True
        if len(s) >= 6 and s[:6] in ta:
            return True
    tw = set(_meaningful_words(raw_a))
    if tw & artist_tokens:
        return True
    for w in _meaningful_words(ta):
        if w in artist_tokens:
            return True
    return False


def _rec_favorite_artist_sets(favorites: List[Dict]) -> Tuple[set, set]:
    """Нормализованные артисты из избранного (строки + токены)."""
    return _rec_favorite_artist_profile(favorites)


def _rec_artist_exact_key(s: str) -> str:
    return _normalize_for_match(str(s or ""))


def _rec_artist_in_favorites_penalty(track: Dict, fav_artist_strings: set) -> int:
    """Штраф если артист трека совпадает с артистом из избранного (чтобы не крутить одни и те же группы)."""
    if not fav_artist_strings:
        return 0
    ta = _rec_artist_exact_key(track.get("artist") or "")
    if not ta or len(ta) < 3:
        return 0
    # точное совпадение или сильное включение
    if ta in fav_artist_strings:
        return 1
    for s in fav_artist_strings:
        if len(s) >= 6 and (s in ta or ta in s):
            return 1
    return 0


# Разбор строки artist для поиска: основной исполнитель / фиты (лимит 2 или 3 трека на ключ).
_SEARCH_ARTIST_SPLIT_RE = re.compile(
    r"\s*(?:,|\bfeat\.?\b|\bft\.?\b|\bfeaturing\b|\bx\b|&|vs\.?|/|\+)\s*",
    re.I,
)


def _search_primary_artist_key(artist: str) -> str:
    s = str(artist or "").strip()
    if not s:
        return ""
    first = _SEARCH_ARTIST_SPLIT_RE.split(s)[0].strip()
    return _rec_artist_exact_key(first) if first else ""


def _search_secondary_artist_keys(artist: str, primary_key: str) -> List[str]:
    s = str(artist or "").strip()
    if not s:
        return []
    parts = [p.strip() for p in _SEARCH_ARTIST_SPLIT_RE.split(s) if p.strip()]
    out: List[str] = []
    for p in parts[1:]:
        k = _rec_artist_exact_key(p)
        if k and k != primary_key and len(k) >= 2:
            out.append(k)
    return out


def _rec_apply_artist_feature_caps(items: List[Dict], *, max_total: Optional[int] = None) -> List[Dict]:
    """
    Только для персональных рекомендаций: не больше 2 треков с одним основным исполнителем;
    до 3 — если этот исполнитель где-то вторым (фит и т.п.) в том же списке.
    """
    if not items:
        return []
    feat_keys: Set[str] = set()
    for t in items:
        if not isinstance(t, dict):
            continue
        pk = _search_primary_artist_key(str(t.get("artist") or ""))
        for sk in _search_secondary_artist_keys(str(t.get("artist") or ""), pk):
            feat_keys.add(sk)
    counts: Dict[str, int] = defaultdict(int)
    out: List[Dict] = []
    for t in items:
        if not isinstance(t, dict):
            continue
        pk = _search_primary_artist_key(str(t.get("artist") or ""))
        if not pk:
            out.append(t)
            if max_total is not None and len(out) >= max_total:
                break
            continue
        cap = 3 if pk in feat_keys else 2
        if counts[pk] >= cap:
            continue
        counts[pk] += 1
        out.append(t)
        if max_total is not None and len(out) >= max_total:
            break
    return out


def _rec_last_unique_primary_artists_from_main(main: List[Dict], max_unique: int = 10) -> List[Tuple[str, Dict]]:
    """С конца избранного: до max_unique треков с разными основными исполнителями (сырое имя + dict)."""
    out: List[Tuple[str, Dict]] = []
    seen: set = set()
    for tr in reversed(main or []):
        if not isinstance(tr, dict):
            continue
        art = str(tr.get("artist") or "").strip()
        if len(art) < 2:
            continue
        parts = [p.strip() for p in _SEARCH_ARTIST_SPLIT_RE.split(art) if p.strip()]
        first = parts[0] if parts else ""
        if len(first) < 2:
            continue
        pk = _rec_artist_exact_key(first)
        if not pk or len(pk) < 2:
            continue
        if pk in seen:
            continue
        seen.add(pk)
        out.append((first, tr))
        if len(out) >= max_unique:
            break
    return out


def _rec_interleave_favorites_into_wave(
    favorites: List[Dict],
    recommendations: List[Dict],
    limit: int,
    salt: int,
    *,
    rng: Optional[random.Random] = None,
) -> List[Dict]:
    """Волна: вкрапливает треки из избранного (~1 к 3 с рекомендациями); порядок вкраплений случайный (rng)."""
    r = rng or random.Random((int(salt) ^ 0xC001D00D) & 0xFFFFFFFF)
    recent_n = min(_REC_PERSONAL_SEED_RECENT_WINDOW_WAVE, len(favorites))
    slice_f = favorites[-recent_n:] if favorites else []
    pool = [t for t in slice_f if _valid_playlist_library_track_id(str(t.get("id") or "").strip())]
    if not pool and favorites:
        pool = [t for t in favorites if _valid_playlist_library_track_id(str(t.get("id") or "").strip())]
    if not pool or limit <= 0:
        return recommendations[:limit]
    k = min(max(5, limit // 5), len(pool), 36)
    picked: List[Dict] = pool[-k:] if len(pool) > k else list(pool)
    fav_entries: List[Dict] = []
    for t in picked:
        t2 = dict(t)
        _rec_ensure_track_genre(t2)
        fav_entries.append(t2)
    r.shuffle(fav_entries)

    seen: set = set()
    out: List[Dict] = []
    fi = ri = 0
    while len(out) < limit:
        progressed = False
        if fi < len(fav_entries):
            t = fav_entries[fi]
            fi += 1
            tid = t.get("id")
            if tid and tid not in seen:
                seen.add(tid)
                out.append(t)
                progressed = True
        if len(out) >= limit:
            break
        for _ in range(3):
            if len(out) >= limit:
                break
            while ri < len(recommendations):
                t = recommendations[ri]
                ri += 1
                tid = t.get("id")
                if tid and tid not in seen:
                    seen.add(tid)
                    out.append(t)
                    progressed = True
                    break
        if not progressed:
            break
    while len(out) < limit and ri < len(recommendations):
        t = recommendations[ri]
        ri += 1
        tid = t.get("id")
        if tid and tid not in seen:
            seen.add(tid)
            out.append(t)
    while len(out) < limit and fi < len(fav_entries):
        t = fav_entries[fi]
        fi += 1
        tid = t.get("id")
        if tid and tid not in seen:
            seen.add(tid)
            out.append(t)
    return out[:limit]


def _rec_extract_release_year(item: Dict) -> Optional[int]:
    for key in ("release_year", "year"):
        v = item.get(key)
        if isinstance(v, int) and 1950 <= v <= 2035:
            return v
    album = item.get("album") or {}
    if isinstance(album, dict):
        for key in ("year", "release_year"):
            v = album.get(key)
            if isinstance(v, int) and 1950 <= v <= 2035:
                return v
    return None


def _vk_http_image_url(v: Any) -> Optional[str]:
    if isinstance(v, str):
        s = v.strip()
        if s.startswith("https://") or s.startswith("http://"):
            return s
    return None


def _vk_covers_from_album_dict(album: Any) -> Optional[str]:
    """Обложка альбома: thumb dict/list, иногда прямой URL в полях album."""
    if not isinstance(album, dict):
        return None
    for k in ("cover_url", "thumb_url", "photo_url", "url"):
        u = _vk_http_image_url(album.get(k))
        if u:
            return u
    thumb = album.get("thumb")
    if isinstance(thumb, list):
        for el in thumb:
            if isinstance(el, dict):
                u = _vk_pick_from_photo_dict(el)
            else:
                u = _vk_http_image_url(el)
            if u:
                return u
    elif isinstance(thumb, dict):
        u = _vk_pick_from_photo_dict(thumb)
        if u:
            return u
    for k in ("cover", "photo", "album_thumb"):
        u = _vk_pick_from_photo_dict(album.get(k))
        if u:
            return u
    return None


def _vk_pick_from_photo_dict(photo: Any) -> Optional[str]:
    """URL картинки из объекта фото ВК (album.thumb, sizes/images)."""
    if not isinstance(photo, dict):
        return None
    for key in (
        "photo_2560",
        "photo_1280",
        "photo_807",
        "photo_604",
        "photo_600",
        "photo_397",
        "photo_360",
        "photo_300",
        "photo_270",
        "photo_256",
        "photo_200",
        "photo_150",
        "photo_135",
        "photo_130",
        "photo_75",
        "photo_68",
        "photo_34",
    ):
        u = _vk_http_image_url(photo.get(key))
        if u:
            return u
    sizes = photo.get("sizes") or photo.get("images")
    if isinstance(sizes, list):
        best_u: Optional[str] = None
        best_w = -1
        for s in sizes:
            if not isinstance(s, dict):
                continue
            u = _vk_http_image_url(s.get("url") or s.get("src"))
            if not u:
                continue
            try:
                wi = int(s.get("width") or 0)
            except (TypeError, ValueError):
                wi = 0
            if wi >= best_w:
                best_w = wi
                best_u = u
        if best_u:
            return best_u
    return _vk_http_image_url(photo.get("url"))


def _vk_audio_cover_url_from_item(item: Dict[str, Any]) -> Optional[str]:
    """Обложка из сырого audio (search/getById): у ВК разные наборы photo_* в thumb."""
    album = item.get("album")
    u = _vk_covers_from_album_dict(album)
    if u:
        return u
    covers = item.get("covers")
    if isinstance(covers, list):
        for el in covers:
            if isinstance(el, dict):
                u = _vk_pick_from_photo_dict(el) or _vk_http_image_url(el.get("url"))
            else:
                u = _vk_http_image_url(el)
            if u:
                return u
    for k in ("thumb", "photo", "photo_thumb", "track_cover"):
        u = _vk_pick_from_photo_dict(item.get(k))
        if u:
            return u
    arts = item.get("main_artists")
    if isinstance(arts, list):
        for a in arts:
            if not isinstance(a, dict):
                continue
            for k in ("photo", "cover", "cover_photo"):
                u = _vk_pick_from_photo_dict(a.get(k))
                if u:
                    return u
    return None


def _parse_tracks(items: List[Dict]) -> List[Dict]:
    tracks = []
    seen = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        oid = item.get("owner_id")
        aid = item.get("id")
        if oid is None or aid is None:
            continue
        track_id = f"{oid}_{aid}"
        if track_id in seen:
            continue
        seen.add(track_id)
        cover_url = _vk_audio_cover_url_from_item(item)
        gid, genre_label = _rec_genre_label_from_item(item)
        ry = _rec_extract_release_year(item)
        row: Dict[str, Any] = {
            "id": track_id,
            "title": item.get("title", ""),
            "artist": item.get("artist", ""),
            "duration": item.get("duration", 0),
            "cover_url": cover_url,
            "genre_id": gid,
            "genre_label": genre_label,
        }
        ak_raw = item.get("access_key")
        if ak_raw is not None and str(ak_raw).strip():
            row["access_key"] = str(ak_raw).strip()
        if ry is not None:
            row["release_year"] = ry
        tracks.append(row)
    return tracks


def _rec_lang_bucket_from_text(title: str, artist: str) -> str:
    s = f"{title} {artist}"
    cyr = sum(1 for c in s if "\u0400" <= c <= "\u04ff")
    lat = sum(1 for c in s if c.isalpha() and "a" <= c.lower() <= "z")
    if cyr >= 3 and cyr >= lat:
        return "ru"
    if lat >= 3 and lat > cyr:
        return "en"
    return "other"


def _rec_lang_bucket_track(t: Dict) -> str:
    return _rec_lang_bucket_from_text(str(t.get("title") or ""), str(t.get("artist") or ""))


def _rec_merge_track_meta_from_redis(t: Dict, meta: Optional[Dict]) -> bool:
    """Подмешать метаданные из Redis; True если ключ был в кэше."""
    if not meta:
        return False
    gid = meta.get("genre_id")
    if gid is not None and t.get("genre_id") is None:
        if isinstance(gid, int):
            t["genre_id"] = gid
        elif isinstance(gid, str) and gid.strip().isdigit():
            t["genre_id"] = int(gid.strip())
        gl = meta.get("genre_label")
        if gl:
            t["genre_label"] = gl
        else:
            _rec_ensure_track_genre(t)
    ry = meta.get("release_year")
    if isinstance(ry, int) and 1950 <= ry <= 2035 and t.get("release_year") is None:
        t["release_year"] = ry
    return True


def _rec_build_taste_profile(
    favorites: List[Dict],
    play_weights: List[Tuple[str, float]],
    meta_map: Dict[str, Dict],
) -> Dict[str, Any]:
    """Веса жанров / языков / центр года по избранному и недавним play (мета из Redis)."""
    genre_w: Dict[int, float] = defaultdict(float)
    lang_w: Dict[str, float] = defaultdict(float)
    years_acc: List[Tuple[int, float]] = []

    n = len(favorites)
    for i, fav in enumerate(favorites):
        tid = str(fav.get("id") or "").strip()
        if not tid:
            continue
        w = 0.55 + 0.45 * (i / max(1, n - 1)) if n > 1 else 1.0
        t = dict(fav)
        if tid in meta_map:
            _rec_merge_track_meta_from_redis(t, meta_map[tid])
        _rec_ensure_track_genre(t)
        gid = t.get("genre_id")
        if isinstance(gid, int):
            genre_w[gid] += w
        lang_w[_rec_lang_bucket_track(t)] += w
        ry = t.get("release_year")
        if isinstance(ry, int) and 1950 <= ry <= 2035:
            years_acc.append((ry, w))

    for tid, cnt in play_weights:
        w = min(4.0, 0.35 + float(cnt) * 0.12)
        m = meta_map.get(tid)
        if not m:
            continue
        t = dict(m)
        _rec_merge_track_meta_from_redis(t, m)
        _rec_ensure_track_genre(t)
        gid = t.get("genre_id")
        if isinstance(gid, int):
            genre_w[gid] += w
        lang_w[_rec_lang_bucket_track(t)] += w
        ry = t.get("release_year")
        if isinstance(ry, int) and 1950 <= ry <= 2035:
            years_acc.append((ry, w))

    gs = sum(genre_w.values()) or 1.0
    genre_w_norm = {k: v / gs for k, v in genre_w.items()}
    ls = sum(lang_w.values()) or 1.0
    lang_w_norm = {k: v / ls for k, v in lang_w.items()}
    year_center: Optional[int] = None
    if years_acc:
        tw = sum(w for _, w in years_acc)
        if tw > 0:
            year_center = int(round(sum(y * w for y, w in years_acc) / tw))

    return {
        "genre_weights": genre_w_norm,
        "lang_weights": lang_w_norm,
        "year_center": year_center,
    }


def _rec_taste_match_score(track: Dict, profile: Dict[str, Any]) -> int:
    score = 0
    gw = profile.get("genre_weights") or {}
    gid = track.get("genre_id")
    if isinstance(gid, int) and gid in gw:
        score += int(10000 * float(gw[gid]))
    yc = profile.get("year_center")
    ry = track.get("release_year")
    if isinstance(yc, int) and isinstance(ry, int):
        score += max(0, 800 - min(abs(ry - yc), 800))
    lw = profile.get("lang_weights") or {}
    lb = _rec_lang_bucket_track(track)
    if lb in lw:
        score += int(5000 * float(lw[lb]))
    return score


def _rec_trace_enabled_for_user(user_id: int) -> bool:
    if not REC_TRACE:
        return False
    if not REC_TRACE_USER_IDS:
        return True
    return int(user_id) in REC_TRACE_USER_IDS


def _rec_trace_log_candidates(
    *,
    user_id: int,
    request_id: str,
    mode: str,
    seed_ids: List[str],
    taste_profile: Dict[str, Any],
    candidates_by_source: Dict[str, List[Dict]],
) -> None:
    if not _rec_trace_enabled_for_user(user_id):
        return
    try:
        seeds_short = ",".join(seed_ids[:8])
        for src, items in candidates_by_source.items():
            for t in items[:REC_TRACE_TOP_K]:
                tid = str(t.get("id") or "")
                if not tid:
                    continue
                gid = t.get("genre_id")
                ry = t.get("release_year")
                lb = _rec_lang_bucket_track(t)
                score = _rec_taste_match_score(t, taste_profile)
                cached = 1 if t.get("_rec_meta_cached") else 0
                logger.info(
                    "rec_trace user_id=%s req=%s mode=%s src=%s seed=%s track=%s cached=%s genre=%s year=%s lang=%s taste_score=%s",
                    int(user_id),
                    request_id,
                    mode,
                    src,
                    seeds_short,
                    tid,
                    cached,
                    gid,
                    ry,
                    lb,
                    score,
                )
    except Exception:
        return


async def _rec_batch_redis_meta_map(track_ids: List[str]) -> Dict[str, Dict]:
    if not track_ids:
        return {}
    uniq: List[str] = []
    seen: set = set()
    for x in track_ids:
        tid = str(x or "").strip()
        if not tid or tid in seen or not _valid_track_id(tid):
            continue
        seen.add(tid)
        uniq.append(tid)
        if len(uniq) >= 320:
            break
    if not uniq:
        return {}
    out: Dict[str, Dict] = {}
    chunk = 48
    for i in range(0, len(uniq), chunk):
        part = uniq[i : i + chunk]
        rows = await asyncio.gather(*[_redis_get_track_meta(tid) for tid in part])
        for tid, m in zip(part, rows):
            if m:
                out[tid] = m
    return out


def _rec_enrich_tracks_meta_inplace(tracks: List[Dict], meta_map: Dict[str, Dict]) -> None:
    for t in tracks:
        tid = str(t.get("id") or "").strip()
        if not tid:
            continue
        m = meta_map.get(tid)
        if m:
            t["_rec_meta_cached"] = _rec_merge_track_meta_from_redis(t, m)
        else:
            t["_rec_meta_cached"] = False
    return None


def _rec_strip_internal_track_fields(items: List[Dict]) -> List[Dict]:
    """Убирает служебные ключи _rec_* из ответа API."""
    out: List[Dict] = []
    for t in items:
        if not isinstance(t, dict):
            continue
        row = {k: v for k, v in t.items() if not str(k).startswith("_rec_")}
        row["vk_legacy"] = False
        out.append(row)
    return out


def _api_tracks_mark_modern_resolve(items: Optional[List[Dict]]) -> None:
    """Поиск / гостевые рекомендации: не трактовать как старое избранное VK (YouTube-fallback в resolve при необходимости)."""
    if not items:
        return
    for t in items:
        if isinstance(t, dict):
            t["vk_legacy"] = False


def _rec_recent_served_key(user_id: int) -> str:
    return _cache_ns("rec", "user", str(int(user_id)), "recent_served_ids")


def _rec_taste_profile_key(user_id: int) -> str:
    return _cache_ns("rec", "user", str(int(user_id)), "taste_profile")


def _rec_taste_decay_factor(dt_sec: float) -> float:
    # exp(-ln2 * dt / half_life)
    hl = float(_REC_TASTE_HALFLIFE_DAYS) * 86400.0
    if hl <= 1:
        return 0.0
    if dt_sec <= 0:
        return 1.0
    return float(pow(2.0, -dt_sec / hl))


def _rec_taste_apply_decay(profile: Dict, now_ts: int) -> Dict:
    last = int(profile.get("last_ts") or 0)
    if last <= 0:
        profile["last_ts"] = int(now_ts)
        return profile
    f = _rec_taste_decay_factor(max(0.0, float(now_ts - last)))
    if f >= 0.999:
        profile["last_ts"] = int(now_ts)
        return profile
    for k in ("genre", "lang", "year"):
        d = profile.get(k)
        if not isinstance(d, dict):
            profile[k] = {}
            continue
        nd = {}
        for kk, vv in d.items():
            try:
                x = float(vv) * f
            except Exception:
                continue
            if x >= 0.02:
                nd[str(kk)] = x
        profile[k] = nd
    profile["last_ts"] = int(now_ts)
    return profile


async def _rec_get_taste_profile(user_id: int) -> Optional[Dict]:
    p = await _redis_get_json(_rec_taste_profile_key(user_id))
    if not isinstance(p, dict):
        return None
    now_ts = int(time.time())
    return _rec_taste_apply_decay(p, now_ts)


async def _rec_set_taste_profile(user_id: int, profile: Dict) -> None:
    await _redis_set_json(_rec_taste_profile_key(user_id), profile, ex=_REC_TASTE_PROFILE_TTL_SEC)


def _rec_profile_year_center(profile: Dict) -> Optional[int]:
    yd = profile.get("year")
    if not isinstance(yd, dict) or not yd:
        return None
    acc = 0.0
    wsum = 0.0
    for k, v in yd.items():
        try:
            y = int(str(k))
            w = float(v)
        except Exception:
            continue
        if 1950 <= y <= 2035 and w > 0:
            acc += y * w
            wsum += w
    if wsum <= 0:
        return None
    return int(round(acc / wsum))


def _rec_taste_profile_to_scoring_model(profile: Dict) -> Dict[str, Any]:
    # Convert to the shape expected by _rec_taste_match_score (genre_weights/lang_weights/year_center)
    g = profile.get("genre") if isinstance(profile.get("genre"), dict) else {}
    l = profile.get("lang") if isinstance(profile.get("lang"), dict) else {}
    gs = sum(float(v) for v in g.values()) or 1.0
    ls = sum(float(v) for v in l.values()) or 1.0
    genre_weights = {}
    for k, v in g.items():
        try:
            genre_weights[int(str(k))] = float(v) / gs
        except Exception:
            pass
    lang_weights = {str(k): float(v) / ls for k, v in l.items() if isinstance(k, str) or k is not None}
    return {"genre_weights": genre_weights, "lang_weights": lang_weights, "year_center": _rec_profile_year_center(profile)}


async def _rec_rebuild_taste_profile_from_sqlite(
    user_id: int,
    *,
    only_track_ids: Optional[Set[str]] = None,
) -> Optional[Dict]:
    try:
        import analytics_db

        analytics_db.init_db()
        agg = analytics_db.get_user_taste_aggregates(
            user_id,
            days=_REC_TASTE_REBUILD_DAYS,
            only_track_ids=only_track_ids,
        )
        if not isinstance(agg, dict):
            return None
        now_ts = int(time.time())
        p = {
            "genre": agg.get("genre") or {},
            "lang": agg.get("lang") or {},
            "year": agg.get("year") or {},
            "last_ts": now_ts,
            "rebuilt_from_sqlite": now_ts,
        }
        return p
    except Exception:
        return None


async def _rec_update_taste_profile(
    user_id: int,
    *,
    genre_id: Optional[int],
    release_year: Optional[int],
    lang_bucket: Optional[str],
    weight: float,
) -> None:
    if not user_id or weight <= 0:
        return
    now_ts = int(time.time())
    p = await _rec_get_taste_profile(user_id)
    if p is None:
        p = await _rec_rebuild_taste_profile_from_sqlite(user_id) or {"genre": {}, "lang": {}, "year": {}, "last_ts": now_ts}
    p = _rec_taste_apply_decay(p, now_ts)
    if genre_id is not None:
        g = p.setdefault("genre", {})
        g[str(int(genre_id))] = float(g.get(str(int(genre_id)), 0.0)) + float(weight)
    if release_year is not None and 1950 <= int(release_year) <= 2035:
        y = p.setdefault("year", {})
        y[str(int(release_year))] = float(y.get(str(int(release_year)), 0.0)) + float(weight)
    if lang_bucket:
        lb = str(lang_bucket)[:16]
        l = p.setdefault("lang", {})
        l[lb] = float(l.get(lb, 0.0)) + float(weight)
    # Keep profile bounded (top-N for genre/lang, top years)
    try:
        for key, cap in (("genre", 40), ("lang", 8), ("year", 40)):
            d = p.get(key)
            if isinstance(d, dict) and len(d) > cap:
                items = sorted(((k, float(v)) for k, v in d.items()), key=lambda kv: kv[1], reverse=True)[:cap]
                p[key] = {k: v for k, v in items}
    except Exception:
        pass
    p["last_ts"] = now_ts
    await _rec_set_taste_profile(user_id, p)


async def _rec_get_recent_served_ids(user_id: int) -> List[str]:
    """Последние выданные в персональных рекомендациях track_id (новые раньше), до CAP."""
    data = await _redis_get_json(_rec_recent_served_key(user_id))
    if not isinstance(data, list):
        return []
    out: List[str] = []
    for x in data:
        tid = str(x or "").strip()
        if tid and _valid_track_id(tid):
            out.append(tid)
    return out[: _REC_PERSONAL_EXCLUDE_RECENT_CAP + 10]


async def _rec_merge_recent_served_ids(user_id: int, new_ids: List[str]) -> None:
    """Дописать выдачу в историю (новая порция вперёди), обрезать до CAP."""
    cap = _REC_PERSONAL_EXCLUDE_RECENT_CAP
    prev = await _rec_get_recent_served_ids(user_id)
    merged: List[str] = []
    seen: set = set()
    for tid in new_ids + prev:
        t = str(tid or "").strip()
        if not t or not _valid_playlist_library_track_id(t) or t in seen:
            continue
        seen.add(t)
        merged.append(t)
        if len(merged) >= cap:
            break
    await _redis_set_json(_rec_recent_served_key(user_id), merged, ex=_REC_PERSONAL_RECENT_IDS_TTL_SEC)


def _rec_youtube_id_boost(t: Dict[str, Any]) -> int:
    """Выше в сортировке: радио YTM, затем любой YouTube-id (без VK-меты треки иначе уходят в хвост)."""
    if t.get("_rec_ytm_radio"):
        return 2
    tid = str(t.get("id") or "").lower()
    if "youtube.com" in tid or "youtu.be" in tid:
        return 1
    return 0


async def _rec_personal_finalize_output(
    user_id: int,
    candidates: List[Dict],
    out_limit: int,
    rng: random.Random,
) -> List[Dict]:
    """
    Перемешивает кандидатов перед отбором (без группировки по скрипту/порядку пулов);
    отсекает последние выданные id (Redis) и дизлайки.
    """
    prev = await _rec_get_recent_served_ids(user_id)
    exclude = set(prev)
    try:
        import analytics_db

        analytics_db.init_db()
        for _tid in analytics_db.get_disliked_track_ids(user_id):
            if _tid:
                exclude.add(_tid)
    except Exception:
        pass
    pool = [t for t in candidates if isinstance(t, dict) and t.get("id")]
    rng.shuffle(pool)
    out: List[Dict] = []
    used: set = set()
    for t in pool:
        tid = str(t.get("id"))
        if tid in used or tid in exclude:
            continue
        used.add(tid)
        out.append(t)
        if len(out) >= out_limit:
            break
    final = out[:out_limit]
    served = [str(t.get("id")) for t in final if t.get("id")]
    if served:
        await _rec_merge_recent_served_ids(user_id, served)
    return final


def _rec_personal_wave_flat(
    vk_items: List[Dict],
    collab: List[Dict],
    search: List[Dict],
    fav_set: set,
    limit: int,
    rng: random.Random,
    *,
    wave_mode: bool = False,
    favorites: Optional[List[Dict]] = None,
    fav_ids_list: Optional[List[str]] = None,
    taste_profile: Optional[Dict[str, Any]] = None,
    genre_ranking_only: bool = False,
    affinity_min_favorites: int = 3,
    strict_affinity_min_favorites: int = 5,
    search_first: bool = False,
    anchor_query_tokens: Optional[AbstractSet[str]] = None,
    disliked_track_ids: Optional[AbstractSet[str]] = None,
    artist_show_penalties: Optional[Dict[str, int]] = None,
    genre_show_penalties: Optional[Dict[int, int]] = None,
) -> List[Dict]:
    """Сортировка пулов: по умолчанию основной → поиск → коллаб; при search_first — поиск первым (YTM без VK)."""
    favorites = favorites or []
    fav_ids_list = fav_ids_list or []
    profile = taste_profile or {"genre_weights": {}, "lang_weights": {}, "year_center": None}
    af_n = min(_REC_ARTIST_PROFILE_RECENT, len(favorites))
    affinity_favs = favorites[-af_n:] if favorites else []
    strs, toks = _rec_favorite_artist_profile(affinity_favs)
    fav_artist_strings, _fav_artist_tokens = _rec_favorite_artist_sets(favorites)
    use_affinity = (
        False
        if genre_ranking_only
        else (len(fav_ids_list) >= affinity_min_favorites and (strs or toks))
    )
    vk_strict_affinity = False if genre_ranking_only else (use_affinity and len(fav_ids_list) >= strict_affinity_min_favorites)

    d_tid = disliked_track_ids or set()
    ap = artist_show_penalties or {}
    gp = genre_show_penalties or {}
    fav_norms = {_rec_norm_library_track_id(str(x)) for x in fav_set if str(x).strip()}
    aq = anchor_query_tokens or set()

    def _overlap_score(t: Dict) -> int:
        if not aq:
            return 0
        blob = _normalize_for_match(str(t.get("artist", "") or ""))
        n = 0
        for w in aq:
            if len(w) >= 3 and w in blob:
                n += 1
        return min(n, 8)

    def pool_good(raw: List[Dict], *, affinity: bool) -> List[Dict]:
        out: List[Dict] = []
        loc: set = set()
        for t in raw:
            tid = t.get("id")
            if not tid or tid in loc:
                continue
            nid = _rec_norm_library_track_id(str(tid))
            if nid and nid in fav_norms:
                continue
            if str(tid).strip() in fav_set:
                continue
            stid = str(tid).strip()
            # Дизлайкнутый трек по id никогда снова не попадает в персональную подборку.
            if stid and stid in d_tid:
                continue
            _rec_ensure_track_genre(t)
            t_ak = _rec_artist_exact_key(str(t.get("artist") or ""))
            pa = int(ap.get(t_ak, 0) or 0) if t_ak else 0
            if pa > 0 and _rec_show_penalty_hides_track(rng, pa):
                continue
            g_raw = t.get("genre_id")
            try:
                gi = int(g_raw) if g_raw is not None and str(g_raw).strip() != "" else None
            except (TypeError, ValueError):
                gi = None
            if _rec_genre_id_strong_for_show_penalty(gi):
                pg = int(gp.get(int(gi), 0) or 0) if gi is not None else 0
                if _rec_show_penalty_hides_track(rng, pg):
                    continue
            if not _rec_track_quality_ok(t):
                continue
            # Раньше это был жёсткий фильтр, из-за чего «похожие по жанру» артисты отрезались.
            # Теперь оставляем как soft-сигнал для сортировки.
            if affinity and use_affinity:
                t["_rec_artist_affinity"] = 1 if _rec_track_matches_favorite_artists(t, strs, toks) else 0
            else:
                t["_rec_artist_affinity"] = 0
            t["_rec_artist_in_favs"] = _rec_artist_in_favorites_penalty(t, fav_artist_strings)
            t["_rec_query_overlap"] = _overlap_score(t)
            loc.add(tid)
            out.append(t)
        return out

    pvk = pool_good(vk_items, affinity=vk_strict_affinity)
    if not pvk and vk_strict_affinity:
        pvk = pool_good(vk_items, affinity=False)
    pc = pool_good(collab, affinity=use_affinity)
    ps = pool_good(search, affinity=use_affinity)

    def sort_pool(lst: List[Dict]) -> None:
        lst.sort(
            key=lambda t: (
                -_rec_youtube_id_boost(t),
                -(1 if t.get("_rec_meta_cached") else 0),
                -int(t.get("_rec_query_overlap") or 0),
                -_rec_taste_match_score(t, profile),
                # bonus: пересечение по артистам из недавних лайков (soft)
                -(1 if t.get("_rec_artist_affinity") else 0),
                # penalty: тот же артист, что уже в избранном
                (1 if t.get("_rec_artist_in_favs") else 0),
                str(t.get("id") or ""),
            )
        )

    sort_pool(pvk)
    sort_pool(ps)
    sort_pool(pc)

    merged: List[Dict] = []
    seen: set = set()
    pools_rr = [ps, pvk, pc] if search_first else [pvk, ps, pc]
    max_len = max((len(p) for p in pools_rr), default=0)
    for i in range(max_len):
        for pool in pools_rr:
            if len(merged) >= limit:
                return merged[:limit]
            if i >= len(pool):
                continue
            t = pool[i]
            tid = t.get("id")
            if not tid or tid in seen:
                continue
            seen.add(tid)
            merged.append(t)
    return merged[:limit]


# ─── Рекомендации VK: общий кэш по seed, лимит живых вызовов/мин, fallback popular ─

async def _rec_get_seed_async_lock(seed: str) -> asyncio.Lock:
    async with _rec_seed_async_locks_guard:
        lk = _rec_seed_async_locks.get(seed)
        if lk is None:
            if len(_rec_seed_async_locks) >= 4000:
                drop = next(iter(_rec_seed_async_locks))
                del _rec_seed_async_locks[drop]
            lk = asyncio.Lock()
            _rec_seed_async_locks[seed] = lk
        return lk


async def _rec_budget_try_consume() -> bool:
    if _RECOMMENDATIONS_VK_MAX_PER_MINUTE <= 0:
        return True
    r = await get_redis()
    if not r:
        return True
    minute = int(time.time() // 60)
    k = _cache_ns("rec", "vk_budget", str(minute))
    try:
        v = await r.incr(k)
        if v == 1:
            await r.expire(k, 120)
        if v > _RECOMMENDATIONS_VK_MAX_PER_MINUTE:
            await r.decr(k)
            return False
        return True
    except Exception as e:
        print(f"⚠️ recommendations budget redis error: {e}")
        return True


def _rec_response_items(full: List[Dict], seed: str, limit: int) -> List[Dict]:
    return [t for t in full if t.get("id") != seed][:limit]


def _rec_memory_set(seed: str, items: List[Dict], source: str) -> None:
    now = time.time()
    _rec_memory_cache[seed] = (items, now, source)
    _rec_memory_cache.move_to_end(seed)
    while len(_rec_memory_cache) > _REC_MEMORY_MAX:
        _rec_memory_cache.popitem(last=False)


def _rec_memory_get(seed: str) -> Optional[Tuple[List[Dict], str]]:
    now = time.time()
    if seed not in _rec_memory_cache:
        return None
    items, ts, src = _rec_memory_cache[seed]
    if now - ts > _RECOMMENDATIONS_CACHE_TTL_SEC:
        del _rec_memory_cache[seed]
        return None
    _rec_memory_cache.move_to_end(seed)
    return items, src


async def _vk_audio_get_recommendations_raw(seed: str, count: int) -> List[Dict]:
    data = await _vk_api_call(
        "audio.getRecommendations",
        {"target_audio": seed, "count": min(100, max(1, count))},
    )
    if "error" in data:
        return []
    resp = data.get("response")
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        return resp.get("items") or []
    return []


async def _vk_audio_get_popular_raw(count: int) -> List[Dict]:
    data = await _vk_api_call(
        "audio.getPopular",
        {"count": min(100, max(1, count)), "offset": 0},
    )
    if "error" in data:
        return []
    resp = data.get("response")
    if isinstance(resp, list):
        return resp
    if isinstance(resp, dict):
        return resp.get("items") or []
    return []


async def _rec_fetch_popular_parsed(count: int) -> List[Dict]:
    async with _rec_vk_sem:
        raw = await _vk_audio_get_popular_raw(count)
    return _parse_tracks(raw)


async def _rec_ensure_popular_parsed(n: int) -> List[Dict]:
    key = _cache_ns("rec", "popular", "global")
    lock_key = _cache_ns("lock", "rec", "popular")
    now = time.time()
    redis = await get_redis()

    if redis:
        try:
            raw = await redis.get(key)
            if raw:
                data = json.loads(raw)
                items = data.get("items")
                ts = float(data.get("ts") or 0)
                if isinstance(items, list) and (now - ts) < _RECOMMENDATIONS_POPULAR_TTL_SEC:
                    return items[:n]
        except Exception as e:
            print(f"⚠️ recommendations popular get redis: {e}")

    pop_mem = _rec_memory_get("__popular__")
    if pop_mem and pop_mem[0]:
        return pop_mem[0][:n]

    if not redis:
        lk = await _rec_get_seed_async_lock("__popular__")
        async with lk:
            pop_mem2 = _rec_memory_get("__popular__")
            if pop_mem2 and pop_mem2[0]:
                return pop_mem2[0][:n]
            items = await _rec_fetch_popular_parsed(max(n, _RECOMMENDATIONS_VK_FETCH_COUNT))
            _rec_memory_set("__popular__", items, "popular")
            return items[:n]

    got = False
    try:
        got = bool(await redis.set(lock_key, "1", nx=True, ex=25))
    except Exception:
        got = False

    if not got:
        for _ in range(40):
            await asyncio.sleep(0.2)
            now = time.time()
            try:
                raw = await redis.get(key)
                if raw:
                    data = json.loads(raw)
                    items = data.get("items")
                    ts = float(data.get("ts") or 0)
                    if isinstance(items, list) and (now - ts) < _RECOMMENDATIONS_POPULAR_TTL_SEC:
                        return items[:n]
            except Exception:
                pass
        pop_tail = _rec_memory_get("__popular__")
        if pop_tail and pop_tail[0]:
            return pop_tail[0][:n]
        return []

    try:
        items = await _rec_fetch_popular_parsed(max(n, _RECOMMENDATIONS_VK_FETCH_COUNT))
        payload = json.dumps({"items": items, "ts": time.time()}, ensure_ascii=False)
        try:
            await redis.set(key, payload, ex=_RECOMMENDATIONS_POPULAR_TTL_SEC)
        except Exception as e:
            print(f"⚠️ recommendations popular set redis: {e}")
        _rec_memory_set("__popular__", items, "popular")
        return items[:n]
    finally:
        try:
            await redis.delete(lock_key)
        except Exception:
            pass


async def _rec_do_vk_or_popular(
    seed: str,
    vk_n: int,
    *,
    redis: Optional[Any],
    cache_key: str,
) -> Tuple[List[Dict], str]:
    items: List[Dict] = []
    source = "vk"
    if _REC_SKIP_VK:
        budget_ok = False
    else:
        budget_ok = await _rec_budget_try_consume()
    if budget_ok:
        async with _rec_vk_sem:
            raw = await _vk_audio_get_recommendations_raw(seed, vk_n + 10)
        items = _parse_tracks(raw)
    if not items:
        source = "popular_fallback"
        if not _REC_SKIP_VK:
            items = await _rec_ensure_popular_parsed(vk_n)
    if not items:
        try:
            meta = await _redis_get_track_meta(_vk_canonical_track_id(seed))
            q = _REC_YT_SEED_FALLBACK_QUERY
            if meta and isinstance(meta, dict):
                qy = _rec_ytm_query_from_track_dict(meta)
                if qy:
                    q = qy[:200]
            items = await asyncio.to_thread(search_youtube_tracks, q, min(30, vk_n + 8))
            if items:
                source = "youtube_seed"
        except Exception as e:
            print(f"⚠️ recommendations seed youtube: {e}")
    ttl = _RECOMMENDATIONS_NEGATIVE_TTL_SEC if not items else _RECOMMENDATIONS_CACHE_TTL_SEC
    payload = json.dumps({"items": items, "ts": time.time(), "source": source}, ensure_ascii=False)
    if redis:
        try:
            await redis.set(cache_key, payload, ex=ttl)
        except Exception as e:
            print(f"⚠️ recommendations seed redis set: {e}")
    _rec_memory_set(seed, items, source)
    return items, source


async def _rec_read_cached_full(seed: str) -> Optional[Tuple[List[Dict], str]]:
    m = _rec_memory_get(seed)
    if m:
        return m
    redis = await get_redis()
    if not redis:
        return None
    try:
        raw = await redis.get(_cache_ns("rec", "seed", seed))
        if not raw:
            return None
        data = json.loads(raw)
        items = data.get("items")
        src = data.get("source") or "cache"
        if not isinstance(items, list):
            return None
        return items, str(src)
    except Exception:
        return None


async def _rec_leader_fill(seed: str) -> Tuple[List[Dict], str]:
    cache_key = _cache_ns("rec", "seed", seed)
    lock_key = _cache_ns("lock", "rec", "seed", seed)
    redis = await get_redis()
    vk_n = _RECOMMENDATIONS_VK_FETCH_COUNT

    again = await _rec_read_cached_full(seed)
    if again:
        return again

    if not redis:
        mem_lock = await _rec_get_seed_async_lock(seed)
        async with mem_lock:
            again2 = await _rec_read_cached_full(seed)
            if again2:
                return again2
            return await _rec_do_vk_or_popular(seed, vk_n, redis=None, cache_key=cache_key)

    try:
        got_redis_lock = bool(await redis.set(lock_key, "1", nx=True, ex=_RECOMMENDATIONS_LOCK_SEC))
    except Exception:
        got_redis_lock = False

    if not got_redis_lock:
        for _ in range(50):
            await asyncio.sleep(0.2)
            w = await _rec_read_cached_full(seed)
            if w:
                return w
        pop = await _rec_ensure_popular_parsed(vk_n)
        return pop, "popular_wait"

    try:
        again3 = await _rec_read_cached_full(seed)
        if again3:
            return again3
        return await _rec_do_vk_or_popular(seed, vk_n, redis=redis, cache_key=cache_key)
    finally:
        try:
            await redis.delete(lock_key)
        except Exception:
            pass


async def _rec_fetch_full_for_seed(seed: str) -> Tuple[List[Dict], str]:
    """Кэш или _rec_leader_fill для одного seed (для merge нескольких опорных треков)."""
    hit = await _rec_read_cached_full(seed)
    if hit:
        return hit
    return await _rec_leader_fill(seed)


def _rec_merge_round_robin(lists: List[List[Dict]], limit: int, exclude_ids: set) -> List[Dict]:
    """Чередование списков, уникальность по id, без seed-треков."""
    seen: set = set()
    for x in exclude_ids:
        nx = _rec_norm_library_track_id(str(x))
        if nx:
            seen.add(nx)
    out: List[Dict] = []
    max_len = max((len(L) for L in lists), default=0)
    for i in range(max_len):
        for L in lists:
            if len(out) >= limit:
                return out
            if i >= len(L):
                continue
            item = L[i]
            tid_raw = str(item.get("id") or "").strip()
            tid = _rec_norm_library_track_id(tid_raw) or tid_raw
            if not tid or tid in seen:
                continue
            seen.add(tid)
            out.append(item)
    return out[:limit]


async def _rec_merge_for_seeds(seed_ids: List[str], limit: int) -> Tuple[List[Dict], str]:
    """N seed: параллельно кэш/VK, склеиваем round-robin (публичный ?seeds= ограничен отдельно)."""
    if not seed_ids:
        return [], "empty"
    pairs = await asyncio.gather(*[_rec_fetch_full_for_seed(s) for s in seed_ids])
    lists = [p[0] for p in pairs]
    sources = [p[1] for p in pairs]
    excl = set(seed_ids)
    merged = _rec_merge_round_robin(lists, limit, excl)
    if any("popular" in (s or "") for s in sources):
        src = "vk_merged_mixed"
    else:
        src = "vk_merged"
    return merged, src


async def _rec_fetch_genre_discovery_pool(
    allowed_genres: set[int],
    pool_cap: int,
    fav_set: set[str],
    rng: random.Random,
) -> Tuple[List[Dict], str]:
    """Кандидаты: audio.search с genre_id и нейтральным q (см. _vk_audio_search_by_genre_raw), без getRecommendations."""
    if not allowed_genres or pool_cap <= 0:
        return [], "genre_empty"
    gids = list(allowed_genres)
    rng.shuffle(gids)
    per_g = min(100, max(35, pool_cap // max(1, len(gids)) + 12))

    async def one(gid: int) -> List[Dict]:
        ok = await _rec_budget_try_consume()
        if not ok:
            return []
        async with _rec_vk_sem:
            raw = await _vk_audio_search_by_genre_raw(gid, per_g, 0)
        return raw

    raws = await asyncio.gather(*[one(g) for g in gids])
    lists: List[List[Dict]] = []
    for raw in raws:
        parsed = _parse_tracks(raw)
        lists.append([x for x in parsed if str(x.get("id") or "").strip() not in fav_set])
    excl = set(fav_set)
    merged = _rec_merge_round_robin(lists, pool_cap, excl)
    return merged, "genre_search_merged"


def _favorite_track_ids_ordered(favorites: List[Dict]) -> List[str]:
    out: List[str] = []
    seen: set = set()
    for t in favorites:
        raw = str(t.get("id") or "").strip()
        if not _valid_playlist_library_track_id(raw):
            continue
        eff = _playlist_library_track_id_stored(raw)
        if eff in seen:
            continue
        seen.add(eff)
        out.append(eff)
    return out


def _evenly_spaced_seed_ids(ids: List[str], max_seeds: int) -> List[str]:
    if not ids or max_seeds <= 0:
        return []
    if len(ids) <= max_seeds:
        return ids
    out: List[str] = []
    n = len(ids)
    for i in range(max_seeds):
        idx = int(round(i * (n - 1) / max(1, max_seeds - 1)))
        tid = ids[idx]
        if tid not in out:
            out.append(tid)
    return out


def _evenly_spaced_seed_ids_refreshed(ids: List[str], max_seeds: int, refresh_salt: int) -> List[str]:
    """Равномерные опорные id по хвосту избранного; при refresh_salt != 0 — перемешивание хвоста (другая комбинация seed без лишних VK-вызовов)."""
    if not ids or max_seeds <= 0:
        return []
    if refresh_salt == 0:
        return _evenly_spaced_seed_ids(ids, max_seeds)
    perm = ids.copy()
    random.Random(int(refresh_salt) & 0xFFFFFFFF).shuffle(perm)
    return _evenly_spaced_seed_ids(perm, max_seeds)


def _rec_seeds_from_recent_favorites(
    fav_ids_ordered: List[str],
    max_seeds: int,
    refresh_salt: int,
    *,
    wave: bool,
    favorites: Optional[List[Dict]] = None,
) -> List[str]:
    """Опорные VK-треки из последних уникальных исполнителей (порядок в файле: старые → новые)."""
    if not fav_ids_ordered or max_seeds <= 0:
        return []
    cap = _REC_STRICT_ANCHOR_ARTISTS
    anchor_ids = _rec_anchor_track_ids_by_recent_artists(favorites or [], fav_ids_ordered, cap)
    tail = anchor_ids if anchor_ids else (fav_ids_ordered[-min(cap, len(fav_ids_ordered)) :] if fav_ids_ordered else [])
    return _evenly_spaced_seed_ids_refreshed(tail, max_seeds, refresh_salt)


def _rec_tracks_subset_by_ids(favorites: List[Dict], ids: List[str]) -> List[Dict]:
    by_id = {str(t.get("id") or "").strip(): t for t in favorites if t.get("id")}
    out: List[Dict] = []
    for i in ids:
        tid = str(i).strip()
        if tid in by_id:
            out.append(dict(by_id[tid]))
    return out


def _rec_anchor_track_ids_by_recent_artists(
    favorites: List[Dict],
    fav_ids_ordered: List[str],
    max_artists: int,
) -> List[str]:
    """
    С конца fav_ids_ordered (новые добавления) собираем до max_artists разных исполнителей;
    на каждого — один самый новый трек. Порядок в ответе: по возрастанию позиции в плейлисте (как у хвоста).
    """
    if not fav_ids_ordered or max_artists <= 0:
        return []
    by_id = {str(t.get("id") or "").strip(): t for t in favorites if t.get("id")}
    picked: List[str] = []
    seen_artists: set[str] = set()
    for tid in reversed(fav_ids_ordered):
        if len(picked) >= max_artists:
            break
        tid = str(tid).strip()
        t = by_id.get(tid)
        if not t:
            continue
        akey = _rec_artist_exact_key(str(t.get("artist") or ""))
        if not akey:
            akey = f"_u:{tid}"
        if akey in seen_artists:
            continue
        seen_artists.add(akey)
        picked.append(tid)
    return list(reversed(picked))


def _rec_anchor_slot_split(n_total: int, pct_main: int, pct_custom: int) -> Tuple[int, int, int]:
    n_main = n_total * pct_main // 100
    n_custom = n_total * pct_custom // 100
    n_search = max(0, n_total - n_main - n_custom)
    return n_main, n_custom, n_search


def _rec_track_artist_key(t: Optional[Dict], tid: str) -> str:
    if not t:
        return f"_u:{tid}"
    k = _rec_artist_exact_key(str(t.get("artist") or ""))
    return k or f"_u:{tid}"


def _rec_anchor_track_ids_by_recent_artists_excluding(
    favorites: List[Dict],
    fav_ids_ordered: List[str],
    max_artists: int,
    exclude_artist_keys: set[str],
) -> List[str]:
    if not fav_ids_ordered or max_artists <= 0:
        return []
    by_id = {str(t.get("id") or "").strip(): t for t in favorites if t.get("id")}
    picked: List[str] = []
    seen_artists: set[str] = set(exclude_artist_keys)
    for tid in reversed(fav_ids_ordered):
        if len(picked) >= max_artists:
            break
        tid = str(tid).strip()
        t = by_id.get(tid)
        if not t:
            continue
        akey = _rec_artist_exact_key(str(t.get("artist") or ""))
        if not akey:
            akey = f"_u:{tid}"
        if akey in seen_artists:
            continue
        seen_artists.add(akey)
        picked.append(tid)
    return list(reversed(picked))


def _rec_anchor_ids_weighted_fav_custom_search(
    main_favorites: List[Dict],
    main_ids_ordered: List[str],
    custom_favorites: List[Dict],
    custom_ids_ordered: List[str],
    search_favorites: List[Dict],
    search_ids_ordered: List[str],
    n_total: int,
    pct_main: int,
    pct_custom: int,
) -> List[str]:
    """Якорь: сначала избранное, затем кастом, затем один слот по недавним поискам; при нехватке — добор из объединённого списка."""
    n_main, n_custom, n_search = _rec_anchor_slot_split(n_total, pct_main, pct_custom)
    seen_keys: set[str] = set()
    out: List[str] = []
    by_main = {str(t.get("id") or "").strip(): t for t in main_favorites if t.get("id")}
    by_cust = {str(t.get("id") or "").strip(): t for t in custom_favorites if t.get("id")}
    by_srch = {str(t.get("id") or "").strip(): t for t in search_favorites if t.get("id")}

    am = _rec_anchor_track_ids_by_recent_artists_excluding(main_favorites, main_ids_ordered, n_main, seen_keys)
    for tid in am:
        seen_keys.add(_rec_track_artist_key(by_main.get(tid), tid))
    out.extend(am)

    ac = _rec_anchor_track_ids_by_recent_artists_excluding(custom_favorites, custom_ids_ordered, n_custom, seen_keys)
    for tid in ac:
        seen_keys.add(_rec_track_artist_key(by_cust.get(tid), tid))
    out.extend(ac)

    asrch = _rec_anchor_track_ids_by_recent_artists_excluding(search_favorites, search_ids_ordered, n_search, seen_keys)
    for tid in asrch:
        seen_keys.add(_rec_track_artist_key(by_srch.get(tid), tid))
    out.extend(asrch)

    if len(out) < n_total:
        need = n_total - len(out)
        merged_favs = main_favorites + custom_favorites
        merged_ids = main_ids_ordered + custom_ids_ordered
        by_m = {str(t.get("id") or "").strip(): t for t in merged_favs if t.get("id")}
        extra = _rec_anchor_track_ids_by_recent_artists_excluding(merged_favs, merged_ids, need, seen_keys)
        out.extend(extra)

    return out[:n_total]


def _rec_genre_allowlist_from_tracks(tracks: List[Dict]) -> set[int]:
    s: set[int] = set()
    for t in tracks:
        _rec_ensure_track_genre(t)
        gid = t.get("genre_id")
        if isinstance(gid, int):
            s.add(gid)
    return s


def _rec_filter_tracks_by_genre_allowlist(
    tracks: List[Dict],
    allowed: set[int],
    *,
    keep_unknown: Optional[bool] = None,
) -> List[Dict]:
    if not allowed:
        return tracks
    ku = _REC_GENRE_FILTER_KEEP_UNKNOWN if keep_unknown is None else bool(keep_unknown)
    out: List[Dict] = []
    for t in tracks:
        _rec_ensure_track_genre(t)
        gid = t.get("genre_id")
        gi: Optional[int] = None
        if isinstance(gid, int):
            gi = gid
        elif isinstance(gid, str) and gid.strip().isdigit():
            gi = int(gid.strip())
        if gi is not None and gi in allowed:
            out.append(t)
        elif ku and gi is None:
            out.append(t)
    return out


def _rec_ytm_query_from_track_dict(t: Dict) -> str:
    """Для рекомендаций — только артист; название трека не подмешиваем (иначе узкие/случайные выдачи)."""
    art = str(t.get("artist") or "").strip()
    if art:
        return art[:200]
    tit = str(t.get("title") or "").strip()
    return tit[:200]


def _rec_track_dict_by_id_from_merged(merged: List[Dict], tid: str) -> Optional[Dict]:
    want = _rec_norm_library_track_id(tid)
    if not want:
        return None
    for tr in merged:
        raw = str(tr.get("id") or "").strip()
        if not raw:
            continue
        if raw == tid or _rec_norm_library_track_id(raw) == want:
            return tr
    return None


def _rec_anchor_token_cloud(
    recent_qs: List[str],
    merged: List[Dict],
    fav_ids_ordered: List[str],
    *,
    max_tokens: int = 56,
) -> Set[str]:
    """Только исполнители из хвоста избранного — без слов из истории поиска (поиск часто = название трека)."""
    _ = recent_qs
    out: Set[str] = set()
    tail = fav_ids_ordered[-22:] if fav_ids_ordered else []
    for tid in reversed(tail):
        tr = _rec_track_dict_by_id_from_merged(merged, tid)
        if not tr:
            continue
        for w in _meaningful_words(str(tr.get("artist", "") or "")):
            if len(w) >= 3:
                out.add(_normalize_for_match(w))
            if len(out) >= max_tokens:
                return out
    for tr in merged[-20:]:
        if not isinstance(tr, dict):
            continue
        for w in _meaningful_words(str(tr.get("artist", "") or "")):
            if len(w) >= 3:
                out.add(_normalize_for_match(w))
            if len(out) >= max_tokens:
                return out
    return out


def _rec_collect_youtube_queries_personal(
    merged: List[Dict],
    main_tracks: List[Dict],
    recent_ids: List[str],
    fav_ids_ordered: List[str],
    recent_qs: List[str],
    play_weights: List[Tuple[str, float]],
    *,
    has_anchor: bool,
    max_queries: int,
    rng: random.Random,
) -> List[str]:
    """Строки для YTM: поиски; до 10 последних добавленных с разными основными артистами → похожие из YTM."""
    _ = recent_qs
    seen: set[str] = set()
    out: List[str] = []
    related_source_artists: set[str] = set()

    def push(q: str) -> None:
        qq = (q or "").strip()
        if len(qq) < 2:
            return
        k = qq.lower()
        if k in seen:
            return
        seen.add(k)
        out.append(qq)

    def push_related_artists(art: str) -> None:
        """Подмешивает имён из YTM «Похожие» на странице артиста (один вызов на уникального артиста)."""
        raw = (art or "").strip()
        if len(raw) < 3:
            return
        ak = raw.lower()[:160]
        if ak in related_source_artists:
            return
        related_source_artists.add(ak)
        try:
            for rel in youtube_related_artist_names(raw, max_names=10):
                push(str(rel)[:120])
                if len(out) >= max_queries:
                    return
        except Exception as e:
            print(f"⚠️ recommendations related_artists {raw[:40]!r}: {e}")

    for raw_primary, tr in _rec_last_unique_primary_artists_from_main(main_tracks, 10):
        push(_rec_ytm_query_from_track_dict(tr))
        if len(out) >= max_queries:
            return out[:max_queries]
        push_related_artists(raw_primary)
        if len(out) >= max_queries:
            return out[:max_queries]
    id_ring = list(recent_ids) if has_anchor else list(fav_ids_ordered[-35:])
    rng.shuffle(id_ring)
    for tid in id_ring:
        tr = _rec_track_dict_by_id_from_merged(merged, tid)
        if tr:
            push(_rec_ytm_query_from_track_dict(tr))
        if len(out) >= max_queries:
            return out
    for tid, _w in play_weights[:28]:
        tr = _rec_track_dict_by_id_from_merged(merged, tid)
        if tr:
            push(_rec_ytm_query_from_track_dict(tr))
        if len(out) >= max_queries:
            break
    return out[:max_queries]


async def _rec_fetch_youtube_personal_candidates(
    queries: List[str],
    budget: int,
    fav_set: set,
) -> List[Dict]:
    if not queries or budget <= 0:
        return []
    fav_n = {_rec_norm_library_track_id(str(x)) for x in fav_set if str(x).strip()}
    per = min(_REC_YT_PERSONAL_PER_QUERY, max(5, budget // max(1, len(queries)) + 4))
    sem = asyncio.Semaphore(_REC_YT_PERSONAL_CONCURRENCY)

    async def one(q: str) -> List[Dict]:
        async with sem:
            try:
                return await asyncio.to_thread(search_youtube_tracks, q, per)
            except Exception as e:
                print(f"⚠️ YTM personal rec q={q[:48]!r}: {e}")
                return []

    lists = await asyncio.gather(*(one(q) for q in queries))
    merged_rr = _rec_merge_round_robin(lists, budget, fav_n)
    out: List[Dict] = []
    for t in merged_rr:
        tid = str(t.get("id") or "").strip()
        if not tid:
            continue
        n = _rec_norm_library_track_id(tid)
        if n in fav_n:
            continue
        out.append(t)
    return out[:budget]


def _rec_weighted_choice_unique(
    rng: random.Random,
    items: List[str],
    weights: List[float],
    k: int,
) -> List[str]:
    if k <= 0 or not items:
        return []
    out: List[str] = []
    picked: set = set()
    # small-k => simple O(k*n) scan is fine
    for _ in range(min(k, len(items))):
        total = 0.0
        for it, w in zip(items, weights):
            if it in picked:
                continue
            if w > 0:
                total += w
        if total <= 0:
            break
        r = rng.random() * total
        acc = 0.0
        chosen = None
        for it, w in zip(items, weights):
            if it in picked or w <= 0:
                continue
            acc += w
            if acc >= r:
                chosen = it
                break
        if not chosen:
            break
        picked.add(chosen)
        out.append(chosen)
    return out


def _rec_seed_ids_weighted_by_taste(
    favorites: List[Dict],
    fav_ids_ordered: List[str],
    max_seeds: int,
    salt: int,
    *,
    wave: bool,
    taste_profile: Dict[str, Any],
    meta_map: Dict[str, Dict],
) -> List[str]:
    """
    Seed'ы для VK: из хвоста избранного, но с весами по жанрам пользователя
    и сильным бустом от последних уникальных исполнителей (по одному треку на артиста).
    """
    if not fav_ids_ordered or max_seeds <= 0:
        return []
    cap = _REC_STRICT_ANCHOR_ARTISTS
    tail_ids = _rec_anchor_track_ids_by_recent_artists(favorites, fav_ids_ordered, cap)
    if not tail_ids:
        tail_ids = fav_ids_ordered[-min(cap, len(fav_ids_ordered)) :] if fav_ids_ordered else []
    recent_anchor = set(tail_ids)
    rng = random.Random(int(salt) & 0xFFFFFFFF)

    gw = taste_profile.get("genre_weights") or {}
    lw = taste_profile.get("lang_weights") or {}
    yc = taste_profile.get("year_center")

    items: List[str] = []
    weights: List[float] = []
    for tid in tail_ids:
        if not tid:
            continue
        items.append(tid)
        base = 1.0
        if tid in recent_anchor:
            base += 4.0  # последний mood пользователя
        m = meta_map.get(tid) or {}
        gid = m.get("genre_id")
        try:
            gid_int = int(gid) if gid is not None else None
        except Exception:
            gid_int = None
        if gid_int is not None and gid_int in gw:
            base += 6.0 * float(gw[gid_int])
        # мягко учитываем язык/год, если есть
        try:
            lb = _rec_lang_bucket_track(m) if (m.get("title") or m.get("artist")) else None
        except Exception:
            lb = None
        if lb and lb in lw:
            base += 1.5 * float(lw[lb])
        ry = m.get("release_year")
        if isinstance(yc, int) and isinstance(ry, int):
            base += max(0.0, 1.2 - min(abs(int(ry) - int(yc)), 60) / 60.0)
        weights.append(max(0.1, base))

    picked = _rec_weighted_choice_unique(rng, items, weights, max_seeds)
    if picked:
        return picked
    # fallback: как раньше
    return _rec_seeds_from_recent_favorites(fav_ids_ordered, max_seeds, salt, wave=wave, favorites=favorites)


async def _rec_resolve_radio_seed_video_id(seed_ids_ordered: List[str]) -> Optional[str]:
    """Сид радио YTM: идём с конца списка id (для избранного — последний append в JSON, см. POST /api/playlist) → videoId или VK→YT из Redis. Без воспроизведения: только серверный вызов get_watch_playlist."""
    for tid in reversed(seed_ids_ordered or []):
        raw = (tid or "").strip()
        if not raw:
            continue
        c = _canonical_share_track_id(raw) or raw
        if _is_youtube_video_id(c):
            return c.strip()
        vid = extract_video_id(raw)
        if vid and _is_youtube_video_id(vid):
            return vid
    for tid in reversed(seed_ids_ordered or []):
        raw = (tid or "").strip()
        if not raw or not _valid_track_id(raw):
            continue
        try:
            v = await _redis_get_vk_yt_fallback_video_id(_vk_canonical_track_id(raw))
        except Exception:
            v = None
        if v and _is_youtube_video_id(str(v)):
            return str(v).strip()
    return None


_REC_COLLAB_MARK_RE = re.compile(
    r"\b(feat\.?|ft\.?|featuring|при участии|совместно|prod\.?|прод\.?| x | × )\s*",
    re.I,
)


def _rec_catalog_search_q_from_track_artist(artist: str) -> str:
    """Строка для VK audio.search по «главному» исполнителю (без хвоста фита)."""
    s = (artist or "").strip()
    if not s:
        return ""
    low = s.lower()
    for sep in (" feat.", " feat ", " ft.", " ft ", " x ", " × ", " при участии ", " совместно "):
        idx = low.find(sep)
        if idx > 0:
            s = s[:idx].strip()
            low = s.lower()
    if "," in s:
        s = s.split(",", 1)[0].strip()
    return s[:120]


def _rec_collab_heuristic(artist: str, title: str) -> bool:
    blob = f"{artist} {title}"
    if _REC_COLLAB_MARK_RE.search(blob):
        return True
    if " & " in artist or " и " in artist.lower():
        return True
    a = artist.strip()
    if a.count(",") >= 1 and len([x for x in a.split(",") if x.strip()]) >= 2:
        return True
    return False


def _rec_recent_vk_catalog_anchor_rows(
    main: List[Dict],
    main_ids: List[str],
    max_artists: int,
    exclude_artist_keys: AbstractSet[str],
) -> List[Tuple[str, str]]:
    """До max_artists пар (строка поиска VK, artist_key): с конца избранного — последние добавления первыми."""
    by_id = {str(t.get("id") or "").strip(): t for t in main if t.get("id")}
    seen_keys: Set[str] = set(exclude_artist_keys)
    out: List[Tuple[str, str]] = []
    for tid in reversed(main_ids):
        if len(out) >= max_artists:
            break
        tr = by_id.get(str(tid).strip())
        if not tr:
            continue
        akey = _rec_artist_exact_key(str(tr.get("artist") or ""))
        if not akey:
            akey = f"_u:{str(tid).strip()}"
        if akey in seen_keys:
            continue
        q = _rec_catalog_search_q_from_track_artist(str(tr.get("artist") or ""))
        if len(q) < 2:
            continue
        seen_keys.add(akey)
        out.append((q, akey))
    return out


def _rec_pick_five_vk_catalog_slice(
    rows: List[Dict],
    catalog_query: str,
    exclude_ids: AbstractSet[str],
) -> List[Dict]:
    """До 3 без явного фита + до 2 с фитом (порядок: соло, затем фиты)."""
    parsed = _parse_tracks(rows)
    solos: List[Dict] = []
    feats: List[Dict] = []
    solo_fill: List[Dict] = []
    feat_fill: List[Dict] = []
    for t in parsed:
        tid = str(t.get("id") or "").strip()
        if not tid or tid in exclude_ids:
            continue
        art = str(t.get("artist") or "")
        tit = str(t.get("title") or "")
        if not _artist_matches_catalog_query(art, catalog_query):
            continue
        if not _rec_collab_heuristic(art, tit):
            (solos if len(solos) < 3 else solo_fill).append(t)
        else:
            (feats if len(feats) < 2 else feat_fill).append(t)
    while len(solos) < 3 and solo_fill:
        solos.append(solo_fill.pop(0))
    while len(feats) < 2 and feat_fill:
        feats.append(feat_fill.pop(0))
    return (solos[:3] + feats[:2])[:5]


async def _rec_personal_vk_hundred_from_recent_artists(
    user_id: int,
    main: List[Dict],
    main_ids: List[str],
    out_limit: int,
    *,
    base_exclude: AbstractSet[str],
) -> Tuple[List[Dict], str]:
    """
    10 последних уникальных исполнителей из избранного + ещё 10 следующих;
    на каждого до 5 треков из VK-каталога (3 без явного фита + 2 с фитом), до out_limit.
    """
    cap = max(50, min(int(out_limit), 100))
    rows_a = _rec_recent_vk_catalog_anchor_rows(main, main_ids, 10, set())
    keys_a = {k for _, k in rows_a}
    rows_b = _rec_recent_vk_catalog_anchor_rows(main, main_ids, 10, keys_a)
    rows_all = rows_a + rows_b
    if not rows_all:
        return [], "vk_recent_artists_empty"

    sem = asyncio.Semaphore(5)

    async def _fetch_one(q: str) -> List[Dict]:
        async with sem:
            try:
                return await _vk_search_artist_catalog(q, 120)
            except Exception as e:
                print(f"⚠️ vk catalog q={q[:48]!r}: {e}")
                return []

    catalogs = await asyncio.gather(*[_fetch_one(q) for q, _ in rows_all])
    blocks: List[List[Dict]] = []
    for raw, (q, _) in zip(catalogs, rows_all):
        blocks.append(_rec_pick_five_vk_catalog_slice(raw, q, base_exclude))

    merged: List[Dict] = []
    seen: Set[str] = set(base_exclude)
    for slot in range(5):
        for blk in blocks:
            if slot >= len(blk):
                continue
            t = blk[slot]
            tid = str(t.get("id") or "").strip()
            if not tid or tid in seen:
                continue
            seen.add(tid)
            merged.append(t)
            if len(merged) >= cap:
                for row in merged:
                    _rec_ensure_track_genre(row)
                return merged, "vk_20x5_recent_artists"
    for row in merged:
        _rec_ensure_track_genre(row)
    return merged, "vk_20x5_recent_artists"


async def _rec_personal_blend_for_user(
    user_id: int,
    limit: int,
    *,
    refresh_salt: int = 0,
    wave: bool = False,
    refresh: bool = False,
) -> Tuple[List[Dict], str]:
    """
    Персонально: якорь из избранного / кастома / поисков (доли REC_ANCHOR_PCT_*).
    По умолчанию REC_PERSONAL_SKIP_VK: без VK — YTM (радио по последнему лайку + поиск).
    При REC_PERSONAL_SKIP_VK=0: пул VK, коллаборатив SQLite, недавние поиски VK.
    """
    import analytics_db

    analytics_db.init_db()
    req_id = uuid.uuid4().hex[:10]
    rng = random.Random(secrets.randbelow(2**31))
    salt = refresh_salt if refresh_salt != 0 else secrets.randbelow(2**31)

    n_search_q = _REC_STRICT_ANCHOR_SEARCHES
    try:
        recent_qs = analytics_db.get_recent_search_q_norms(user_id, limit=n_search_q, days=90)
    except Exception:
        recent_qs = []

    split_task = asyncio.create_task(_rec_playlist_split_for_recs(user_id))
    # Не подмешиваем выдачу по истории поиска: запросы часто совпадают с названием трека (шум в реках).
    search_prefetch_task = None

    try:
        main, main_ids, custom_tracks, custom_ids, merged = await split_task
    except BaseException:
        if search_prefetch_task is not None and not search_prefetch_task.done():
            search_prefetch_task.cancel()
            try:
                await search_prefetch_task
            except asyncio.CancelledError:
                pass
        raise
    fav_ids_list = _favorite_track_ids_ordered(merged)
    fav_set = set(fav_ids_list)
    d_track: Set[str] = set()
    ap_map: Dict[str, int] = {}
    gp_map: Dict[int, int] = {}
    try:
        d_track = set(analytics_db.get_disliked_track_ids(user_id))
        d_track |= set(analytics_db.get_removed_library_track_ids(user_id))
        ap_map = analytics_db.get_rec_artist_show_penalties(user_id)
        gp_map = analytics_db.get_rec_genre_show_penalties(user_id)
    except Exception:
        pass
    try:
        if analytics_db.count_user_library_tracks(user_id) == 0 and (main_ids or custom_ids):
            analytics_db.replace_user_library_tracks(
                user_id,
                list(dict.fromkeys([*main_ids, *custom_ids])),
            )
    except Exception:
        pass
    if not _REC_PERSONAL_SKIP_VK and not wave and main_ids:
        try:
            excl = set(fav_set) | set(d_track)
            raw_h, src_h = await _rec_personal_vk_hundred_from_recent_artists(
                user_id, main, main_ids, min(limit, 100), base_exclude=excl
            )
            if len(raw_h) >= min(40, max(20, limit // 2)):
                if search_prefetch_task is not None and not search_prefetch_task.done():
                    search_prefetch_task.cancel()
                    try:
                        await search_prefetch_task
                    except asyncio.CancelledError:
                        pass
                flat_h = await _rec_personal_finalize_output(user_id, raw_h, limit, rng)
                return flat_h, src_h
        except Exception as e:
            logger.warning("_rec_personal_vk_hundred_from_recent_artists uid=%s: %s", user_id, e)
    mult = _REC_PERSONAL_BLEND_POOL_MULT_WAVE if wave else _REC_PERSONAL_BLEND_POOL_MULT
    # При явном обновлении подборки — больше кандидатов, чтобы после исключения «уже показанных» хватало слотов.
    if refresh and not wave:
        mult = min(12, mult + 5)
    pool_cap = min(450, max(limit * mult, limit + 40))

    n_anchor = _REC_STRICT_ANCHOR_ARTISTS

    search_prefetch_tracks: List[Dict] = []
    search_prefetch_ids: List[str] = []
    if search_prefetch_task is not None:
        try:
            raw_sq = await search_prefetch_task
            if _REC_PERSONAL_SKIP_VK:
                search_prefetch_tracks = [x for x in (raw_sq or []) if isinstance(x, dict) and x.get("id")]
            else:
                search_prefetch_tracks = _parse_tracks(raw_sq)
            search_prefetch_ids = [
                str(t.get("id") or "").strip() for t in search_prefetch_tracks if t.get("id")
            ]
        except Exception as e:
            print(f"⚠️ recommendations search_prefetch: {e}")

    recent_ids = _rec_anchor_ids_weighted_fav_custom_search(
        main,
        main_ids,
        custom_tracks,
        custom_ids,
        search_prefetch_tracks,
        search_prefetch_ids,
        n_anchor,
        _REC_ANCHOR_PCT_MAIN,
        _REC_ANCHOR_PCT_CUSTOM,
    )
    recent_set = set(recent_ids)
    favorites_anchor = _rec_tracks_subset_by_ids(merged, recent_ids)
    has_anchor = bool(recent_ids)

    def _rec_taste_scoring_has_signal(tp: Dict[str, Any]) -> bool:
        gw = tp.get("genre_weights") or {}
        lw = tp.get("lang_weights") or {}
        gsum = sum(float(v) for v in gw.values()) if isinstance(gw, dict) else 0.0
        lsum = sum(float(v) for v in lw.values()) if isinstance(lw, dict) else 0.0
        return gsum > 1e-9 or lsum > 1e-9 or (tp.get("year_center") is not None)

    try:
        play_weights_full = analytics_db.get_user_track_play_weights(user_id, limit=56, days=120)
    except Exception:
        play_weights_full = []
    if has_anchor:
        play_weights = [(tid, w) for tid, w in play_weights_full if tid in recent_set]
    else:
        play_weights = [(tid, w) for tid, w in play_weights_full if tid in fav_set]

    fav_meta_map = await _rec_batch_redis_meta_map(fav_ids_list)
    pw_ids_for_meta = [tid for tid, _ in play_weights if tid not in fav_meta_map][:64]
    if pw_ids_for_meta:
        fav_meta_map = {**fav_meta_map, **(await _rec_batch_redis_meta_map(pw_ids_for_meta))}
    sp_rest = [tid for tid in search_prefetch_ids if tid and tid not in fav_meta_map][:48]
    if sp_rest:
        fav_meta_map = {**fav_meta_map, **(await _rec_batch_redis_meta_map(sp_rest))}

    if not fav_ids_list:
        p_sql = await _rec_rebuild_taste_profile_from_sqlite(user_id, only_track_ids=None)
        taste_profile = _rec_taste_profile_to_scoring_model(p_sql or {})
    else:
        taste_profile = _rec_build_taste_profile(favorites_anchor, play_weights, fav_meta_map)
        if not _rec_taste_scoring_has_signal(taste_profile):
            p_sql = await _rec_rebuild_taste_profile_from_sqlite(user_id, only_track_ids=recent_set)
            if p_sql:
                taste_profile = _rec_taste_profile_to_scoring_model(p_sql)

    seed_ids: List[str] = list(recent_ids)
    max_seeds = _REC_PERSONAL_MAX_SEEDS_WAVE if wave else _REC_PERSONAL_MAX_SEEDS
    seed_ids_for_similar: List[str] = []
    if recent_ids:
        seed_ids_for_similar = _evenly_spaced_seed_ids_refreshed(recent_ids, max_seeds, salt)

    src_parts: List[str] = ["anchor_weighted_fav_custom_search"]
    vk_merged: List[Dict] = []
    vk_base = min(450, limit * (12 if wave else 3))
    vk_pool_cap = min(450, max(vk_base, pool_cap))

    allowed_genres: set[int] = set()
    if has_anchor:
        for tid in recent_ids:
            m = fav_meta_map.get(tid) or {}
            gid = m.get("genre_id")
            if isinstance(gid, int):
                allowed_genres.add(gid)
            elif isinstance(gid, str) and gid.strip().isdigit():
                allowed_genres.add(int(gid.strip()))
    for t in search_prefetch_tracks:
        tid = str(t.get("id") or "").strip()
        if tid:
            _rec_merge_track_meta_from_redis(t, fav_meta_map.get(tid))
        _rec_ensure_track_genre(t)
    allowed_genres |= _rec_genre_allowlist_from_tracks(search_prefetch_tracks)

    vk_parts: List[List[Dict]] = []
    cap_chunk = min(300, max(vk_pool_cap // 2 + 40, vk_pool_cap))
    if not _REC_PERSONAL_SKIP_VK:
        if seed_ids_for_similar and allowed_genres:
            (sim_merged, sim_src), (gen_merged, gen_src) = await asyncio.gather(
                _rec_merge_for_seeds(seed_ids_for_similar, cap_chunk),
                _rec_fetch_genre_discovery_pool(allowed_genres, cap_chunk, fav_set, rng),
            )
            vk_parts.extend([sim_merged, gen_merged])
            src_parts.extend([sim_src, gen_src])
        else:
            if seed_ids_for_similar:
                sim_merged, sim_src = await _rec_merge_for_seeds(seed_ids_for_similar, cap_chunk)
                vk_parts.append(sim_merged)
                src_parts.append(sim_src)
            if allowed_genres:
                gen_merged, gen_src = await _rec_fetch_genre_discovery_pool(allowed_genres, cap_chunk, fav_set, rng)
                vk_parts.append(gen_merged)
                src_parts.append(gen_src)

        if vk_parts:
            vk_merged = _rec_merge_round_robin(vk_parts, vk_pool_cap, fav_set)
        else:
            pop = await _rec_ensure_popular_parsed(max(pool_cap, _RECOMMENDATIONS_VK_FETCH_COUNT))
            vk_merged = [x for x in pop if x.get("id") and x.get("id") not in fav_set]
            src_parts.append("popular_fallback")
            try:
                tr_ids = analytics_db.get_global_trending_track_ids(limit=18)
                if tr_ids:
                    raw = await _vk_batch_get_by_id(tr_ids[:22])
                    vk_merged.extend(_parse_tracks(raw))
                    src_parts.append("trending")
            except Exception as e:
                print(f"⚠️ recommendations trending: {e}")

        if not vk_merged:
            pop = await _rec_ensure_popular_parsed(max(pool_cap, _RECOMMENDATIONS_VK_FETCH_COUNT))
            vk_merged = [x for x in pop if x.get("id") and x.get("id") not in fav_set]
            if "popular_fallback" not in src_parts:
                src_parts.append("popular_topup")
    else:
        src_parts.append("skip_vk")

    yt_merged: List[Dict] = []
    yt_queries = _rec_collect_youtube_queries_personal(
        merged,
        main,
        recent_ids,
        fav_ids_list,
        recent_qs,
        play_weights,
        has_anchor=has_anchor,
        max_queries=_REC_YT_PERSONAL_MAX_QUERIES,
        rng=rng,
    )
    yt_cap = min(pool_cap, max(vk_pool_cap // 2 + 40, limit * 5))
    if yt_queries:
        try:
            yt_merged = await _rec_fetch_youtube_personal_candidates(yt_queries, yt_cap, fav_set)
            if yt_merged:
                src_parts.append("youtube_personal")
        except Exception as e:
            print(f"⚠️ recommendations youtube_personal: {e}")
    if yt_merged:
        if vk_merged:
            vk_merged = _rec_merge_round_robin([vk_merged, yt_merged], vk_pool_cap, fav_set)
        else:
            vk_merged = yt_merged
    if not vk_merged:
        try:
            cold_n = min(40, max(limit + 12, pool_cap // 5))
            cold_q = "" if _REC_PERSONAL_SKIP_VK else _REC_YT_COLDSTART_QUERY
            if fav_ids_list:
                arts: List[str] = []
                for tid in reversed(fav_ids_list[-10:]):
                    tr = _rec_track_dict_by_id_from_merged(merged, tid)
                    if not tr:
                        continue
                    a = str(tr.get("artist") or "").strip()
                    if len(a) >= 2 and a.lower() not in {x.lower() for x in arts}:
                        arts.append(a)
                    if len(arts) >= 5:
                        break
                if arts:
                    cold_q = " ".join(arts[:5])[:200]
            if _REC_PERSONAL_SKIP_VK and (not cold_q or not str(cold_q).strip()):
                tr0: Optional[Dict] = None
                for tid in reversed(main_ids or fav_ids_list):
                    tr0 = _rec_track_dict_by_id_from_merged(merged, tid)
                    if tr0:
                        break
                if tr0:
                    q0 = str(tr0.get("artist", "") or "").strip()
                    if len(q0) >= 4:
                        cold_q = q0[:200]
            if not cold_q or not str(cold_q).strip():
                cold_q = _REC_YT_SEED_FALLBACK_QUERY if _REC_PERSONAL_SKIP_VK else _REC_YT_COLDSTART_QUERY
            cold = await asyncio.to_thread(search_youtube_tracks, cold_q, cold_n)
            exf = {_rec_norm_library_track_id(str(x)) for x in fav_set if str(x).strip()}
            vk_merged = []
            for x in cold or []:
                if not isinstance(x, dict) or not x.get("id"):
                    continue
                nid = _rec_norm_library_track_id(str(x["id"]))
                if nid and nid not in exf:
                    vk_merged.append(x)
            if vk_merged:
                src_parts.append("youtube_cold_start")
        except Exception as e:
            print(f"⚠️ recommendations youtube_cold_start: {e}")

    radio_pool: List[Dict] = []
    # Только избранное (main): последний лайк = конец массива в файле; merged с кастомом не подменяет сид.
    rad_vid = await _rec_resolve_radio_seed_video_id(main_ids or fav_ids_list)
    if rad_vid:
        try:
            rad_lim = 90 if wave else 55
            radio_pool = await asyncio.to_thread(youtube_radio_tracks_from_video_id, rad_vid, rad_lim)
        except Exception as e:
            print(f"⚠️ recommendations yt_radio_seed: {e}")
    if radio_pool and main_ids:
        seed_tr = _rec_track_dict_by_id_from_merged(merged, main_ids[-1])
        if seed_tr:
            sk = _rec_artist_exact_key(str(seed_tr.get("artist") or ""))
            if sk and len(sk) >= 3:
                rp_f = [
                    t
                    for t in radio_pool
                    if isinstance(t, dict) and _rec_artist_exact_key(str(t.get("artist") or "")) != sk
                ]
                if rp_f:
                    radio_pool = rp_f
    if radio_pool:
        src_parts.append("yt_radio_seed")
        for _rt in radio_pool:
            if isinstance(_rt, dict):
                _rt["_rec_ytm_radio"] = True
        rr_cap = max(vk_pool_cap, len(radio_pool) + 40)
        vk_merged = _rec_merge_round_robin([radio_pool, vk_merged], rr_cap, fav_set)
    if _REC_PERSONAL_SKIP_VK and radio_pool and vk_merged:
        strictly_yt: List[Dict] = []
        seen_strict: set = set()
        for t in vk_merged:
            if not isinstance(t, dict) or not t.get("id"):
                continue
            if _rec_youtube_id_boost(t) <= 0:
                continue
            tid_s = str(t.get("id") or "").strip()
            nk = _rec_norm_library_track_id(tid_s) or tid_s
            if nk in seen_strict:
                continue
            seen_strict.add(nk)
            strictly_yt.append(t)
        if strictly_yt:
            vk_merged = strictly_yt
            src_parts.append("youtube_only_pool")

    collab_parsed: List[Dict] = []
    try:
        anchor_lib = list(dict.fromkeys([*main_ids, *custom_ids]))
        if anchor_lib and not _REC_PERSONAL_SKIP_VK:
            excl_collab = set(fav_set) | d_track
            cl_lim = 44 if wave else 30
            cl_ids = analytics_db.get_collaborative_library_track_ids(
                anchor_lib,
                user_id,
                excl_collab,
                limit=cl_lim,
            )
            if cl_ids:
                raw_collab = await _vk_batch_get_by_id(cl_ids[: min(52, len(cl_ids) + 12)])
                collab_parsed = [x for x in _parse_tracks(raw_collab) if x.get("id")]
                if collab_parsed:
                    src_parts.append("collab_library")
    except Exception as e:
        print(f"⚠️ recommendations collab_library: {e}")

    search_parsed: List[Dict] = []
    seen_sp: set[str] = set()
    for t in search_prefetch_tracks:
        tid = str(t.get("id") or "").strip()
        if tid and tid not in fav_set:
            seen_sp.add(tid)
            search_parsed.append(t)
    meta_map = dict(fav_meta_map)
    rest_ids: List[str] = []
    for t in vk_merged + collab_parsed + search_parsed:
        tid = str(t.get("id") or "").strip()
        if tid and tid not in meta_map:
            rest_ids.append(tid)
    meta_map.update(await _rec_batch_redis_meta_map(rest_ids))

    _rec_enrich_tracks_meta_inplace(vk_merged, meta_map)
    _rec_enrich_tracks_meta_inplace(collab_parsed, meta_map)
    _rec_enrich_tracks_meta_inplace(search_parsed, meta_map)

    if has_anchor and allowed_genres:
        vk_merged = _rec_filter_tracks_by_genre_allowlist(vk_merged, allowed_genres)
        search_parsed = _rec_filter_tracks_by_genre_allowlist(search_parsed, allowed_genres)
        collab_parsed = _rec_filter_tracks_by_genre_allowlist(collab_parsed, allowed_genres)

    vk_merged = _rec_apply_artist_feature_caps(vk_merged)
    search_parsed = _rec_apply_artist_feature_caps(search_parsed)
    collab_parsed = _rec_apply_artist_feature_caps(collab_parsed)

    _rec_trace_log_candidates(
        user_id=user_id,
        request_id=req_id,
        mode="wave" if wave else "personal",
        seed_ids=seed_ids,
        taste_profile=taste_profile,
        candidates_by_source={"vk": vk_merged, "search_affinity": search_parsed, "collab": collab_parsed},
    )

    wf_favorites = favorites_anchor if has_anchor else merged
    wf_ids = recent_ids if has_anchor else fav_ids_list
    src = "+".join(src_parts) if src_parts else "mixed"
    anchor_tokens: Set[str] = set()
    if fav_ids_list:
        try:
            anchor_tokens = _rec_anchor_token_cloud(recent_qs, merged, fav_ids_list)
        except Exception:
            anchor_tokens = set()
    flat = _rec_personal_wave_flat(
        vk_merged,
        collab_parsed,
        search_parsed,
        fav_set,
        pool_cap,
        rng,
        wave_mode=wave,
        favorites=wf_favorites,
        fav_ids_list=wf_ids,
        taste_profile=taste_profile,
        genre_ranking_only=not _REC_PERSONAL_SKIP_VK,
        affinity_min_favorites=1 if _REC_PERSONAL_SKIP_VK else 3,
        strict_affinity_min_favorites=2 if _REC_PERSONAL_SKIP_VK else 5,
        search_first=bool(_REC_PERSONAL_SKIP_VK),
        anchor_query_tokens=anchor_tokens if (_REC_PERSONAL_SKIP_VK or anchor_tokens) else None,
        disliked_track_ids=d_track,
        artist_show_penalties=ap_map,
        genre_show_penalties=gp_map,
    )
    if wave and wf_ids and REC_WAVE_INTERLEAVE_FAVORITES:
        flat = _rec_interleave_favorites_into_wave(wf_favorites, flat, pool_cap, salt, rng=rng)
        src_parts.append("wave_fav_mix")
        src = "+".join(src_parts) if src_parts else "mixed"
    flat = await _rec_personal_finalize_output(user_id, flat, limit, rng)
    return flat, src


def _normalize_for_match(s: str) -> str:
    """Убирает пунктуацию и схлопывает пробелы для сравнения."""
    return re.sub(r'\s+', ' ', re.sub(r'[^\w\s]', ' ', s.lower())).strip()


def _meaningful_words(s: str) -> List[str]:
    """Значимые слова (>= 2 символов, без пунктуации)."""
    return [w for w in re.sub(r'[^\w\s]', ' ', s.lower()).split() if len(w) >= 2]


def _relevance_score(track: Dict, query: str) -> tuple:
    """Скоринг релевантности. Меньше = лучше (для сортировки).
    Уровни:
    0 — точное совпадение
    1 — full или title начинается с запроса
    2 — все значимые слова запроса содержатся (бонус если title начинается с конца запроса)
    3 — запрос как подстрока
    4 — частичное совпадение слов
    5 — нет совпадений
    """
    q_norm = _normalize_for_match(query)
    # Альтернативная форма для кросс-языковых совпадений:
    # кириллица → латиница, латиница → кириллица.
    q_alt_norm = ""
    if _has_cyrillic(query) and not _has_latin(query):
        q_alt_norm = _normalize_for_match(_transliterate_to_latin(query))
    elif _has_latin(query) and not _has_cyrillic(query):
        q_alt_norm = _normalize_for_match(_transliterate_to_russian(query))
    title_norm = _normalize_for_match(track.get("title", ""))
    artist_norm = _normalize_for_match(track.get("artist", ""))
    full_norm = f"{artist_norm} {title_norm}"

    # Специальный буст: cruiser aurora + «птицы» → треки с таким артистом и словом «птиц» в названии всегда наверху
    if (
        ("cruiser" in q_norm and "aurora" in q_norm and "птиц" in q_norm)
        and ("cruiser" in artist_norm and "aurora" in artist_norm and "птиц" in title_norm)
    ):
        return (-1, 0, 0)

    # Точное совпадение: по названию, артисту или полной строке артист+название
    if q_norm in (title_norm, artist_norm, full_norm, f"{artist_norm}   {title_norm}"):
        return (0, 0, 0)
    if q_alt_norm and q_alt_norm in (title_norm, artist_norm, full_norm, f"{artist_norm}   {title_norm}"):
        # Альтернативная форма (транслит) — почти точное совпадение
        return (0, 1, 0)
    # full "artist - title" или artist/title начинается с запроса — наивысший приоритет
    if full_norm.startswith(q_norm) or title_norm.startswith(q_norm) or artist_norm.startswith(q_norm):
        return (1, 0, 0)
    if q_alt_norm and (full_norm.startswith(q_alt_norm) or title_norm.startswith(q_alt_norm) or artist_norm.startswith(q_alt_norm)):
        return (1, 1, 0)

    q_words = _meaningful_words(query)
    if not q_words:
        return (5, 0, 0)

    full_words_set = set(_meaningful_words(f"{track.get('artist', '')} {track.get('title', '')}"))
    title_words_set = set(_meaningful_words(track.get("title", "")))

    matched_in_title = sum(1 for w in q_words if w in title_words_set)
    matched_in_full = sum(1 for w in q_words if w in full_words_set)

    if matched_in_full == len(q_words):
        # Все слова есть. Бонус: title начинается с кириллической части запроса (после латиницы)
        lat_count = sum(1 for w in q_words if _has_latin(w) and not _has_cyrillic(w))
        cyr_part = " ".join(q_words[lat_count:]) if lat_count < len(q_words) else ""
        title_starts_cyr = 0 if cyr_part and title_norm.startswith(cyr_part) else 1
        return (2, 0 if matched_in_title == len(q_words) else 1, title_starts_cyr)

    if q_norm in full_norm:
        return (3, 0, 0)
    if q_norm in title_norm:
        return (3, 1, 0)

    if matched_in_full > 0:
        return (4, len(q_words) - matched_in_full, 0)

    return (5, 0, 0)


async def vk_audio_search(query: str, limit: int = 50) -> List[Dict]:
    """Поиск через VK execute + smart queries + ранжирование по релевантности."""
    query = _fix_common_typos(query)
    if not query or len(query.strip()) < 3:
        return []
    queries = _build_search_queries(query)
    all_items = await _vk_execute_search(queries, limit)
    if not all_items and queries:
        all_items = await _vk_search_raw_fallback(queries[0], limit, 0)
    tracks = _parse_tracks(all_items)
    tracks.sort(key=lambda t: _relevance_score(t, query))
    return tracks[:limit]


async def vk_audio_search_paginated(query: str, offset: int = 0, limit: int = 10) -> List[Dict]:
    """Поиск с пагинацией (только raw audio.search — единый порядок для всех страниц)."""
    query = _fix_common_typos(query)
    if not query or len(query.strip()) < 3:
        return []
    raw_items = await _vk_search_raw_fallback(query, limit, offset)
    return _parse_tracks(raw_items)


async def _vk_search_for_http(query: str, offset: int, limit: int) -> List[Dict]:
    """Поиск для HTTP API с единым ранжированием и постраничной выдачей.

    Для каждой страницы вызываем умный vk_audio_search с увеличенным лимитом
    (offset+limit, но не более 100), затем берём срез [offset:offset+limit].
    Так порядок треков стабилен между страницами, а VK не перегружается.
    """
    effective_limit = offset + limit
    # Минимум = запрошенный limit, максимум = 100, чтобы не перегружать VK
    effective_limit = max(limit, min(effective_limit, 100))
    tracks = await vk_audio_search(query, limit=effective_limit)
    if not tracks:
        return []
    page = tracks[offset : offset + limit]
    await _vk_enrich_tracks_album_covers_via_get_by_id(page)
    return page


async def _vk_get_audio_url_impl(raw_id: str, canon: str) -> Optional[str]:
    # 1) Негативный кэш (трек удалён / geo / приватный) — ключ по канону owner_audio
    if await _redis_get_track_negative(canon):
        _cache_metrics["negative_hit"] += 1
        return None

    # 2) Redis (основной кэш)
    src = await _redis_get_track_source(canon)
    if src:
        url = src.get("direct_url") or src.get("hls_url")
        if url:
            _cache_metrics["source_hit"] += 1
            return url

    # 3) In-memory fallback
    cached = _url_cache_fallback_get(canon)
    if cached:
        _cache_metrics["source_hit"] += 1
        return cached

    # 4) Запрос к VK: сначала id как в избранном; при пустом ответе — с access_key из Redis meta
    _cache_metrics["source_miss"] += 1
    m = _VK_TRACK_ID_RE.match(raw_id.strip())
    if not m:
        await _redis_set_track_negative(canon)
        return None
    ids_to_try: List[str] = []
    if m.group(3):
        ids_to_try.append(raw_id.strip())
    else:
        ids_to_try.append(raw_id.strip())
        meta = await _redis_get_track_meta(canon)
        ak = str(meta.get("access_key") or "").strip() if meta else ""
        if ak:
            enriched = f"{canon}_{ak}"
            if enriched not in ids_to_try:
                ids_to_try.append(enriched)

    items: List[Dict] = []
    url: Optional[str] = None
    for aud in ids_to_try:
        items = await _vk_batch_get_by_id([aud])
        if items:
            u = items[0].get("url")
            if u:
                url = str(u)
                break
    if not url:
        await _redis_set_track_negative(canon)
        return None
    if _is_hls_url(url):
        await _redis_set_track_source(canon, {"hls_url": url})
    else:
        await _redis_set_track_source(canon, {"direct_url": url})
    _url_cache_fallback_set(canon, url)
    return url


async def vk_get_audio_url(track_id: str) -> Optional[str]:
    """Request coalescing: один in-flight на канонический owner_audio."""
    raw = (track_id or "").strip()
    if not _valid_track_id(raw):
        return None
    canon = _vk_canonical_track_id(raw)
    async with _source_singleflight_lock:
        fut = _source_singleflight.get(canon)
        if fut is None:
            fut = asyncio.ensure_future(_vk_get_audio_url_impl(raw, canon))
            _source_singleflight[canon] = fut
            is_leader = True
        else:
            is_leader = False

    try:
        return await fut
    finally:
        if is_leader:
            async with _source_singleflight_lock:
                _source_singleflight.pop(canon, None)


_VK_LEGACY_YT_FALLBACK_TTL = int(os.getenv("TGPLAY_VK_YT_FALLBACK_TTL", str(30 * 86400)))


def _yt_search_query_from_track_meta(title: str, artist: str) -> Optional[str]:
    t = (title or "").strip()
    a = (artist or "").strip()
    if t and a:
        q = f"{a} {t}"
    elif t:
        q = t
    elif a:
        q = a
    else:
        return None
    q = re.sub(r"\s+", " ", q).strip()
    if len(q) < 2:
        return None
    return q[:500]


async def _redis_get_vk_yt_fallback_video_id(canon_vk_id: str) -> Optional[str]:
    """Уже сопоставленный owner_audio → YouTube videoId (без VK и без поиска)."""
    cache_key = _cache_ns("vk_yt_fb", canon_vk_id)
    redis = await get_redis()
    if not redis:
        return None
    try:
        raw = await redis.get(cache_key)
        if not raw:
            return None
        vid = raw.decode() if isinstance(raw, bytes) else str(raw)
        vid = vid.strip()
        if vid and re.match(r"^[\w-]{11}$", vid):
            return vid
    except Exception:
        pass
    return None


async def _youtube_video_id_for_legacy_vk_track(canon_vk_id: str, title: str, artist: str) -> Optional[str]:
    """Когда VK не отдаёт аудио по owner_audio id (мёртвый токен / удалённый трек): ищем в YouTube Music по метаданным."""
    q = _yt_search_query_from_track_meta(title, artist)
    if not q:
        return None
    cached = await _redis_get_vk_yt_fallback_video_id(canon_vk_id)
    if cached:
        return cached
    cache_key = _cache_ns("vk_yt_fb", canon_vk_id)
    redis = await get_redis()
    try:
        tracks = await asyncio.to_thread(search_youtube_tracks, q, 6)
    except Exception as e:
        logger.debug("youtube fallback search failed: %s", e)
        return None
    if not tracks:
        return None
    for row in tracks:
        vid = extract_video_id(str(row.get("id") or ""))
        if vid and re.match(r"^[\w-]{11}$", vid):
            if redis:
                try:
                    await redis.set(cache_key, vid, ex=_VK_LEGACY_YT_FALLBACK_TTL)
                except Exception:
                    pass
            return vid
    return None


def _batch_source_cache_key(ids: List[str]) -> str:
    """Ключ кэша батча execute (одинаковые 25 id → один ключ)."""
    return _cache_ns("batch", "source", hashlib.sha256(",".join(sorted(ids)).encode()).hexdigest()[:24])


async def vk_batch_get_audio_urls(track_ids: List[str]) -> Dict[str, Optional[str]]:
    """Получить URL для нескольких треков. Кэш: по id, по батчу execute, негативный кэш."""
    result: Dict[str, Optional[str]] = {}
    uncached: List[str] = []

    for tid in track_ids:
        raw = str(tid).strip()
        if not _valid_track_id(raw):
            result[raw] = None
            continue
        canon = _vk_canonical_track_id(raw)
        if await _redis_get_track_negative(canon):
            result[raw] = None
            _cache_metrics["negative_hit"] += 1
            continue
        src = await _redis_get_track_source(canon)
        if src:
            url = src.get("direct_url") or src.get("hls_url")
            if url:
                result[raw] = url
                _cache_metrics["source_hit"] += 1
                continue
        fallback = _url_cache_fallback_get(canon)
        if fallback:
            result[raw] = fallback
            _cache_metrics["source_hit"] += 1
            continue
        uncached.append(raw)
    if not uncached:
        return result

    redis = await get_redis()
    for chunk in _chunks(uncached, 25):
        chunk_sorted = sorted(chunk)
        batch_key = _batch_source_cache_key(chunk_sorted)
        batch_cached: Optional[List[Dict[str, Optional[str]]]] = None
        if redis:
            try:
                raw = await redis.get(batch_key)
                if raw:
                    batch_cached = json.loads(raw)
            except Exception:
                pass
        if batch_cached is not None:
            for rec in batch_cached:
                rid = rec.get("id")
                url = rec.get("url")
                if rid is not None:
                    for o in chunk:
                        if _vk_canonical_track_id(o) == str(rid).strip():
                            result[o] = url
            continue
        items = await _vk_batch_get_by_id(chunk)
        batch_payload: List[Dict[str, Optional[str]]] = []
        retry_pairs: List[Tuple[str, str]] = []
        for tid in chunk:
            canon = _vk_canonical_track_id(tid)
            url = None
            for item in items:
                if f"{item.get('owner_id')}_{item.get('id')}" == canon:
                    url = item.get("url")
                    break
            if url:
                result[tid] = url
                if _is_hls_url(url):
                    await _redis_set_track_source(canon, {"hls_url": url})
                else:
                    await _redis_set_track_source(canon, {"direct_url": url})
                _url_cache_fallback_set(canon, url)
            else:
                # Батч audio.getById без access_key часто не отдаёт url у «закрытых» аудио;
                # одиночный resolve добирает ключ из Redis meta — не ставим negative до этого пути.
                retry_pairs.append((tid, canon))

        async def _batch_retry_one(tid: str, canon: str) -> Optional[str]:
            await _redis_delete_track_negative(canon)
            return await _vk_get_audio_url_impl(tid, canon)

        if retry_pairs:
            retries = await asyncio.gather(*[_batch_retry_one(t, c) for t, c in retry_pairs])
            for (tid, _), retry_url in zip(retry_pairs, retries):
                result[tid] = retry_url

        for tid in chunk:
            canon = _vk_canonical_track_id(tid)
            batch_payload.append({"id": canon, "url": result.get(tid)})
        if redis and batch_payload:
            try:
                await redis.set(batch_key, json.dumps(batch_payload, ensure_ascii=False), ex=min(_TRACK_SOURCE_REDIS_TTL, 7 * 86400))
            except Exception:
                pass
    for tid in uncached:
        if tid not in result:
            result[tid] = None
    return result


def _execute_batch_sizes(total: int) -> List[int]:
    """Разбивает число ID на батчи с вариативными размерами (5–10, 11–18, 19–25).

    Для max 25 ID применяется распределение:
      - 5–10  → ~20%
      - 11–18 → ~40%
      - 19–25 → ~40% (с 10% шансом отправить неполный батч, даже если есть 25)
    Для меньших хвостов распределение подстраивается под оставшееся количество.
    """
    if total <= 0:
        return []
    sizes: List[int] = []
    remaining = total
    while remaining > 0:
        max_step = min(25, remaining)
        if max_step < 5:
            size = remaining
        else:
            r = random.random()
            if max_step >= 19:
                if r < 0.2:
                    size = random.randint(5, 10)
                elif r < 0.6:
                    size = random.randint(11, 18)
                else:
                    # 19–25, но 10% шанса не использовать полный 25
                    if max_step == 25 and random.random() < 0.1:
                        size = random.randint(5, 24)
                    else:
                        size = random.randint(19, max_step)
            elif max_step >= 11:
                if r < 0.2:
                    size = random.randint(5, 10)
                else:
                    size = random.randint(11, max_step)
            else:  # 5–10
                size = random.randint(5, max_step)
        if size > remaining:
            size = remaining
        sizes.append(size)
        remaining -= size
    return sizes


async def _vk_batch_get_by_id(track_ids: List[str]) -> List[Dict]:
    """Batch audio.getById через execute — до 25 треков за 1 API-вызов.

    Добавлен ENTROPY-слой:
      - случайное перемешивание ID;
      - вариативные размеры батчей (5–10, 11–18, 19–25);
      - иногда (10%) неполный батч вместо полного 25.
    """
    if not track_ids:
        return []

    ids = list(dict.fromkeys(track_ids))  # убираем дубли, сохраняем порядок
    random.shuffle(ids)
    total = len(ids)
    sizes = _execute_batch_sizes(total)
    all_items: List[Dict] = []
    idx = 0

    for size in sizes:
        batch = ids[idx : idx + size]
        idx += size
        if not batch:
            continue
        audios_str = ",".join(batch)
        code = f'return API.audio.getById({{"audios":"{_vk_escape(audios_str)}"}});'
        data = await _vk_api_call("execute", {"code": code}, post=True)
        if "error" in data:
            items = await _vk_get_by_id_fallback(batch)
        else:
            response = data.get("response", [])
            items = response if isinstance(response, list) else []
        if items:
            all_items.extend(items)

    return all_items


async def _vk_get_by_id_fallback(track_ids: List[str]) -> List[Dict]:
    """Fallback: обычный audio.getById."""
    data = await _vk_api_call("audio.getById", {"audios": ",".join(track_ids)})
    if "error" in data:
        return []
    return data.get("response", [])


_SEARCH_CACHE_COVER_BACKFILL = os.getenv("SEARCH_CACHE_COVER_BACKFILL", "1").strip() != "0"
_SEARCH_CACHE_COVER_BACKFILL_MAX = max(0, min(200, int(os.getenv("SEARCH_CACHE_COVER_BACKFILL_MAX", "100"))))


async def _search_backfill_covers_cached(
    tracks: List[Dict],
    *,
    redis,
    redis_key: Optional[str],
    cache_ts: float,
) -> None:
    """Кэш мог сохранить выдачу без обложек (старый код / пустой getById) — догружаем через audio.getById."""
    if not tracks or not _SEARCH_CACHE_COVER_BACKFILL or _SEARCH_CACHE_COVER_BACKFILL_MAX <= 0:
        return
    if not any(t.get("id") and not str(t.get("cover_url") or "").strip() for t in tracks):
        return
    await _vk_enrich_tracks_album_covers_via_get_by_id(tracks, max_tracks=_SEARCH_CACHE_COVER_BACKFILL_MAX)
    if redis is not None and redis_key:
        try:
            ts_out = cache_ts if cache_ts > 0 else time.time()
            payload = json.dumps({"items": tracks, "ts": ts_out}, ensure_ascii=False)
            await redis.set(redis_key, payload, ex=_SEARCH_CACHE_TTL)
        except Exception as e:
            print(f"⚠️ Redis search cover backfill set: {e}")


async def _vk_enrich_tracks_album_covers_via_get_by_id(
    tracks: List[Dict],
    *,
    max_tracks: Optional[int] = None,
) -> None:
    """Дописать cover_url (и при необходимости genre) из audio.getById.

    После audio.search (в т.ч. Kate) те же owner_id_audio_id запрашиваются через getById:
    в выдаче поиска album/thumb часто пустой, в getById — нормальные метаданные и обложки.
    """
    if not tracks:
        return
    missing = [t for t in tracks if t.get("id") and not (str(t.get("cover_url") or "").strip())]
    if max_tracks is not None and max_tracks > 0:
        missing = missing[:max_tracks]
    if not missing:
        return
    ids: List[str] = []
    for t in missing:
        tid = str(t["id"]).strip()
        if not tid:
            continue
        ak = str(t.get("access_key") or "").strip()
        ids.append(f"{tid}_{ak}" if ak else tid)
    raw_items = await _vk_batch_get_by_id(ids)
    if not raw_items:
        return
    by_id = {t["id"]: t for t in _parse_tracks(raw_items) if t.get("id")}
    for t in tracks:
        tid = t.get("id")
        if not tid or (str(t.get("cover_url") or "").strip()):
            continue
        src = by_id.get(tid)
        if not src:
            continue
        cu = str(src.get("cover_url") or "").strip()
        if cu:
            t["cover_url"] = cu
        if t.get("genre_id") is None and src.get("genre_id") is not None:
            t["genre_id"] = src["genre_id"]
        if not (t.get("genre_label") or "").strip() and (src.get("genre_label") or "").strip():
            t["genre_label"] = src["genre_label"]


def _chunks(lst: List, n: int):
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


async def _vk_get_track_info_impl(track_id: str) -> Optional[Dict]:
    now = time.time()
    meta = await _redis_get_track_meta(track_id)
    if meta:
        _cache_metrics["meta_hit"] += 1
        return meta
    if track_id in _track_info_cache:
        cached = _track_info_cache[track_id]
        if now - cached[1] < _TRACK_INFO_TTL:
            _track_info_cache.move_to_end(track_id)
            _cache_metrics["meta_hit"] += 1
            return cached[0]
    _cache_metrics["meta_miss"] += 1
    items = await _vk_batch_get_by_id([track_id])
    if not items:
        return None
    parsed = _parse_tracks(items)
    if not parsed:
        return None
    meta = parsed[0]
    _track_info_cache[track_id] = (meta, now)
    _track_info_cache.move_to_end(track_id)
    while len(_track_info_cache) > _TRACK_INFO_MAX:
        _track_info_cache.popitem(last=False)
    await _redis_set_track_meta(track_id, meta)
    return meta


async def vk_get_track_info(track_id: str) -> Optional[Dict]:
    """Метаданные трека. Request coalescing: один in-flight на track_id."""
    if not _valid_track_id(track_id):
        return None
    async with _meta_singleflight_lock:
        fut = _meta_singleflight.get(track_id)
        if fut is None:
            fut = asyncio.ensure_future(_vk_get_track_info_impl(track_id))
            _meta_singleflight[track_id] = fut
            is_leader = True
        else:
            is_leader = False
    try:
        return await fut
    finally:
        if is_leader:
            async with _meta_singleflight_lock:
                _meta_singleflight.pop(track_id, None)


async def _youtube_resolve_meta_for_api(video_id: str) -> Optional[Dict]:
    full_url = f"https://www.youtube.com/watch?v={video_id}"
    for redis_key in (full_url, video_id):
        meta = await _redis_get_track_meta(redis_key)
        if meta:
            return meta
    loop = asyncio.get_event_loop()
    meta = await loop.run_in_executor(None, fetch_youtube_track_meta_sync, video_id)
    if not meta:
        return None
    await _redis_set_track_meta(full_url, meta)
    await _redis_set_track_meta(video_id, meta)
    return meta


def _sc_ready() -> bool:
    return SC_CLIENT.configured


async def _sc_search_tracks(query: str, limit: int, offset: int) -> List[Dict]:
    if not _sc_ready():
        return []
    session = await get_session()
    return await SC_CLIENT.search_tracks(session, query, limit=limit, offset=offset)


async def _sc_search_tracks_multi(query: str, limit: int, offset: int, *, max_pages: int = 3) -> List[Dict]:
    """Aggressive SC search: pull multiple pages and dedupe, up to limit."""
    if not _sc_ready():
        return []
    session = await get_session()
    out: List[Dict] = []
    seen: Set[str] = set()
    # Page 1: uses offset; next pages use internal pagination in SC client via /resolve? (not exposed here),
    # so we approximate by offset stepping (SC supports offset on /tracks, and we cap pages to keep it light).
    step = 100
    pages = 0
    cur_off = max(0, int(offset))
    while len(out) < limit and pages < max_pages:
        pages += 1
        chunk = await SC_CLIENT.search_tracks(session, query, limit=min(step, limit - len(out)), offset=cur_off)
        if not chunk:
            break
        for t in chunk:
            tid = str((t or {}).get("id") or "").strip()
            if not tid or tid in seen:
                continue
            seen.add(tid)
            out.append(t)
            if len(out) >= limit:
                break
        if len(chunk) < min(step, limit - len(out) + 1):
            break
        cur_off += step
    return out[:limit]


async def _sc_artist_catalog_tracks(artist_query: str, limit: int) -> List[Dict]:
    if not _sc_ready():
        return []
    session = await get_session()
    eff_limit = min(900, max(1, int(limit)))
    q = unicodedata.normalize("NFKC", (artist_query or "").strip()).lower().strip()[:120]
    if not q:
        return []
    redis = await get_redis()
    user_key = _cache_ns("sc", "artist_user_v2", q)
    tracks_key = _cache_ns("sc", "artist_tracks_v2", q, str(eff_limit))
    if redis is not None:
        try:
            cached = await _redis_get_json(tracks_key)
            if isinstance(cached, dict) and isinstance(cached.get("items"), list):
                return cached.get("items") or []
        except Exception:
            pass
    uid: Optional[int] = None
    if redis is not None:
        try:
            u = await _redis_get_json(user_key)
            if isinstance(u, dict) and isinstance(u.get("user_id"), int):
                uid = int(u["user_id"])
        except Exception:
            pass
    if uid is None:
        uid = await SC_CLIENT.resolve_artist_user_id(session, artist_query)
        if uid is None:
            return []
        if redis is not None:
            try:
                await _redis_set_json(user_key, {"user_id": uid, "ts": time.time()}, ex=86400)
            except Exception:
                pass
    tracks = await SC_CLIENT.artist_catalog_tracks(session, artist_query, limit=eff_limit)
    if redis is not None:
        try:
            await _redis_set_json(tracks_key, {"items": tracks, "ts": time.time()}, ex=6 * 3600)
        except Exception:
            pass
    return tracks


async def _sc_resolve_url(track_id: str) -> Optional[str]:
    sid = parse_soundcloud_track_id(track_id)
    if sid is None or not _sc_ready():
        return None
    session = await get_session()
    return await SC_CLIENT.resolve_stream_url(session, sid)


async def _sc_track_meta(track_id: str) -> Optional[Dict]:
    sid = parse_soundcloud_track_id(track_id)
    if sid is None or not _sc_ready():
        return None
    cached = await _redis_get_track_meta(track_id)
    if cached:
        return cached
    session = await get_session()
    raw = await SC_CLIENT.get_track(session, sid)
    if not raw:
        return None
    from sc_client_simple import normalize_track

    meta = normalize_track(raw)
    if not meta.get("id"):
        return None
    if not meta.get("cover_url"):
        meta["cover_url"] = raw.get("artwork_url") or (
            ((raw.get("user") or {}) if isinstance(raw.get("user"), dict) else {}).get("avatar_url")
        )
    await _redis_set_track_meta(track_id, meta)
    return meta


async def _sc_related_tracks(seed_ids: List[str], limit: int) -> List[Dict]:
    if not _sc_ready():
        return []
    session = await get_session()
    unique: List[int] = []
    for sid_raw in seed_ids:
        sid = parse_soundcloud_track_id(sid_raw)
        if sid is None or sid in unique:
            continue
        unique.append(sid)
        if len(unique) >= 3:
            break
    if not unique:
        return []
    per_seed = max(10, min(60, int(limit * 1.6 / max(1, len(unique)))))
    chunks = await asyncio.gather(*[SC_CLIENT.related_tracks(session, sid, limit=per_seed) for sid in unique], return_exceptions=True)
    merged: List[Dict] = []
    seen: Set[str] = set()
    for chunk in chunks:
        if isinstance(chunk, Exception):
            continue
        if not isinstance(chunk, list):
            continue
        for t in chunk:
            tid = str(t.get("id") or "").strip()
            if not tid or tid in seen or tid in seed_ids:
                continue
            seen.add(tid)
            merged.append(t)
            if len(merged) >= limit:
                return merged
    return merged[:limit]


async def _sc_personal_related(
    seed_ids: List[str],
    limit: int,
    exclude_ids: Optional[Set[str]] = None,
) -> List[Dict]:
    """
    Персональная лента из SoundCloud related по нескольким seed (последние добавленные треки).
    Отличия от `_sc_related_tracks`:
      • до 5 seed (а не 3) — шире покрытие вкуса по последним добавленным;
      • большой пул related с запасом, затем случайная выборка `limit` (свежая лента при каждом входе);
      • исключаются сами seed и уже добавленные в избранное (exclude_ids).
    """
    if not _sc_ready():
        return []
    session = await get_session()
    exclude = {str(x).strip() for x in (exclude_ids or set())}
    unique: List[int] = []
    seed_str: Set[str] = set()
    for sid_raw in seed_ids:
        sid = parse_soundcloud_track_id(sid_raw)
        if sid is None or sid in unique:
            continue
        unique.append(sid)
        seed_str.add(str(sid))
        seed_str.add(build_soundcloud_track_id(sid))
        if len(unique) >= 5:
            break
    if not unique:
        return []
    # Пул с запасом, чтобы было из чего случайно выбирать (свежесть при каждом заходе).
    pool_target = max(limit * 3, limit + 60)
    per_seed = max(20, min(100, int(pool_target / max(1, len(unique)))))
    chunks = await asyncio.gather(
        *[SC_CLIENT.related_tracks(session, sid, limit=per_seed) for sid in unique],
        return_exceptions=True,
    )
    pool: List[Dict] = []
    seen: Set[str] = set()
    for chunk in chunks:
        if isinstance(chunk, Exception) or not isinstance(chunk, list):
            continue
        for t in chunk:
            tid = str(t.get("id") or "").strip()
            if not tid or tid in seen or tid in seed_str or tid in exclude:
                continue
            c = build_soundcloud_track_id(parse_soundcloud_track_id(tid) or 0)
            if c in exclude:
                continue
            seen.add(tid)
            pool.append(t)
    if not pool:
        return []
    rng = random.Random(secrets.randbelow(2**31))
    rng.shuffle(pool)
    return pool[:limit]


def _disliked_track_ids_sync(uid: int) -> List[str]:
    """Синхронное чтение дизлайков (для asyncio.to_thread, чтобы не блокировать event loop)."""
    try:
        import analytics_db

        analytics_db.init_db()
        return analytics_db.get_disliked_track_ids(int(uid))
    except Exception:
        return []


def _bot_audio_delivered_sync(uid_int: int) -> tuple[List[str], List[str]]:
    """Синхронное чтение «доставлено в бота» (для asyncio.to_thread; эндпоинт часто опрашивается)."""
    import analytics_db

    analytics_db.init_db()
    return (
        analytics_db.get_bot_audio_delivered_track_ids(uid_int),
        analytics_db.get_bot_audio_delivered_verified_live_track_ids(uid_int),
    )


async def _get_track_meta_unified(track_id: str) -> Optional[Dict]:
    canon = _canonical_share_track_id(track_id)
    if not canon:
        return None
    if is_soundcloud_track_id(canon):
        return await _sc_track_meta(canon)
    if _valid_track_id(canon):
        return await vk_get_track_info(canon)
    if _is_youtube_video_id(canon):
        return await _youtube_resolve_meta_for_api(canon)
    return None


# ─── ffmpeg streaming (оптимизированный) ─────────────────────────

async def ffmpeg_stream_mp3(source_url: str):
    """HLS→MP3 стриминг: ручная загрузка сегментов + remux (без перекодировки)."""
    if _is_hls_url(source_url):
        raw_data = await _download_hls_segments(source_url)
        if raw_data:
            cmd = [
                FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
                "-i", "pipe:0", "-vn", "-c:a", "copy",
                "-fflags", "+flush_packets", "-f", "mp3", "pipe:1",
            ]
            proc = await asyncio.create_subprocess_exec(
                *cmd, stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )

            async def _feed():
                try:
                    proc.stdin.write(raw_data)
                    await proc.stdin.drain()
                    proc.stdin.close()
                    await proc.stdin.wait_closed()
                except Exception:
                    pass

            asyncio.create_task(_feed())

            try:
                while True:
                    chunk = await proc.stdout.read(32 * 1024)
                    if not chunk:
                        break
                    yield chunk
            finally:
                if proc.returncode is None:
                    proc.kill()
                await proc.wait()
            return

    # ── Fallback: ffmpeg с прямым URL (не-HLS) ──────────────────────────────
    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "error",
        "-fflags", "+nobuffer+fastseek+discardcorrupt",
        "-analyzeduration", "2000000", "-probesize", "2000000",
        "-user_agent", VK_USER_AGENT, "-i", source_url,
        "-vn", "-c:a", "copy",
        "-fflags", "+flush_packets", "-f", "mp3", "pipe:1",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        while True:
            chunk = await proc.stdout.read(32 * 1024)
            if not chunk:
                break
            yield chunk
    finally:
        if proc.returncode is None:
            proc.kill()
        await proc.wait()


# ─── Routes ──────────────────────────────────────────────────────

def _cache_in_flight_count() -> int:
    return (
        len(_search_singleflight)
        + len(_source_singleflight)
        + len(_meta_singleflight)
        + len(_rec_singleflight)
    )


@app.get("/api/status")
async def api_status():
    redis = await get_redis()
    search_total = _cache_metrics["search_hit"] + _cache_metrics["search_miss"]
    source_total = _cache_metrics["source_hit"] + _cache_metrics["source_miss"]
    meta_total = _cache_metrics["meta_hit"] + _cache_metrics["meta_miss"]
    return {
        "status": "online",
        "message": "TGPlay Lite API",
        "redis_connected": redis is not None,
        "cache": {
            "version": CACHE_VERSION,
            "in_flight": _cache_in_flight_count(),
            "search": {
                "hit": _cache_metrics["search_hit"],
                "miss": _cache_metrics["search_miss"],
                "ratio": _cache_metrics["search_hit"] / search_total if search_total > 0 else None,
                "avg_ttl_age_sec": (
                    _cache_metrics["search_age_sum"] / _cache_metrics["search_age_count"]
                    if _cache_metrics["search_age_count"] > 0
                    else None
                ),
            },
            "source": {
                "hit": _cache_metrics["source_hit"],
                "miss": _cache_metrics["source_miss"],
                "ratio": _cache_metrics["source_hit"] / source_total if source_total > 0 else None,
            },
            "meta": {
                "hit": _cache_metrics["meta_hit"],
                "miss": _cache_metrics["meta_miss"],
                "ratio": _cache_metrics["meta_hit"] / meta_total if meta_total > 0 else None,
            },
            "negative_hit": _cache_metrics["negative_hit"],
        },
    }


def _search_cache_key(q: str, limit: int, offset: int = 0, artist_catalog: bool = False) -> str:
    """Нормализованный ключ поиска: lowerCase, trim, двойные пробелы в один, без спецсимволов по краям.
    Eminem / eminem / Eminem - дают один ключ."""
    s = unicodedata.normalize("NFKC", q.strip()[:100])
    s = re.sub(r"\s+", " ", s).strip()
    s = s.casefold()
    # Убрать ведущие/замыкающие спецсимволы (пунктуация, скобки)
    s = re.sub(r"^[\W_]+|[\W_]+$", "", s)
    if not s:
        s = "_"
    ac = ":ac" if artist_catalog else ""
    return f"{s}:{offset}:{limit}{ac}"


# Каталог исполнителя: несколько страниц VK audio.search (sort=популярность), максимум треков за один HTTP-запрос
_ARTIST_CATALOG_MAX = max(100, min(900, int(os.getenv("ARTIST_CATALOG_MAX_TRACKS", "600"))))


def _artist_matches_catalog_query(track_artist: str, query_artist: str) -> bool:
    """Оставить треки, у которых поле artist соответствует запросу (имя исполнителя)."""
    qn = _normalize_for_match(query_artist)
    an = _normalize_for_match(track_artist)
    if len(qn) < 2 or not an:
        return False
    if qn == an or qn in an or an in qn:
        return True
    q_words = _meaningful_words(query_artist)
    if not q_words:
        return qn in an
    return all(w in an for w in q_words)


async def _vk_search_artist_catalog(artist_query: str, max_total: int) -> List[Dict]:
    """Все доступные треки по запросу «имя артиста»: VK sort=2 (популярные первыми), фильтр по artist."""
    q = _fix_common_typos(artist_query.strip())
    if len(q) < 2:
        return []
    max_total = max(50, min(max_total, _ARTIST_CATALOG_MAX))
    all_items: List[Dict] = []
    seen_ids: set = set()
    page_size = 300

    def _consume_batch(batch: List[Dict]) -> None:
        for item in batch:
            try:
                tid = f"{item['owner_id']}_{item['id']}"
            except Exception:
                continue
            if tid in seen_ids:
                continue
            ta = str(item.get("artist", "") or "")
            if not _artist_matches_catalog_query(ta, q):
                continue
            seen_ids.add(tid)
            all_items.append(item)

    # Первые три окна VK параллельно — один RTT вместо трёх подряд (заметно на каталоге исполнителя).
    first_chunks = await asyncio.gather(
        _vk_search_raw_fallback(q, page_size, 0, sort=2),
        _vk_search_raw_fallback(q, page_size, page_size, sort=2),
        _vk_search_raw_fallback(q, page_size, page_size * 2, sort=2),
    )
    for batch in first_chunks:
        _consume_batch(batch)
        if len(all_items) >= max_total:
            return all_items[:max_total]

    offset = page_size * 3
    while len(all_items) < max_total:
        need = min(page_size, max_total - len(all_items))
        batch = await _vk_search_raw_fallback(q, need, offset, sort=2)
        if not batch:
            break
        _consume_batch(batch)
        if len(batch) < need:
            break
        offset += len(batch)
        if offset > 2000:
            break
    return all_items[:max_total]


# Ответ поиска не кэшируем в браузере — чтобы каждый запрос шёл на сервер и все пользователи получали общий кэш из Redis.
_SEARCH_RESPONSE_HEADERS = {"Cache-Control": "no-store, no-cache, must-revalidate", "Pragma": "no-cache", "Expires": "0"}


@app.get("/api/music/recommendations")
async def api_music_recommendations(
    seed: Optional[str] = Query(None, description="Один VK track id: owner_id_audio_id"),
    seeds: Optional[str] = Query(None, description="До 3 id через запятую (приоритетнее seed)"),
    limit: int = Query(20, ge=1, le=120, description="Max items"),
):
    """Рекомендации по треку(ам): кэш по каждому seed; seeds= — merge round-robin до limit."""
    seed_ids: List[str] = []
    if seeds and seeds.strip():
        for p in seeds.split(","):
            p = p.strip()
            if _valid_track_id(p) and p not in seed_ids:
                seed_ids.append(p)
            if len(seed_ids) >= 3:
                break
    elif seed and seed.strip():
        s = seed.strip()
        if _valid_track_id(s):
            seed_ids = [s]
    if not seed_ids:
        raise HTTPException(400, "Invalid or missing seed / seeds (up to 3 track ids)")

    if _sc_ready():
        sc_seed_ids = [build_soundcloud_track_id(parse_soundcloud_track_id(sid)) for sid in seed_ids if parse_soundcloud_track_id(sid) is not None]
        if not sc_seed_ids:
            raise HTTPException(400, "SoundCloud seed id expected (format sc:<id>)")
        rec_items = await _sc_related_tracks(sc_seed_ids, limit)
        _fire_cache_track_meta_items(rec_items)
        return Response(
            content=json.dumps({"items": rec_items, "source": "soundcloud_related"}, ensure_ascii=False),
            media_type="application/json",
            headers={**_SEARCH_RESPONSE_HEADERS, "X-Rec-Cache": "soundcloud"},
        )

    if len(seed_ids) > 1:
        full, src = await _rec_merge_for_seeds(seed_ids, limit)
        _fire_cache_track_meta_items(full)
        _api_tracks_mark_modern_resolve(full)
        return Response(
            content=json.dumps({"items": full, "source": src}, ensure_ascii=False),
            media_type="application/json",
            headers={**_SEARCH_RESPONSE_HEADERS, "X-Rec-Cache": "fill"},
        )

    seed_one = seed_ids[0]
    hit = await _rec_read_cached_full(seed_one)
    if hit:
        full, src = hit
        out = _rec_response_items(full, seed_one, limit)
        _fire_cache_track_meta_items(out)
        _api_tracks_mark_modern_resolve(out)
        return Response(
            content=json.dumps({"items": out, "source": src}, ensure_ascii=False),
            media_type="application/json",
            headers={**_SEARCH_RESPONSE_HEADERS, "X-Rec-Cache": "hit"},
        )

    async with _rec_singleflight_lock:
        fut = _rec_singleflight.get(seed_one)
        if fut is None:
            loop = asyncio.get_running_loop()
            fut = loop.create_future()
            _rec_singleflight[seed_one] = fut
            is_leader = True
        else:
            is_leader = False

    if is_leader:
        try:
            hit2 = await _rec_read_cached_full(seed_one)
            if hit2:
                result = hit2
            else:
                result = await _rec_leader_fill(seed_one)
            fut.set_result(result)
        except Exception as exc:
            fut.set_exception(exc)
            raise
        finally:
            async with _rec_singleflight_lock:
                _rec_singleflight.pop(seed_one, None)
        full, src = result
    else:
        full, src = await fut

    out = _rec_response_items(full, seed_one, limit)
    _fire_cache_track_meta_items(out)
    _api_tracks_mark_modern_resolve(out)
    return Response(
        content=json.dumps({"items": out, "source": src}, ensure_ascii=False),
        media_type="application/json",
        headers={**_SEARCH_RESPONSE_HEADERS, "X-Rec-Cache": "fill"},
    )


@app.get("/api/music/recommendations/personal")
async def api_music_recommendations_personal(
    authorization: Optional[str] = Header(None),
    limit: int = Query(100, ge=1, le=150),
    refresh: int = Query(0, ge=0, le=1, description="1 — другая выборка seed по избранному"),
    wave: int = Query(0, ge=0, le=1, description="1 — длинная лента (мин. 80 треков), для «Моя волна»"),
):
    """
    Персональные рекомендации. Требует `Authorization: tma …` или `Bearer …`.

    При настроенном SoundCloud (`_sc_ready()`, основной путь на проде): seed = последние
    5 добавленных в избранное треков (8 для «Моей волны»), related по каждому, пул
    перемешивается случайно при каждом запросе. Исключаются уже добавленные и дизлайкнутые.
    Параметр `refresh` для SC не нужен (выдача и так свежая на каждый запрос); `wave`
    увеличивает лимит и число seed.

    Legacy-ветка (VK/YTM, `_sc_ready()` == False) сохранена для совместимости/тестов.
    """
    user = get_user_from_header(authorization)
    uid = int(user["id"])
    eff_limit = min(150, limit)
    if wave and eff_limit < 80:
        eff_limit = 80
    if _sc_ready():
        # Файловые/SQLite чтения выносим в thread-пул: синхронный I/O в async-хендлере
        # на каждый запрос рекомендаций иначе блокирует event loop.
        fav_tracks = await asyncio.to_thread(load_playlist, uid)
        custom_playlists = await asyncio.to_thread(load_custom_playlists, uid)
        disliked_raw = await asyncio.to_thread(_disliked_track_ids_sync, uid)
        # seed = ПОСЛЕДНИЕ добавленные треки (избранное append-ит в конец списка),
        # новейшие первыми; для «Моей волны» берём больше якорей.
        seed_cap = 8 if wave else 5
        # Множество SC-id, которые НЕ рекомендуем: уже в избранном + дизлайкнутые.
        fav_sc_ids: Set[str] = set()
        for d in disliked_raw:
            d_sid = parse_soundcloud_track_id(str(d))
            if d_sid is not None:
                fav_sc_ids.add(build_soundcloud_track_id(d_sid))
                fav_sc_ids.add(str(d_sid))
        seed_ids: List[str] = []
        for t in reversed(fav_tracks):
            tid = _playlist_library_track_id_stored(str((t or {}).get("id") or ""))
            sid = parse_soundcloud_track_id(tid)
            if sid is None:
                continue
            c = build_soundcloud_track_id(sid)
            fav_sc_ids.add(c)
            fav_sc_ids.add(str(sid))
            if len(seed_ids) < seed_cap and c not in seed_ids:
                seed_ids.append(c)
        if len(seed_ids) < seed_cap:
            for pl in custom_playlists:
                for t in reversed(pl.get("tracks") or []):
                    tid = _playlist_library_track_id_stored(str((t or {}).get("id") or ""))
                    sid = parse_soundcloud_track_id(tid)
                    if sid is None:
                        continue
                    c = build_soundcloud_track_id(sid)
                    if c not in seed_ids:
                        seed_ids.append(c)
                    if len(seed_ids) >= seed_cap:
                        break
                if len(seed_ids) >= seed_cap:
                    break
        if not seed_ids:
            return Response(
                content=json.dumps({"items": [], "source": "soundcloud_personal_empty"}, ensure_ascii=False),
                media_type="application/json",
                headers={**_SEARCH_RESPONSE_HEADERS, "X-Rec-Cache": "personal"},
            )
        items = await _sc_personal_related(seed_ids, eff_limit, exclude_ids=fav_sc_ids)
        _fire_cache_track_meta_items(items)
        return Response(
            content=json.dumps({"items": items, "source": "soundcloud_personal_related"}, ensure_ascii=False),
            media_type="application/json",
            headers={**_SEARCH_RESPONSE_HEADERS, "X-Rec-Cache": "personal"},
        )
    do_wave = bool(wave)
    do_refresh = bool(refresh)
    # Каждая выдача: новая перестановка опорных seed (тот же VK-кэш по seed, без лишних вызовов API)
    refresh_salt = secrets.randbelow(2**31)
    items, src = await _rec_personal_blend_for_user(
        uid,
        eff_limit,
        refresh_salt=refresh_salt,
        wave=do_wave,
        refresh=do_refresh,
    )
    _fire_cache_track_meta_items(items)
    return Response(
        content=json.dumps({"items": _rec_strip_internal_track_fields(items), "source": src}, ensure_ascii=False),
        media_type="application/json",
        headers={**_SEARCH_RESPONSE_HEADERS, "X-Rec-Cache": "personal"},
    )


@app.get("/api/music/search")
async def search(
    request: Request,
    q: str = Query(..., description="Search query"),
    # le=900: каталог исполнителя запрашивает до 600+; обычный поиск ниже режем до 300
    limit: int = Query(10, ge=1, le=900, description="Max results"),
    offset: int = Query(0, ge=0, description="Skip first N results"),
    artist_catalog: int = Query(0, ge=0, le=1, description="1 — все треки исполнителя, VK sort по популярности"),
):
    trimmed = unicodedata.normalize("NFKC", q.strip())
    is_ac = artist_catalog == 1
    min_chars = 2 if is_ac else 3
    if not trimmed or len(trimmed) < min_chars:
        raise HTTPException(400, f"Query too short (min {min_chars} chars)")
    eff_limit = min(limit, _ARTIST_CATALOG_MAX) if is_ac else min(limit, 300)
    eff_offset = 0 if is_ac else offset
    key = _search_cache_key(trimmed, eff_limit, eff_offset, is_ac)
    now = time.time()
    extra_headers = {**_SEARCH_RESPONSE_HEADERS, **({"X-Artist-Catalog": "1"} if is_ac else {})}

    if _sc_ready():
        redis = await get_redis()
        if is_ac:
            redis_key = _cache_ns("search_sc_artist_v1", key)
        else:
            redis_key = _cache_ns("search_sc_playable_v4", key)
        if redis is not None:
            try:
                cached = await redis.get(redis_key)
                if cached:
                    data = json.loads(cached)
                    if isinstance(data, dict) and isinstance(data.get("items"), list):
                        items = data.get("items") or []
                        _fire_cache_track_meta_items(items)
                        return Response(
                            content=json.dumps({"items": items}, ensure_ascii=False),
                            media_type="application/json",
                            headers={**extra_headers, "X-Search-Cache": "redis"},
                        )
            except Exception:
                pass
        try:
            if is_ac:
                tracks_live = await _sc_artist_catalog_tracks(trimmed, eff_limit)
            else:
                # Maximize results by mixing multiple SC sources for the same query
                sc_q = trimmed
                sc_tr = _translit_ru_to_lat(trimmed)
                qt = [x for x in _tokens(trimmed) if x]
                tasks = [
                    _sc_search_tracks_multi(sc_q, eff_limit, eff_offset, max_pages=3),
                ]
                if sc_tr and sc_tr != sc_q:
                    tasks.append(_sc_search_tracks_multi(sc_tr, eff_limit, eff_offset, max_pages=2))
                # For artist-like queries, also pull artist catalog and merge.
                if len(sc_q.split()) == 1:
                    tasks.append(_sc_artist_catalog_tracks(sc_q, min(600, eff_limit)))
                # For multiword queries: also search the first token alone (often artist) both as
                # plain search and artist-catalog, plus the remaining tokens. We MERGE everything and
                # only RANK (never drop) so recall stays maximal.
                if len(qt) >= 2:
                    first_tok = qt[0]
                    tasks.append(_sc_search_tracks_multi(first_tok, eff_limit, 0, max_pages=2))
                    tasks.append(_sc_artist_catalog_tracks(first_tok, min(600, eff_limit)))
                    rest_q = " ".join(qt[1:])
                    if rest_q:
                        tasks.append(_sc_search_tracks_multi(rest_q, eff_limit, 0, max_pages=2))
                        rest_tr = _translit_ru_to_lat(rest_q)
                        if rest_tr and rest_tr != rest_q:
                            tasks.append(_sc_search_tracks_multi(rest_tr, eff_limit, 0, max_pages=2))
                chunks = await asyncio.gather(*tasks, return_exceptions=True)
                # Collect a generous pool (more than eff_limit) so ranking has material to work with.
                pool_cap = max(eff_limit, 400)
                merged: List[Dict] = []
                seen: Set[str] = set()
                for ch in chunks:
                    if isinstance(ch, Exception) or not isinstance(ch, list):
                        continue
                    for t in ch:
                        tid = str((t or {}).get("id") or "").strip()
                        if not tid or tid in seen:
                            continue
                        seen.add(tid)
                        merged.append(t)
                    if len(merged) >= pool_cap:
                        break
                # Rank (do NOT drop) for multiword: float tracks matching more query tokens to the top.
                if len(qt) >= 2:
                    qvariants = set(qt)
                    qvariants.update(x for x in _tokens(_translit_ru_to_lat(trimmed)) if x)
                    first_variants = {qt[0]}
                    _ftr = _tokens(_translit_ru_to_lat(qt[0]))
                    if _ftr:
                        first_variants.add(_ftr[0])

                    def _rank(t: Dict) -> int:
                        blob = f"{t.get('artist') or ''} {t.get('title') or ''}"
                        hb = set(_tokens(blob))
                        score = sum(1 for x in qvariants if x in hb)
                        if first_variants & hb:
                            score += 5  # strong boost when the (probable) artist token matches
                        return score

                    merged.sort(key=_rank, reverse=True)
                tracks_live = merged[:eff_limit]
        except Exception as e:
            raise HTTPException(502, f"SoundCloud search failed: {str(e)[:120]}")
        if tracks_live and redis is not None:
            try:
                await redis.set(
                    redis_key,
                    json.dumps({"items": tracks_live, "ts": time.time()}, ensure_ascii=False),
                    ex=min(_SEARCH_CACHE_TTL, 900),
                )
            except Exception:
                pass
        if tracks_live:
            _fire_cache_track_meta_items(tracks_live)
        return Response(
            content=json.dumps({"items": tracks_live}, ensure_ascii=False),
            media_type="application/json",
            headers={**extra_headers, "X-Search-Cache": "live"},
        )

    redis_key = _cache_ns("search", key)
    lock_key = _cache_ns("lock", "search", key)

    # ─── Сначала пробуем Redis (глобальная библиотека поиска) + SWR ───
    redis = await get_redis()
    if redis is not None:
        try:
            cached = await redis.get(redis_key)
            if cached:
                try:
                    data = json.loads(cached)
                except Exception:
                    await redis.delete(redis_key)
                else:
                    tracks_cached: Optional[List[Dict]] = None
                    ts = 0.0
                    if isinstance(data, dict) and "items" in data:
                        tracks_cached = data.get("items") or []
                        ts_raw = data.get("ts")
                        try:
                            ts = float(ts_raw) if ts_raw is not None else 0.0
                        except (TypeError, ValueError):
                            ts = 0.0
                    elif isinstance(data, list):
                        tracks_cached = data
                        ts = 0.0
                    if tracks_cached is not None:
                        age = now - ts if ts > 0 else 0.0
                        _cache_metrics["search_hit"] += 1
                        _cache_metrics["search_age_sum"] += age
                        _cache_metrics["search_age_count"] += 1
                        # SWR: после SOFT TTL отдаём кэш и обновляем в фоне
                        if ts > 0 and _SEARCH_CACHE_SOFT_TTL > 0 and age >= _SEARCH_CACHE_SOFT_TTL and age < _SEARCH_CACHE_TTL:
                            async def _refresh():
                                try:
                                    if is_ac:
                                        raw = await _vk_search_artist_catalog(trimmed, eff_limit)
                                        fresh = _parse_tracks(raw)
                                        await _vk_enrich_tracks_album_covers_via_get_by_id(fresh, max_tracks=100)
                                    else:
                                        fresh = await _vk_search_for_http(trimmed, offset=eff_offset, limit=eff_limit)
                                except Exception:
                                    return
                                if not fresh:
                                    return
                                payload = json.dumps({"items": fresh, "ts": time.time()}, ensure_ascii=False)
                                try:
                                    await redis.set(redis_key, payload, ex=_SEARCH_CACHE_TTL)
                                except Exception as e:
                                    print(f"⚠️ Redis search refresh set error: {e}")
                                _search_cache[key] = (fresh, time.time())
                                _search_cache.move_to_end(key)
                            _safe_ensure_future(_refresh())

                        if tracks_cached:
                            await _search_backfill_covers_cached(
                                tracks_cached, redis=redis, redis_key=redis_key, cache_ts=ts
                            )
                            _fire_cache_track_meta_items(tracks_cached)
                        _api_tracks_mark_modern_resolve(tracks_cached)
                        return Response(
                            content=json.dumps({"items": tracks_cached}, ensure_ascii=False),
                            media_type="application/json",
                            headers={**extra_headers, "X-Search-Cache": "redis"},
                        )
        except Exception as e:
            print(f"⚠️ Redis search get error: {e}")

    # ─── Затем in-memory кэш (LRU) ───
    if key in _search_cache:
        cached_tracks, ts = _search_cache[key]
        if now - ts < _SEARCH_CACHE_TTL:
            _search_cache.move_to_end(key)
            _cache_metrics["search_hit"] += 1
            _cache_metrics["search_age_sum"] += now - ts
            _cache_metrics["search_age_count"] += 1
            if cached_tracks:
                rk_mem = _cache_ns("search", key) if redis is not None else None
                await _search_backfill_covers_cached(
                    cached_tracks, redis=redis, redis_key=rk_mem, cache_ts=ts
                )
                _fire_cache_track_meta_items(cached_tracks)
                _safe_ensure_future(
                    _warm_youtube_direct_for_ids(
                        [t.get("id") for t in cached_tracks[:SEARCH_PRESOLVE_TOP_N] if t.get("id")]
                    )
                )
            _api_tracks_mark_modern_resolve(cached_tracks)
            return Response(
                content=json.dumps({"items": cached_tracks}, ensure_ascii=False),
                media_type="application/json",
                headers={**extra_headers, "X-Search-Cache": "memory"},
            )
        del _search_cache[key]

    # ─── Живой запрос к VK с singleflight ───
    _cache_metrics["search_miss"] += 1

    # Перед походом к VK применяем динамический лимит только на live-search (кэш не учитывается).
    allowed, retry_after = _check_live_search_limit(request)
    if not allowed:
        payload = {"detail": "Too Many Requests", "retry_after_sec": retry_after}
        return Response(
            status_code=429,
            content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            media_type="application/json",
        )

    async def _leader_search() -> List[Dict]:
        # Защита от cache stampede: только один процесс обновляет (Redis lock)
        got_lock = False
        if redis is not None:
            try:
                got_lock = await redis.set(lock_key, "1", nx=True, ex=30)
            except Exception:
                pass
        if not got_lock and redis is not None:
            for _ in range(10):
                await asyncio.sleep(1)
                try:
                    cached = await redis.get(redis_key)
                    if cached:
                        data = json.loads(cached)
                        items = data.get("items") if isinstance(data, dict) else data
                        if isinstance(items, list):
                            return items
                except Exception:
                    pass
        # Живой поиск: YTM (полная выдача по limit, без усечения по артисту — это только в /recommendations/personal).
        tracks_live = search_youtube_tracks(trimmed, limit=eff_limit)
        now_inner = time.time()
        if redis is not None:
            try:
                await redis.delete(lock_key)
            except Exception:
                pass
        if tracks_live:
            _search_cache[key] = (tracks_live, now_inner)
            _search_cache.move_to_end(key)
            while len(_search_cache) > _SEARCH_CACHE_MAX:
                _search_cache.popitem(last=False)
            if redis is not None:
                payload = json.dumps({"items": tracks_live, "ts": now_inner}, ensure_ascii=False)
                for attempt in range(2):
                    try:
                        await redis.set(redis_key, payload, ex=_SEARCH_CACHE_TTL)
                        break
                    except Exception as e:
                        print(f"⚠️ Redis search set error (attempt {attempt + 1}/2): {e}")
                try:
                    await redis.zincrby(_cache_ns("search", "popular"), 1, key)
                    await redis.zremrangebyrank(_cache_ns("search", "popular"), 0, -501)
                except Exception:
                    pass
        return tracks_live

    # Singleflight (request coalescing): один живой запрос на ключ, остальные ждут
    async with _search_singleflight_lock:
        fut = _search_singleflight.get(key)
        if fut is None:
            loop = asyncio.get_event_loop()
            fut = loop.create_future()
            _search_singleflight[key] = fut
            is_leader = True
        else:
            is_leader = False

    if is_leader:
        try:
            tracks = await _leader_search()
            fut.set_result(tracks)
        except Exception as exc:
            fut.set_exception(exc)
            raise
        finally:
            async with _search_singleflight_lock:
                _search_singleflight.pop(key, None)
    else:
        tracks = await fut

    if tracks:
        top_ids = [t["id"] for t in tracks[:SEARCH_PRESOLVE_TOP_N]]
        _safe_ensure_future(_batch_presolve(top_ids))
        _safe_ensure_future(_warm_youtube_direct_for_ids(top_ids))
        _fire_cache_track_meta_items(tracks)

    _api_tracks_mark_modern_resolve(tracks)
    return Response(
        content=json.dumps({"items": tracks}, ensure_ascii=False),
        media_type="application/json",
        headers={**extra_headers, "X-Search-Cache": "live"},
    )


def _safe_ensure_future(coro) -> None:
    """Запуск корутины в фоне; исключения логируются, процесс не падает."""
    task = asyncio.ensure_future(coro)

    def _done(t):
        try:
            exc = t.exception()
            if exc is not None:
                print(f"⚠️ [bg task] {exc}")
        except asyncio.CancelledError:
            pass

    task.add_done_callback(_done)


def _fire_cache_track_meta_items(items: Optional[List[Dict]]) -> None:
    """Фон: положить метаданные треков в Redis (подборки, быстрый resolve)."""
    if not items:
        return
    _safe_ensure_future(_redis_cache_tracks_meta_batch(items))


async def _batch_presolve(track_ids: List[str]):
    """Фоновая предзагрузка audio URLs через batch execute (1 API-вызов на 25 треков)."""
    try:
        await vk_batch_get_audio_urls(track_ids)
    except Exception:
        pass


async def _warm_search_cache() -> None:
    """Прогрев кэша поиска: топ-500 популярных запросов раз в сутки через HTTP-поиск."""
    redis = await get_redis()
    if not redis:
        return
    try:
        keys_raw = await redis.zrevrange(_cache_ns("search", "popular"), 0, 499)
    except Exception as e:
        print(f"⚠️ warm_search_cache zrevrange: {e}")
        return
    for key in keys_raw:
        try:
            parts = key.split(":")
            if len(parts) >= 4 and parts[-1] == "ac":
                query = ":".join(parts[:-3])
                offset_s, limit_s = parts[-3], parts[-2]
            elif len(parts) == 3:
                query, offset_s, limit_s = parts[0], parts[1], parts[2]
            else:
                continue
            offset, limit = int(offset_s), int(limit_s)
        except (ValueError, IndexError):
            continue
        redis_key = _cache_ns("search", key)
        try:
            existing = await redis.get(redis_key)
            if existing:
                continue
        except Exception:
            continue
        try:
            if parts[-1] == "ac":
                raw = await _vk_search_artist_catalog(query, min(limit, _ARTIST_CATALOG_MAX))
                tracks = _parse_tracks(raw)
                await _vk_enrich_tracks_album_covers_via_get_by_id(tracks, max_tracks=100)
            else:
                tracks = await _vk_search_for_http(query, offset=offset, limit=limit)
            if tracks:
                await redis.set(
                    redis_key,
                    json.dumps({"items": tracks, "ts": time.time()}, ensure_ascii=False),
                    ex=_SEARCH_CACHE_TTL,
                )
        except Exception as e:
            print(f"⚠️ warm_search_cache key={key}: {e}")


@app.get("/api/music/resolve/{track_id:path}")
async def resolve_url(
    track_id: str = Param(...),
    refresh: bool = Query(False, description="Сбросить кэш и получить свежую ссылку из VK (при мёртвой ссылке)"),
    title: Optional[str] = Query(
        None,
        description="Для старых VK id в избранном: название — fallback-поиск в YouTube Music, если VK не отдал URL.",
    ),
    artist: Optional[str] = Query(
        None,
        description="Для старых VK id: исполнитель — вместе с title для fallback на YouTube.",
    ),
    next_id: Optional[str] = Query(
        None,
        description="Опционально: следующий track_id для фоновой предзагрузки URL (pre-cache).",
    ),
):
    """Возвращает прямой URL для воспроизведения.
    Для YouTube треков — URL на /api/music/youtube-download/ (стриминг через наш сервер).
    Для VK — прямой VK CDN URL.
    При refresh=true сбрасываем кэш и тянем новую ссылку из VK.
    Если VK недоступен, а переданы title и/или artist (как в избранном), подбираем тот же трек через YouTube Music."""
    import re as _re_yt
    def _maybe_warm_next(next_tid: Optional[str]) -> None:
        if not next_tid or not isinstance(next_tid, str):
            return
        tid = next_tid.strip()
        if not tid:
            return
        if _valid_track_id(tid):
            async def _precache_next_vk(vk_tid: str) -> None:
                try:
                    await vk_get_audio_url(vk_tid)
                except Exception:
                    pass
            _safe_ensure_future(_precache_next_vk(tid))
            return
        m2 = re.search(r'(?:v=|youtu\.be/|shorts/)([\w-]{11})', tid)
        yt_vid = m2.group(1) if m2 else (tid if re.match(r'^[\w-]{11}$', tid) else None)
        if yt_vid:
            _safe_ensure_future(_warm_youtube_direct_for_ids([yt_vid]))

    sc_id = parse_soundcloud_track_id(track_id)
    if sc_id is not None and _sc_ready():
        url = await _sc_resolve_url(build_soundcloud_track_id(sc_id))
        if not url:
            raise HTTPException(404, "SoundCloud track unavailable")
        return Response(
            content=json.dumps({"url": url, "hls": _is_hls_url(url), "provider": "soundcloud"}),
            media_type="application/json",
            headers={"Cache-Control": "public, max-age=120"},
        )

    # Если track_id — YouTube URL (проверяем до VK, т.к. URL может быть декодирован из %2F)
    is_yt = bool(_re_yt.search(r'(?:youtube\.com|youtu\.be)', track_id))
    if is_yt or track_id.startswith("http://") or track_id.startswith("https://"):
        m = _re_yt.search(r'(?:v=|youtu\.be/|shorts/)([\w-]{11})', track_id)
        vid = m.group(1) if m else None
        if not vid:
            raise HTTPException(400, "Invalid YouTube URL")
        _maybe_warm_next(next_id)
        proxy_url = f"/api/music/youtube-direct/{vid}"
        _safe_ensure_future(_warm_youtube_direct_for_ids([vid]))
        return Response(
            content=json.dumps({"url": proxy_url, "hls": False}),
            media_type="application/json",
            headers={"Cache-Control": "public, max-age=120"},
        )

    # VK до «голого» 11-симв. id — чтобы не путать с YouTube video id
    if _valid_track_id(track_id):
        t0 = time.time()
        canon = _vk_canonical_track_id(track_id)
        legacy_meta_q = _yt_search_query_from_track_meta(title or "", artist or "")
        # Redis vk_yt_fb: раньше без query тоже отдавал youtube-direct — избранное снова подхватывает кэш.
        # Живой поиск YTM при пустом VK — только если есть legacy_meta_q (title/artist от клиента).
        if not refresh:
            yt_cached = await _redis_get_vk_yt_fallback_video_id(canon)
            if yt_cached:
                proxy_url = f"/api/music/youtube-direct/{yt_cached}"
                _safe_ensure_future(_warm_youtube_direct_for_ids([yt_cached]))
                return Response(
                    content=json.dumps({"url": proxy_url, "hls": False}),
                    media_type="application/json",
                    headers={"Cache-Control": "public, max-age=300"},
                )
        if refresh:
            await _redis_delete_track_source(track_id)
        url = await vk_get_audio_url(track_id)
        elapsed = time.time() - t0
        if elapsed > 2.0:
            print(f"⏱ resolve {track_id}: {elapsed:.1f}s")
        if not url:
            yt_vid = await _youtube_video_id_for_legacy_vk_track(
                canon,
                title or "",
                artist or "",
            )
            if yt_vid:
                proxy_url = f"/api/music/youtube-direct/{yt_vid}"
                _safe_ensure_future(_warm_youtube_direct_for_ids([yt_vid]))
                return Response(
                    content=json.dumps({"url": proxy_url, "hls": False}),
                    media_type="application/json",
                    headers={"Cache-Control": "public, max-age=120"},
                )
            raise HTTPException(404, "Track not found or restricted")

        _maybe_warm_next(next_id)

        return Response(
            content=json.dumps({"url": url, "hls": _is_hls_url(url)}),
            media_type="application/json",
            headers={"Cache-Control": "public, max-age=120"},
        )

    if _re_yt.match(r'^[\w-]{11}$', track_id):
        _maybe_warm_next(next_id)
        proxy_url = f"/api/music/youtube-direct/{track_id}"
        _safe_ensure_future(_warm_youtube_direct_for_ids([track_id]))
        return Response(
            content=json.dumps({"url": proxy_url, "hls": False}),
            media_type="application/json",
            headers={"Cache-Control": "public, max-age=120"},
        )

    raise HTTPException(400, "Invalid track ID format")


@app.post("/api/music/resolve-batch")
async def resolve_batch(request: Request):
    """Batch resolve: до 25 треков за 1 VK API вызов.
    Как одиночный GET /resolve: при vk_yt_fb в Redis — сразу youtube-direct, без VK
    (иначе preload после поиска кэшировал «чужой» VK URL и повторное воспроизведение оставалось медленным)."""
    body = await request.json()
    ids = body.get("ids", [])
    if not ids or not isinstance(ids, list):
        raise HTTPException(400, "ids required")
    ids = [tid for tid in ids[:25] if isinstance(tid, str) and _valid_track_id(tid)]
    if not ids:
        raise HTTPException(400, "No valid track IDs")

    if _sc_ready():
        out: Dict[str, Dict[str, Any]] = {}
        tasks = []
        task_ids: List[str] = []
        for tid in ids:
            sid = parse_soundcloud_track_id(tid)
            if sid is None:
                continue
            c = build_soundcloud_track_id(sid)
            task_ids.append(c)
            tasks.append(_sc_resolve_url(c))
        if tasks:
            urls = await asyncio.gather(*tasks, return_exceptions=True)
            for idx, raw in enumerate(urls):
                if isinstance(raw, Exception):
                    continue
                if isinstance(raw, str) and raw.strip():
                    out[task_ids[idx]] = {"url": raw.strip(), "hls": _is_hls_url(raw.strip())}
        return Response(
            content=json.dumps(out, ensure_ascii=False),
            media_type="application/json",
            headers={"Cache-Control": "public, max-age=120"},
        )

    async def _yt_fb_for_tid(tid: str) -> Tuple[str, Optional[str]]:
        canon = _vk_canonical_track_id(tid)
        vid = await _redis_get_vk_yt_fallback_video_id(canon)
        return tid, vid

    fb_pairs = await asyncio.gather(*[_yt_fb_for_tid(t) for t in ids])
    result: Dict[str, Dict[str, Any]] = {}
    vk_ids: List[str] = []
    warm_yt: List[str] = []
    for tid, vid in fb_pairs:
        if vid:
            result[tid] = {"url": f"/api/music/youtube-direct/{vid}", "hls": False}
            warm_yt.append(vid)
        else:
            vk_ids.append(tid)
    if warm_yt:
        _safe_ensure_future(_warm_youtube_direct_for_ids(list(dict.fromkeys(warm_yt))))

    urls = await vk_batch_get_audio_urls(vk_ids) if vk_ids else {}
    for tid, url in urls.items():
        if url:
            result[tid] = {"url": url, "hls": _is_hls_url(url)}
    return Response(
        content=json.dumps(result, ensure_ascii=False),
        media_type="application/json",
        headers={"Cache-Control": "public, max-age=120"},
    )


@app.get("/api/music/download/{track_id}")
async def download(
    track_id: str = Param(...),
    title: Optional[str] = Query(None, description="Старые VK id: метаданные для fallback на YouTube."),
    artist: Optional[str] = Query(None),
):
    """302 redirect на VK CDN для прямых MP3. ffmpeg только для HLS.
    Если track_id — YouTube URL, используем youtube-download."""
    # Если это YouTube URL — перенаправляем на youtube-download
    if track_id.startswith("http://") or track_id.startswith("https://"):
        return await youtube_download(track_id)

    sc_id = parse_soundcloud_track_id(track_id)
    if sc_id is not None and _sc_ready():
        canon_sc = build_soundcloud_track_id(sc_id)
        url = await _sc_resolve_url(canon_sc)
        if not url:
            raise HTTPException(404, "SoundCloud track unavailable")
        track_info = await _fetch_track_info(canon_sc)
        title = str(track_info.get("title") or "Track")[:200]
        artist = str(track_info.get("artist") or "Artist")[:200]
        duration = int(track_info.get("duration") or 0)
        mp3_data = await _get_mp3_data(canon_sc, url)
        if not mp3_data:
            raise HTTPException(502, "SoundCloud download failed")
        mp3_data = await _ffmpeg_tag_mp3_bytes(
            mp3_data, title=title, artist=artist, duration_sec=duration
        )
        fname = f"{artist} - {title}.mp3"
        return Response(
            content=mp3_data,
            media_type="audio/mpeg",
            headers={
                "Content-Disposition": _content_disposition_attachment(fname),
                "Cache-Control": "private, max-age=300",
                "Content-Length": str(len(mp3_data)),
            },
        )

    if not _valid_track_id(track_id):
        raise HTTPException(400, "Invalid track ID format")
    canon = _vk_canonical_track_id(track_id)
    yt_cached = await _redis_get_vk_yt_fallback_video_id(canon)
    if yt_cached:
        return RedirectResponse(f"/api/music/youtube-direct/{yt_cached}", status_code=302)
    t0 = time.time()
    url = await vk_get_audio_url(track_id)
    resolve_sec = time.time() - t0
    if resolve_sec > 2.0:
        print(f"⏱ download resolve {track_id}: {resolve_sec:.1f}s")
    if not url:
        yt_vid = await _youtube_video_id_for_legacy_vk_track(
            canon,
            title or "",
            artist or "",
        )
        if yt_vid:
            return RedirectResponse(f"/api/music/youtube-direct/{yt_vid}", status_code=302)
        raise HTTPException(404, "Track not found or restricted")

    if not _is_hls_url(url):
        return RedirectResponse(url, status_code=302)

    return StreamingResponse(
        ffmpeg_stream_mp3(url),
        media_type="audio/mpeg",
        headers={
            "Cache-Control": "public, max-age=300",
            "Accept-Ranges": "none",
            "Transfer-Encoding": "chunked",
        },
    )


# ─── Auth route ──────────────────────────────────────────────────

@app.post("/api/auth/login")
async def login(request: Request):
    body = await request.json()
    init_data = body.get("initData", "")
    if not init_data:
        raise HTTPException(400, "Missing initData")
    user = validate_init_data(init_data, BOT_TOKEN)
    if not user:
        raise HTTPException(401, "Invalid or expired Telegram initData")
    _register_bot_subscriber_from_telegram_user(user, "auth_login_body", force=True)
    # Не возвращаем лишние данные — только id, first_name, username
    safe_user = {
        "id": user.get("id"),
        "first_name": user.get("first_name", ""),
        "username": user.get("username"),
    }
    return {"status": "ok", "user": safe_user}


@app.post("/api/auth/telegram")
async def auth_telegram_oidc(request: Request):
    """
    Обмен OIDC id_token (Telegram.Login / oauth.telegram.org) на сессионный JWT для Authorization: Bearer.
    """
    if not TELEGRAM_OAUTH_CLIENT_ID or not TGPLAY_WEB_SESSION_SECRET:
        raise HTTPException(503, "Telegram OAuth web login is not configured")
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        raise HTTPException(400, "Invalid JSON body")
    raw_id = (body.get("id_token") or body.get("idToken") or "").strip()
    if not raw_id:
        raise HTTPException(400, "Missing id_token")
    extra = body.get("user")
    if extra is not None and not isinstance(extra, dict):
        extra = None
    raw_nonce = body.get("nonce")
    expected_nonce = None
    if isinstance(raw_nonce, str) and raw_nonce.strip():
        expected_nonce = raw_nonce.strip()
    return await asyncio.to_thread(_telegram_web_login_response_dict, raw_id, extra, expected_nonce)


@app.post("/api/auth/telegram/code")
async def auth_telegram_oauth_code(request: Request):
    """
    Обмен authorization_code (PKCE redirect flow) на id_token через oauth.telegram.org/token,
    затем та же сессия TGPlay, что и у POST /api/auth/telegram.
    Нужны TELEGRAM_OAUTH_CLIENT_SECRET и зарегистрированный redirect_uri в BotFather.
    """
    if not TELEGRAM_OAUTH_CLIENT_ID or not TGPLAY_WEB_SESSION_SECRET:
        raise HTTPException(503, "Telegram OAuth web login is not configured")
    if not TELEGRAM_OAUTH_CLIENT_SECRET:
        raise HTTPException(
            503,
            "TELEGRAM_OAUTH_CLIENT_SECRET is required for web login redirect (set in BotFather Web Login)",
        )
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        raise HTTPException(400, "Invalid JSON body")
    code = (body.get("code") or "").strip()
    redirect_uri = (body.get("redirect_uri") or body.get("redirectUri") or "").strip()
    code_verifier = (body.get("code_verifier") or body.get("codeVerifier") or "").strip()
    if not code or not redirect_uri or not code_verifier:
        raise HTTPException(400, "Missing code, redirect_uri, or code_verifier")
    if len(redirect_uri) > 2048 or len(code_verifier) > 256:
        raise HTTPException(400, "Invalid redirect_uri or code_verifier length")

    payload = urlencode(
        {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": redirect_uri,
            "client_id": TELEGRAM_OAUTH_CLIENT_ID,
            "code_verifier": code_verifier,
        }
    )
    basic = base64.b64encode(
        f"{TELEGRAM_OAUTH_CLIENT_ID}:{TELEGRAM_OAUTH_CLIENT_SECRET}".encode()
    ).decode("ascii")

    try:
        session = await get_session()
        async with session.post(
            "https://oauth.telegram.org/token",
            data=payload,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
            },
            timeout=aiohttp.ClientTimeout(total=22),
        ) as resp:
            text = await resp.text()
            if resp.status != 200:
                print(f"⚠️ oauth.telegram.org/token {resp.status}: {text[:800]}")
                raise HTTPException(401, "Telegram refused authorization code")
            try:
                tok = json.loads(text)
            except json.JSONDecodeError:
                raise HTTPException(502, "Invalid token response") from None
            id_tok = (tok.get("id_token") or "").strip()
            if not id_tok:
                raise HTTPException(502, "No id_token in token response")
    except HTTPException:
        raise
    except aiohttp.ClientError as e:
        print(f"⚠️ oauth token request: {e}")
        raise HTTPException(502, "OAuth token request failed") from e

    return await asyncio.to_thread(_telegram_web_login_response_dict, id_tok, None)


@app.post("/api/auth/logout")
async def auth_logout():
    """
    Stateless Bearer: инвалидации на сервере нет — клиент удаляет токен.
    Точка для единообразного выхода и будущего cookie/revoke.
    """
    return {"ok": True}


@app.get("/api/me/photo")
async def get_my_profile_photo(
    authorization: Optional[str] = Header(None),
    expected_user_id: Optional[int] = Query(None),
):
    """
    Аватар пользователя через Bot API (прокси на наш домен).
    Прямые photo_url из initData (t.me / CDN) без VPN часто не грузятся в WebView.
    """
    user = get_user_from_header(authorization)
    uid = user.get("id")
    if expected_user_id is not None:
        try:
            if int(expected_user_id) != int(uid):
                raise HTTPException(409, "Session user mismatch")
        except Exception:
            raise HTTPException(409, "Session user mismatch")
    if not uid or not BOT_TOKEN:
        raise HTTPException(404, "No profile photo")
    try:
        session = await get_session()
        photos_url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUserProfilePhotos"
        async with session.get(photos_url, params={"user_id": int(uid), "limit": 1}) as resp:
            pdata = await resp.json()
        if not pdata.get("ok"):
            raise HTTPException(404, "No profile photo")
        photos = (pdata.get("result") or {}).get("photos") or []
        if not photos or not photos[0]:
            raise HTTPException(404, "No profile photo")
        sizes = photos[0]
        best = sizes[-1] if sizes else None
        file_id = (best or {}).get("file_id")
        if not file_id:
            raise HTTPException(404, "No profile photo")
        async with session.get(
            f"https://api.telegram.org/bot{BOT_TOKEN}/getFile", params={"file_id": file_id}
        ) as fresp:
            fdata = await fresp.json()
        if not fdata.get("ok"):
            raise HTTPException(404, "No profile photo")
        file_path = (fdata.get("result") or {}).get("file_path")
        if not file_path or ".." in str(file_path):
            raise HTTPException(404, "No profile photo")
        file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_path}"
        async with session.get(file_url) as img_resp:
            if img_resp.status != 200:
                raise HTTPException(502, "Photo fetch failed")
            body = await img_resp.read()
            if len(body) > 5_000_000:
                raise HTTPException(502, "Photo too large")
            ct = img_resp.headers.get("Content-Type", "image/jpeg").split(";")[0].strip() or "image/jpeg"
        return Response(
            content=body,
            media_type=ct,
            headers={
                "Cache-Control": "no-store, private",
                "Pragma": "no-cache",
                "Vary": "Authorization",
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        print(f"⚠️ /api/me/photo: {e}")
        raise HTTPException(502, "Photo error") from e


@app.post("/api/me/register")
async def register_me_from_miniapp(request: Request):
    """
    Явная регистрация numeric user id (initData в теле и/или в Authorization).
    Вызывать с фронта при старте Mini App — дублирует middleware, но ловит edge-кейсы.
    """
    user = None
    try:
        body = await request.json()
        if isinstance(body, dict):
            init_data = (body.get("initData") or "").strip()
            if init_data:
                user = validate_init_data(init_data, BOT_TOKEN)
    except Exception:
        pass
    if not user:
        user = _telegram_user_from_auth_header(request.headers.get("Authorization"))
    if user:
        _register_bot_subscriber_from_telegram_user(user, "api_me_register", force=True)
    return {"ok": True, "registered": bool(user and user.get("id"))}


@app.post("/api/me/dislike")
async def api_me_dislike(request: Request, authorization: Optional[str] = Header(None)):
    """Дизлайк трека в персональных рекомендациях: трек + артист/жанр (SQLite), фильтр в подборке."""
    user = get_user_from_header(authorization)
    uid = int(user["id"])
    try:
        body = await request.json()
    except Exception:
        body = {}
    b = body if isinstance(body, dict) else {}
    track_id = str(b.get("track_id") or "").strip()
    if not _valid_playlist_library_track_id(track_id):
        raise HTTPException(400, "Invalid track_id")
    body_artist = str(b.get("artist") or "").strip()
    genre_from_body: Optional[int] = None
    raw_gid = b.get("genre_id")
    if raw_gid is not None:
        try:
            genre_from_body = int(raw_gid)
        except (TypeError, ValueError):
            genre_from_body = None
    meta = await _redis_get_track_meta(track_id)
    gid_meta, _, _ = _rec_meta_fields_from_cached_meta(meta)
    artist_for_key = body_artist
    if not artist_for_key and isinstance(meta, dict):
        artist_for_key = str(meta.get("artist") or "").strip()
    akey = _rec_artist_exact_key(artist_for_key) if artist_for_key else ""
    gid = genre_from_body if genre_from_body is not None else gid_meta
    import analytics_db

    analytics_db.init_db()
    gid_penalty = gid if _rec_genre_id_strong_for_show_penalty(gid) else None
    analytics_db.record_track_dislike(uid, track_id, artist_key=akey or None, genre_id=gid_penalty)
    try:
        analytics_db.log_button_click(
            telegram_user_id=uid,
            username=user.get("username") or user.get("first_name"),
            button_id="dislike_track",
            context="recommendations",
            extra={"track_id": track_id},
        )
    except Exception:
        pass
    return {"ok": True}


@app.get("/api/me/bot-audio-delivered")
async def api_me_bot_audio_delivered(authorization: Optional[str] = Header(None)):
    """track_id треков, по которым аудио уже успешно доставлено в чат пользователя с ботом (для UI «скачано»)."""
    user = get_user_from_header(authorization)
    uid = user.get("id")
    if not uid:
        raise HTTPException(401, "Missing user id")
    uid_int = int(uid)
    # Эндпоинт опрашивается клиентом часто — синхронный SQLite выносим в thread-пул.
    track_ids, verified_live = await asyncio.to_thread(_bot_audio_delivered_sync, uid_int)
    # Оба списка с LIMIT: старые verified_live могли не попасть в track_ids при большом числе
    # строк с verified_live=0 — UI тогда скрывал кнопку, но не показывал галочку.
    seen: set[str] = set()
    merged: list[str] = []
    for tid in track_ids + verified_live:
        if tid in seen:
            continue
        seen.add(tid)
        merged.append(tid)
    return {"track_ids": merged, "verified_live_track_ids": verified_live}


# ─── Playlist routes ─────────────────────────────────────────────

@app.get("/api/playlist")
async def get_playlist(authorization: Optional[str] = Header(None)):
    user = get_user_from_header(authorization)
    tracks = await asyncio.to_thread(load_playlist, user["id"])
    # Сначала новые (последние добавленные), потом старые
    return {"items": list(reversed(tracks))}


@app.post("/api/playlist")
async def add_to_playlist(track: TrackPayload, authorization: Optional[str] = Header(None)):
    raw_tid = (track.id or "").strip()
    if not _valid_playlist_library_track_id(raw_tid):
        raise HTTPException(400, "Invalid track ID format")
    eff_id = _playlist_library_track_id_stored(raw_tid)
    user = get_user_from_header(authorization)
    tracks = load_playlist(user["id"])
    if len(tracks) >= 500:
        raise HTTPException(400, "Playlist limit reached (500)")
    if any(t["id"] == eff_id or t["id"] == raw_tid for t in tracks):
        return {"status": "already_exists", "count": len(tracks)}
    # Санитизация: только http(s) для обложки (защита от javascript:/data:)
    raw_cover = (track.cover_url or "").strip()[:500]
    cover_url = raw_cover if (raw_cover.startswith("http://") or raw_cover.startswith("https://")) else None
    safe_track = {
        "id": eff_id,
        "title": track.title[:200],
        "artist": track.artist[:200],
        "duration": min(max(track.duration, 0), 36000),
        "cover_url": cover_url or None,
    }
    if track.vk_legacy is not None:
        safe_track["vk_legacy"] = bool(track.vk_legacy)
    tracks.append(safe_track)
    save_playlist(user["id"], tracks)
    try:
        import analytics_db

        analytics_db.init_db()
        meta = await _redis_get_track_meta(eff_id)
        gid, ry, lb = _rec_meta_fields_from_cached_meta(meta)
        # Like/save — сильный сигнал вкуса, но слабее чем длительные прослушивания.
        await _rec_update_taste_profile(int(user["id"]), genre_id=gid, release_year=ry, lang_bucket=lb, weight=1.2)
        akey_fav = _rec_artist_exact_key(str(track.artist or "").strip())
        gid_relief = gid if _rec_genre_id_strong_for_show_penalty(gid) else None
        analytics_db.bump_rec_penalties_on_favorite(
            int(user["id"]),
            artist_key=akey_fav or None,
            genre_id=gid_relief,
        )
    except Exception:
        pass
    return {"status": "saved", "count": len(tracks)}


@app.delete("/api/playlist/{track_id}")
async def remove_from_playlist(track_id: str, authorization: Optional[str] = Header(None)):
    raw = (track_id or "").strip()
    if not _valid_playlist_library_track_id(raw):
        raise HTTPException(400, "Invalid track ID format")
    eff = _playlist_library_track_id_stored(raw)
    user = get_user_from_header(authorization)
    tracks = load_playlist(user["id"])
    tracks = [t for t in tracks if t["id"] != eff and t["id"] != raw]
    save_playlist(user["id"], tracks)
    return {"status": "removed", "count": len(tracks)}


# ─── Список плейлистов (Избранное + кастомные), лимиты, шары ─────

def _safe_track(t: Dict) -> Dict:
    raw_cover = (t.get("cover_url") or "").strip()[:500]
    cover_url = raw_cover if (raw_cover.startswith("http://") or raw_cover.startswith("https://")) else None
    row = {
        "id": (t.get("id") or "")[:50],
        "title": (t.get("title") or "")[:200],
        "artist": (t.get("artist") or "")[:200],
        "duration": min(max(int(t.get("duration") or 0), 0), 36000),
        "cover_url": cover_url,
    }
    vl = t.get("vk_legacy")
    if vl is not None:
        row["vk_legacy"] = bool(vl)
    return row


@app.get("/api/playlists")
async def list_playlists(authorization: Optional[str] = Header(None)):
    """Список плейлистов: Избранное + до MAX_FREE_PLAYLISTS кастомных. Требует авторизации."""
    user = get_user_from_header(authorization)
    uid = user["id"]
    favorites = load_playlist(uid)
    custom = load_custom_playlists(uid)
    return {
        "favorites": list(reversed(favorites)),
        "playlists": [
            {
                "id": p["id"],
                "name": p.get("name", ""),
                "is_public": bool(p.get("is_public")),
                "share_id": p.get("share_id"),
                "track_count": len(p.get("track_ids", [])),
            }
            for p in custom
        ],
        "max_free_playlists": MAX_FREE_PLAYLISTS,
    }


@app.post("/api/playlists")
async def create_playlist(
    request: Request,
    authorization: Optional[str] = Header(None),
):
    """Создать плейлист. Лимит MAX_FREE_PLAYLISTS. Тело: { \"name\": \"...\" }."""
    user = get_user_from_header(authorization)
    uid = user["id"]
    try:
        body = await request.json()
    except Exception as e:
        logger.warning("create_playlist: body parse failed: %s", e)
        body = {}
    name = (body.get("name") or "").strip()[:100] or "Новый плейлист"
    custom = load_custom_playlists(uid)
    if len(custom) >= MAX_FREE_PLAYLISTS:
        raise HTTPException(400, f"Playlist limit reached ({MAX_FREE_PLAYLISTS})")
    playlist_id = uuid.uuid4().hex
    share_id = uuid.uuid4().hex
    custom.append({
        "id": playlist_id,
        "name": name,
        "track_ids": [],
        "is_public": False,
        "share_id": share_id,
    })
    save_custom_playlists(uid, custom)
    try:
        import analytics_db

        analytics_db.init_db()
        analytics_db.log_playlist_event(
            telegram_user_id=uid,
            username=user.get("username") or user.get("first_name"),
            playlist_id=playlist_id,
            action="create",
            extra={"name": name},
        )
    except Exception as e:
        logger.warning("create_playlist: analytics log failed: %s", e)
    return {"id": playlist_id, "share_id": share_id, "name": name}


@app.patch("/api/playlists/{playlist_id}")
async def update_playlist(
    playlist_id: str,
    request: Request,
    authorization: Optional[str] = Header(None),
):
    """Переименовать плейлист. Тело: { \"name\": \"...\" }."""
    user = get_user_from_header(authorization)
    uid = user["id"]
    custom = load_custom_playlists(uid)
    idx = next((i for i, p in enumerate(custom) if p["id"] == playlist_id), None)
    if idx is None:
        raise HTTPException(404, "Playlist not found")
    try:
        body = await request.json()
    except Exception as e:
        logger.warning("update_playlist: body parse failed: %s", e)
        body = {}
    name = (body.get("name") or "").strip()[:100]
    if name:
        custom[idx]["name"] = name
    if "is_public" in body and body["is_public"] is False:
        custom[idx]["is_public"] = False
        share_id = custom[idx].get("share_id")
        if share_id:
            shares = load_shares()
            if share_id in shares and "payload" in shares[share_id]:
                shares[share_id]["payload"]["is_public"] = False
                save_shares(shares)
    save_custom_playlists(uid, custom)
    try:
        import analytics_db

        analytics_db.init_db()
        analytics_db.log_playlist_event(
            telegram_user_id=uid,
            username=user.get("username") or user.get("first_name"),
            playlist_id=playlist_id,
            action="rename",
            extra={"name": custom[idx]["name"]},
        )
    except Exception as e:
        logger.warning("update_playlist: analytics log failed: %s", e)
    return {"id": playlist_id, "name": custom[idx]["name"]}


@app.delete("/api/playlists/{playlist_id}")
async def delete_playlist(
    playlist_id: str,
    authorization: Optional[str] = Header(None),
):
    user = get_user_from_header(authorization)
    uid = user["id"]
    custom = load_custom_playlists(uid)
    before = list(custom)
    custom = [p for p in custom if p["id"] != playlist_id]
    save_custom_playlists(uid, custom)
    try:
        deleted_name = next((p.get("name") for p in before if p.get("id") == playlist_id), None)
    except Exception:
        deleted_name = None
    try:
        import analytics_db

        analytics_db.init_db()
        analytics_db.log_playlist_event(
            telegram_user_id=uid,
            username=user.get("username") or user.get("first_name"),
            playlist_id=playlist_id,
            action="delete",
            extra={"name": deleted_name},
        )
    except Exception:
        pass
    return {"status": "removed"}


def _tracks_by_ids(
    user_id: int,
    track_ids: List[str],
    favorites: List[Dict],
    track_meta: Optional[Dict[str, Dict]] = None,
) -> List[Dict]:
    """Треки по id: из избранного, затем из track_meta (метаданные при добавлении из поиска), иначе заглушка."""
    fav_map = {t["id"]: t for t in favorites}
    meta = track_meta or {}
    out = []
    for tid in track_ids:
        if tid in fav_map:
            out.append(fav_map[tid])
        elif tid in meta:
            m = meta[tid]
            row = {
                "id": tid,
                "title": m.get("title") or "",
                "artist": m.get("artist") or "",
                "duration": m.get("duration") or 0,
                "cover_url": m.get("cover_url"),
            }
            if m.get("vk_legacy") is not None:
                row["vk_legacy"] = bool(m.get("vk_legacy"))
            out.append(row)
        else:
            out.append({"id": tid, "title": "", "artist": "", "duration": 0, "cover_url": None})
    return out


@app.get("/api/playlists/{playlist_id}")
async def get_playlist_tracks(
    playlist_id: str,
    authorization: Optional[str] = Header(None),
):
    """Треки кастомного плейлиста. Избранное отдаётся через GET /api/playlist."""
    user = get_user_from_header(authorization)
    uid = user["id"]
    favorites = load_playlist(uid)
    custom = load_custom_playlists(uid)
    pl = next((p for p in custom if p["id"] == playlist_id), None)
    if not pl:
        raise HTTPException(404, "Playlist not found")
    track_ids = pl.get("track_ids", [])
    track_meta = pl.get("track_meta", {})
    tracks = _tracks_by_ids(uid, track_ids, favorites, track_meta)
    return {"items": tracks}


@app.post("/api/playlists/{playlist_id}/tracks")
async def add_track_to_playlist(
    playlist_id: str,
    track: TrackPayload,
    authorization: Optional[str] = Header(None),
):
    raw_tid = (track.id or "").strip()
    if not _valid_playlist_library_track_id(raw_tid):
        raise HTTPException(400, "Invalid track ID format")
    eff_id = _playlist_library_track_id_stored(raw_tid)
    user = get_user_from_header(authorization)
    uid = user["id"]
    custom = load_custom_playlists(uid)
    idx = next((i for i, p in enumerate(custom) if p["id"] == playlist_id), None)
    if idx is None:
        raise HTTPException(404, "Playlist not found")
    track_ids = custom[idx].get("track_ids", [])
    if eff_id in track_ids or raw_tid in track_ids:
        return {"status": "already_exists", "count": len(track_ids)}
    track_ids.append(eff_id)
    custom[idx]["track_ids"] = track_ids
    if (
        track.title
        or track.artist
        or (track.duration and track.duration > 0)
        or track.cover_url
        or track.vk_legacy is not None
    ):
        meta = custom[idx].setdefault("track_meta", {})
        meta[eff_id] = {
            "title": track.title or "",
            "artist": track.artist or "",
            "duration": track.duration or 0,
            "cover_url": track.cover_url,
        }
        if track.vk_legacy is not None:
            meta[eff_id]["vk_legacy"] = bool(track.vk_legacy)
    save_custom_playlists(uid, custom)
    try:
        import analytics_db

        analytics_db.init_db()
        analytics_db.log_playlist_event(
            telegram_user_id=uid,
            username=user.get("username") or user.get("first_name"),
            playlist_id=playlist_id,
            action="add_track",
            extra={"track_id": eff_id},
        )
    except Exception:
        pass
    return {"status": "saved", "count": len(track_ids)}


@app.delete("/api/playlists/{playlist_id}/tracks/{track_id}")
async def remove_track_from_playlist(
    playlist_id: str,
    track_id: str,
    authorization: Optional[str] = Header(None),
):
    raw = (track_id or "").strip()
    if not _valid_playlist_library_track_id(raw):
        raise HTTPException(400, "Invalid track ID format")
    eff = _playlist_library_track_id_stored(raw)
    user = get_user_from_header(authorization)
    uid = user["id"]
    custom = load_custom_playlists(uid)
    idx = next((i for i, p in enumerate(custom) if p["id"] == playlist_id), None)
    if idx is None:
        raise HTTPException(404, "Playlist not found")
    custom[idx]["track_ids"] = [tid for tid in custom[idx].get("track_ids", []) if tid != eff and tid != raw]
    meta = custom[idx].get("track_meta", {})
    for k in (eff, raw):
        if k and k in meta:
            del meta[k]
    custom[idx]["track_meta"] = meta
    save_custom_playlists(uid, custom)
    try:
        import analytics_db

        analytics_db.init_db()
        analytics_db.log_playlist_event(
            telegram_user_id=uid,
            username=user.get("username") or user.get("first_name"),
            playlist_id=playlist_id,
            action="remove_track",
            extra={"track_id": eff},
        )
    except Exception:
        pass
    return {"status": "removed"}


@app.get("/api/playlist/shared/{share_id}")
async def get_shared_playlist(share_id: str):
    """Публичный плейлист по share_id. Без авторизации. Только is_public."""
    shares = load_shares()
    rec = shares.get(share_id)
    if not rec or rec.get("type") != "playlist":
        raise HTTPException(404, "Not found")
    payload = rec.get("payload", {})
    if not payload.get("is_public", True):
        raise HTTPException(404, "Not found")
    return {
        "name": payload.get("name", ""),
        "items": payload.get("tracks", []),
    }


@app.get("/api/track/{track_id}")
async def get_track_info_http(track_id: str):
    """Метаданные трека по id (deep link tr_*). VK или YouTube video id. Без авторизации."""
    info = await _get_track_meta_unified(track_id)
    if not info:
        raise HTTPException(404, "Track not found")
    return info


def _rgb_clamp(v: float) -> int:
    return max(0, min(255, int(round(v))))


def _adjust_rgb(rgb: Tuple[int, int, int], *, brighten: float = 1.0, saturate: float = 1.0) -> Tuple[int, int, int]:
    """HSV-подстройка яркости/насыщенности для градиента фона сторис."""
    import colorsys

    r, g, b = (max(0, min(255, int(x))) / 255.0 for x in rgb)
    h, s, v = colorsys.rgb_to_hsv(r, g, b)
    v = min(1.0, max(0.0, v * brighten))
    s = min(1.0, max(0.0, s * saturate))
    rr, gg, bb = colorsys.hsv_to_rgb(h, s, v)
    return (_rgb_clamp(rr * 255), _rgb_clamp(gg * 255), _rgb_clamp(bb * 255))


def _cover_gradient_stops(cover_rgb: Any) -> Tuple[Tuple[int, int, int], Tuple[int, int, int]]:
    """Два опорных цвета градиента из верхней и нижней половины обложки."""
    from PIL import Image, ImageStat

    sample = cover_rgb.convert("RGB").resize((96, 96), Image.Resampling.LANCZOS)
    sw, sh = sample.size
    top_half = sample.crop((0, 0, sw, sh // 2))
    bot_half = sample.crop((0, sh // 2, sw, sh))
    top_rgb = tuple(int(x) for x in ImageStat.Stat(top_half).mean[:3])
    bot_rgb = tuple(int(x) for x in ImageStat.Stat(bot_half).mean[:3])
    return (
        _adjust_rgb(top_rgb, brighten=1.18, saturate=1.08),
        _adjust_rgb(bot_rgb, brighten=0.72, saturate=1.05),
    )


def _text_width(draw: Any, text: str, font: Any) -> int:
    if hasattr(draw, "textbbox"):
        bbox = draw.textbbox((0, 0), text, font=font)
        return bbox[2] - bbox[0]
    if hasattr(draw, "textsize"):
        return draw.textsize(text, font=font)[0]
    return len(text) * 10


def _line_height(draw: Any, font: Any) -> int:
    if hasattr(draw, "textbbox"):
        bbox = draw.textbbox((0, 0), "Ay", font=font)
        return max(1, bbox[3] - bbox[1])
    return getattr(font, "size", 24)


def _wrap_text_lines(draw: Any, text: str, font: Any, max_width: int, max_lines: int) -> List[str]:
    words = (text or "").split()
    if not words:
        return [""]
    lines: List[str] = []
    current = ""
    consumed = 0
    for word in words:
        consumed += 1
        test = f"{current} {word}".strip()
        if _text_width(draw, test, font) <= max_width:
            current = test
        else:
            if current:
                lines.append(current)
            current = word
            if len(lines) >= max_lines:
                break
    if current and len(lines) < max_lines:
        lines.append(current)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
    full = " ".join(words)
    rendered = " ".join(lines)
    if lines and rendered != full:
        last = lines[-1]
        while last and _text_width(draw, f"{last}…", font) > max_width:
            last = last[:-1].rstrip()
        lines[-1] = f"{last}…" if last else "…"
    return lines or [""]


def _pick_compact_title(draw: Any, title: str, font_path: str, base_size: int, min_size: int, max_width: int) -> Tuple[Any, List[str]]:
    from PIL import ImageFont

    clean = " ".join((title or "Трек").split())
    for size in range(base_size, min_size - 1, -2):
        try:
            font = ImageFont.truetype(font_path, size)
        except OSError:
            font = ImageFont.load_default()
            return font, [clean[:32]]
        lines = _wrap_text_lines(draw, clean, font, max_width, 2)
        if len(lines) <= 2 and all(_text_width(draw, ln, font) <= max_width for ln in lines):
            return font, lines
    try:
        font = ImageFont.truetype(font_path, min_size)
    except OSError:
        font = ImageFont.load_default()
    return font, _wrap_text_lines(draw, clean, font, max_width, 2)


def _story_watermark_logo(logo_path: Path, size: int, opacity: float) -> Optional[Any]:
    """Белый силуэт логотипа без квадратного фона (icon.png — чёрный фон, иначе тёмное «поле»)."""
    from PIL import Image

    if not logo_path.exists():
        return None
    try:
        logo = Image.open(logo_path).convert("RGBA")
        logo = logo.resize((size, size), Image.Resampling.LANCZOS)
        out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        src = logo.load()
        dst = out.load()
        sw, sh = logo.size
        for y in range(sh):
            for x in range(sw):
                r, g, b, a = src[x, y]
                if a < 12:
                    continue
                lum = (r * 0.299 + g * 0.587 + b * 0.114) / 255.0
                # Белые штрихи иконки; чёрный/цветной фон квадрата — прозрачный.
                if lum < 0.45:
                    continue
                strength = min(1.0, ((lum - 0.45) / 0.55) ** 0.75)
                alpha = int(255 * opacity * strength * (a / 255.0))
                if alpha > 1:
                    dst[x, y] = (255, 255, 255, min(255, alpha))
        return out
    except Exception:
        return None


def _build_cover_backdrop(cover_rgb: Any, w: int, h: int) -> Any:
    """Фон сторис: размытая обложка + вертикальный градиент по её цветам + виньетка."""
    from PIL import Image, ImageDraw, ImageFilter

    top_col, bot_col = _cover_gradient_stops(cover_rgb)
    blurred = cover_rgb.convert("RGB").resize((max(64, w // 6), max(96, h // 6)), Image.Resampling.LANCZOS)
    blurred = blurred.filter(ImageFilter.GaussianBlur(radius=16))
    blurred = blurred.resize((w, h), Image.Resampling.LANCZOS)

    gradient = Image.new("RGB", (w, h))
    gdraw = ImageDraw.Draw(gradient)
    denom = max(1, h - 1)
    for y in range(h):
        t = y / denom
        # Плавная кривая: верх светлее, низ глубже
        blend = t ** 1.15
        col = tuple(_rgb_clamp(top_col[i] * (1.0 - blend) + bot_col[i] * blend) for i in range(3))
        gdraw.line([(0, y), (w, y)], fill=col)

    img = Image.blend(blurred, gradient, alpha=0.58)

    # Лёгкое затемнение только у самого низа — без боковых полос и без «тёмного поля» по центру.
    vignette = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    vdraw = ImageDraw.Draw(vignette)
    denom = max(1, h - 1)
    for y in range(h):
        t = y / denom
        bottom_a = int(55 * (max(0.0, (t - 0.82) / 0.18) ** 1.4))
        if bottom_a:
            vdraw.line([(0, y), (w, y)], fill=(0, 0, 0, min(100, bottom_a)))
    return Image.alpha_composite(img.convert("RGBA"), vignette).convert("RGB")


def _make_fallback_png_540x960() -> bytes:
    """Минимальный валидный PNG 540x960 (серый фон) без Pillow — если Pillow не установлен или ошибка."""
    w, h = 540, 960
    r, g, b = 38, 38, 48
    row = bytes([0] + [r, g, b] * w)
    raw = row * h
    # PNG IDAT — raw deflate (без zlib-обёртки), wbits=-15
    z = zlib.compressobj(9, zlib.DEFLATED, -15)
    compressed = z.compress(raw) + z.flush()
    def png_chunk(name: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + name + data + struct.pack(">I", 0xFFFFFFFF & zlib.crc32(name + data))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0)  # 8bit RGB, no filter
    signature = b"\x89PNG\r\n\x1a\n"
    ihdr_chunk = png_chunk(b"IHDR", ihdr)
    idat_chunk = png_chunk(b"IDAT", compressed)
    iend_chunk = png_chunk(b"IEND", b"")
    return signature + ihdr_chunk + idat_chunk + iend_chunk


def _generate_track_card_sync(
    track: Dict,
    cover_bytes: Optional[bytes],
    *,
    width: int = 540,
    height: int = 960,
) -> bytes:
    """Генерирует PNG-карточку 9:16: размытый градиент от обложки, обложка и название по центру, бренд с логотипом."""
    try:
        from PIL import Image, ImageDraw, ImageFont, ImageFilter
    except ImportError as e:
        print(f"⚠️  track-card: Pillow не установлен: {e}. Установите: pip install Pillow")
        return b""
    try:
        w, h = max(360, int(width)), max(640, int(height))
        scale = w / 540.0

        def _square_cover_rgb(src: Image.Image, side: int) -> Image.Image:
            """Квадрат без искажений: центр-кроп по короткой стороне, затем resize."""
            src = src.convert("RGB")
            iw, ih = src.size
            if iw <= 0 or ih <= 0:
                return Image.new("RGB", (side, side), (48, 48, 56))
            edge = min(iw, ih)
            lx = (iw - edge) // 2
            ty = (ih - edge) // 2
            sq = src.crop((lx, ty, lx + edge, ty + edge))
            return sq.resize((side, side), Image.Resampling.LANCZOS)

        font_bold_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        font_reg_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

        cover_full = None
        if cover_bytes:
            try:
                cover_full = Image.open(io.BytesIO(cover_bytes)).convert("RGB")
            except Exception:
                cover_full = None

        # Фон: размытая обложка + градиент по её цветам + мягкая виньетка
        if cover_full is not None:
            try:
                img = _build_cover_backdrop(cover_full, w, h)
            except Exception:
                img = Image.new("RGB", (w, h), (38, 38, 48))
        else:
            img = Image.new("RGB", (w, h), (38, 38, 48))
            overlay = Image.new("RGBA", (w, h))
            odraw = ImageDraw.Draw(overlay)
            for y in range(h):
                t = y / max(1, h - 1)
                a = int(255 * (0.12 + 0.55 * (t ** 1.2)))
                odraw.line([(0, y), (w, y)], fill=(0, 0, 0, min(255, a)))
            img = Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB")
        draw = ImageDraw.Draw(img)
        # Обложка: квадрат по центру (кроп, не растяжение), крупнее под ширину 540
        cover_size = int(448 * scale)
        radius = min(int(40 * scale), cover_size // 9)
        margin_top = int(40 * scale)
        left = (w - cover_size) // 2
        top = margin_top
        if cover_full is not None:
            try:
                cover = _square_cover_rgb(cover_full, cover_size)
                mask = Image.new("L", (cover_size, cover_size), 0)
                mdraw = ImageDraw.Draw(mask)
                mdraw.rounded_rectangle((0, 0, cover_size, cover_size), radius=radius, fill=255)
                img.paste(cover, (left, top), mask=mask)
            except Exception:
                pass

        text_pad = int(40 * scale)
        text_max_w = w - text_pad * 2
        # Логотип — полупрозрачный водяной знак сразу ПОД обложкой (на градиенте, не на фото)
        logo_size = int(200 * scale)
        logo_opacity = 0.30
        logo_y = top + cover_size + int(10 * scale)
        logo_x = (w - logo_size) // 2
        logo_path = Path(__file__).parent / "static" / "icon.png"
        watermark = _story_watermark_logo(logo_path, logo_size, logo_opacity)
        if watermark is not None:
            img = img.convert("RGBA")
            img.paste(watermark, (logo_x, logo_y), watermark)
            img = img.convert("RGB")
            draw = ImageDraw.Draw(img)

        text_y = logo_y + logo_size + int(14 * scale)
        title_raw = track.get("title") or "Трек"
        artist_raw = track.get("artist") or "Исполнитель"

        try:
            font_title, title_lines = _pick_compact_title(
                draw,
                title_raw,
                font_bold_path,
                base_size=max(22, int(34 * scale)),
                min_size=max(18, int(24 * scale)),
                max_width=text_max_w,
            )
            font_artist = ImageFont.truetype(font_reg_path, max(16, int(22 * scale)))
        except OSError:
            font_title = ImageFont.load_default()
            title_lines = [(title_raw or "Трек")[:32]]
            font_artist = font_title

        # Компактный заголовок: до 2 строк, авто-уменьшение шрифта, тень для читаемости
        line_gap = max(4, int(6 * scale))
        lh_title = _line_height(draw, font_title)
        y_cursor = text_y
        for line in title_lines:
            tw = _text_width(draw, line, font_title)
            x = (w - tw) // 2
            draw.text((x + 1, y_cursor + 1), line, fill=(0, 0, 0), font=font_title)
            draw.text((x, y_cursor), line, fill=(255, 255, 255), font=font_title)
            y_cursor += lh_title + line_gap

        artist_clean = " ".join(str(artist_raw).split())
        artist_lines = _wrap_text_lines(draw, artist_clean, font_artist, text_max_w, 1)
        artist_line = artist_lines[0] if artist_lines else ""
        aw = _text_width(draw, artist_line, font_artist)
        artist_y = y_cursor + int(8 * scale)
        draw.text(((w - aw) // 2 + 1, artist_y + 1), artist_line, fill=(0, 0, 0), font=font_artist)
        draw.text(((w - aw) // 2, artist_y), artist_line, fill=(210, 214, 228), font=font_artist)

        buf = io.BytesIO()
        img.save(buf, format="PNG", optimize=True)
        return buf.getvalue()
    except Exception as e:
        print(f"⚠️  track-card: ошибка генерации: {e}")
        return b""


def _png_to_jpeg_bytes(png_bytes: bytes) -> bytes:
    """Конвертация PNG в JPEG для InlineQueryResultPhoto (Telegram принимает только JPEG)."""
    try:
        from PIL import Image
        img = Image.open(io.BytesIO(png_bytes)).convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=92)
        return buf.getvalue()
    except Exception as e:
        print(f"⚠️  png_to_jpeg: {e}")
        return b""


def _jpeg_image_response(
    jpeg_bytes: bytes,
    *,
    filename: str = "story.jpg",
    cache_control: str = "public, max-age=3600",
) -> Response:
    return Response(
        content=jpeg_bytes,
        media_type="image/jpeg",
        headers={
            "Cache-Control": cache_control,
            "Content-Length": str(len(jpeg_bytes)),
            "Content-Disposition": f'inline; filename="{filename}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


async def _track_card_jpeg_bytes_for_canon(canon: str, *, story: bool = False) -> bytes:
    """JPEG 9:16 для сторис / shareToStory."""
    info = await _get_track_meta_unified(canon)
    if not info:
        raise HTTPException(404, "Track not found")
    cover_bytes: Optional[bytes] = None
    cover_url = info.get("cover_url")
    if cover_url:
        session = await get_session()
        cu = str(cover_url)
        img_ua = VK_USER_AGENT
        if "sndcdn.com" in cu or "soundcloud.com" in cu:
            img_ua = "Mozilla/5.0 (compatible; TGPlay/1.0)"
        elif "ytimg.com" in cu or "ggpht.com" in cu or "youtube.com" in cu:
            img_ua = "Mozilla/5.0 (compatible; TGPlay/1.0)"
        try:
            async with session.get(cu, headers={"User-Agent": img_ua}) as resp:
                if resp.status == 200:
                    cover_bytes = await resp.read()
        except Exception:
            pass
    loop = asyncio.get_event_loop()
    card_w, card_h = (1080, 1920) if story else (540, 960)

    def _render() -> bytes:
        return _generate_track_card_sync(info, cover_bytes, width=card_w, height=card_h)

    png_bytes = await loop.run_in_executor(None, _render)
    if not png_bytes:
        png_bytes = _make_fallback_png_540x960()
    jpeg_bytes = _png_to_jpeg_bytes(png_bytes)
    if not jpeg_bytes:
        raise HTTPException(500, "JPEG conversion failed")
    return jpeg_bytes


@app.get("/api/story-card/{track_token}.jpg")
async def get_story_card_jpeg(track_token: str):
    """Публичный JPEG для shareToStory: путь заканчивается на .jpg (Telegram не путает с видео)."""
    canon = _canonical_share_track_id(track_token)
    if not canon:
        raise HTTPException(400, "Invalid track id")
    jpeg_bytes = await _track_card_jpeg_bytes_for_canon(canon, story=True)
    # Telegram агрессивно кэширует media_url сторис — не кэшируем ответ (клиент добавляет ?v=…).
    return _jpeg_image_response(jpeg_bytes, cache_control="no-store, no-cache, must-revalidate")


@app.get("/api/track-card/{track_id}")
async def get_track_card(track_id: str, format: Optional[str] = None):  # noqa: A002 — query param ?format=jpeg для inline
    """PNG-карточка трека для шеринга (превью в истории/чате). ?format=jpeg — для inline (Telegram принимает только JPEG). Без авторизации."""
    cache_key = _canonical_share_track_id(track_id)
    if not cache_key:
        raise HTTPException(400, "Invalid track id")
    now = time.time()
    want_jpeg = (format or "").strip().lower() == "jpeg"
    # Ответ из кеша — без генерации и без запроса VK/обложки
    entry = _card_cache.get(cache_key)
    if entry and (now - entry[1]) < _CARD_CACHE_TTL:
        out = entry[0]
        if want_jpeg:
            out = _png_to_jpeg_bytes(out)
            if not out:
                raise HTTPException(500, "JPEG conversion failed")
            return Response(content=out, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=3600"})
        return Response(
            content=out,
            media_type="image/png",
            headers={"Cache-Control": "public, max-age=3600"},
        )
    info = await _get_track_meta_unified(track_id)
    if not info:
        raise HTTPException(404, "Track not found")
    cover_bytes: Optional[bytes] = None
    cover_url = info.get("cover_url")
    if cover_url:
        session = await get_session()
        cu = str(cover_url)
        img_ua = VK_USER_AGENT
        if "ytimg.com" in cu or "ggpht.com" in cu or "youtube.com" in cu or "googleusercontent.com" in cu:
            img_ua = "Mozilla/5.0 (compatible; TGPlay/1.0)"
        try:
            async with session.get(cu, headers={"User-Agent": img_ua}) as resp:
                if resp.status == 200:
                    cover_bytes = await resp.read()
        except Exception:
            pass
    loop = asyncio.get_event_loop()
    try:
        png_bytes = await loop.run_in_executor(
            None,
            _generate_track_card_sync,
            info,
            cover_bytes,
        )
    except Exception as e:
        print(f"⚠️  track-card executor: {e}")
        png_bytes = b""
    if not png_bytes:
        png_bytes = _make_fallback_png_540x960()
    _card_cache[cache_key] = (png_bytes, now)
    if len(_card_cache) > _CARD_CACHE_MAX:
        cutoff = now - _CARD_CACHE_TTL
        for k in list(_card_cache.keys()):
            if _card_cache[k][1] < cutoff:
                del _card_cache[k]
        while len(_card_cache) > _CARD_CACHE_MAX:
            oldest = min(_card_cache.keys(), key=lambda x: _card_cache[x][1])
            del _card_cache[oldest]
    if want_jpeg:
        jpeg_bytes = _png_to_jpeg_bytes(png_bytes)
        if not jpeg_bytes:
            raise HTTPException(500, "JPEG conversion failed")
        return Response(content=jpeg_bytes, media_type="image/jpeg", headers={"Cache-Control": "public, max-age=3600"})
    return Response(
        content=png_bytes,
        media_type="image/png",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@app.post("/api/track-card/{track_id}/invalidate")
async def invalidate_track_card(track_id: str, authorization: Optional[str] = Header(None)):
    """Сброс кеша карточки трека. Требует авторизации (защита от DoS)."""
    get_user_from_header(authorization)
    cache_key = _canonical_share_track_id(track_id)
    if not cache_key:
        raise HTTPException(400, "Invalid track id")
    _card_cache.pop(cache_key, None)
    return {"ok": True}


@app.post("/api/track-card/invalidate-all")
async def invalidate_all_track_cards(authorization: Optional[str] = Header(None)):
    """Сброс кеша всех карточек треков. Требует авторизации (защита от DoS)."""
    get_user_from_header(authorization)
    n = len(_card_cache)
    _card_cache.clear()
    return {"ok": True, "cleared": n}


def _share_track_html(track_id: str, title: str, artist: str) -> str:
    """Страница шеринга: две гиперссылки (картинка — название трека, бот — Слушать в TGPlay) + OG для превью."""
    bot_link = f"https://t.me/{BOT_USERNAME}?startapp=tr_{_startapp_track_token(track_id)}"
    card_url = f"{WEBAPP_URL_CANONICAL}/api/track-card/{track_id}"
    share_page_url = f"{WEBAPP_URL_CANONICAL}/share/track/{track_id}"
    t = html.escape(title or "Трек")
    og_title = html.escape(f"{title or 'Трек'} — {artist or 'Исполнитель'}")
    og_desc = html.escape(f"Я слушаю {title or 'Трек'} — {artist or 'Исполнитель'} в TGPlay")
    card_url_esc = card_url.replace("&", "&amp;").replace('"', "&quot;")
    bot_link_esc = bot_link.replace("&", "&amp;").replace('"', "&quot;")
    og_image_url = f"{card_url}?format=jpeg"
    return f"""<!DOCTYPE html>
<html prefix="og: http://ogp.me/ns# music: http://ogp.me/ns/music#">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{t}</title>
<meta property="og:type" content="website">
<meta property="og:url" content="{share_page_url}">
<meta property="og:image" content="{og_image_url}">
<meta property="og:image:type" content="image/jpeg">
<meta property="og:image:width" content="540">
<meta property="og:image:height" content="960">
<meta property="og:title" content="{og_title}">
<meta property="og:description" content="{og_desc}">
</head>
<body style="font-family:system-ui;padding:1rem;max-width:360px;margin:0 auto;">
<p><a href="{card_url_esc}">{t}</a></p>
<p><a href="{bot_link_esc}">Слушать в TGPlay</a></p>
<p style="color:#666;font-size:0.9em;">Перенаправление…</p>
<script>setTimeout(function() {{ location.href = {json.dumps(bot_link)}; }}, 800);</script>
</body>
</html>"""


@app.get("/share/track/{track_id}")
async def share_track_page(track_id: str):
    """Страница шеринга трека: OG-превью для ссылки + редирект на бота."""
    canon = _canonical_share_track_id(track_id)
    if not canon:
        raise HTTPException(400, "Invalid track id")
    info = await _get_track_meta_unified(canon)
    if not info:
        raise HTTPException(404, "Track not found")
    title = info.get("title") or "Трек"
    artist = info.get("artist") or "Исполнитель"
    html = _share_track_html(canon, title, artist)
    return Response(content=html, media_type="text/html; charset=utf-8")


@app.get("/s/{track_id}")
async def share_short(track_id: str):
    """Короткая ссылка для шеринга: превью = карточка (og:image на track-card), редирект в бота. В сообщении показывается /s/xxx, а не api/track-card — ссылка на карточку невидима."""
    canon = _canonical_share_track_id(track_id)
    if not canon:
        raise HTTPException(400, "Invalid track id")
    info = await _get_track_meta_unified(canon)
    if not info:
        raise HTTPException(404, "Track not found")
    title = info.get("title") or "Трек"
    artist = info.get("artist") or "Исполнитель"
    html = _share_track_html(canon, title, artist)
    return Response(content=html, media_type="text/html; charset=utf-8")


@app.post("/api/playlist/share")
async def create_playlist_share(
    request: Request,
    authorization: Optional[str] = Header(None),
):
    """Создать публичную ссылку на плейлист. Тело: { \"playlist_id\": \"...\" } или playlist_id = \"favorites\"."""
    user = get_user_from_header(authorization)
    uid = user["id"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    playlist_id = body.get("playlist_id") or ""
    favorites = load_playlist(uid)
    custom = load_custom_playlists(uid)
    if playlist_id == "favorites":
        name = "Избранное"
        tracks = list(reversed(favorites))
    else:
        pl = next((p for p in custom if p["id"] == playlist_id), None)
        if not pl:
            raise HTTPException(404, "Playlist not found")
        name = pl.get("name", "")
        track_ids = pl.get("track_ids", [])
        track_meta = pl.get("track_meta", {})
        tracks = _tracks_by_ids(uid, track_ids, favorites, track_meta)
    share_id = uuid.uuid4().hex
    shares = load_shares()
    shares[share_id] = {
        "type": "playlist",
        "created_at": time.time(),
        "payload": {
            "name": name,
            "is_public": True,
            "tracks": [_safe_track(t) for t in tracks],
        },
    }
    save_shares(shares)
    if playlist_id and playlist_id != "favorites":
        custom = load_custom_playlists(uid)
        for p in custom:
            if p["id"] == playlist_id:
                p["share_id"] = share_id
                p["is_public"] = True
                break
        save_custom_playlists(uid, custom)
    try:
        import analytics_db

        analytics_db.init_db()
        analytics_db.log_playlist_event(
            telegram_user_id=uid,
            username=user.get("username") or user.get("first_name"),
            playlist_id=playlist_id or "favorites",
            action="share_playlist",
            extra={"share_id": share_id, "name": name},
        )
        analytics_db.log_button_click(
            telegram_user_id=uid,
            username=user.get("username") or user.get("first_name"),
            button_id="button_share_playlist",
            context="playlist_share",
            extra={"playlist_id": playlist_id or "favorites", "share_id": share_id},
        )
    except Exception:
        pass
    return {"share_id": share_id, "url": f"https://t.me/tgplayxbot?start=pl_{share_id}"}


# ─── Telegram file_id кеш ─────────────────────────────────────────
# Когда бот отправляет файл, Telegram возвращает file_id.
# По file_id повторная отправка — мгновенная (0 upload, <1s).
# Это то, как быстрые музыкальные боты отдают треки за 2-3 секунды.

_TG_FILEID_DB = Path(__file__).parent / "tg_fileid.json"
_tg_fileid_cache: Dict[str, str] = {}

def _load_fileid_cache():
    global _tg_fileid_cache
    if _TG_FILEID_DB.exists():
        try:
            _tg_fileid_cache = json.loads(_TG_FILEID_DB.read_text())
        except Exception:
            _tg_fileid_cache = {}

def _save_fileid_cache():
    try:
        _TG_FILEID_DB.write_text(json.dumps(_tg_fileid_cache, ensure_ascii=False))
    except Exception:
        pass

def _get_tg_file_id(track_id: str) -> Optional[str]:
    return _tg_fileid_cache.get(track_id)

def _set_tg_file_id(track_id: str, file_id: str):
    _tg_fileid_cache[track_id] = file_id
    _save_fileid_cache()

_load_fileid_cache()

# ─── MP3 кеш на диске ────────────────────────────────────────────

CACHE_DIR = Path(__file__).parent / "mp3_cache"
CACHE_DIR.mkdir(exist_ok=True)
_MAX_CACHE_FILES = 200
# Ограничение одновременных ffmpeg/загрузок — без этого при нагрузке OOM и падение сервера
_mp3_concurrency: Optional[asyncio.Semaphore] = None

def _get_mp3_semaphore() -> asyncio.Semaphore:
    global _mp3_concurrency
    if _mp3_concurrency is None:
        _mp3_concurrency = asyncio.Semaphore(5)
    return _mp3_concurrency

def _cache_mp3_path(track_id: str) -> Path:
    # Только валидный формат VK — защита от path traversal
    if not _valid_track_id(track_id):
        track_id = hashlib.sha256(track_id.encode()).hexdigest()[:32]
    return CACHE_DIR / f"{track_id}.mp3"

def _cleanup_cache():
    """Удаляем самые старые файлы если кеш переполнен."""
    files = sorted(CACHE_DIR.glob("*.mp3"), key=lambda f: f.stat().st_mtime)
    while len(files) > _MAX_CACHE_FILES:
        files.pop(0).unlink(missing_ok=True)


def _is_hls_url(url: str) -> bool:
    return ".m3u8" in url.lower() or "/index.m3u8" in url.lower()


async def _download_hls_segments(url: str) -> Optional[bytes]:
    """Скачивает HLS сегменты с ручной AES-128 расшифровкой.
    VK шифрует часть сегментов AES-128 → ffmpeg 4.x теряет данные.
    Ручная загрузка: все 186s вместо 126s из ffmpeg."""
    session = await get_session()
    try:
        async with session.get(url, headers={"User-Agent": VK_USER_AGENT}) as r:
            m3u8_text = await r.text()
    except Exception as e:
        print(f"⚠️ Failed to fetch m3u8: {e}")
        return None

    base_url = url.rsplit("/", 1)[0]
    lines = m3u8_text.strip().splitlines()

    key_url = None
    encrypted = False
    segments: List[tuple] = []

    for line in lines:
        if line.startswith("#EXT-X-KEY:"):
            if "METHOD=AES-128" in line:
                encrypted = True
                m = re.search(r'URI="([^"]+)"', line)
                if m:
                    key_url = m.group(1)
            elif "METHOD=NONE" in line:
                encrypted = False
        elif not line.startswith("#") and line.strip():
            seg_url = line.strip()
            if not seg_url.startswith("http"):
                seg_url = base_url + "/" + seg_url
            segments.append((seg_url, encrypted, key_url))

    if not segments:
        print("⚠️ No segments found in m3u8")
        return None

    # Скачиваем AES ключи
    keys: Dict[str, bytes] = {}
    for _, enc, ku in segments:
        if enc and ku and ku not in keys:
            try:
                async with session.get(ku, headers={"User-Agent": VK_USER_AGENT}) as r:
                    keys[ku] = await r.read()
            except Exception as e:
                print(f"⚠️ Failed to fetch AES key: {e}")
                return None

    has_crypto = False
    try:
        from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
        has_crypto = True
    except ImportError:
        print("⚠️ cryptography not installed, encrypted segments will fail")

    async def _dl_one(idx: int, seg_url: str, enc: bool, ku: str) -> bytes:
        try:
            async with session.get(seg_url, headers={"User-Agent": VK_USER_AGENT}) as r:
                seg_data = await r.read()
        except Exception as e:
            print(f"⚠️ Segment {idx} download failed: {e}")
            return b""
        if enc and ku in keys and has_crypto:
            try:
                iv = idx.to_bytes(16, "big")
                cipher = Cipher(algorithms.AES(keys[ku]), modes.CBC(iv))
                d = cipher.decryptor()
                seg_data = d.update(seg_data) + d.finalize()
                pad_len = seg_data[-1]
                if 0 < pad_len <= 16:
                    seg_data = seg_data[:-pad_len]
            except Exception as e:
                print(f"⚠️ Segment {idx} decrypt failed: {e}")
        return seg_data

    tasks = [_dl_one(i, u, e, k) for i, (u, e, k) in enumerate(segments)]
    parts = await asyncio.gather(*tasks)
    raw = bytearray()
    for p in parts:
        raw.extend(p)

    if len(raw) < 10000:
        print(f"⚠️ Raw HLS data too small: {len(raw)} bytes")
        return None
    if len(raw) > TG_MAX_FILE_BYTES:
        print(f"⚠️ HLS total {len(raw)//1024}KB > 50MB, skip process")
        return None

    return bytes(raw)


async def _get_mp3_data(track_id: str, url: str) -> Optional[bytes]:
    """Скачивает трек: HLS → ручная загрузка + remux (копия кодека, ~0.17s).
    Remux 320kbps: быстрая обработка без перекодировки.
    Файлы > 50 MB не качаем и не держим в памяти (лимит Telegram, защита от тормозов)."""
    cache_path = _cache_mp3_path(track_id)
    if cache_path.exists():
        try:
            size = cache_path.stat().st_size
        except OSError:
            size = 0
        if size > TG_MAX_FILE_BYTES:
            cache_path.unlink(missing_ok=True)
            print(f"⚠️ Cache too large for TG ({size//1024}KB), removed: {track_id}")
            return None
        data = cache_path.read_bytes()
        if len(data) > 50000:
            print(f"⚡ Cache hit: {track_id} ({len(data)//1024}KB)")
            return data
        cache_path.unlink(missing_ok=True)

    async with _get_mp3_semaphore():
        t0 = time.time()

        if _is_hls_url(url):
            print(f"📥 HLS download: {track_id}")
            raw_data = await _download_hls_segments(url)
            if not raw_data:
                return await _ffmpeg_direct(track_id, url, cache_path)
            t_dl = time.time()
            print(f"  Segments: {len(raw_data)//1024}KB in {t_dl-t0:.1f}s")
            mp3_data = await _ffmpeg_remux_ts(raw_data)
            if not mp3_data:
                mp3_data = await _ffmpeg_reencode_stdin(raw_data)
        else:
            print(f"📥 Direct download: {track_id}")
            session = await get_session()
            try:
                async with session.get(url, headers={"User-Agent": VK_USER_AGENT}) as r:
                    cl = r.content_length
                    if cl is not None and cl > TG_MAX_FILE_BYTES:
                        print(f"⚠️ Content-Length {cl//1024}KB > 50MB, skip: {track_id}")
                        return None
                    if cl is not None:
                        raw_data = await r.read()
                    else:
                        raw_data = bytearray()
                        while True:
                            chunk = await r.content.read(512 * 1024)
                            if not chunk:
                                break
                            raw_data.extend(chunk)
                            if len(raw_data) > TG_MAX_FILE_BYTES:
                                print(f"⚠️ Stream > 50MB, abort: {track_id}")
                                return None
                        raw_data = bytes(raw_data)
            except Exception as e:
                print(f"⚠️ Direct download failed: {e}")
                return None
            t_dl = time.time()
            print(f"  Downloaded: {len(raw_data)//1024}KB in {t_dl-t0:.1f}s")

            if raw_data[:3] == b"ID3" or raw_data[:2] in (b"\xff\xfb", b"\xff\xf3", b"\xff\xf2"):
                mp3_data = raw_data
            else:
                mp3_data = await _ffmpeg_remux_ts(raw_data)
                if not mp3_data:
                    mp3_data = await _ffmpeg_reencode_stdin(raw_data)

        if not mp3_data or len(mp3_data) < 50000:
            print(f"⚠️ Result too small ({len(mp3_data) if mp3_data else 0} bytes)")
            return None

        t_total = time.time()
        print(f"✅ {track_id}: {len(mp3_data)//1024}KB total {t_total-t0:.1f}s")

        try:
            cache_path.write_bytes(mp3_data)
            _cleanup_cache()
        except Exception:
            pass

        return mp3_data


async def _ffmpeg_remux_ts(raw_data: bytes) -> Optional[bytes]:
    """Извлекает MP3 из TS-контейнера без перекодировки (-c:a copy). ~0.1s."""
    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-i", "pipe:0", "-vn", "-c:a", "copy", "-f", "mp3", "pipe:1",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    mp3_data, stderr = await proc.communicate(input=raw_data)
    if proc.returncode != 0 or not mp3_data or len(mp3_data) < 10000:
        err = stderr.decode(errors="replace")[:300] if stderr else ""
        print(f"⚠️ ffmpeg remux failed: {err}")
        return None
    return mp3_data


def _ascii_filename_part(name: str) -> str:
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", (name or "").strip())
    return (safe or "track")[:180]


def _content_disposition_attachment(filename: str) -> str:
    ascii_name = _ascii_filename_part(filename)
    if ascii_name == filename:
        return f'attachment; filename="{ascii_name}"'
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(filename)}"


async def _ffmpeg_tag_mp3_bytes(
    mp3_data: bytes,
    *,
    title: str,
    artist: str,
    duration_sec: int = 0,
) -> bytes:
    """ID3-теги и длительность для MP3 (Telegram / скачивание в файловый менеджер)."""
    if not mp3_data or len(mp3_data) < 1000:
        return mp3_data
    meta_args = [
        "-metadata",
        f"title={(title or 'Track')[:200]}",
        "-metadata",
        f"artist={(artist or 'Artist')[:200]}",
    ]
    if duration_sec > 0:
        meta_args.extend(["-metadata", f"length={duration_sec}"])
    cmd = [
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
        *meta_args,
        "-write_id3v2",
        "1",
        "-id3v2_version",
        "3",
        "-f",
        "mp3",
        "pipe:1",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    out, stderr = await proc.communicate(input=mp3_data)
    if proc.returncode != 0 or not out or len(out) < 1000:
        err = stderr.decode(errors="replace")[:200] if stderr else ""
        print(f"⚠️ ffmpeg id3 tag failed: {err}")
        return mp3_data
    return out


async def _ffmpeg_reencode_stdin(raw_data: bytes) -> Optional[bytes]:
    """Перекодирует аудио из stdin в MP3 192kbps (fallback если remux не сработал)."""
    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-i", "pipe:0",
        "-vn", "-acodec", "libmp3lame", "-b:a", "192k", "-ar", "44100", "-ac", "2",
        "-f", "mp3", "pipe:1",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    mp3_data, stderr = await proc.communicate(input=raw_data)
    if proc.returncode != 0 or not mp3_data:
        err = stderr.decode(errors="replace")[:300] if stderr else ""
        print(f"⚠️ ffmpeg re-encode failed: {err}")
        return None
    return mp3_data


async def _ffmpeg_direct(track_id: str, url: str, cache_path: Path) -> Optional[bytes]:
    """Fallback: ffmpeg напрямую с URL."""
    cmd = [
        FFMPEG, "-hide_banner", "-loglevel", "error", "-y",
        "-user_agent", VK_USER_AGENT, "-i", url,
        "-vn", "-c:a", "copy", "-f", "mp3", "pipe:1",
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    mp3_data, stderr = await proc.communicate()
    if proc.returncode != 0 or not mp3_data or len(mp3_data) < 50000:
        return None
    try:
        cache_path.write_bytes(mp3_data)
        _cleanup_cache()
    except Exception:
        pass
    return mp3_data


# ─── Send track to Telegram bot chat ─────────────────────────────

def _notify_bot_audio_delivered(chat_id: int, track_id: str) -> None:
    """Успешный sendAudio в чат пользователя: SQLite для UI + событие аналитики (раньше писалось при постановке в очередь)."""
    try:
        import analytics_db

        analytics_db.init_db()
        analytics_db.record_bot_audio_delivered(int(chat_id), track_id)
        analytics_db.log_track_usage(
            telegram_user_id=int(chat_id),
            username=None,
            track_id=track_id,
            action="download_to_bot",
        )
    except Exception:
        pass


async def _fetch_track_info(track_id: str) -> Dict:
    """Получает инфо о треке через execute (token pool + batching)."""
    sid = parse_soundcloud_track_id(track_id)
    if sid is not None and _sc_ready():
        meta = await _sc_track_meta(build_soundcloud_track_id(sid))
        return meta or {}
    items = await _vk_batch_get_by_id([track_id])
    return items[0] if items else {}


async def _send_youtube_track_to_telegram(chat_id: int, video_id: str) -> None:
    """YouTube video id → полное скачивание mp3 → sendAudio (без VK file_id)."""
    vid = (video_id or "").strip()
    if not _is_youtube_video_id(vid):
        return
    t0 = time.time()
    watch = f"https://www.youtube.com/watch?v={vid}"
    file_path: Optional[str] = None
    thumb_path: Optional[str] = None
    try:
        try:
            info = await download_youtube_audio(watch)
        except Exception as e:
            print(f"⚠️ [bg] YT download failed {vid}: {e}")
            return
        file_path = str(info.get("file_path") or "")
        thumb_path = info.get("thumbnail_path") if isinstance(info.get("thumbnail_path"), str) else None
        if not file_path or not os.path.isfile(file_path):
            print(f"⚠️ [bg] YT no mp3 file for {vid}")
            return
        with open(file_path, "rb") as f:
            mp3_data = f.read()
        if len(mp3_data) > TG_MAX_FILE_BYTES:
            print(f"⚠️ [bg] YT file too large for Telegram ({len(mp3_data)//1024}KB)")
            return
        title = str(info.get("title") or "Unknown")[:100]
        artist = str(info.get("performer") or "Unknown")[:100]
        duration = int(info.get("duration") or 0)
        print(f"📤 [bg] YT {artist} — {title} ({duration}s) ready in {time.time()-t0:.1f}s")

        tg_session = await _get_tg_upload_session()
        tg_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendAudio"
        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id))
        form.add_field("title", title)
        form.add_field("performer", artist)
        if duration > 0:
            form.add_field("duration", str(duration))
        form.add_field("audio", mp3_data, filename=f"{artist} - {title}.mp3", content_type="audio/mpeg")

        async with tg_session.post(tg_url, data=form) as resp:
            result = await resp.json()
        if not result.get("ok"):
            print(f"⚠️ [bg] YT TG error: {result.get('description')}")
            return
        _notify_bot_audio_delivered(chat_id, vid)
        try:
            audio_obj = result.get("result", {}).get("audio", {})
            fid = audio_obj.get("file_id")
            if fid:
                _set_tg_file_id(vid, fid)
                print(f"💾 [bg] YT file_id saved for {vid}")
        except Exception:
            pass
        print(f"✅ [bg] YT sent in {time.time()-t0:.1f}s")
    finally:
        for p in (file_path, thumb_path):
            if p:
                try:
                    os.remove(p)
                except Exception:
                    pass


async def _send_track_to_telegram(chat_id: int, track_id: str) -> None:
    """Отправка трека в Telegram. Стратегия скорости:
    1. Есть file_id → отправка по file_id (~0.3s, без upload)
    2. Нет file_id → download HLS + remux + upload bytes, сохранить file_id"""
    tid = (track_id or "").strip()
    if _is_youtube_video_id(tid):
        await _send_youtube_track_to_telegram(chat_id, tid)
        return
    sc_id = parse_soundcloud_track_id(tid)
    if sc_id is not None and _sc_ready():
        canon_sc = build_soundcloud_track_id(sc_id)
        track_info = await _fetch_track_info(canon_sc)
        title = str(track_info.get("title") or "Track")[:100]
        artist = str(track_info.get("artist") or "Artist")[:100]
        duration = int(track_info.get("duration") or 0)
        url = await _sc_resolve_url(canon_sc)
        if not url:
            return
        mp3_data = await _get_mp3_data(canon_sc, url)
        if not mp3_data:
            print(f"⚠️ [bg] SC mp3 download failed: {canon_sc}")
            return
        mp3_data = await _ffmpeg_tag_mp3_bytes(
            mp3_data, title=title, artist=artist, duration_sec=duration
        )
        fname = _ascii_filename_part(f"{artist} - {title}.mp3")
        tg_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendAudio"
        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id))
        form.add_field("title", title)
        form.add_field("performer", artist)
        if duration > 0:
            form.add_field("duration", str(duration))
        form.add_field("audio", mp3_data, filename=fname, content_type="audio/mpeg")
        try:
            tg_session = await _get_tg_upload_session()
            async with tg_session.post(tg_url, data=form) as resp:
                result = await resp.json()
            if result.get("ok"):
                _notify_bot_audio_delivered(chat_id, tid)
                return
            print(f"⚠️ [bg] SC sendAudio failed: {result.get('description')}")
        except Exception as e:
            print(f"⚠️ [bg] SC sendAudio exception: {e}")
        return
    if not _valid_track_id(tid):
        return

    t0 = time.time()

    # ─── Шаг 0: проверяем file_id кеш ───
    cached_fid = _get_tg_file_id(tid)
    if cached_fid:
        track_info = await _fetch_track_info(tid)
        title = track_info.get("title", "Unknown")[:100]
        artist = track_info.get("artist", "Unknown")[:100]
        duration = track_info.get("duration", 0)
        print(f"⚡ [bg] file_id hit: {artist} — {title}")

        tg_session = await _get_tg_upload_session()
        payload: Dict[str, Any] = {
            "chat_id": chat_id, "audio": cached_fid,
            "title": title, "performer": artist,
        }
        if duration and duration > 0:
            payload["duration"] = duration
        try:
            async with tg_session.post(
                f"https://api.telegram.org/bot{BOT_TOKEN}/sendAudio",
                json=payload,
            ) as resp:
                result = await resp.json()
            if result.get("ok"):
                print(f"✅ [bg] Sent via file_id in {time.time()-t0:.1f}s")
                _notify_bot_audio_delivered(chat_id, tid)
                return
            print(f"⚠️ [bg] file_id failed: {result.get('description')}, re-uploading")
        except Exception as e:
            print(f"⚠️ [bg] file_id error: {e}, re-uploading")

    # ─── Шаг 1: получаем URL + инфо параллельно ───
    url, track_info = await asyncio.gather(
        vk_get_audio_url(tid), _fetch_track_info(tid),
        return_exceptions=True,
    )
    if isinstance(url, Exception) or not url:
        print(f"⚠️ [bg] No VK url for {tid}: {url}")
        return
    if isinstance(track_info, Exception):
        track_info = {}

    title = track_info.get("title", "Unknown")[:100]
    artist = track_info.get("artist", "Unknown")[:100]
    duration = track_info.get("duration", 0)
    t1 = time.time()
    print(f"📤 [bg] {artist} — {title} ({duration}s) resolve {t1-t0:.1f}s")

    # ─── Шаг 2: download + convert ───
    mp3_data = await _get_mp3_data(tid, url)
    if not mp3_data:
        print(f"⚠️ [bg] MP3 failed for {tid}")
        return
    t2 = time.time()
    print(f"📦 [bg] {len(mp3_data)//1024}KB ready in {t2-t1:.1f}s, uploading...")

    if len(mp3_data) > TG_MAX_FILE_BYTES:
        print(f"⚠️ [bg] File too large for Telegram ({len(mp3_data)//1024}KB > 50MB), skip upload")
        return

    # ─── Шаг 3: upload в Telegram ───
    tg_session = await _get_tg_upload_session()
    tg_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendAudio"
    form = aiohttp.FormData()
    form.add_field("chat_id", str(chat_id))
    form.add_field("title", title)
    form.add_field("performer", artist)
    if duration and duration > 0:
        form.add_field("duration", str(duration))
    form.add_field("audio", mp3_data, filename=f"{artist} - {title}.mp3", content_type="audio/mpeg")

    try:
        async with tg_session.post(tg_url, data=form) as resp:
            result = await resp.json()
    except Exception as e:
        print(f"⚠️ [bg] TG upload error: {e}")
        return

    t3 = time.time()
    if not result.get("ok"):
        print(f"⚠️ [bg] TG error: {result.get('description')}")
        return

    _notify_bot_audio_delivered(chat_id, tid)

    # ─── Шаг 4: сохраняем file_id для мгновенных повторов ───
    try:
        audio_obj = result.get("result", {}).get("audio", {})
        fid = audio_obj.get("file_id")
        if fid:
            _set_tg_file_id(tid, fid)
            print(f"💾 [bg] file_id saved for {tid}")
    except Exception:
        pass

    print(f"✅ [bg] Sent in {t3-t0:.1f}s (upload {t3-t2:.1f}s)")


@app.post("/api/send-to-bot/{track_id}")
async def send_to_bot(
    track_id: str,
    authorization: Optional[str] = Header(None),
    background: BackgroundTasks = None,
):
    """Эндпоинт для Mini App: быстро подтверждает запрос и
    отправляет трек в чат в фоне, чтобы ничего не «висело»."""
    user = get_user_from_header(authorization)
    chat_id = user["id"]

    raw = (track_id or "").strip()
    if not _valid_playlist_library_track_id(raw):
        raise HTTPException(400, "Invalid track ID format")
    eff = _playlist_library_track_id_stored(raw)

    if background is None:
        # fallback (не должен срабатывать, но на всякий случай)
        asyncio.create_task(_send_track_to_telegram(chat_id, eff))
    else:
        background.add_task(_send_track_to_telegram, chat_id, eff)

    return {"status": "queued", "chat_id": chat_id}


@app.post("/api/share/send-card-to-me")
async def share_send_card_to_me(
    request: Request,
    authorization: Optional[str] = Header(None),
):
    """Mini App: отправить карточку трека (PNG + подпись-ссылка) в чат пользователя с ботом.
    Юзер потом просто пересылает это сообщение другу — без кнопок и взаимодействия с ботом."""
    user = get_user_from_header(authorization)
    chat_id = user["id"]
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "JSON body required")
    track_id = (body.get("track_id") or "").strip()
    canon = _canonical_share_track_id(track_id)
    if not canon:
        raise HTTPException(400, "Invalid track_id")
    card_url = f"{WEBAPP_URL_CANONICAL}/api/track-card/{canon}"
    bot_link = f"https://t.me/{BOT_USERNAME}?startapp=tr_{_startapp_track_token(canon)}"
    session = await get_session()
    async with session.get(card_url) as card_resp:
        if card_resp.status != 200:
            raise HTTPException(502, "Card generation failed")
        card_bytes = await card_resp.read()
    form = aiohttp.FormData()
    form.add_field("chat_id", str(chat_id))
    form.add_field("caption", "")
    form.add_field("photo", card_bytes, filename="card.png", content_type="image/png")
    form.add_field("reply_markup", json.dumps({
        "inline_keyboard": [[{"text": "Слушать в TGPlay", "url": bot_link}]],
    }))
    async with session.post(f"{TG_API}/sendPhoto", data=form) as resp:
        if resp.status != 200:
            body_text = await resp.text()
            print(f"⚠️  send-card-to-me sendPhoto: {resp.status} {body_text[:200]}")
            raise HTTPException(502, "Failed to send photo")
    try:
        import analytics_db

        analytics_db.init_db()
        analytics_db.log_button_click(
            telegram_user_id=chat_id,
            username=user.get("username") or user.get("first_name"),
            button_id="share_card_to_self",
            context="share_track_sheet",
            extra={"track_id": track_id},
        )
    except Exception:
        pass
    return {"ok": True}


# ─── Нативный выбор пользователя для шеринга (KeyboardButtonRequestUsers) ─

@app.post("/api/share/request-user-picker")
async def share_request_user_picker(
    request: Request,
    authorization: Optional[str] = Header(None),
):
    """Mini App вызывает: «Поделиться → Пользователям». Бот шлёт юзеру сообщение с кнопкой «Выбрать друга»;
    после выбора контакта бот отправит выбранному карточку трека (фото + подпись)."""
    user = get_user_from_header(authorization)
    sender_id = user["id"]
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "JSON body required")
    track_id = (body.get("track_id") or "").strip()
    canon = _canonical_share_track_id(track_id)
    if not canon:
        raise HTTPException(400, "Invalid track_id")
    request_id = random.randint(1, 2**31 - 1)
    now = time.time()
    _pending_share[(sender_id, request_id)] = (canon, now)
    if len(_pending_share) > _PENDING_SHARE_MAX:
        cutoff = now - _PENDING_SHARE_TTL
        for k in list(_pending_share.keys()):
            if _pending_share[k][1] < cutoff:
                del _pending_share[k]
        while len(_pending_share) > _PENDING_SHARE_MAX:
            oldest = min(_pending_share.keys(), key=lambda x: _pending_share[x][1])
            del _pending_share[oldest]
    session = await get_session()
    payload = {
        "chat_id": sender_id,
        "text": "Кому отправим трек? Выбери друга из списка контактов.",
        "reply_markup": {
            "keyboard": [[
                {
                    "text": "Выбрать друга",
                    "request_users": {
                        "request_id": request_id,
                        "user_is_bot": False,
                        "max_quantity": 1,
                    },
                },
            ]],
            "one_time_keyboard": True,
            "resize_keyboard": True,
        },
    }
    async with session.post(f"{TG_API}/sendMessage", json=payload) as resp:
        if resp.status != 200:
            body_text = await resp.text()
            print(f"⚠️  share request-user-picker sendMessage: {resp.status} {body_text[:200]}")
            raise HTTPException(502, "Failed to send keyboard")
    try:
        import analytics_db

        analytics_db.init_db()
        analytics_db.log_button_click(
            telegram_user_id=sender_id,
            username=user.get("username") or user.get("first_name"),
            button_id="share_request_user_picker",
            context="share_track_sheet",
            extra={"track_id": track_id, "request_id": request_id},
        )
    except Exception:
        pass
    return {"ok": True, "request_id": request_id}


# ─── Prepared message для shareMessage (Mini App API 7.10+) ───────────────────
# Двухэтапный шеринг: backend вызывает savePreparedInlineMessage → prepared_message_id,
# frontend вызывает Telegram.WebApp.shareMessage(id) → нативный выбор чата → бот отправляет сообщение.
# Если Bot API вернёт 404 (метод не в HTTP API) — возвращаем 501, клиент использует fallback (share/url).

@app.post("/api/share/prepare-message")
@app.post("/api/prepare-share")
async def share_prepare_message(
    request: Request,
    authorization: Optional[str] = Header(None),
):
    """Готовит сообщение для shareMessage: фото-карточка трека + подпись + кнопка «Слушать».
    Возвращает prepared_message_id для вызова Telegram.WebApp.shareMessage(id).
    При недоступности метода в Bot API — 501 (клиент делает fallback на t.me/share/url)."""
    user = get_user_from_header(authorization)
    user_id = user["id"]
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "JSON body required")
    track_id = (body.get("track_id") or "").strip()
    canon = _canonical_share_track_id(track_id)
    if not canon:
        raise HTTPException(400, "Invalid track_id")
    card_url = f"{WEBAPP_URL_CANONICAL}/api/track-card/{canon}?format=jpeg"
    bot_link = f"https://t.me/{BOT_USERNAME}?startapp=tr_{_startapp_track_token(canon)}"
    result_id = re.sub(r"[^a-zA-Z0-9_]", "", _startapp_track_token(canon))[:64] or "card"
    title = "Зацени трек!"
    caption = "🎧 Слушаю этот трек в TGPlay"
    info = await _get_track_meta_unified(canon)
    if info:
        title = (info.get("title") or "Трек").strip()[:100]
        caption = f"🎧 {info.get('artist') or 'Исполнитель'} — {title}"

    result = {
        "type": "photo",
        "id": result_id,
        "photo_url": card_url,
        "title": title[:64],
        "caption": caption[:1024],
        "reply_markup": {
            "inline_keyboard": [[
                {"text": "Слушать 🎧", "url": bot_link}
            ]]
        }
    }
    api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/savePreparedInlineMessage"
    payload = {"user_id": user_id, "result": result}
    session = await get_session()
    resp = await session.post(api_url, json=payload)
    try:
        data = await resp.json()
    except Exception:
        data = {}
    if resp.status == 200 and data.get("ok"):
        res = data.get("result")
        if isinstance(res, dict):
            prepared_id = res.get("id") or res.get("prepared_message_id")
        elif isinstance(res, str):
            prepared_id = res
        else:
            prepared_id = data.get("prepared_message_id")
        if prepared_id:
            return {"ok": True, "prepared_message_id": str(prepared_id)}
    desc = (data.get("description") or "") or str(resp.status)
    if "method" in desc.lower() or resp.status == 404:
        raise HTTPException(501, "savePreparedInlineMessage not available")
    raise HTTPException(502, desc[:200] or "savePreparedInlineMessage failed")


# ─── Telegram Webhook (ответ на /start, /playlist) ─────────────────

TG_API = f"https://api.telegram.org/bot{BOT_TOKEN}"


async def _handle_users_shared(update: dict) -> None:
    """После выбора контакта через KeyboardButtonRequestUsers: отправляем выбранному карточку трека (фото+подпись).
    Если бот не может написать пользователю (403), пишем отправителю: отправить ссылку самому."""
    try:
        message = update.get("message")
        if not message:
            return
        users_shared = message.get("users_shared")
        if not users_shared:
            return
        sender_id = (message.get("from") or {}).get("id")
        if not sender_id:
            return
        request_id = users_shared.get("request_id")
        users_list = users_shared.get("users") or []
        if request_id is None or not users_list:
            return
        key = (sender_id, request_id)
        entry = _pending_share.pop(key, None)
        if not entry:
            session = await get_session()
            await session.post(
                f"{TG_API}/sendMessage",
                json={
                    "chat_id": sender_id,
                    "text": "Время выбора истекло. Нажми «Поделиться» в приложении ещё раз.",
                    "reply_markup": {"remove_keyboard": True},
                },
            )
            return
        track_id, _ = entry
        canon = _canonical_share_track_id(track_id)
        if not canon:
            return
        target_user_id = users_list[0].get("user_id") if isinstance(users_list[0], dict) else getattr(users_list[0], "user_id", None)
        if target_user_id is None:
            return
        card_url = f"{WEBAPP_URL_CANONICAL}/api/track-card/{canon}"
        bot_link = f"https://t.me/{BOT_USERNAME}?startapp=tr_{_startapp_track_token(canon)}"
        session = await get_session()
        # Невидимая ссылка (&#8203; = zero-width space): Telegram подтягивает превью с card_url, но текст ссылки не виден. Затем видимая ссылка на трек.
        card_url_escaped = card_url.replace("&", "&amp;").replace('"', "&quot;")
        html_text = f'<a href="{card_url_escaped}">&#8203;</a> {bot_link}'
        send_resp = await session.post(
            f"{TG_API}/sendMessage",
            json={
                "chat_id": target_user_id,
                "text": html_text,
                "parse_mode": "HTML",
            },
        )
        if send_resp.status == 200:
            await session.post(
                f"{TG_API}/sendMessage",
                json={
                    "chat_id": sender_id,
                    "text": "Готово! Карточку отправил.",
                    "reply_markup": {"remove_keyboard": True},
                },
            )
        else:
            err_body = await send_resp.text()
            if "403" in err_body or "blocked" in err_body.lower() or "bot was blocked" in err_body.lower():
                await session.post(
                    f"{TG_API}/sendMessage",
                    json={
                        "chat_id": sender_id,
                        "text": f"Этот пользователь ещё не запускал TGPlay. Отправь ему ссылку сам: {bot_link}",
                        "reply_markup": {"remove_keyboard": True},
                    },
                )
            else:
                await session.post(
                    f"{TG_API}/sendMessage",
                    json={
                        "chat_id": sender_id,
                        "text": "Не удалось отправить. Попробуй позже.",
                        "reply_markup": {"remove_keyboard": True},
                    },
                )
    except Exception as e:
        print(f"⚠️  _handle_users_shared: {e}")


def _maybe_mark_private_chat_open_from_update(update: dict) -> None:
    """
    Приватный диалог с ботом (нужен для рассылки в ЛС). Мини‑апп даёт user id, но без /start
    Bot API вернёт chat not found — фиксируем только реальные private‑updates (сообщение, callback в ЛС, my_chat_member).
    """
    try:
        import analytics_db

        uid = None
        if update.get("message"):
            m = update["message"]
            ch = m.get("chat") or {}
            if ch.get("type") == "private":
                f = m.get("from") or {}
                uid = f.get("id")
                if uid is None:
                    uid = ch.get("id")
        elif update.get("edited_message"):
            m = update["edited_message"]
            ch = m.get("chat") or {}
            if ch.get("type") == "private":
                f = m.get("from") or {}
                uid = f.get("id") or ch.get("id")
        elif update.get("callback_query"):
            cq = update["callback_query"]
            msg = cq.get("message") or {}
            ch = msg.get("chat") or {}
            if ch.get("type") == "private":
                f = cq.get("from") or {}
                uid = f.get("id")
        elif update.get("my_chat_member"):
            mcm = update["my_chat_member"]
            ch = mcm.get("chat") or {}
            if ch.get("type") == "private":
                new = mcm.get("new_chat_member") or {}
                st = (new.get("status") or "").lower()
                if st in ("member", "administrator", "creator"):
                    f = mcm.get("from") or {}
                    uid = f.get("id")
        if uid is not None:
            analytics_db.init_db()
            analytics_db.mark_bot_private_chat_open(int(uid))
    except Exception as e:
        print(f"⚠️ _maybe_mark_private_chat_open_from_update: {e}")


def _register_user_from_telegram_update(update: dict) -> None:
    """
    Любой Update от Telegram, где есть from / user — сохраняем numeric id для рассылок.
    Охватывает: message, edited_message, inline_query, callback_query, chosen_inline_result,
    my_chat_member, chat_join_request, shipping_query, pre_checkout_query и т.д.
    """
    try:
        _maybe_mark_private_chat_open_from_update(update)
        uid = None
        uname = None
        src = "tg_webhook"

        if update.get("inline_query"):
            f = update["inline_query"].get("from") or {}
            uid, uname = f.get("id"), f.get("username")
            src = "inline_query"
        elif update.get("callback_query"):
            f = update["callback_query"].get("from") or {}
            uid, uname = f.get("id"), f.get("username")
            src = "callback_query"
        elif update.get("chosen_inline_result"):
            f = update["chosen_inline_result"].get("from") or {}
            uid, uname = f.get("id"), f.get("username")
            src = "chosen_inline_result"
        elif update.get("message"):
            m = update["message"]
            f = m.get("from") or {}
            ch = m.get("chat") or {}
            uid = f.get("id")
            if uid is None and ch.get("type") == "private":
                uid = ch.get("id")
            uname = f.get("username")
            src = "tg_message"
        elif update.get("edited_message"):
            m = update["edited_message"]
            f = m.get("from") or {}
            ch = m.get("chat") or {}
            uid = f.get("id")
            if uid is None and ch.get("type") == "private":
                uid = ch.get("id")
            uname = f.get("username")
            src = "tg_edited_message"
        elif update.get("my_chat_member"):
            f = update["my_chat_member"].get("from") or {}
            uid, uname = f.get("id"), f.get("username")
            src = "my_chat_member"
        elif update.get("chat_join_request"):
            f = update["chat_join_request"].get("from") or {}
            uid, uname = f.get("id"), f.get("username")
            src = "chat_join_request"
        elif update.get("shipping_query"):
            f = update["shipping_query"].get("from") or {}
            uid, uname = f.get("id"), f.get("username")
            src = "shipping_query"
        elif update.get("pre_checkout_query"):
            f = update["pre_checkout_query"].get("from") or {}
            uid, uname = f.get("id"), f.get("username")
            src = "pre_checkout_query"
        elif update.get("poll_answer"):
            f = update["poll_answer"].get("user") or {}
            uid, uname = f.get("id"), f.get("username")
            src = "poll_answer"

        if uid is not None:
            user_obj = {"id": uid, "username": uname}
            _register_bot_subscriber_from_telegram_user(user_obj, src, force=True)
    except Exception as e:
        print(f"⚠️ _register_user_from_telegram_update: {e}")


async def _handle_telegram_update(update: dict) -> None:
    """В фоне обрабатывает Update: /start (в т.ч. share_tr_* → карточка+ссылка одним сообщением), /playlist → PLAY."""
    try:
        # #region agent log
        _agent_debug_log(
            "H3",
            "server_lite:_handle_telegram_update:entry",
            "handler_entered",
            {
                "update_id": update.get("update_id"),
                "has_message": bool(update.get("message")),
                "has_edited_message": bool(update.get("edited_message")),
            },
        )
        # #endregion
        message = update.get("message")
        if not message:
            # #region agent log
            _agent_debug_log("H3", "server_lite:_handle_telegram_update", "early_exit_no_message", {})
            # #endregion
            return
        if message.get("users_shared"):
            # #region agent log
            _agent_debug_log("H3", "server_lite:_handle_telegram_update", "early_exit_users_shared", {})
            # #endregion
            return
        chat_id = message.get("chat", {}).get("id")
        if not chat_id:
            # #region agent log
            _agent_debug_log("H3", "server_lite:_handle_telegram_update", "early_exit_no_chat_id", {})
            # #endregion
            return
        text = (message.get("text") or "").strip()
        if not text.startswith("/start") and not text.startswith("/playlist"):
            # #region agent log
            _agent_debug_log(
                "H3",
                "server_lite:_handle_telegram_update",
                "early_exit_not_start_or_playlist",
                {"text_len": len(text), "has_entities": bool(message.get("entities"))},
            )
            # #endregion
            return
        session = await get_session()
        # share_tr_* — отправитель в Mini App поделился; tr_* — получатель открыл ссылку. В обоих случаях шлём карточку+подпись.
        track_id = None
        if text.startswith("/start share_tr_"):
            track_id = text.replace("/start share_tr_", "", 1).strip()
        elif text.startswith("/start tr_"):
            track_id = text.replace("/start tr_", "", 1).strip()
        if track_id:
            canon = _canonical_share_track_id(track_id)
        else:
            canon = None
        if canon:
            card_url = f"{WEBAPP_URL_CANONICAL}/api/track-card/{canon}"
            bot_link = f"https://t.me/{BOT_USERNAME}?startapp=tr_{_startapp_track_token(canon)}"
            print(f"📩 Webhook /start tr_* от chat_id={chat_id}, отправляю карточку как фото+ссылку…")
            try:
                async with session.get(card_url) as card_resp:
                    if card_resp.status != 200:
                        print(f"⚠️  Не удалось загрузить карточку: {card_resp.status}")
                    else:
                        card_bytes = await card_resp.read()
                        form = aiohttp.FormData()
                        form.add_field("chat_id", str(chat_id))
                        form.add_field("caption", "")
                        form.add_field("photo", card_bytes, filename="card.png", content_type="image/png")
                        form.add_field("reply_markup", json.dumps({
                            "inline_keyboard": [[{"text": "Слушать в TGPlay", "url": bot_link}]],
                        }))
                        async with session.post(f"{TG_API}/sendPhoto", data=form) as resp:
                            if resp.status != 200:
                                body = await resp.text()
                                print(f"⚠️  TG sendPhoto: {resp.status} {body[:200]}")
                            else:
                                print(f"✅ Карточка (фото)+кнопка отправлены в chat_id={chat_id}")
            except Exception as e:
                print(f"⚠️  share_tr_ sendPhoto: {e}")
            return
        print(f"📩 Webhook /start или /playlist от chat_id={chat_id}, отправляю ответ…")
        async with session.post(
            f"{TG_API}/sendMessage",
            json={
                "chat_id": chat_id,
                "text": WELCOME_MESSAGE,
                "reply_markup": {
                    "inline_keyboard": [
                        [{"text": "PLAY", "web_app": {"url": WEBAPP_URL_CANONICAL}}],
                    ],
                },
            },
        ) as resp:
            if resp.status != 200:
                body = await resp.text()
                print(f"⚠️  TG sendMessage: {resp.status} {body[:200]}")
                # #region agent log
                _agent_debug_log(
                    "H4",
                    "server_lite:_handle_telegram_update:sendMessage",
                    "sendMessage_failed",
                    {"status": resp.status, "body_prefix": (body or "")[:120]},
                )
                # #endregion
            else:
                print(f"✅ Ответ на /start отправлен в chat_id={chat_id}")
                # #region agent log
                _agent_debug_log(
                    "H4",
                    "server_lite:_handle_telegram_update:sendMessage",
                    "sendMessage_ok",
                    {"status": resp.status},
                )
                # #endregion
    except Exception as e:
        print(f"⚠️  _handle_telegram_update: {e}")
        # #region agent log
        _agent_debug_log("H4", "server_lite:_handle_telegram_update", "handler_exception", {"err_type": type(e).__name__})
        # #endregion


async def _handle_inline_query(update: dict) -> None:
    """Обработка inline_query: share_tr_* → один результат «фото карточки + подпись со ссылкой».
    InlineQueryResultPhoto принимает только JPEG — используем ?format=jpeg. В BotFather: /setinline."""
    try:
        iq = update.get("inline_query")
        if not iq:
            return
        iq_id = iq.get("id")
        query = (iq.get("query") or "").strip()
        if not iq_id or not query.startswith("share_tr_"):
            return
        track_id = query.replace("share_tr_", "", 1).strip()
        canon = _canonical_share_track_id(track_id)
        if not canon:
            return
        # Telegram принимает только JPEG для inline photo — отдаём карточку в JPEG
        card_url = f"{WEBAPP_URL_CANONICAL}/api/track-card/{canon}?format=jpeg"
        bot_link = f"https://t.me/{BOT_USERNAME}?startapp=tr_{_startapp_track_token(canon)}"
        result_id = re.sub(r"[^a-zA-Z0-9]", "", canon)[:64] or "card"
        session = await get_session()
        payload = {
            "inline_query_id": iq_id,
            "results": [
                {
                    "type": "photo",
                    "id": result_id,
                    "photo_url": card_url,
                    "thumbnail_url": card_url,
                    "caption": bot_link,
                }
            ],
            "cache_time": 300,
        }
        async with session.post(f"{TG_API}/answerInlineQuery", json=payload) as resp:
            body = await resp.text()
            if resp.status != 200:
                print(f"⚠️  answerInlineQuery: {resp.status} {body[:400]}")
            else:
                print(f"✅ Inline результат (карточка JPEG) отправлен query_id={iq_id[:16]}…")
    except Exception as e:
        print(f"⚠️  _handle_inline_query: {e}")


@app.post("/api/telegram-webhook")
async def telegram_webhook(request: Request, background: BackgroundTasks):
    """Принимает обновления от Telegram (webhook). Отвечает 200 сразу, обработку в фоне."""
    if TELEGRAM_WEBHOOK_SECRET:
        secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token") or ""
        if secret != TELEGRAM_WEBHOOK_SECRET:
            # #region agent log
            _agent_debug_log(
                "H1",
                "server_lite:telegram_webhook",
                "rejected_403_secret_mismatch",
                {"has_secret_header": bool(secret)},
            )
            # #endregion
            return Response(status_code=403)
    try:
        body = await request.json()
    except Exception:
        body = {}
    # #region agent log
    _msg = body.get("message") if isinstance(body.get("message"), dict) else None
    _agent_debug_log(
        "H2",
        "server_lite:telegram_webhook",
        "webhook_body_received",
        {
            "update_id": body.get("update_id"),
            "top_keys": sorted(body.keys()) if isinstance(body, dict) else [],
            "has_message": bool(body.get("message")),
            "has_inline_query": bool(body.get("inline_query")),
            "text_prefix": ((_msg.get("text") or "")[:24] if _msg else ""),
        },
    )
    # #endregion
    background.add_task(_register_user_from_telegram_update, body)
    if body.get("inline_query"):
        background.add_task(_handle_inline_query, body)
    elif body.get("message") and (body["message"].get("users_shared")):
        background.add_task(_handle_users_shared, body)
    else:
        msg = body.get("message") or {}
        text = (msg.get("text") or "").strip()
        if text.startswith("/start") or text.startswith("/playlist"):
            print(f"📥 Webhook: получен {text[:20]!r} от chat_id={msg.get('chat', {}).get('id')}")
        background.add_task(_handle_telegram_update, body)
    return Response(status_code=200)


# ─── Временное хранилище картинок для сторис (URL → Telegram) ───
_story_images: Dict[str, tuple] = {}  # id -> (bytes, timestamp, mime)
_STORY_IMAGE_TTL = 900  # 15 мин
_STORY_IMAGE_MAX = 50


def _story_cleanup():
    now = time.time()
    to_del = [k for k, v in _story_images.items() if now - v[1] > _STORY_IMAGE_TTL]
    for k in to_del:
        del _story_images[k]
    while len(_story_images) > _STORY_IMAGE_MAX:
        oldest = min(_story_images.items(), key=lambda x: x[1][1])
        del _story_images[oldest[0]]


@app.post("/api/share/story-media")
async def share_story_media(
    request: Request,
    authorization: Optional[str] = Header(None),
):
    """JPEG на нашем CDN с путём .jpg — для WebApp.shareToStory (не video/black screen)."""
    get_user_from_header(authorization)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(400, "JSON body required")
    track_id = (body.get("track_id") or "").strip()
    canon = _canonical_share_track_id(track_id)
    if not canon:
        raise HTTPException(400, "Invalid track_id")
    token = _startapp_track_token(canon)
    media_url = f"{WEBAPP_URL_CANONICAL}/api/story-card/{token}.jpg?v={int(time.time() * 1000)}"
    return {"ok": True, "media_url": media_url}


_STORY_SID_RE = re.compile(r"^[a-f0-9]{12}$")


@app.post("/api/story-image")
async def upload_story_image(request: Request, file: UploadFile = File(...)):
    """Принимает PNG, возвращает публичный URL — Telegram должен уметь запросить картинку по нему."""
    if file.content_type and "image" not in file.content_type:
        raise HTTPException(400, "Only image allowed")
    data = await file.read()
    if len(data) > 5 * 1024 * 1024:  # 5 MB
        raise HTTPException(400, "Image too large")
    _story_cleanup()
    sid = str(uuid.uuid4())[:12]
    mime = "image/jpeg" if file.content_type and "jpeg" in file.content_type else "image/png"
    _story_images[sid] = (data, time.time(), mime)
    host = request.headers.get("x-forwarded-host") or request.headers.get("host") or ""
    proto = request.headers.get("x-forwarded-proto") or "https"
    if host and host.startswith(("localhost", "127.")) is False:
        base = f"{proto}://{host.split(',')[0].strip()}".rstrip("/")
    else:
        base = str(request.base_url).rstrip("/")
        env_path = Path(__file__).parent / ".env"
        if env_path.exists():
            for line in env_path.read_text("utf-8").splitlines():
                line = line.strip()
                if line.startswith("WEBAPP_URL=") and "=" in line:
                    base = line.split("=", 1)[1].strip().strip("'\"").rstrip("/")
                    break
        if not str(base).startswith("http"):
            env_web = (os.getenv("WEBAPP_URL") or "").strip().rstrip("/")
            base = (env_web if env_web.startswith("http") else WEBAPP_URL_CANONICAL).rstrip("/")
    url = f"{base}/api/story-image/{sid}"
    return {"url": url, "id": sid}


@app.get("/api/story-image/{sid_path:path}")
async def get_story_image(sid_path: str):
    """Отдаёт картинку по id — Telegram подтягивает превью для сторис/шаринга."""
    sid = sid_path.strip()
    if sid.endswith(".jpg"):
        sid = sid[:-4]
    if not _STORY_SID_RE.match(sid):
        raise HTTPException(400, "Invalid id")
    _story_cleanup()
    if sid not in _story_images:
        raise HTTPException(404, "Not found")
    entry = _story_images[sid]
    data = entry[0]
    mime = entry[2] if len(entry) > 2 else "image/png"
    return Response(
        content=data,
        media_type=mime,
        headers={
            "Cache-Control": "public, max-age=300",
            "Content-Length": str(len(data)),
            "Content-Disposition": 'inline; filename="story.jpg"',
            "X-Content-Type-Options": "nosniff",
        },
    )


# ─── Health check ────────────────────────────────────────────────

@app.get("/api/health")
async def health():
    return {"status": "ok", "url_cache": "redis"}


async def _probe_public_health_base(base: str) -> Dict[str, Any]:
    """GET /api/health с этого VPS — как при выборе webhook. Без отключения проверки SSL."""
    b = base.strip().rstrip("/")
    if not b.startswith("http"):
        return {"base": b, "reachable": False, "error_kind": "invalid", "error_summary": "not https"}
    try:
        session = await get_session()
        async with session.get(
            f"{b}/api/health",
            timeout=aiohttp.ClientTimeout(total=8),
            allow_redirects=False,
        ) as hr:
            return {"base": b, "reachable": hr.status == 200, "http_status": int(hr.status)}
    except Exception as e:
        msg = str(e).lower()
        kind = "ssl"
        if "certificate" not in msg and "ssl" not in msg and "tls" not in msg:
            kind = "network"
        return {"base": b, "reachable": False, "error_kind": kind, "error_summary": str(e)[:200]}


@app.get("/api/webhook-info")
async def webhook_info():
    """Проверка: какой webhook видит Telegram (для отладки «нет ответа на /start»)."""
    if not BOT_TOKEN:
        return {"ok": False, "error": "no BOT_TOKEN"}
    try:
        session = await get_session()
        async with session.get(f"https://api.telegram.org/bot{BOT_TOKEN}/getWebhookInfo") as resp:
            data = await resp.json() if resp.status == 200 else {}
        result = data.get("result", {})
        lem = result.get("last_error_message")
        hint = None
        probe_fun = await _probe_public_health_base("https://tgplay.fun")
        if isinstance(lem, str) and lem.strip():
            low = lem.lower()
            if "timeout" in low or "timed out" in low or "connection" in low:
                hint = (
                    "Telegram не достучался до URL вебхука с своих серверов. "
                    "Проверьте firewall/nginx и домен: после перезапуска бэкенд предпочитает https://tgplay.fun "
                    "для webhook, либо задайте TELEGRAM_WEBHOOK_BASE_URL. OAuth с сайта идёт с браузера на ваш API — это отдельный путь от вебхука."
                )
                if not probe_fun.get("reachable") and probe_fun.get("error_kind") == "ssl":
                    hint += (
                        " С этого же сервера HTTPS к tgplay.fun не проходит проверку сертификата (SAN/имя) — "
                        "исправьте TLS в nginx для tgplay.fun."
                    )
        return {
            "ok": data.get("ok"),
            "url": result.get("url") or "(не установлен)",
            "pending_update_count": result.get("pending_update_count"),
            "last_error_message": lem,
            "last_error_date": result.get("last_error_date"),
            "max_connections": result.get("max_connections"),
            "diagnostic_hint": hint,
            "health_probe_from_server": {"tgplay_fun": probe_fun},
        }
    except Exception as e:
        return {"ok": False, "error": str(e)}


# ─── Аналитика (отдельно от бота: заходы, клики, прослушивания, ошибки, удержание) ─

# Ключи для просмотра аналитики.
# - ANALYTICS_ADMIN_KEY: основной ключ
# - ANALYTICS_ADMIN_KEYS: дополнительные ключи через запятую (для ротации ключа без даунтайма)
_ANALYTICS_ADMIN_KEY_FALLBACKS = {
    # Legacy keys only via ANALYTICS_ADMIN_KEYS env — в публичном репозитории дефолтов нет.
}
_ANALYTICS_ADMIN_KEY_PRIMARY = (os.getenv("ANALYTICS_ADMIN_KEY") or "").strip()
_ANALYTICS_ADMIN_KEY_EXTRA_RAW = (os.getenv("ANALYTICS_ADMIN_KEYS") or "").strip()
ANALYTICS_ADMIN_KEYS = {
    k
    for k in (
        list(_ANALYTICS_ADMIN_KEY_FALLBACKS)
        + [_ANALYTICS_ADMIN_KEY_PRIMARY]
        + [x.strip() for x in _ANALYTICS_ADMIN_KEY_EXTRA_RAW.split(",")]
    )
    if k
}


def _admin_key_ok(key: str) -> bool:
    k = (key or "").strip()
    return bool(k) and k in ANALYTICS_ADMIN_KEYS

def _user_hash_from_auth(authorization: Optional[str]) -> Optional[str]:
    """Хеш user id из initData для аналитики (без хранения сырого id)."""
    if not authorization:
        return None
    user = None
    try:
        user = get_user_from_header(authorization)
    except HTTPException:
        return None
    uid = user.get("id")
    if uid is None:
        return None
    return hashlib.sha256(f"tg_{uid}".encode()).hexdigest()[:32]


@app.post("/api/analytics/event")
async def analytics_event(request: Request):
    """
    Приём событий от фронта.

    Поддерживаем несколько типов:
    - user_event    — высокоуровневые действия пользователя (экраны, поиски и т.п.)
    - button_click  — клики по кнопкам
    - error         — ошибки на фронтенде
    - track_usage   — события треков (play/complete/download_to_bot)
    - playlist      — операции с плейлистами
    """
    try:
        body = await request.json()
    except Exception:
        return Response(status_code=200)

    legacy_event = body.get("event")
    if legacy_event and isinstance(legacy_event, str):
        legacy_event = legacy_event.strip()
    kind = (body.get("kind") or "").strip()
    if not kind and legacy_event:
        if legacy_event in (
            "button_add_playlist",
            "button_add_send",
            "button_remove",
            "button_share_channel",
            "button_share_chat",
            "button_share_track",
            "button_share_story",
            "button_share_chat_direct",
            "button_share_playlist",
            "button_share_to_users",
            "button_profile_open",
            "button_profile_from_player",
            "button_profile_logout_web",
            "button_create_playlist",
            "button_reset_search",
            "button_recommendations_refresh",
            "button_my_wave",
            "button_download",
            "button_add_to_favorites",
            "button_add_to_custom_playlist",
        ):
            kind = "button_click"
        elif legacy_event in ("track_play", "track_finish"):
            kind = "track_usage"
        elif legacy_event == "error":
            kind = "error"
        else:
            kind = "user_event"
    if not kind:
        kind = "user_event"

    payload = body.get("payload") or {}
    if not isinstance(payload, dict):
        payload = {}

    auth = request.headers.get("Authorization")
    tg_user = None
    try:
        if auth:
            tg_user = get_user_from_header(auth)
    except HTTPException:
        tg_user = None

    telegram_user_id = None
    username = None
    if isinstance(tg_user, dict):
        telegram_user_id = tg_user.get("id")
        username = tg_user.get("username") or tg_user.get("first_name")

    country_code = payload.get("country_code")
    city_region = payload.get("city_region")

    # Пытаемся определить регион по IP, если он ещё не проставлен.
    ip = None
    try:
        fwd = request.headers.get("X-Forwarded-For") or request.headers.get("x-forwarded-for")
        if fwd:
            ip = fwd.split(",")[0].strip()
        if not ip and request.client:
            ip = request.client.host
    except Exception:
        ip = None

    async def _geoip(ip_addr: str) -> tuple[Optional[str], Optional[str]]:
        if not ip_addr:
            return None, None
        # Локальные адреса нам неинтересны
        if ip_addr.startswith("127.") or ip_addr.startswith("10.") or ip_addr.startswith("192.168.") or ip_addr.startswith("172.16."):
            return None, None
        cached = _geoip_cache.get(ip_addr)
        if cached is not None:
            return cached
        try:
            session = await get_session()
            async with session.get(f"http://ip-api.com/json/{ip_addr}?fields=status,countryCode,regionName,city") as resp:
                data = await resp.json()
            if data.get("status") == "success":
                cc = data.get("countryCode") or None
                region_name = data.get("regionName") or ""
                city = data.get("city") or ""
                region = ", ".join([p for p in (region_name, city) if p]) or None
                _geoip_cache[ip_addr] = (cc, region)
                return cc, region
        except Exception:
            pass
        return None, None

    if ip and (not country_code or not city_region):
        cc, region = await _geoip(ip)
        if not country_code:
            country_code = cc
        if not city_region:
            city_region = region

    try:
        import analytics_db

        analytics_db.init_db()
        # Регистрация user id для рассылок: middleware RegisterTelegramUserMiddleware + /api/me/register + webhook

        if kind == "button_click":
            button_id = str(payload.get("button_id") or (legacy_event if legacy_event else ""))
            analytics_db.log_button_click(
                telegram_user_id=telegram_user_id,
                username=username,
                button_id=button_id or "unknown",
                context=payload.get("context"),
                extra=payload.get("extra") or {},
            )
        elif kind == "error":
            analytics_db.log_error_event(
                telegram_user_id=telegram_user_id,
                username=username,
                error_key=str(payload.get("error_key") or (payload.get("place") or "frontend_error")),
                message=payload.get("message"),
                stack=payload.get("stack"),
                country_code=country_code,
                city_region=city_region,
                extra=payload.get("extra") or {},
            )
        elif kind == "track_usage":
            action = str(payload.get("action") or ("complete" if legacy_event == "track_finish" else "play"))
            track_id = str(payload.get("track_id") or "").strip()
            meta = await _redis_get_track_meta(track_id) if _valid_track_id(track_id) else None
            gid, ry, lb = _rec_meta_fields_from_cached_meta(meta)
            try:
                w = 1.0 if action == "play" else (1.7 if action == "complete" else 0.6)
                await _rec_update_taste_profile(telegram_user_id, genre_id=gid, release_year=ry, lang_bucket=lb, weight=w)
            except Exception:
                pass
            analytics_db.log_track_usage(
                telegram_user_id=telegram_user_id,
                username=username,
                track_id=track_id,
                action=action,
                duration_sec=payload.get("duration_sec"),
                from_cache=bool(payload.get("from_cache")),
                region=payload.get("region"),
                genre_id=gid,
                release_year=ry,
                lang_bucket=lb,
                extra=payload.get("extra") or {},
            )
        elif kind == "playlist":
            analytics_db.log_playlist_event(
                telegram_user_id=telegram_user_id,
                username=username,
                playlist_id=payload.get("playlist_id"),
                action=str(payload.get("action") or "create"),
                extra=payload.get("extra") or {},
            )
        else:
            event_type = str(payload.get("event_type") or legacy_event or "open_app")
            if event_type == "app_open":
                event_type = "open_app"
            if event_type == "search":
                try:
                    extra = payload.get("extra") or {}
                    qn = str(extra.get("q_norm") or "").strip()[:120]
                    if qn:
                        lb = "ru" if _has_cyrillic(qn) and not _has_latin(qn) else ("en" if _has_latin(qn) and not _has_cyrillic(qn) else "other")
                        await _rec_update_taste_profile(telegram_user_id, genre_id=None, release_year=None, lang_bucket=lb, weight=0.12)
                except Exception:
                    pass
            analytics_db.log_user_event(
                telegram_user_id=telegram_user_id,
                username=username,
                country_code=country_code,
                city_region=city_region,
                event_type=event_type,
                event_source=payload.get("event_source") or "miniapp",
                extra=payload.get("extra") or {},
            )
    except Exception as e:
        print(f"⚠️  analytics insert: {e}")
    return Response(status_code=200)


@app.get("/api/analytics/summary")
async def analytics_summary(key: str = Query("", alias="key")):
    """
    Базовая сводка (для обратной совместимости):
    визиты, пользователи, треки, ошибки, retention.
    Доступ: ?key=ANALYTICS_ADMIN_KEY.
    """
    if not _admin_key_ok(key):
        raise HTTPException(403, "Forbidden")
    try:
        import analytics_db

        analytics_db.init_db()
        return analytics_db.get_summary()
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/admin/tokens/clear-cooldown")
async def admin_tokens_clear_cooldown(key: str = Query("", alias="key")):
    """Снять cooldown со всех VK-токенов (чтобы снова пробовать запросы)."""
    if not _admin_key_ok(key):
        raise HTTPException(403, "Forbidden")
    for state in _token_pool._states:
        state.cooldown_until = 0.0
    return {"ok": True, "message": "Cooldown сброшен для всех токенов", "total_tokens": _token_pool.count}


COOLDOWN_ALL_HOURS_DEFAULT = 2

@app.get("/api/admin/tokens/cooldown-all")
@app.post("/api/admin/tokens/cooldown-all")
async def admin_tokens_cooldown_all(
    key: str = Query("", alias="key"),
    hours: float = Query(COOLDOWN_ALL_HOURS_DEFAULT, alias="hours"),
):
    """Проверить каждый токен одним запросом (ошибка 9 / 502?). Если ВСЕ выдают 9/502 — отправить все в cooldown на N часов (по умолчанию 2)."""
    if not _admin_key_ok(key):
        raise HTTPException(403, "Forbidden")
    cooldown_sec = max(60, min(86400, int(hours * 3600)))
    now = time.time()
    check_results: List[Dict] = []
    for state in _token_pool._states:
        suffix = state._label()
        res = await _vk_check_single_token(state)
        if res.get("ok"):
            check_results.append({"suffix": suffix, "status": "ok", "still_error": False})
        elif res.get("error_code") == 9:
            check_results.append({"suffix": suffix, "status": "error_9", "still_error": True, "error_msg": res.get("error_msg", "")[:80]})
        elif res.get("http_error") and res.get("http_error") >= 500:
            check_results.append({"suffix": suffix, "status": "http_5xx", "still_error": True, "http_error": res.get("http_error")})
        elif res.get("network_error") and res.get("is_502"):
            check_results.append({"suffix": suffix, "status": "network_502", "still_error": True})
        else:
            check_results.append({"suffix": suffix, "status": "other", "still_error": False, "detail": res})
    still_bad = sum(1 for r in check_results if r.get("still_error"))
    total = _token_pool.count
    all_bad = total > 0 and still_bad == total
    if all_bad:
        for state in _token_pool._states:
            state.cooldown_until = now + cooldown_sec
        print(f"🛑 Все {total} токенов выдают 9/502 → отправлены в cooldown на {cooldown_sec}s ({hours}h).")
        message = f"Все токены выдают ошибку 9/502. Отправлены в cooldown на {hours} ч."
    else:
        message = f"Не все токены выдают 9/502 (с ошибкой: {still_bad}/{total}). Cooldown не установлен."
    return {
        "ok": True,
        "message": message,
        "cooldown_applied": all_bad,
        "cooldown_seconds": cooldown_sec if all_bad else 0,
        "cooldown_hours": hours if all_bad else 0,
        "total_tokens": total,
        "tokens_still_error_9_or_502": still_bad,
        "check_results": check_results,
    }


@app.post("/api/admin/cache/clear-empty")
async def admin_cache_clear_empty(key: str = Query("", alias="key")):
    """Удалить из кэша поиска все записи с пустым ответом (in-memory и Redis)."""
    if not _admin_key_ok(key):
        raise HTTPException(403, "Forbidden")
    # In-memory: удалить ключи, у которых закэширован пустой список
    memory_removed = 0
    for k in list(_search_cache.keys()):
        tracks, _ = _search_cache[k]
        if not tracks:
            del _search_cache[k]
            memory_removed += 1
    # Redis: удалить versioned search-ключи с пустым значением
    redis_removed = 0
    redis_client = await get_redis()
    if redis_client is not None:
        try:
            pattern = f"{CACHE_VERSION}:search:*"
            async for rkey in redis_client.scan_iter(match=pattern, count=200):
                val = await redis_client.get(rkey)
                if val in ("[]", "{}", '{"items":[]}'):
                    await redis_client.delete(rkey)
                    redis_removed += 1
        except Exception as e:
            print(f"⚠️ Redis clear-empty scan/delete error: {e}")
    return {
        "ok": True,
        "message": "Пустые ответы поиска удалены из кэша",
        "memory_removed": memory_removed,
        "redis_removed": redis_removed,
    }


@app.post("/api/admin/cache/clear-search")
async def admin_cache_clear_search(key: str = Query("", alias="key")):
    """Полностью очистить кэш поиска (in-memory и Redis) для всех пользователей."""
    if not _admin_key_ok(key):
        raise HTTPException(403, "Forbidden")
    # In-memory
    memory_before = len(_search_cache)
    _search_cache.clear()
    # Redis
    redis_removed = 0
    redis_client = await get_redis()
    if redis_client is not None:
        try:
            patterns = (
                f"{CACHE_VERSION}:search:*",
                f"{CACHE_VERSION}:search_sc_playable_v2:*",
                f"{CACHE_VERSION}:search_sc_playable_v3:*",
                f"{CACHE_VERSION}:search_sc_playable_v4:*",
                f"{CACHE_VERSION}:search_sc_artist_v1:*",
            )
            for pattern in patterns:
                async for rkey in redis_client.scan_iter(match=pattern, count=500):
                    await redis_client.delete(rkey)
                    redis_removed += 1
        except Exception as e:
            print(f"⚠️ Redis clear-search scan/delete error: {e}")
    return {
        "ok": True,
        "message": "Кэш поиска полностью очищен",
        "memory_before": memory_before,
        "redis_removed": redis_removed,
    }


async def _redis_scan_delete(redis_client, pattern: str) -> int:
    removed = 0
    async for rkey in redis_client.scan_iter(match=pattern, count=500):
        await redis_client.delete(rkey)
        removed += 1
    return removed


@app.post("/api/admin/cache/clear-soundcloud")
async def admin_cache_clear_soundcloud(key: str = Query("", alias="key")):
    """Сброс SC/рекомендаций после смены preview→full stream (поиск, rec, URL fallback)."""
    if not _admin_key_ok(key):
        raise HTTPException(403, "Forbidden")
    memory_search_before = len(_search_cache)
    memory_rec_before = len(_rec_memory_cache)
    _search_cache.clear()
    _rec_memory_cache.clear()
    fallback_removed = 0
    for tid in list(_url_cache_fallback.keys()):
        if str(tid).startswith("sc:"):
            _url_cache_fallback.pop(tid, None)
            fallback_removed += 1
    redis_removed = 0
    redis_client = await get_redis()
    if redis_client is not None:
        try:
            patterns = (
                f"{CACHE_VERSION}:search_sc_playable_v2:*",
                f"{CACHE_VERSION}:search_sc_playable_v3:*",
                f"{CACHE_VERSION}:search_sc_playable_v4:*",
                f"{CACHE_VERSION}:search_sc_artist_v1:*",
                f"{CACHE_VERSION}:sc:*",
                f"{CACHE_VERSION}:rec:*",
                f"{CACHE_VERSION}:track:sc:*:source",
                f"{CACHE_VERSION}:track:sc:*:meta",
                f"{CACHE_VERSION}:track:sc:*:meta_sc2",
                f"{CACHE_VERSION}:track:sc:*:meta_sc3",
            )
            for pattern in patterns:
                redis_removed += await _redis_scan_delete(redis_client, pattern)
        except Exception as e:
            print(f"⚠️ Redis clear-soundcloud scan/delete error: {e}")
    return {
        "ok": True,
        "message": "SoundCloud и рекомендации: кэш сброшен",
        "memory_search_before": memory_search_before,
        "memory_rec_before": memory_rec_before,
        "url_fallback_removed": fallback_removed,
        "redis_removed": redis_removed,
    }


@app.post("/api/admin/captcha/clear-events")
async def admin_captcha_clear_events(key: str = Query("", alias="key")):
    """Удалить все записи капч/кулдаунов из аналитики (если они были ошибочно посчитаны, напр. ошибка 9 как капча)."""
    if not _admin_key_ok(key):
        raise HTTPException(403, "Forbidden")
    try:
        import analytics_db
        analytics_db.init_db()
        deleted = analytics_db.clear_captcha_events()
        return {"ok": True, "message": "События капч/кулдаунов сброшены", "deleted": deleted}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/admin/tokens")
async def admin_tokens(key: str = Query("", alias="key")):
    """Token pool health dashboard — per-token stats, cache hit rates."""
    if not _admin_key_ok(key):
        raise HTTPException(403, "Forbidden")
    now = time.time()
    cache_stats = {
        "search_cache": {"size": len(_search_cache), "max": _SEARCH_CACHE_MAX, "ttl": _SEARCH_CACHE_TTL},
        "url_cache": {"backend": "redis", "fallback_size": len(_url_cache_fallback), "ttl": _TRACK_SOURCE_REDIS_TTL},
        "track_info_cache": {"size": len(_track_info_cache), "max": _TRACK_INFO_MAX, "ttl": _TRACK_INFO_TTL},
    }
    # Пиковая ёмкость: N токенов × 2.5 RPS / ~0.15 req/s на пользователя ≈ N×17, но не больше limit_concurrency
    _users_per_token = 17
    peak_capacity_users = min(_token_pool.count * _users_per_token, LIMIT_CONCURRENCY)

    return {
        "tokens": _token_pool.stats(),
        "total_tokens": _token_pool.count,
        "healthy_tokens": _token_pool.healthy_count,
        "peak_capacity_users": peak_capacity_users,
        "rucaptcha_enabled": bool(_rucaptcha_key),
        "caches": cache_stats,
        "uptime_seconds": int(now - _server_start_time) if _server_start_time else 0,
        "init_data_validation_failures": _init_data_validation_failures,
    }


# ─── Admin stats API (overview, series, errors, captcha, buttons, tracks, playlists) ─


@app.get("/api/admin/stats/overview")
async def admin_stats_overview(key: str = Query("", alias="key")):
    if not _admin_key_ok(key):
        raise HTTPException(403, "Forbidden")
    try:
        import analytics_db

        analytics_db.init_db()
        summary = analytics_db.get_summary()
        # Добавляем данные пула VK-токенов (round-robin): сколько токенов и пиковая ёмкость
        n = _token_pool.count
        summary["vk_tokens_total"] = n
        summary["vk_tokens_healthy"] = _token_pool.healthy_count
        summary["peak_capacity_users"] = min(n * 17, LIMIT_CONCURRENCY)
        # Cache stats (per-process, since last restart)
        search_total = _cache_metrics["search_hit"] + _cache_metrics["search_miss"]
        source_total = _cache_metrics["source_hit"] + _cache_metrics["source_miss"]
        meta_total = _cache_metrics["meta_hit"] + _cache_metrics["meta_miss"]
        summary["cache"] = {
            "version": CACHE_VERSION,
            "in_flight": _cache_in_flight_count(),
            "negative_hit": _cache_metrics.get("negative_hit", 0),
            "search": {
                "hit": _cache_metrics["search_hit"],
                "miss": _cache_metrics["search_miss"],
                "ratio": _cache_metrics["search_hit"] / search_total if search_total > 0 else None,
                "avg_ttl_age_sec": (
                    _cache_metrics["search_age_sum"] / _cache_metrics["search_age_count"]
                    if _cache_metrics.get("search_age_count", 0) > 0
                    else None
                ),
            },
            "source": {
                "hit": _cache_metrics["source_hit"],
                "miss": _cache_metrics["source_miss"],
                "ratio": _cache_metrics["source_hit"] / source_total if source_total > 0 else None,
            },
            "meta": {
                "hit": _cache_metrics["meta_hit"],
                "miss": _cache_metrics["meta_miss"],
                "ratio": _cache_metrics["meta_hit"] / meta_total if meta_total > 0 else None,
            },
        }
        return summary
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/admin/stats/metric")
async def admin_stats_metric(
    metric: str = Query(..., description="visits|search_count|track_plays|track_finishes|downloads|errors_count"),
    days: int = Query(30, ge=1, le=365),
    key: str = Query("", alias="key"),
):
    if not _admin_key_ok(key):
        raise HTTPException(403, "Forbidden")
    try:
        import analytics_db

        analytics_db.init_db()
        return analytics_db.get_metric_series(metric=metric, days=days)
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/admin/stats/playlists/recent")
async def admin_stats_playlists_recent(
    key: str = Query("", alias="key"),
    limit: int = Query(200, ge=1, le=1000),
):
    if not _admin_key_ok(key):
        raise HTTPException(403, "Forbidden")
    try:
        import analytics_db

        analytics_db.init_db()
        events = analytics_db.get_recent_playlist_events(limit=limit)
        return {"events": events}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/admin/stats/user-timeline")
async def admin_stats_user_timeline(
    telegram_user_id: int = Query(..., description="Telegram user ID"),
    limit: int = Query(100, ge=1, le=500),
    key: str = Query("", alias="key"),
):
    """Лента событий пользователя (активность, ошибки, воспроизведения) по времени — для разбора сценариев."""
    if not _admin_key_ok(key):
        raise HTTPException(403, "Forbidden")
    try:
        import analytics_db

        analytics_db.init_db()
        events = analytics_db.get_user_timeline(telegram_user_id=telegram_user_id, limit=limit)
        return {"telegram_user_id": telegram_user_id, "events": events}
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/admin/stats/user-vk-summary")
async def admin_stats_user_vk_summary(
    telegram_user_id: int = Query(..., description="Telegram user ID"),
    date_utc: str = Query("", description="YYYY-MM-DD (UTC), пусто = сегодня"),
    key: str = Query("", alias="key"),
):
    """Сводка по пользователю за день: поиски, воспроизведения из кэша vs из VK, ошибки."""
    if not _admin_key_ok(key):
        raise HTTPException(403, "Forbidden")
    try:
        import analytics_db

        analytics_db.init_db()
        summary = analytics_db.get_user_vk_activity_summary(
            telegram_user_id=telegram_user_id,
            date_utc=date_utc.strip() or None,
        )
        return summary
    except Exception as e:
        raise HTTPException(500, str(e))


@app.get("/api/admin/stats/user-search-count")
async def admin_stats_user_search_count(
    telegram_user_id: int = Query(..., description="Telegram user ID"),
    start_utc: str = Query(..., description="Начало интервала UTC: YYYY-MM-DD или YYYY-MM-DDTHH:MM:SS"),
    end_utc: str = Query(..., description="Конец интервала UTC: YYYY-MM-DD или YYYY-MM-DDTHH:MM:SS"),
    key: str = Query("", alias="key"),
):
    """Количество поисков пользователя в заданном интервале [start_utc, end_utc) UTC."""
    if not _admin_key_ok(key):
        raise HTTPException(403, "Forbidden")
    try:
        for part, s in (("start_utc", start_utc.strip()), ("end_utc", end_utc.strip())):
            if not s:
                raise HTTPException(400, f"{part} is required")
        # Парсим дату/время в unix timestamp (UTC)
        def parse_utc(s: str) -> int:
            s = s.strip().replace("Z", "").replace(" ", "T")
            if "T" in s:
                dt = datetime.strptime(s[:19], "%Y-%m-%dT%H:%M:%S")
            else:
                dt = datetime.strptime(s[:10], "%Y-%m-%d")
            return int(dt.replace(tzinfo=timezone.utc).timestamp())

        start_ts = parse_utc(start_utc)
        end_ts = parse_utc(end_utc)
        if start_ts >= end_ts:
            raise HTTPException(400, "start_utc must be before end_utc")
        import analytics_db

        analytics_db.init_db()
        count = analytics_db.get_user_search_count_in_interval(
            telegram_user_id=telegram_user_id,
            start_ts=start_ts,
            end_ts=end_ts,
        )
        return {
            "telegram_user_id": telegram_user_id,
            "start_utc": start_utc.strip(),
            "end_utc": end_utc.strip(),
            "search_count": count,
        }
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(400, f"Invalid date format: {e}") from e
    except Exception as e:
        raise HTTPException(500, str(e)) from e


@app.post("/api/admin/limits/reset-user")
async def admin_limits_reset_user(
    telegram_user_id: int = Query(..., description="Telegram user ID"),
    key: str = Query("", alias="key"),
):
    """Сбросить для пользователя все счётчики лимитов (дневной поиск, почасовой resolve/download) — как будто сегодня не было запросов."""
    if not _admin_key_ok(key):
        raise HTTPException(403, "Forbidden")
    global _rate_limit_vk_daily, _rate_limit_hourly
    uid = str(telegram_user_id)
    today_utc = datetime.utcnow().strftime("%Y-%m-%d")
    daily_key = f"vkdaily:{today_utc}:tg:{uid}"
    hourly_key = f"vk_hourly:tg:{uid}"
    _rate_limit_vk_daily.pop(daily_key, None)
    _rate_limit_hourly.pop(hourly_key, None)
    return {"ok": True, "telegram_user_id": telegram_user_id, "cleared": ["daily", "hourly"]}


# Маршрут GET /admin/stats убран: запрос отдаётся в SPA (index.html), React показывает новый дашборд.


# ─── Статика: раздаём собранный фронтенд (dist/) напрямую ─────

from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

DIST_DIR = Path(__file__).parent.parent / "dist"
_static_dir = Path(__file__).parent / "static"
_front = DIST_DIR if DIST_DIR.is_dir() else (_static_dir if _static_dir.is_dir() else None)

# ─── YouTube HTTP сессия (одна на все стримы) ─────────────────
_youtube_http_session: Optional[aiohttp.ClientSession] = None

async def _get_youtube_session() -> aiohttp.ClientSession:
    global _youtube_http_session
    if _youtube_http_session is None or _youtube_http_session.closed:
        timeout = aiohttp.ClientTimeout(total=30, connect=10)
        _youtube_http_session = aiohttp.ClientSession(timeout=timeout)
    return _youtube_http_session



# ─── YouTube-стриминг ──────────────────────────────────────────

@app.get("/api/music/youtube-direct/{video_id}")
async def youtube_direct(video_id: str, request: Request):
    """
    Прозрачный прокси к YouTube CDN с поддержкой Range-запросов.
    Формат: 140 (AAC/M4A) — гарантированный audio/mp4.
    Без ffmpeg, без pipe. TTFB < 500ms, перемотка мгновенная.
    Повторный визит: готовый файл в mp3_cache/yt_{id}.m4a — отдаём с диска.
    """
    # 1. Определяем video_id
    vid = extract_video_id(video_id)
    if not vid:
        if re.match(r'^[\w-]{11}$', video_id):
            vid = video_id
        else:
            raise HTTPException(400, "Invalid YouTube video ID")

    try:
        disk_path = _yt_disk_cache_path(vid)
    except ValueError:
        disk_path = None
    if disk_path and _yt_disk_cache_file_ready(disk_path):
        try:
            sz = disk_path.stat().st_size
        except OSError:
            sz = 0
        if sz > 0:
            return _yt_cached_m4a_file_response(str(disk_path), sz, request, vid)

    # 2. Получаем прямую ссылку CDN (из Redis или yt-dlp)
    stream_url = await _get_or_set_direct_url(vid)
    _schedule_youtube_disk_cache_fill(vid, stream_url)

    # 3. Запрашиваем у YouTube CDN, проксируя Range от клиента
    session = await _get_youtube_session()
    range_header = request.headers.get("range")
    yt_headers = {}
    if range_header:
        yt_headers["Range"] = range_header

    try:
        resp = await session.get(stream_url, headers=yt_headers)
    except Exception as e:
        raise HTTPException(502, f"Failed to connect to YouTube CDN: {e}")

    # 4. Формируем ответ — проксируем поток байтов с правильными заголовками
    response_headers = {
        "Accept-Ranges": "bytes",
        "Access-Control-Allow-Origin": "*",
        "Cache-Control": "public, max-age=300",
    }
    for h in ("Content-Type", "Content-Length", "Content-Range"):
        val = resp.headers.get(h)
        if val:
            response_headers[h] = val

    return StreamingResponse(
        resp.content,
        status_code=resp.status,  # 200 или 206 Partial Content
        headers=response_headers,
    )



# Без кэша — Telegram всегда подтягивает свежий index.html и новый дизайн
_NO_CACHE_HEADERS = {
    "Cache-Control": "no-cache, no-store, must-revalidate",
    "Pragma": "no-cache",
    "Expires": "0",
}

if _front:
    _index = _front / "index.html"

    _assets = _front / "assets"
    if _assets.is_dir():
        app.mount("/assets", StaticFiles(directory=str(_assets)), name="assets")

    @app.get("/")
    async def serve_index():
        return FileResponse(str(_index), media_type="text/html", headers=_NO_CACHE_HEADERS)

    @app.get("/{path:path}")
    async def spa_fallback(path: str):
        # Защита от path traversal: нормализуем и проверяем, что внутри _front
        resolved = (_front / path).resolve()
        if not str(resolved).startswith(str(_front.resolve())):
            return FileResponse(str(_index), media_type="text/html", headers=_NO_CACHE_HEADERS)
        if resolved.is_file():
            if path == "telegram-web-app.js":
                return FileResponse(
                    str(resolved),
                    media_type="application/javascript",
                    headers={"Cache-Control": "public, max-age=86400"},
                )
            if path.endswith(".webmanifest"):
                return FileResponse(
                    str(resolved),
                    media_type="application/manifest+json",
                    headers=_NO_CACHE_HEADERS,
                )
            # Статичные бренд-ассеты из public/ (не хэшируются Vite): иконки, шрифты, robots.txt.
            # Кэшируем на 30 дней — заметно ускоряет повторные визиты, но без «вечной» застойности.
            if path.lower().endswith((".png", ".jpg", ".jpeg", ".webp", ".svg", ".ico", ".woff", ".woff2", ".ttf")):
                return FileResponse(
                    str(resolved),
                    headers={"Cache-Control": "public, max-age=2592000"},
                )
            return FileResponse(str(resolved))
        return FileResponse(str(_index), media_type="text/html", headers=_NO_CACHE_HEADERS)

    print(f"📁 Serving frontend from {_front} (no-cache for index)")
else:
    print(f"⚠️  dist/ не найдена. Запусти: npm run build")


if __name__ == "__main__":
    import uvicorn
    print(f"▶️ TGPlay Lite API on http://0.0.0.0:{PORT}")
    print(f"👥 Max concurrent: {LIMIT_CONCURRENCY} | Keep-alive: 120s")
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=PORT,
        timeout_keep_alive=120,     # Держим соединения дольше
        limit_concurrency=LIMIT_CONCURRENCY,
        limit_max_requests=10000,   # Рестарт worker после 10k запросов (утечки памяти)
        backlog=256,                # Большая очередь входящих
        access_log=False,           # Отключаем access log для скорости
    )
