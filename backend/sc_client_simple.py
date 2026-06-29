from __future__ import annotations

import asyncio
import base64
import re
import time
from typing import Any, Dict, List, Optional, Tuple

import aiohttp
import unicodedata


def build_soundcloud_track_id(track_id: int | str) -> str:
    return f"sc:{int(str(track_id).strip())}"


def parse_soundcloud_track_id(raw: str) -> Optional[int]:
    s = (raw or "").strip()
    if not s:
        return None
    if s.startswith("sc_"):
        s = s[3:]
    elif s.startswith("sc:"):
        s = s[3:]
    if not s.isdigit():
        return None
    return int(s)


def is_soundcloud_track_id(raw: str) -> bool:
    return parse_soundcloud_track_id(raw) is not None


_TITLE_SEPARATORS = (" - ", " – ", " — ", " | ", " / ")
_DASH_SPLIT_RE = re.compile(r"\s*[-–—]\s*")


def _norm_text(s: str) -> str:
    return " ".join((s or "").split())

_RU_TO_LAT = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "e",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "y",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "h",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "sch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
}


def _translit_ru_to_lat(s: str) -> str:
    t = unicodedata.normalize("NFKC", (s or "")).strip().lower()
    out = []
    for ch in t:
        if ch in _RU_TO_LAT:
            out.append(_RU_TO_LAT[ch])
        else:
            out.append(ch if (ch.isalnum() or ch in (" ", "-", "_")) else " ")
    return _norm_text("".join(out))


def _translit_lat_to_ru(s: str) -> str:
    """Best-effort reverse translit for russian artist usernames (e.g. pasosh -> пасош)."""
    t = unicodedata.normalize("NFKC", (s or "")).strip().lower()
    if not t:
        return ""
    # keep only latin letters, digits and separators; if contains other scripts, don't touch
    for ch in t:
        if ch.isalpha() and not ("a" <= ch <= "z"):
            return ""
    mapping = [
        ("sch", "щ"),
        ("sh", "ш"),
        ("ch", "ч"),
        ("zh", "ж"),
        ("ts", "ц"),
        ("yu", "ю"),
        ("ya", "я"),
        ("yo", "ё"),
        ("e", "е"),
        ("a", "а"),
        ("b", "б"),
        ("v", "в"),
        ("g", "г"),
        ("d", "д"),
        ("z", "з"),
        ("i", "и"),
        ("y", "й"),
        ("k", "к"),
        ("l", "л"),
        ("m", "м"),
        ("n", "н"),
        ("o", "о"),
        ("p", "п"),
        ("r", "р"),
        ("s", "с"),
        ("t", "т"),
        ("u", "у"),
        ("f", "ф"),
        ("h", "х"),
        ("j", "й"),
        ("q", "к"),
        ("w", "в"),
        ("x", "кс"),
        ("c", "к"),
    ]
    out: List[str] = []
    i = 0
    while i < len(t):
        if t[i].isalnum() or t[i] in (" ", "-", "_"):
            # handle multi-letter first
            if t[i].isalpha():
                matched = False
                for src, dst in mapping:
                    if t.startswith(src, i):
                        out.append(dst)
                        i += len(src)
                        matched = True
                        break
                if matched:
                    continue
            out.append(t[i])
        else:
            out.append(" ")
        i += 1
    return _norm_text("".join(out))


def _split_artist_title(text: str) -> Optional[Tuple[str, str]]:
    t = _norm_text(text)
    if not t:
        return None
    for sep in _TITLE_SEPARATORS:
        if sep not in t:
            continue
        left, right = t.split(sep, 1)
        left, right = _norm_text(left), _norm_text(right)
        if left and right:
            return left, right
    return None


def _strip_leading_artist(artist: str, title: str) -> str:
    """Убирает «Artist - » в начале названия, в т.ч. повтор «Artist - Artist - …»."""
    a = _norm_text(artist)
    t = _norm_text(title)
    if not a or not t:
        return t
    low_t = t.lower()
    low_a = a.lower()
    for sep in _TITLE_SEPARATORS:
        prefix = f"{a}{sep}"
        if low_t.startswith(prefix.lower()):
            t = _norm_text(t[len(prefix) :])
            low_t = t.lower()
        dup = f"{a}{sep}{a}{sep}"
        if low_t.startswith(dup.lower()):
            t = _norm_text(t[len(dup) :])
            low_t = t.lower()
    if low_t == low_a:
        return ""
    return t


