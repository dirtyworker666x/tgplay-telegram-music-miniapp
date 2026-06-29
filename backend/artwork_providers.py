from __future__ import annotations

import asyncio
import re
import time
from collections import OrderedDict, defaultdict
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Dict, List, Optional, Tuple
from urllib.parse import quote_plus

TrackLike = Dict[str, Any]
FetchAny = Callable[[str], Awaitable[Tuple[Optional[Dict[str, Any]], Optional[str]]]]


def _norm_text(v: str) -> str:
    s = (v or "").strip().lower()
    s = re.sub(r"\s+", " ", s)
    return s


def _title_core(v: str) -> str:
    s = _norm_text(v)
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)
    s = re.sub(r"\b(feat|ft|remix|live|cover|edit|version|ai cover)\b.*$", "", s).strip()
    # Remove common noisy suffixes from social reposts/bot uploads.
    s = re.sub(r"\b(vk\.com/[^\s]+|@[\w_]+|nightcore|sped up|slowed)\b", " ", s)
    return re.sub(r"\s+", " ", s)


def _vk_title_low_trust(title: str) -> bool:
    """VK uploads with these markers often mismatch iTunes/Deezer catalog entries."""
    s = _norm_text(title)
    needles = (
        "ai cover",
        "aicover",
        "nightcore",
        "nightcorebot",
        "sped up",
        "slowed",
        "vk.com/",
        "форум ai",
    )
    return any(n in s for n in needles)


def _titles_close_enough(request_title: str, cand_title: str) -> bool:
    """Guard: reject obvious VK-vs-catalog mismatches.  Returns True if title is plausibly same track."""
    a, b = _title_core(request_title), _title_core(cand_title)
    if not a or not b:
        return False
    if a == b:
        return True
    if a in b or b in a:
        return min(len(a), len(b)) >= 4
    wa, wb = set(a.split()), set(b.split())
    if not wa or not wb:
        return False
    if len(wa & wb) >= 2:
        return True
    if len(wa) == 1 and next(iter(wa)) in wb:
        return True
    if len(wb) == 1 and next(iter(wb)) in wa:
        return True
    # If no common word, title is unrelated — reject even if score is high.
    return False


_RU_TO_EN = str.maketrans({
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ё": "e", "ж": "zh", "з": "z",
    "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n", "о": "o", "п": "p", "р": "r",
    "с": "s", "т": "t", "у": "u", "ф": "f", "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch",
    "ъ": "", "ы": "y", "ь": "", "э": "e", "ю": "yu", "я": "ya",
})


def _translit_ru_to_en(text: str) -> str:
    out: List[str] = []
    for ch in (text or "").lower():
        if ch in _RU_TO_EN:
            out.append(str(_RU_TO_EN[ch]))
        else:
            out.append(ch)
    return "".join(out)


def _artist_variants(artist: str) -> List[str]:
    base = _norm_text(artist)
    out = [base] if base else []
    for sep in [",", "&", ";", " feat ", " ft ", " x ", "/"]:
        if sep in base:
            out.extend([x.strip() for x in base.split(sep) if x.strip()])
    uniq: List[str] = []
    seen = set()
    for s in out:
        if s and s not in seen:
            seen.add(s)
            uniq.append(s)
    return uniq[:5]


def _query_candidates(title: str, artist: str) -> List[str]:
    t = _title_core(title)
    av = _artist_variants(artist) or [""]
    out: List[str] = []
    for a in av:
        q = " ".join(p for p in (a, t) if p).strip()
        if q:
            out.append(q)
            tr = _translit_ru_to_en(q)
            if tr and tr != q:
                out.append(tr)
    # Title-only search causes many false iTunes/Deezer matches; use only when artist is unknown.
    if t and not str(artist or "").strip():
        out.append(t)
        tt = _translit_ru_to_en(t)
        if tt and tt != t:
            out.append(tt)
    uniq: List[str] = []
    seen = set()
    for q in out:
        if q not in seen:
            seen.add(q)
            uniq.append(q)
    return uniq[:8]