def _publisher_meta(track: Dict[str, Any]) -> Dict[str, Any]:
    pm = track.get("publisher_metadata")
    return pm if isinstance(pm, dict) else {}


def _tokens(s: str) -> List[str]:
    t = unicodedata.normalize("NFKD", (s or "")).casefold()
    # Strip combining marks (e.g. "й" vs "й") for stable matching.
    t = "".join(ch for ch in t if unicodedata.category(ch) != "Mn")
    t = re.sub(r"[^\w\s]+", " ", t, flags=re.UNICODE)
    return [x for x in t.split() if x]


def _artist_relevant(artist: str, query: str) -> bool:
    a = " ".join(_tokens(artist))
    q = " ".join(_tokens(query))
    if not a or not q:
        return False
    if q in a:
        return True
    at = set(a.split())
    qt = [x for x in q.split() if len(x) >= 2]
    return bool(qt) and all(x in at for x in qt)


def _split_matches_multiword_query(left: str, right: str, query: str) -> bool:
    """Для запроса вида 'Artist Track' принимаем split 'Artist — Track' даже если левый блок не содержит все токены."""
    q = _tokens(query)
    if len(q) < 2:
        return False
    lt = set(_tokens(left))
    rt = set(_tokens(right))
    if not lt or not rt:
        return False
    # часть токенов должна матчиться артисту, часть — названию
    qset = set(q)
    in_left = len(qset & lt)
    in_right = len(qset & rt)
    return in_left >= 1 and in_right >= 1 and (in_left + in_right) >= min(len(qset), 3)


def _split_artist_title_from_title(raw_title: str) -> Optional[Tuple[str, str]]:
    t = _norm_text(raw_title)
    if not t:
        return None
    # Prefer em/en dash and hyphen split without requiring spaces.
    parts = _DASH_SPLIT_RE.split(t, maxsplit=1)
    if len(parts) == 2:
        left, right = _norm_text(parts[0]), _norm_text(parts[1])
        if left and right:
            return left, right
    # Fallback to old separators with spaces (kept for | / patterns).
    return _split_artist_title(t)


def _track_matches_any_query_token(track: Dict[str, Any], query: str) -> bool:
    qt = [x for x in _tokens(query) if len(x) >= 2]
    qt2 = [x for x in _tokens(_translit_ru_to_lat(query)) if len(x) >= 2]
    if not qt:
        return True
    hay = " ".join(
        _tokens(
            f"{track.get('artist') or ''} {track.get('title') or ''} "
            f"{track.get('metadata_artist') or ''} {( _publisher_meta(track).get('artist') or '')}"
        )
    )
    if not hay:
        return False
    # Combine original + translit tokens to handle Cyrillic query vs Latin metadata.
    all_qt = qt + [x for x in qt2 if x not in qt]
    # Multiword queries: require the FIRST token (usually artist) and at least one more token.
    # This prevents trash matches like query "пасош каждый день" returning any song with only "каждый".
    if len(all_qt) >= 2:
        # Prefer first token from original query; if it doesn't match anything, allow translit-first.
        first = qt[0] if qt else all_qt[0]
        if first not in hay:
            first2 = qt2[0] if qt2 else ""
            if not first2 or first2 not in hay:
                return False
        # Need one extra token (for 2 tokens it's effectively "both").
        rest = []
        if qt:
            rest.extend(qt[1:])
        if qt2:
            rest.extend([x for x in qt2[1:] if x not in rest])
        return any(tok in hay for tok in rest)
    # Single word: any match.
    return (qt[0] in hay) or (qt2[0] in hay if qt2 else False)