def _safe_http_url(v: Any) -> Optional[str]:
    if isinstance(v, str):
        s = v.strip()
        if s.startswith("https://") or s.startswith("http://"):
            return s
    return None


def _is_placeholder_artwork(url: str) -> bool:
    u = (url or "").lower()
    bad = (
        "lastfm_logo",
        "logo_facebook",
        "apple-touch-icon",
        "/favicon",
        "default_avatar",
        "placeholder",
        "no_cover",
    )
    return any(x in u for x in bad)


def _cache_key(title: str, artist: str) -> str:
    return f"{_norm_text(artist)}::{_norm_text(title)}"


def _str_score(title: str, artist: str, cand_title: str, cand_artist: str) -> int:
    nt, na = _title_core(title), _norm_text(artist)
    ct, ca = _title_core(cand_title), _norm_text(cand_artist)
    score = 0
    if nt and ct:
        if nt == ct:
            score += 60
        elif nt in ct or ct in nt:
            # Between exact and noise: require some title signal if artist matches.
            score += 32
    if na and ca:
        if na == ca:
            score += 40
        elif na in ca or ca in na:
            score += 12
    # Penalize obvious mismatch noise in candidate title.
    noisy = _norm_text(cand_title)
    if any(x in noisy for x in ("nightcore", "sped up", "slowed", "ai cover", "aicover")):
        score -= 35
    # Requested VK title looks like a reupload/bootleg — do not accept weak catalog matches.
    if _vk_title_low_trust(title):
        score -= 45
    return score


def _itunes_pick_artwork_url(item: Any) -> Optional[str]:
    if not isinstance(item, dict):
        return None
    for k in ("artworkUrl100", "artworkUrl60", "artworkUrl30"):
        raw = _safe_http_url(item.get(k))
        if not raw:
            continue
        return re.sub(r"/\d+x\d+bb\.", "/1200x1200bb.", raw, count=1)
    return None


def _deezer_pick_artwork_url(item: Any) -> Optional[str]:
    if not isinstance(item, dict):
        return None
    album = item.get("album")
    if isinstance(album, dict):
        for k in ("cover_xl", "cover_big", "cover_medium", "cover"):
            u = _safe_http_url(album.get(k))
            if u:
                return u
    return None


def _musicbrainz_pick_release_mbid(payload: Dict[str, Any]) -> Optional[str]:
    releases = payload.get("releases")
    if not isinstance(releases, list):
        return None
    for rel in releases:
        if not isinstance(rel, dict):
            continue
        rel_id = rel.get("id")
        if isinstance(rel_id, str) and rel_id.strip():
            return rel_id.strip()
    return None


def _html_first_url(pattern: str, html: Optional[str]) -> Optional[str]:
    if not html:
        return None
    m = re.search(pattern, html, re.IGNORECASE)
    if not m:
        return None
    return _safe_http_url(m.group(1))


def _html_all_urls(pattern: str, html: Optional[str]) -> List[str]:
    if not html:
        return []
    out: List[str] = []
    for m in re.finditer(pattern, html, re.IGNORECASE):
        u = _safe_http_url(m.group(1))
        if u:
            out.append(u)
    return out


@dataclass
class ArtworkResolverConfig:
    enabled: bool = True
    max_per_request: int = 10
    cache_max: int = 4000
    aggressive_mode: bool = True
    min_confidence: int = 70