def infer_sc_artist_title(
    track: Dict[str, Any],
    *,
    query: Optional[str] = None,
    catalog_artist: Optional[str] = None,
) -> Tuple[str, str]:
    """Пытается вернуть идеальные (title, artist) для UI.

    Правило релевантности (если задан query):
    - если метаданные артиста нерелевантны, но левый блок `title` релевантен, используем split title.
    """
    raw_title = _norm_text(str(track.get("title") or ""))
    meta_artist = _norm_text(str(track.get("metadata_artist") or ""))
    pm = _publisher_meta(track)
    pm_artist = _norm_text(str(pm.get("artist") or ""))
    pm_title = _norm_text(str(pm.get("title") or ""))
    split = _split_artist_title_from_title(raw_title)

    q = (query or "").strip()
    def _has_cyr(s: str) -> bool:
        ss = (s or "").casefold()
        return any(("а" <= ch <= "я") or ch == "ё" for ch in ss)

    title_has_cyr = _has_cyr(raw_title) or _has_cyr(pm_title)
    # Helper: strip "Artist — " prefix from titles when we know artist.
    def _title_without_artist_prefix(title: str, artist: str) -> str:
        t = _strip_leading_artist(artist, title)
        return t or title

    # 0) ПРИОРИТЕТ: явный «Artist - Title» в названии (разделитель С пробелами: " - ", " – ", " — ", " | ", " / ").
    # По требованию владельца: исполнитель — это то, что стоит перед длинным тире, название — после.
    # Это лечит mis-tagged загрузки, где в metadata_artist стоит имя загрузчика (uploader), а реальный
    # артист закодирован в title. Применяем только к «пробельному» split, чтобы не ломать дефисы внутри слов.
    spaced_split = _split_artist_title(raw_title)
    if spaced_split:
        left, right = spaced_split
        # Guard: левая часть должна выглядеть как имя исполнителя (1–5 слов), правая — непустая.
        if left and right and len(left.split()) <= 5:
            cleaned = _strip_leading_artist(left, right) or right
            return cleaned, left

    # 1) metadata_artist (structured; trust it regardless of query)
    # Prefer publisher_metadata.artist if it matches the title script better (common case: username latin, display name cyrillic)
    if title_has_cyr and _has_cyr(pm_artist):
        title = pm_title or raw_title or "Трек"
        return _title_without_artist_prefix(title, pm_artist), pm_artist
    if meta_artist:
        title = raw_title or pm_title or "Трек"
        return _title_without_artist_prefix(title, meta_artist), meta_artist

    # 2) publisher_metadata (structured; trust it regardless of query)
    if pm_artist and (pm_title or raw_title):
        # pm_title is often missing; keep raw_title but still use structured artist.
        title = pm_title or raw_title or "Трек"
        return _title_without_artist_prefix(title, pm_artist), pm_artist

    # 3) title split (Artist — Title)
    if split:
        left, right = split
        # Split is a formatting rule: if the uploader encoded "Artist — Title" in the title,
        # we show it as such even when the search query is a track name (e.g. "каждый день").
        return right, left

    # 4) if metadata/publisher exists but нерелевантны — не используем их
    # 5) fallback: в каталоге артиста — всегда показываем выбранного артиста
    if catalog_artist:
        a = _norm_text(catalog_artist) or ""
        title = raw_title or pm_title or "Трек"
        return _title_without_artist_prefix(title, a), a

    # 6) last resort: query as artist (лучше чем username/Unknown)
    if q:
        # В обычном q=поиске не подставляем query как artist — это превращает нерелевантные треки в «правильные».
        return (raw_title or pm_title or "Трек"), ""

    # 7) no query: use any available structured field, otherwise keep raw title and empty artist
    if pm_artist:
        title = pm_title or raw_title or "Трек"
        return _title_without_artist_prefix(title, pm_artist), pm_artist
    if meta_artist:
        title = raw_title or "Трек"
        return _title_without_artist_prefix(title, meta_artist), meta_artist
    if split:
        left, right = split
        return right, left
    # As a last resort, use uploader username (better than empty in UI for deep links).
    user = track.get("user") if isinstance(track.get("user"), dict) else {}
    full = _norm_text(str(user.get("full_name") or ""))
    uname = _norm_text(str(user.get("username") or ""))
    a = full or uname
    title = raw_title or "Трек"
    if a and title_has_cyr and not _has_cyr(a):
        ru_guess = _translit_lat_to_ru(a)
        if ru_guess:
            a = ru_guess
    return _title_without_artist_prefix(title, a), (a if a else "")


def _query_is_artist_like(query: str) -> bool:
    qt = _tokens(query)
    if not qt:
        return False
    # Artist-like только для 1 слова: 'пасош'. Для 'пасош улицы' это уже скорее трек/запрос.
    if len(qt) != 1:
        return False
    return len(qt[0]) >= 2 and len(qt[0]) <= 40


def _track_matches_query_tokens(track: Dict[str, Any], query: str) -> bool:
    qt = _tokens(query)
    if not qt:
        return True
    blob = f"{track.get('artist') or ''} {track.get('title') or ''}"
    bt = set(_tokens(blob))
    # Для многословных запросов требуем, чтобы все токены встречались в artist/title (любая комбинация).
    if len(qt) >= 2:
        return all(t in bt for t in qt)
    # Для однословных — достаточно одного вхождения.
    return qt[0] in bt


def sc_track_needs_meta_enrich(track: Dict[str, Any]) -> bool:
    pm = _publisher_meta(track)
    return (
        not _norm_text(str(track.get("metadata_artist") or ""))
        and (not _norm_text(str(pm.get("artist") or "")) or not _norm_text(str(pm.get("title") or "")))
    )


def _pick_best_user(users: List[Dict[str, Any]], query: str) -> Optional[Dict[str, Any]]:
    q = _norm_text(query).casefold()
    if not q:
        return None
    for u in users:
        if not isinstance(u, dict):
            continue
        name = _norm_text(str(u.get("username") or "")).casefold()
        full = _norm_text(str(u.get("full_name") or "")).casefold()
        if name == q or full == q:
            return u
    for u in users:
        if not isinstance(u, dict):
            continue
        name = _norm_text(str(u.get("username") or "")).casefold()
        full = _norm_text(str(u.get("full_name") or "")).casefold()
        if q and (q in name or q in full):
            return u
    return users[0] if users else None


def _cover_url(track: Dict[str, Any]) -> Optional[str]:
    artwork = track.get("artwork_url")
    if isinstance(artwork, str) and artwork.strip():
        return artwork.strip().replace("-large.", "-t500x500.")
    user = track.get("user") if isinstance(track.get("user"), dict) else {}
    avatar = user.get("avatar_url")
    if isinstance(avatar, str) and avatar.strip():
        return avatar.strip().replace("-large.", "-t500x500.")
    return None