class ArtworkResolver:
    """Multi-provider resolver for track artwork with bounded LRU cache."""

    def __init__(self, config: ArtworkResolverConfig) -> None:
        self.config = config
        self._cache: "OrderedDict[str, Optional[str]]" = OrderedDict()
        self._lock = asyncio.Lock()
        self._metrics: Dict[str, Any] = {
            "requests": 0,
            "tracks_examined": 0,
            "tracks_enriched": 0,
            "cache_hit": 0,
            "cache_miss": 0,
            "provider_hit": defaultdict(int),
            "provider_error": defaultdict(int),
            "provider_miss": defaultdict(int),
        }

    async def _cache_get(self, key: str) -> Tuple[bool, Optional[str]]:
        async with self._lock:
            if key in self._cache:
                val = self._cache[key]
                self._cache.move_to_end(key)
                self._metrics["cache_hit"] += 1
                return True, val
            self._metrics["cache_miss"] += 1
            return False, None

    async def _cache_set(self, key: str, value: Optional[str]) -> None:
        async with self._lock:
            self._cache[key] = value
            self._cache.move_to_end(key)
            while len(self._cache) > self.config.cache_max:
                self._cache.popitem(last=False)

    def _provider_order(self) -> List[str]:
        # Clean mode: только стабильные API/структурированные источники.
        return [
            "itunes",
            "deezer",
            "spotify_api",
            "lastfm_api",
            "coverartarchive",
        ]

    async def _from_itunes(self, title: str, artist: str, fetch_any: FetchAny) -> Optional[str]:
        best_url, best = None, -1
        for term in _query_candidates(title, artist):
            payload, _ = await fetch_any(
                f"https://itunes.apple.com/search?term={quote_plus(term)}&media=music&entity=song&limit=8"
            )
            if not isinstance(payload, dict):
                continue
            results = payload.get("results")
            if not isinstance(results, list):
                continue
            for row in results:
                if not isinstance(row, dict):
                    continue
                score = _str_score(title, artist, str(row.get("trackName") or ""), str(row.get("artistName") or ""))
                cand = _itunes_pick_artwork_url(row)
                if cand and not _titles_close_enough(title, str(row.get("trackName") or "")):
                    continue
                if cand and score > best:
                    best = score
                    best_url = cand
        return best_url if best >= self.config.min_confidence else None

    async def _from_deezer(self, title: str, artist: str, fetch_any: FetchAny) -> Optional[str]:
        best_url, best = None, -1
        for q in _query_candidates(title, artist):
            payload, _ = await fetch_any(f"https://api.deezer.com/search?q={quote_plus(q)}&limit=8")
            if not isinstance(payload, dict):
                continue
            data = payload.get("data")
            if not isinstance(data, list):
                continue
            for row in data:
                if not isinstance(row, dict):
                    continue
                art = row.get("artist") if isinstance(row.get("artist"), dict) else {}
                score = _str_score(title, artist, str(row.get("title") or ""), str(art.get("name") or ""))
                cand = _deezer_pick_artwork_url(row)
                if cand and not _titles_close_enough(title, str(row.get("title") or "")):
                    continue
                if cand and score > best:
                    best = score
                    best_url = cand
        return best_url if best >= self.config.min_confidence else None

    async def _from_spotify_api(self, title: str, artist: str, fetch_any: FetchAny) -> Optional[str]:
        best_url, best = None, -1
        for q in _query_candidates(title, artist):
            payload, _ = await fetch_any(f"https://api.spotify.com/v1/search?type=track&limit=6&q={quote_plus(q)}")
            if not isinstance(payload, dict):
                continue
            tracks = (payload.get("tracks") or {}).get("items")
            if not isinstance(tracks, list):
                continue
            for row in tracks:
                if not isinstance(row, dict):
                    continue
                artists = row.get("artists")
                an = ""
                if isinstance(artists, list) and artists and isinstance(artists[0], dict):
                    an = str(artists[0].get("name") or "")
                score = _str_score(title, artist, str(row.get("name") or ""), an)
                album = row.get("album") if isinstance(row.get("album"), dict) else {}
                imgs = album.get("images")
                cand = None
                if isinstance(imgs, list):
                    for el in imgs:
                        if isinstance(el, dict):
                            cand = _safe_http_url(el.get("url"))
                            if cand:
                                break
                if cand and score > best:
                    best = score
                    best_url = cand
        return best_url if best >= self.config.min_confidence else None

    async def _from_lastfm_api(self, title: str, artist: str, fetch_any: FetchAny) -> Optional[str]:
        q = " ".join(p for p in (artist.strip(), title.strip()) if p).strip()
        if not q:
            return None
        payload, _ = await fetch_any(
            "https://ws.audioscrobbler.com/2.0/"
            f"?method=track.search&track={quote_plus(title)}&artist={quote_plus(artist)}&format=json&limit=8"
        )
        if not isinstance(payload, dict):
            return None
        results = (((payload.get("results") or {}).get("trackmatches") or {}).get("track"))
        if isinstance(results, dict):
            results = [results]
        if not isinstance(results, list):
            return None
        best_url, best = None, -1
        for row in results:
            if not isinstance(row, dict):
                continue
            score = _str_score(title, artist, str(row.get("name") or ""), str(row.get("artist") or ""))
            imgs = row.get("image")
            cand = None
            if isinstance(imgs, list):
                for el in reversed(imgs):
                    if isinstance(el, dict):
                        cand = _safe_http_url(el.get("#text"))
                        if cand:
                            break
            if cand and score > best:
                best = score
                best_url = cand
        return best_url if best >= self.config.min_confidence else None

    async def _from_bandcamp(self, title: str, artist: str, fetch_any: FetchAny) -> Optional[str]:
        q = " ".join(p for p in (artist.strip(), title.strip()) if p).strip()
        _, html = await fetch_any(f"https://bandcamp.com/search?q={quote_plus(q)}")
        cand = _html_first_url(r'<img[^>]+src="([^"]*f4\.bcbits\.com[^"]+)"', html)
        return cand

    async def _from_soundcloud(self, title: str, artist: str, fetch_any: FetchAny) -> Optional[str]:
        q = " ".join(p for p in (artist.strip(), title.strip()) if p).strip()
        _, html = await fetch_any(f"https://soundcloud.com/search/sounds?q={quote_plus(q)}")
        # 1) Прямой вытяг из hydration/page JSON
        cand = _html_first_url(r'"artwork_url":"(https:[^"]+)"', html)
        if cand:
            return cand.replace("\\u002F", "/").replace("-large.", "-t500x500.")
        # 2) Попытка извлечь client_id и сходить в v2 API
        client_id = None
        if html:
            m = re.search(r'client_id["\']?\s*[:=]\s*["\']([a-zA-Z0-9]{32})["\']', html)
            if m:
                client_id = m.group(1)
        if client_id:
            payload, _ = await fetch_any(
                "https://api-v2.soundcloud.com/search/tracks"
                f"?q={quote_plus(q)}&client_id={client_id}&limit=10"
            )
            if isinstance(payload, dict):
                coll = payload.get("collection")
                if isinstance(coll, list):
                    for row in coll:
                        if not isinstance(row, dict):
                            continue
                        u = _safe_http_url(row.get("artwork_url"))
                        if u:
                            return u.replace("-large.", "-t500x500.")
        # 3) fallback: если нашли track URL, пробуем oEmbed thumbnail
        for track_url in _html_all_urls(r'href="(https://soundcloud\.com/[^"/]+/[^"?#]+)"', html):
            oembed, _ = await fetch_any(
                f"https://soundcloud.com/oembed?format=json&url={quote_plus(track_url)}"
            )
            if isinstance(oembed, dict):
                u = _safe_http_url(oembed.get("thumbnail_url"))
                if u:
                    return u
        return None

    async def _from_yandex_music(self, title: str, artist: str, fetch_any: FetchAny) -> Optional[str]:
        q = " ".join(p for p in (artist.strip(), title.strip()) if p).strip()
        _, html = await fetch_any(f"https://music.yandex.ru/search?text={quote_plus(q)}")
        cand = _html_first_url(r'"coverUri":"([^"]+)"', html)
        if cand:
            u = cand.replace("\\/", "/")
            if u.startswith("//"):
                u = "https:" + u
            if "%%" in u:
                u = u.replace("%%", "1000x1000")
            if not u.startswith("http"):
                u = "https://" + u.lstrip("/")
            return _safe_http_url(u)
        cand = _html_first_url(r'https://[^"]+/(?:album|track)/[^"]+/cover[^"]+\.(?:jpg|png)', html)
        return cand

    async def _from_coverartarchive(self, title: str, artist: str, fetch_any: FetchAny) -> Optional[str]:
        q = f'release:"{title}" AND artist:"{artist}"'
        # fetch_any must provide a proper UA with contact info for MusicBrainz/CAA rate limit.
        mb, _ = await fetch_any(f"https://musicbrainz.org/ws/2/release/?query={quote_plus(q)}&fmt=json&limit=3")
        if not isinstance(mb, dict):
            return None
        rel_id = _musicbrainz_pick_release_mbid(mb)
        if not rel_id:
            return None
        caa, _ = await fetch_any(f"https://coverartarchive.org/release/{quote_plus(rel_id)}")
        if not isinstance(caa, dict):
            return None
        images = caa.get("images")
        if not isinstance(images, list):
            return None
        for img in images:
            if not isinstance(img, dict):
                continue
            thumbs = img.get("thumbnails")
            if isinstance(thumbs, dict):
                for k in ("1200", "large", "small"):
                    u = _safe_http_url(thumbs.get(k))
                    if u:
                        return u
            u = _safe_http_url(img.get("image"))
            if u:
                return u
        return None

    async def _from_spotify_page(self, title: str, artist: str, fetch_any: FetchAny) -> Optional[str]:
        q = " ".join(p for p in (artist.strip(), title.strip()) if p).strip()
        _, html = await fetch_any(f"https://open.spotify.com/search/{quote_plus(q)}")
        if not html:
            return None
        # Ищем обложки в JSON-гидратации страницы Spotify.
        m = re.search(r'https://i\.scdn\.co/image/[a-zA-Z0-9]+', html)
        if m:
            return _safe_http_url(m.group(0))
        return _html_first_url(r'"image_url":"(https:[^"]*scdn\.co[^"]+)"', html)

    async def _from_lastfm_page(self, title: str, artist: str, fetch_any: FetchAny) -> Optional[str]:
        q = " ".join(p for p in (artist.strip(), title.strip()) if p).strip()
        _, html = await fetch_any(f"https://www.last.fm/search/tracks?q={quote_plus(q)}")
        if not html:
            return None
        # Last.fm часто держит картинки в data-атрибутах/og:image.
        cand = _html_first_url(r'property="og:image"\s+content="([^"]+)"', html)
        if cand:
            return cand
        return _html_first_url(r'https://lastfm\.freetls\.fastly\.net/i/u/[0-9a-z]+/[^"]+\.(jpg|png)', html)

    async def _from_boom(self, title: str, artist: str, fetch_any: FetchAny) -> Optional[str]:
        q = " ".join(p for p in (artist.strip(), title.strip()) if p).strip()
        _, html = await fetch_any(f"https://boom.ru/search?text={quote_plus(q)}")
        if not html:
            return None
        cand = _html_first_url(r'"coverUrl":"(https:[^"]+)"', html)
        if cand:
            return cand.replace("\\/", "/")
        cand = _html_first_url(r'property="og:image"\s+content="([^"]+)"', html)
        if cand:
            return cand
        return _html_first_url(r'https://[^"]+boom[^"]+\.(jpg|jpeg|png)', html)

    async def _from_vk_mobile_web(self, title: str, artist: str, fetch_any: FetchAny) -> Optional[str]:
        q = " ".join(p for p in (artist.strip(), title.strip()) if p).strip()
        # Как в идее про Kate: пытаемся mobile web endpoint'ы (best-effort, часто требует сессию).
        _, html = await fetch_any(
            f"https://m.vk.com/search?c%5Bsection%5D=audio&c%5Bq%5D={quote_plus(q)}"
        )
        if not html:
            return None
        cand = _html_first_url(r'"cover_url":"(https:[^"]+)"', html)
        if cand:
            return cand.replace("\\/", "/")
        cand = _html_first_url(r'"coverUrl":"(https:[^"]+)"', html)
        if cand:
            return cand.replace("\\/", "/")
        cand = _html_first_url(r'https://sun\d+-\d+\.userapi\.com/[^"]+\.(jpg|jpeg|png)', html)
        if cand:
            return cand
        return _html_first_url(r'https://[^"]*userapi\.com[^"]+\.(jpg|jpeg|png)', html)

    async def _from_vk_web_music(self, title: str, artist: str, fetch_any: FetchAny) -> Optional[str]:
        q = " ".join(p for p in (artist.strip(), title.strip()) if p).strip()
        _, html = await fetch_any(f"https://vk.com/search?c%5Bsection%5D=audio&c%5Bq%5D={quote_plus(q)}")
        if not html:
            return None
        cand = _html_first_url(r'"cover_url":"(https:[^"]+)"', html)
        if cand:
            return cand.replace("\\/", "/")
        cand = _html_first_url(r'"coverUrl":"(https:[^"]+)"', html)
        if cand:
            return cand.replace("\\/", "/")
        cand = _html_first_url(r'https://sun\d+-\d+\.userapi\.com/[^"]+\.(jpg|jpeg|png)', html)
        if cand:
            return cand
        return _html_first_url(r'https://[^"]*userapi\.com[^"]+\.(jpg|jpeg|png)', html)

    async def resolve_one(self, title: str, artist: str, fetch_any: FetchAny) -> Optional[str]:
        if not self.config.enabled:
            return None
        key = _cache_key(title, artist)
        hit, cached = await self._cache_get(key)
        if hit:
            return cached

        chosen: Optional[str] = None
        chosen_score: int = -1

        for provider in self._provider_order():
            try:
                if provider == "itunes":
                    url = await self._from_itunes(title, artist, fetch_any)
                elif provider == "deezer":
                    url = await self._from_deezer(title, artist, fetch_any)
                elif provider == "spotify_api":
                    url = await self._from_spotify_api(title, artist, fetch_any)
                elif provider == "lastfm_api":
                    url = await self._from_lastfm_api(title, artist, fetch_any)
                elif provider == "coverartarchive":
                    url = await self._from_coverartarchive(title, artist, fetch_any)
                else:
                    url = None
            except Exception:
                self._metrics["provider_error"][provider] += 1
                continue

            if url and _is_placeholder_artwork(url):
                url = None

            if url:
                self._metrics["provider_hit"][provider] += 1
                # Prefer earlier providers (iTunes/Deezer) over later ones.
                # Position weight: first=100, second=90, third=80, etc.
                rank = self._provider_order().index(provider)
                pos_weight = 100 - rank * 10
                if pos_weight > chosen_score:
                    chosen = url
                    chosen_score = pos_weight
            else:
                self._metrics["provider_miss"][provider] += 1
                if not self.config.aggressive_mode:
                    continue

        # In aggressive mode we do NOT break on first hit; we prefer the best score.
        # When the chain finishes with non-None chosen, we cache and return it.
        await self._cache_set(key, chosen)
        return chosen

    async def enrich_tracks(self, tracks: List[TrackLike], fetch_any: FetchAny) -> int:
        if not self.config.enabled or self.config.max_per_request <= 0 or not tracks:
            return 0
        self._metrics["requests"] += 1
        candidates = [
            t
            for t in tracks
            if t.get("id")
            and not str(t.get("cover_url") or "").strip()
            and (str(t.get("title") or "").strip() or str(t.get("artist") or "").strip())
        ][: self.config.max_per_request]
        self._metrics["tracks_examined"] += len(candidates)
        changed = 0
        for t in candidates:
            u = await self.resolve_one(str(t.get("title") or ""), str(t.get("artist") or ""), fetch_any)
            if u:
                t["cover_url"] = u
                changed += 1
        self._metrics["tracks_enriched"] += changed
        return changed

    def metrics(self) -> Dict[str, Any]:
        return {
            "requests": int(self._metrics["requests"]),
            "tracks_examined": int(self._metrics["tracks_examined"]),
            "tracks_enriched": int(self._metrics["tracks_enriched"]),
            "cache_hit": int(self._metrics["cache_hit"]),
            "cache_miss": int(self._metrics["cache_miss"]),
            "cache_size": len(self._cache),
            "provider_hit": dict(self._metrics["provider_hit"]),
            "provider_miss": dict(self._metrics["provider_miss"]),
            "provider_error": dict(self._metrics["provider_error"]),
            "providers_enabled": self._provider_order(),
            "min_confidence": self.config.min_confidence,
            "aggressive_mode": self.config.aggressive_mode,
            "updated_at": int(time.time()),
        }