def normalize_track(track: Dict[str, Any]) -> Dict[str, Any]:
    tid = track.get("id")
    if tid is None:
        return {}
    if str(track.get("access") or "").strip().lower() != "playable":
        return {}
    duration_ms = track.get("duration")
    duration_sec = 0
    if isinstance(duration_ms, (int, float)) and duration_ms > 0:
        duration_sec = int(duration_ms // 1000)
    title, artist = infer_sc_artist_title(track)
    return {
        "id": build_soundcloud_track_id(tid),
        "title": title,
        "artist": artist,
        "duration": duration_sec,
        "cover_url": _cover_url(track),
        "provider": "soundcloud",
        "vk_legacy": False,
        "genre_id": None,
    }


class SoundCloudClient:
    def __init__(self, client_id: str, client_secret: str, *, token_cache_seconds: int = 3600):
        self.client_id = (client_id or "").strip()
        self.client_secret = (client_secret or "").strip()
        self.token_cache_seconds = max(300, min(int(token_cache_seconds or 3600), 3600))
        self._token: str = ""
        self._token_expire_at: float = 0.0
        self._token_lock = asyncio.Lock()
        # user_id -> (full_name, expire_at)
        self._user_fullname_cache: Dict[int, Tuple[str, float]] = {}

    @property
    def configured(self) -> bool:
        return bool(self.client_id and self.client_secret)

    async def _refresh_token_locked(self, session: aiohttp.ClientSession) -> str:
        payload = {"grant_type": "client_credentials"}
        basic = base64.b64encode(f"{self.client_id}:{self.client_secret}".encode("utf-8")).decode("ascii")
        headers = {
            "Accept": "application/json; charset=utf-8",
            "Content-Type": "application/x-www-form-urlencoded",
            "Authorization": f"Basic {basic}",
        }
        async with session.post(
            "https://secure.soundcloud.com/oauth/token",
            data=payload,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            if resp.status != 200:
                txt = await resp.text()
                raise RuntimeError(f"SoundCloud token request failed: {resp.status} {txt[:160]}")
            data = await resp.json()
        token = str(data.get("access_token") or "").strip()
        if not token:
            raise RuntimeError("SoundCloud token is empty")
        expires_in = data.get("expires_in")
        ttl = 3600
        if isinstance(expires_in, (int, float)) and expires_in > 0:
            ttl = int(expires_in)
        ttl = max(300, min(ttl, self.token_cache_seconds))
        self._token = token
        self._token_expire_at = time.time() + ttl - 20
        return token

    async def get_access_token(self, session: aiohttp.ClientSession) -> str:
        now = time.time()
        if self._token and now < self._token_expire_at:
            return self._token
        async with self._token_lock:
            now2 = time.time()
            if self._token and now2 < self._token_expire_at:
                return self._token
            return await self._refresh_token_locked(session)

    async def _get(
        self,
        session: aiohttp.ClientSession,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        timeout_sec: int = 20,
    ) -> Dict[str, Any]:
        url = f"https://api.soundcloud.com{path}"
        return await self._get_url(session, url, params=params, timeout_sec=timeout_sec)

    async def _get_url(
        self,
        session: aiohttp.ClientSession,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        timeout_sec: int = 20,
    ) -> Any:
        token = await self.get_access_token(session)
        req_params = dict(params or {})
        headers = {"Accept": "application/json; charset=utf-8", "Authorization": f"OAuth {token}"}
        async with session.get(
            url,
            params=req_params,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=timeout_sec),
        ) as resp:
            if resp.status == 401:
                async with self._token_lock:
                    await self._refresh_token_locked(session)
                headers["Authorization"] = f"OAuth {self._token}"
                async with session.get(
                    url,
                    params=req_params,
                    headers=headers,
                    timeout=aiohttp.ClientTimeout(total=timeout_sec),
                ) as resp2:
                    if resp2.status != 200:
                        txt = await resp2.text()
                        raise RuntimeError(f"SoundCloud API failed: {resp2.status} {txt[:200]}")
                    return await resp2.json()
            if resp.status != 200:
                txt = await resp.text()
                raise RuntimeError(f"SoundCloud API failed: {resp.status} {txt[:200]}")
            return await resp.json()

    async def search_tracks(self, session: aiohttp.ClientSession, query: str, *, limit: int, offset: int) -> List[Dict[str, Any]]:
        raw_items, _ = await self._get_track_search_page(session, query, limit=limit, offset=offset)
        items: List[Dict[str, Any]] = raw_items
        out = await self._normalize_items(session, items, enrich_cap=20, query=query)
        if _query_is_artist_like(query):
            # Для однословного «артиста» держим треки, где запрос есть в artist ИЛИ в title.
            out = [
                t
                for t in out
                if _artist_relevant(str(t.get("artist") or ""), query) or _track_matches_query_tokens(t, query)
            ]
        else:
            # Для многословных запросов повышаем recall: достаточно совпадения по любому токену.
            out = [t for t in out if _track_matches_any_query_token(t, query)]
        return out

    async def _get_track_search_page(
        self,
        session: aiohttp.ClientSession,
        query: str,
        *,
        limit: int,
        offset: int = 0,
        next_href: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        if next_href:
            data = await self._get_url(session, next_href, timeout_sec=22)
        else:
            data = await self._get(
                session,
                "/tracks",
                params={
                    "q": query,
                    "limit": max(1, min(100, int(limit))),
                    "offset": max(0, int(offset)),
                    "linked_partitioning": "true",
                    "access": "playable",
                },
                timeout_sec=22,
            )
        coll = data.get("collection") if isinstance(data, dict) else None
        items: List[Dict[str, Any]] = [x for x in coll if isinstance(x, dict)] if isinstance(coll, list) else []
        href = data.get("next_href") if isinstance(data, dict) else None
        return items, (str(href).strip() if isinstance(href, str) and href.strip() else None)

    async def _normalize_items(
        self,
        session: aiohttp.ClientSession,
        items: List[Dict[str, Any]],
        *,
        enrich_cap: int = 20,
        query: Optional[str] = None,
        catalog_artist: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        enrich_ids: List[int] = []
        enrich_idx: List[int] = []
        for i, item in enumerate(items):
            if sc_track_needs_meta_enrich(item) and item.get("id") is not None:
                enrich_idx.append(i)
                enrich_ids.append(int(item["id"]))
        if enrich_ids:
            fulls = await asyncio.gather(
                *[self.get_track(session, tid) for tid in enrich_ids[:enrich_cap]],
                return_exceptions=True,
            )
            for pos, full in zip(enrich_idx[:enrich_cap], fulls):
                if isinstance(full, dict) and full.get("id") is not None:
                    items[pos] = full
        out: List[Dict[str, Any]] = []
        for item in items:
            n = normalize_track(item)
            if n.get("id"):
                # Override title/artist based on query/catalog context
                title, artist = infer_sc_artist_title(item, query=query, catalog_artist=catalog_artist)
                n["title"] = title
                n["artist"] = artist
            if n.get("id"):
                out.append(n)
        return out

    async def search_users(self, session: aiohttp.ClientSession, query: str, *, limit: int = 8) -> List[Dict[str, Any]]:
        data = await self._get(
            session,
            "/users",
            params={
                "q": query,
                "limit": max(1, min(50, int(limit))),
                "linked_partitioning": "true",
            },
            timeout_sec=15,
        )
        if isinstance(data, list):
            return [x for x in data if isinstance(x, dict)]
        coll = data.get("collection") if isinstance(data, dict) else None
        return [x for x in coll if isinstance(x, dict)] if isinstance(coll, list) else []

    async def resolve_artist_user_id(self, session: aiohttp.ClientSession, artist_query: str) -> Optional[int]:
        users = await self.search_users(session, artist_query, limit=12)
        # If Cyrillic query doesn't find the artist profile, try a cheap transliteration.
        tr = _translit_ru_to_lat(artist_query)
        if tr and tr != _norm_text(artist_query).lower():
            try:
                users2 = await self.search_users(session, tr, limit=12)
                # merge by id
                seen = {u.get("id") for u in users if isinstance(u, dict)}
                for u in users2:
                    if isinstance(u, dict) and u.get("id") not in seen:
                        users.append(u)
                        seen.add(u.get("id"))
            except Exception:
                pass
        if not users:
            return None

        # Fast path: exact match
        best = _pick_best_user(users, artist_query)
        best_id: Optional[int] = None
        if best and best.get("id") is not None:
            try:
                best_id = int(best["id"])
            except Exception:
                best_id = None
            if best_id is not None:
                name = _norm_text(str(best.get("username") or "")) or _norm_text(str(best.get("full_name") or ""))
                if name and _artist_relevant(name, artist_query):
                    return best_id

        # Score top candidates by looking at their first page of tracks.
        cand = []
        for u in users[:8]:
            try:
                uid = int(u.get("id"))
            except Exception:
                continue
            cand.append(uid)
        if not cand:
            return None

        sem = asyncio.Semaphore(4)

        async def _score(uid: int) -> Tuple[int, int]:
            async with sem:
                raw_items, _ = await self._get_user_tracks_page(session, uid, limit=60, offset=0)
            good = 0
            total = 0
            for it in raw_items:
                title, artist = infer_sc_artist_title(it, query=artist_query, catalog_artist=artist_query)
                if title:
                    total += 1
                if _artist_relevant(artist, artist_query):
                    good += 1
            return good, total

        scores = await asyncio.gather(*[_score(uid) for uid in cand], return_exceptions=True)
        best_uid = None
        best_score = (-1, -1)
        for uid, sc in zip(cand, scores):
            if isinstance(sc, Exception):
                continue
            s = (int(sc[0]), int(sc[1]))
            if s > best_score:
                best_score = s
                best_uid = uid
        # Require some evidence that this user actually matches the artist query.
        if best_uid is not None and best_score[0] >= 3:
            return best_uid
        # Fallback: if scoring failed (rate limits/timeouts), use best match from search results.
        if best_id is not None:
            return best_id
        return cand[0] if cand else None

    async def resolve_artist_user_id_via_tracks(self, session: aiohttp.ClientSession, artist_query: str) -> Optional[int]:
        """Глобальный fallback: если /users?q= не находит правильный профиль, собираем кандидатов из track-search.

        Идея: по q=artist тянем 1–2 страницы /tracks и собираем user.id из треков, где split/title даёт релевантного артиста.
        Затем выбираем user с максимальным количеством матчей.
        """
        cand_score: Dict[int, int] = {}
        next_href: Optional[str] = None
        offset = 0
        for _ in range(2):
            raw_items, next_href = await self._get_track_search_page(
                session,
                artist_query,
                limit=100,
                offset=offset,
                next_href=next_href,
            )
            if not raw_items:
                break
            for it in raw_items:
                # Only count if we can infer the artist as matching the query
                _, art = infer_sc_artist_title(it, query=artist_query, catalog_artist=artist_query)
                if not art or not _artist_relevant(art, artist_query):
                    continue
                user = it.get("user") if isinstance(it.get("user"), dict) else {}
                uid = user.get("id")
                if isinstance(uid, int):
                    cand_score[uid] = cand_score.get(uid, 0) + 1
            offset += len(raw_items)
            if not next_href:
                break
        if not cand_score:
            return None
        return max(cand_score.items(), key=lambda kv: kv[1])[0]

    async def _get_user_tracks_page(
        self,
        session: aiohttp.ClientSession,
        user_id: int,
        *,
        limit: int,
        offset: int = 0,
        next_href: Optional[str] = None,
    ) -> Tuple[List[Dict[str, Any]], Optional[str]]:
        if next_href:
            data = await self._get_url(session, next_href, timeout_sec=22)
        else:
            data = await self._get(
                session,
                f"/users/{int(user_id)}/tracks",
                params={
                    "limit": max(1, min(200, int(limit))),
                    "offset": max(0, int(offset)),
                    "linked_partitioning": "true",
                    "access": "playable",
                },
                timeout_sec=22,
            )
        coll = data.get("collection") if isinstance(data, dict) else None
        items = [x for x in coll if isinstance(x, dict)] if isinstance(coll, list) else []
        href = data.get("next_href") if isinstance(data, dict) else None
        return items, (str(href).strip() if isinstance(href, str) and href.strip() else None)

    async def get_user_tracks(
        self,
        session: aiohttp.ClientSession,
        user_id: int,
        *,
        limit: int,
        offset: int = 0,
    ) -> List[Dict[str, Any]]:
        items, _ = await self._get_user_tracks_page(session, user_id, limit=limit, offset=offset)
        return await self._normalize_items(session, items, enrich_cap=25)

    async def artist_catalog_tracks(self, session: aiohttp.ClientSession, artist_query: str, *, limit: int) -> List[Dict[str, Any]]:
        uid = await self.resolve_artist_user_id(session, artist_query)
        if uid is None:
            uid = await self.resolve_artist_user_id_via_tracks(session, artist_query)
        async def _fallback_from_track_search() -> List[Dict[str, Any]]:
            out: List[Dict[str, Any]] = []
            offset = 0
            next_href: Optional[str] = None
            pages = 0
            while len(out) < limit:
                pages += 1
                if pages > 8:
                    break
                raw_items, next_href = await self._get_track_search_page(
                    session,
                    artist_query,
                    limit=100,
                    offset=offset,
                    next_href=next_href,
                )
                if not raw_items:
                    break
                chunk = await self._normalize_items(
                    session,
                    raw_items,
                    enrich_cap=20,
                    query=artist_query,
                    catalog_artist=artist_query,
                )
                seen = {t.get("id") for t in out}
                for t in chunk:
                    if t.get("id") and t.get("id") not in seen:
                        out.append(t)
                        seen.add(t.get("id"))
                        if len(out) >= limit:
                            break
                offset += len(raw_items)
                if not next_href or len(chunk) < 80:
                    break
            return out[:limit]

        if uid is None:
            return await _fallback_from_track_search()
        out: List[Dict[str, Any]] = []
        offset = 0
        next_href: Optional[str] = None
        pages = 0
        while len(out) < limit:
            pages += 1
            if pages > 4:
                break
            raw_items, next_href = await self._get_user_tracks_page(
                session,
                uid,
                limit=min(200, limit - len(out)),
                offset=offset,
                next_href=next_href,
            )
            if not raw_items:
                break
            chunk = await self._normalize_items(session, raw_items, enrich_cap=25, query=artist_query, catalog_artist=artist_query)
            if chunk:
                out.extend(chunk)
            offset += len(raw_items)
            if not next_href:
                break
        out = out[:limit]
        # If user catalog looks suspiciously small, fallback to track search pagination.
        if len(out) < 15:
            fb = await _fallback_from_track_search()
            if len(fb) > len(out):
                return fb
        return out

    async def get_track(self, session: aiohttp.ClientSession, track_id: int) -> Optional[Dict[str, Any]]:
        data = await self._get(session, f"/tracks/{track_id}", timeout_sec=15)
        if not isinstance(data, dict):
            return None
        # Some tracks don't include user.full_name; fetch it to display correct artist
        user = data.get("user") if isinstance(data.get("user"), dict) else None
        uid = None
        if isinstance(user, dict) and user.get("id") is not None:
            try:
                uid = int(user["id"])
            except Exception:
                uid = None
        if uid is not None:
            full = _norm_text(str((user or {}).get("full_name") or ""))
            if not full:
                now = time.time()
                cached = self._user_fullname_cache.get(uid)
                if cached and cached[1] > now and cached[0]:
                    (user or {})["full_name"] = cached[0]
                else:
                    try:
                        udata = await self._get(session, f"/users/{uid}", timeout_sec=12)
                        if isinstance(udata, dict):
                            full2 = _norm_text(str(udata.get("full_name") or ""))
                            if full2:
                                user["full_name"] = full2
                                self._user_fullname_cache[uid] = (full2, now + 12 * 3600)
                    except Exception:
                        pass
        return data

    async def related_tracks(self, session: aiohttp.ClientSession, track_id: int, *, limit: int) -> List[Dict[str, Any]]:
        data = await self._get(
            session,
            f"/tracks/{track_id}/related",
            params={"limit": max(1, min(100, int(limit)))},
            timeout_sec=18,
        )
        coll = data.get("collection") if isinstance(data, dict) else data
        if not isinstance(coll, list):
            return []
        out: List[Dict[str, Any]] = []
        for item in coll:
            if isinstance(item, dict):
                n = normalize_track(item)
                if n.get("id"):
                    out.append(n)
        return out

    @staticmethod
    def _is_preview_stream_url(url: str) -> bool:
        u = (url or "").lower()
        return "cf-preview-media" in u or "/preview/" in u or "preview_mp3" in u

    async def _follow_stream_redirect(
        self,
        session: aiohttp.ClientSession,
        stream_api_url: str,
        *,
        headers: Dict[str, str],
    ) -> Optional[str]:
        async with session.get(
            stream_api_url,
            headers=headers,
            timeout=aiohttp.ClientTimeout(total=15),
            allow_redirects=False,
        ) as resp:
            if resp.status in (301, 302, 303, 307, 308):
                loc = resp.headers.get("Location")
                if isinstance(loc, str) and loc.strip():
                    final = loc.strip()
                    if self._is_preview_stream_url(final):
                        return None
                    return final
                return None
            if resp.status == 200:
                final = str(resp.url)
                if self._is_preview_stream_url(final):
                    return None
                return final
            return None

    async def resolve_stream_url(self, session: aiohttp.ClientSession, track_id: int) -> Optional[str]:
        track = await self.get_track(session, track_id)
        if not track or str(track.get("access") or "").strip().lower() != "playable":
            return None
        token = await self.get_access_token(session)
        headers = {"Accept": "application/json; charset=utf-8", "Authorization": f"OAuth {token}"}
        streams = await self._get(session, f"/tracks/{track_id}/streams", timeout_sec=15)
        if not isinstance(streams, dict):
            return None
        for key in ("http_mp3_128_url", "hls_mp3_128_url", "hls_aac_160_url"):
            api_url = streams.get(key)
            if not isinstance(api_url, str) or not api_url.strip():
                continue
            final = await self._follow_stream_redirect(session, api_url.strip(), headers=headers)
            if final:
                return final
        return None
