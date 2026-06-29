"""
YouTube → MP3: гибридный pipe.

Архитектура:
1. yt-dlp | ffmpeg пишет в tmp-файл (shell pipe, как раньше).
2. Старт pipe быстрый — первый чанк mp3 появляется через ~1.5-2.5 сек.
3. StreamingResponse читает хвост файла, отправляя клиенту чанки по мере
   появления — плеер начинает играть сразу.
4. Range-запросы (seek): если запрошенный байт уже записан в файл —
   отдаём 206 Partial Content мгновенно. Если ещё не записан — ждём
   до таймаута, плеер переживёт.
5. Когда pipe завершился — файл финализируется и дальнейшие запросы
   (включая Range в любой точке) обслуживаются из статического файла.
"""

import os
import sys
import uuid
import asyncio
import logging
import re
import time
import shlex
import shutil
from typing import Dict, Any, Optional, List, Tuple

import yt_dlp
from ytmusicapi import YTMusic

logger = logging.getLogger(__name__)

_YT_MUSIC: Optional[YTMusic] = None

# Имена похожих исполнителей (get_artist → related): кэш на процесс, без лишних запросов к YTM.
_related_artists_cache: Dict[str, Tuple[float, List[str]]] = {}
_RELATED_ARTISTS_TTL_SEC = float(os.getenv("YTM_RELATED_ARTISTS_TTL_SEC", "3600"))
_RELATED_ARTISTS_CACHE_MAX = max(50, min(800, int(os.getenv("YTM_RELATED_ARTISTS_CACHE_MAX", "400"))))

_TEMP_DIR = "/tmp/music_downloads"
_TEMP_DIR_TG = "/tmp/music_tg"
_STREAM_CACHE_TTL = 1800  # 30 мин
_last_cleanup: float = 0

os.makedirs(_TEMP_DIR, exist_ok=True)
os.makedirs(_TEMP_DIR_TG, exist_ok=True)

# ─── Кеш полностью скачанных файлов ──────────────────────────────
_download_cache: Dict[str, str] = {}

# ─── In-flight: video_id -> PipeState ─────────────────────────────
# PipeState отслеживает ход выполнения pipe-задачи.
class PipeState:
    __slots__ = ("file_path", "done_future", "started_at", "last_size")
    def __init__(self, file_path: str):
        self.file_path = file_path
        self.done_future: asyncio.Future = asyncio.Future()
        self.started_at = time.time()
        self.last_size = 0

_in_flight: Dict[str, PipeState] = {}

# ─── Таймаут ожидания недостающих байт для Range ─────────────────
_RANGE_WAIT_TIMEOUT = 3.0  # сколько ждать появления нужного байта

# ─── Вспомогательное ────────────────────────────────────────────

_TITLE_CLEANUP_RE = re.compile(
    r"(?i)"
    r"\s*[-–|]\s*(?:official\s*(?:music\s*)?video|topic)"
    r"|\(.*?(?:official|video|audio|lyric|4k|hd).*?\)"
    r"|\[.*?(?:official|video|audio|lyric|4k|hd).*?\]"
)


def _cleanup_title(title: str) -> str:
    if not title:
        return "Unknown"
    title = _TITLE_CLEANUP_RE.sub("", title)
    title = re.sub(r"\s+", " ", title).strip(" -–\t")
    return title if title else "Unknown"


def _make_highres_cover(url: Optional[str], width: int = 512, height: int = 512) -> Optional[str]:
    if not url:
        return None
    url = re.sub(r"=w\d+-h\d+", f"=w{width}-h{height}", url)
    url = re.sub(r"-l90-rj-l90-rj", "-l90-rj", url)
    return url


def _get_ytm() -> YTMusic:
    global _YT_MUSIC
    if _YT_MUSIC is None:
        _YT_MUSIC = YTMusic()
    return _YT_MUSIC


# ─── ID из YouTube URL ────────────────────────────────────────

_YOUTUBE_ID_RE = re.compile(r'(?:v=|youtu\.be/|shorts/)([\w-]{11})')


def extract_video_id(url: str) -> Optional[str]:
    m = _YOUTUBE_ID_RE.search(url)
    return m.group(1) if m else None


def fetch_youtube_track_meta_sync(video_id: str) -> Optional[Dict[str, Any]]:
    """Метаданные ролика для API / карточки шеринга (без скачивания аудио)."""
    raw = (video_id or "").strip()
    vid = raw if re.match(r"^[\w-]{11}$", raw) else None
    if not vid:
        m = _YOUTUBE_ID_RE.search(raw)
        vid = m.group(1) if m else None
    if not vid:
        return None
    url = f"https://www.youtube.com/watch?v={vid}"
    ydl_opts: Dict[str, Any] = {
        "quiet": True,
        "no_warnings": True,
        "skip_download": True,
        "noplaylist": True,
        "ignoreerrors": True,
        "socket_timeout": 15,
        "retries": 1,
    }
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
        if not info:
            return None
        if info.get("entries"):
            entries = info.get("entries") or []
            info = entries[0] if entries else None
        if not info:
            return None
        title = _cleanup_title(str(info.get("title") or "Unknown"))
        artist = (
            info.get("artist")
            or info.get("channel")
            or info.get("uploader")
            or info.get("uploader_id")
            or "Unknown artist"
        )
        if isinstance(artist, list):
            artist = ", ".join(str(x) for x in artist)
        artist = str(artist).strip() or "Unknown artist"
        dur = info.get("duration")
        thumb = info.get("thumbnail")
        thumbs = info.get("thumbnails") or []
        if not thumb and thumbs and isinstance(thumbs[-1], dict):
            thumb = thumbs[-1].get("url")
        thumb = _make_highres_cover(str(thumb)) if thumb else None
        full_url = f"https://www.youtube.com/watch?v={vid}"
        return {
            "id": full_url,
            "title": title,
            "artist": artist,
            "duration": int(dur) if dur is not None else 0,
            "cover_url": thumb,
        }
    except Exception as e:
        logger.warning("fetch_youtube_track_meta_sync: %s", e)
        return None


# ─── Гибридный pipe: yt-dlp|ffmpeg пишет в файл ────────────────

async def _pipe_to_file(video_id: str, out_path: str) -> None:
    """
    Запускает yt-dlp | ffmpeg и пишет результат в out_path.
    Файл начинает расти сразу после получения первого пакета от ffmpeg
    (первые пару секунд). После завершения задачи файл полностью готов.
    """
    url = f"https://www.youtube.com/watch?v={video_id}"
    safe_url = shlex.quote(url)
    safe_path = shlex.quote(out_path)

    cmd = (
        f"{sys.executable} -m yt_dlp -f bestaudio/best -o - "
        f"--quiet --no-warnings --no-playlist {safe_url} "
        f"| ffmpeg -i pipe:0 -f mp3 -acodec libmp3lame "
        f"-ar 44100 -ac 2 -b:a 192k -map_metadata -1 "
        f"-loglevel error pipe:1 > {safe_path}"
    )

    proc = await asyncio.create_subprocess_shell(
        cmd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    ret = await proc.wait()

    if ret != 0 or not os.path.isfile(out_path) or os.path.getsize(out_path) == 0:
        # Удаляем мусор
        try:
            os.remove(out_path)
        except Exception:
            pass
        raise RuntimeError(f"yt-dlp download failed (exit code {ret})")


async def get_or_start_pipe(video_id: str) -> PipeState:
    """
    Центральная точка входа для стриминга и скачивания.

    Возвращает PipeState:
      - state.file_path        — путь к mp3-файлу (может быть недокачан)
      - state.done_future      — Future, которая выполнится когда файл полностью готов
      - state.started_at       — время старта конвертации
      - state.last_size        — последний известный размер

    Если видео уже полностью скачано — возвращает готовый PipeState
    с уже выполненным done_future.
    Если pipe уже запущен — возвращает существующий PipeState.
    """
    # 1. Проверяем кеш (полностью скачанный файл)
    cached = _download_cache.get(video_id)
    if cached and os.path.isfile(cached):
        # Возвращаем уже готовый PipeState с выполненной Future
        ps = PipeState(cached)
        ps.done_future.set_result(cached)
        ps.last_size = os.path.getsize(cached)
        return ps

    # 2. Проверяем на диске (файл есть, кеш памяти сброшен)
    fallback = os.path.join(_TEMP_DIR, f"{video_id}.mp3")
    if os.path.isfile(fallback):
        _download_cache[video_id] = fallback
        ps = PipeState(fallback)
        ps.done_future.set_result(fallback)
        ps.last_size = os.path.getsize(fallback)
        return ps

    # 3. Уже качается
    existing = _in_flight.get(video_id)
    if existing is not None:
        return existing

    # 4. Запускаем новый pipe
    tmp_path = os.path.join(_TEMP_DIR, f"{video_id}.{uuid.uuid4().hex[:8]}.mp3")
    state = PipeState(tmp_path)
    _in_flight[video_id] = state

    try:
        # Запускаем фоновую задачу — скачивание в файл
        asyncio.ensure_future(_do_pipe_and_finalize(video_id, tmp_path, state))
        return state
    except Exception:
        _in_flight.pop(video_id, None)
        raise


async def _do_pipe_and_finalize(video_id: str, tmp_path: str, state: PipeState) -> None:
    """Фоновая задача: pipe + финализация."""
    try:
        logger.info(f"[pipe] start {video_id} -> {tmp_path}")
        await _pipe_to_file(video_id, tmp_path)
        final_size = os.path.getsize(tmp_path)
        state.last_size = final_size

        # Переименовываем в финальное имя
        out_path = os.path.join(_TEMP_DIR, f"{video_id}.mp3")
        shutil.move(tmp_path, out_path)

        # Обновляем кеш
        _download_cache[video_id] = out_path
        state.file_path = out_path

        state.done_future.set_result(out_path)
        logger.info(f"[pipe] done {video_id} size={final_size}")
    except Exception as e:
        logger.error(f"[pipe] error {video_id}: {e}")
        if not state.done_future.done():
            state.done_future.set_exception(e)
        # Удаляем мусор
        try:
            os.remove(tmp_path)
        except Exception:
            pass
    finally:
        _in_flight.pop(video_id, None)


def get_cached_path(video_id: str) -> Optional[str]:
    """Вернуть путь если файл полностью скачан, иначе None."""
    cached = _download_cache.get(video_id)
    if cached and os.path.isfile(cached):
        return cached
    fallback = os.path.join(_TEMP_DIR, f"{video_id}.mp3")
    if os.path.isfile(fallback):
        _download_cache[video_id] = fallback
        return fallback
    return None


def is_downloading(video_id: str) -> bool:
    """Идёт ли процесс pipe прямо сейчас."""
    return video_id in _in_flight


def get_pipe_state(video_id: str) -> Optional[PipeState]:
    """Вернуть PipeState если pipe запущен."""
    return _in_flight.get(video_id)


async def wait_for_bytes(file_path: str, needed_byte: int, timeout: float = _RANGE_WAIT_TIMEOUT) -> bool:
    """
    Ждать, пока файл не вырастет до нужного размера (не дольше timeout).
    Возвращает True если файл достаточно большой, иначе False.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            size = os.path.getsize(file_path)
            if size > needed_byte:
                return True
        except OSError:
            pass
        await asyncio.sleep(0.1)
    # Последняя проверка
    try:
        return os.path.getsize(file_path) > needed_byte
    except OSError:
        return False


# ─── Полное скачивание для Telegram send_audio ─────────────────

async def download_youtube_audio(url: str) -> Dict[str, Any]:
    """
    Полное скачивание с конвертацией в MP3 320kbps + метаданные + обложка.
    Для отправки в Telegram (bot.send_audio).
    """
    temp_id = str(uuid.uuid4())
    out_path = os.path.join(_TEMP_DIR_TG, f"{temp_id}.mp3")

    ydl_opts: Dict[str, Any] = {
        "format": "bestaudio/best",
        "quiet": True,
        "nocheckcertificate": True,
        "outtmpl": os.path.join(_TEMP_DIR_TG, f"{temp_id}.%(ext)s"),
        "postprocessor_args": {
            "ffmpeg": [
                "-ar", "44100",
                "-ac", "2",
                "-write_id3v2", "1",
                "-id3v2_version", "3",
            ],
        },
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "320"},
            {"key": "FFmpegMetadata", "add_metadata": True},
        ],
    }

    def _run() -> Dict[str, Any]:
        import requests as req_lib
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=True)
            if "entries" in info:
                info = info["entries"][0]
            duration = info.get("duration", 0) or 0
            if duration > 1200:
                raise ValueError("Video too long (> 20 min)")
            title = _cleanup_title(info.get("title", ""))
            performer = info.get("artist") or info.get("uploader") or "Unknown"
            thumbnail_url = _make_highres_cover(info.get("thumbnail"))
            thumbnail_path = None
            if thumbnail_url:
                try:
                    resp = req_lib.get(thumbnail_url, timeout=10)
                    if resp.status_code == 200:
                        thumbnail_path = os.path.join(_TEMP_DIR_TG, f"{temp_id}_thumb.jpg")
                        with open(thumbnail_path, "wb") as f:
                            f.write(resp.content)
                except Exception as e:
                    logger.warning(f"Failed to download thumbnail: {e}")
            return {
                "file_path": out_path,
                "title": title,
                "performer": performer,
                "duration": duration,
                "thumbnail_path": thumbnail_path,
            }

    try:
        return await asyncio.to_thread(_run)
    except Exception as e:
        logger.error(f"yt-dlp download error: {e}")
        if os.path.exists(out_path):
            try:
                os.remove(out_path)
            except Exception:
                pass
        raise


# ─── TTL-очистка ──────────────────────────────────────────────

def _periodic_cleanup(force: bool = False):
    global _last_cleanup
    now = time.time()
    if not force and now - _last_cleanup < 60:
        return
    _last_cleanup = now
    for d in (_TEMP_DIR, _TEMP_DIR_TG):
        if not os.path.isdir(d):
            continue
        for fname in os.listdir(d):
            fpath = os.path.join(d, fname)
            try:
                if os.path.isfile(fpath) and now - os.path.getmtime(fpath) > _STREAM_CACHE_TTL:
                    os.remove(fpath)
                    vid = fname.replace(".mp3", "")
                    _download_cache.pop(vid, None)
            except Exception:
                pass


# ─── Поиск через YouTube Music ────────────────────────────────

def search_youtube_tracks(query: str, limit: int = 10) -> List[Dict[str, Any]]:
    search_q = query.strip()
    if not search_q:
        return []

    results: List[Dict[str, Any]] = []
    try:
        ytm = _get_ytm()
        raw = ytm.search(search_q, filter="songs", limit=limit)
        if not raw:
            return results

        for item in raw:
            if not isinstance(item, dict):
                continue
            video_id = item.get("videoId")
            if not video_id:
                continue
            video_url = f"https://www.youtube.com/watch?v={video_id}"
            title = item.get("title", "Unknown")
            artists_data = item.get("artists") or []
            artist = ", ".join(
                a.get("name", "") for a in artists_data if isinstance(a, dict)
            ) or item.get("artist", {}).get("name", "Unknown")
            thumbnails = item.get("thumbnails") or []
            cover_url = None
            if thumbnails:
                smallest_url = thumbnails[-1].get("url") if isinstance(thumbnails[-1], dict) else None
                cover_url = _make_highres_cover(smallest_url)
            duration_sec = None
            duration_raw = item.get("duration")
            if duration_raw and isinstance(duration_raw, str) and ":" in duration_raw:
                parts = duration_raw.split(":")
                if len(parts) == 2:
                    duration_sec = int(parts[0]) * 60 + int(parts[1])
                elif len(parts) == 3:
                    duration_sec = int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
            if duration_sec and duration_sec > 600:
                continue

            results.append({
                "id": video_url,
                "title": title,
                "artist": artist,
                "duration": duration_sec or 0,
                "cover_url": cover_url,
            })
    except Exception as e:
        logger.error(f"ytmusicapi search error: {e}")

    return results


def _ytm_duration_from_watch_item(item: Dict[str, Any]) -> int:
    duration_raw = item.get("length") or item.get("duration")
    if not duration_raw or not isinstance(duration_raw, str) or ":" not in duration_raw:
        return 0
    parts = duration_raw.split(":")
    try:
        if len(parts) == 2:
            return int(parts[0]) * 60 + int(parts[1])
        if len(parts) == 3:
            return int(parts[0]) * 3600 + int(parts[1]) * 60 + int(parts[2])
    except (TypeError, ValueError):
        return 0
    return 0


def youtube_related_artist_names(seed_artist: str, max_names: int = 14) -> List[str]:
    """
    Исполнители из блока «Похожие» на странице артиста YTM (search artists → get_artist.related).
    Используется в персональных реках вместо поиска только по имени одного артиста (иначе одни и те же хиты).
    """
    qa = (seed_artist or "").strip()
    if len(qa) < 2:
        return []
    lim = max(4, min(24, int(max_names)))
    cache_key = qa.lower()[:120]
    now = time.time()
    ent = _related_artists_cache.get(cache_key)
    if ent and now - ent[0] < _RELATED_ARTISTS_TTL_SEC:
        return list(ent[1])[:lim]
    out: List[str] = []
    try:
        ytm = _get_ytm()
        hits = ytm.search(qa, filter="artists", limit=10) or []
        best_bid: Optional[str] = None
        qa_norm = re.sub(r"\s+", " ", qa.lower()).strip()
        for hit in hits:
            if not isinstance(hit, dict) or hit.get("resultType") != "artist":
                continue
            bid = hit.get("browseId")
            name = str(hit.get("artist") or hit.get("title") or "").strip()
            if not bid or not name:
                continue
            nk = name.lower()
            if nk == qa_norm or qa_norm in nk or nk in qa_norm:
                best_bid = bid
                break
        if not best_bid and hits:
            h0 = hits[0]
            if isinstance(h0, dict) and h0.get("resultType") == "artist" and h0.get("browseId"):
                best_bid = str(h0["browseId"])
        if not best_bid:
            _related_artists_cache[cache_key] = (now, [])
            return []
        page = ytm.get_artist(best_bid)
        rel = page.get("related") if isinstance(page, dict) else None
        rows: List[Any] = []
        if isinstance(rel, dict):
            rows = list(rel.get("results") or [])
        seen: set[str] = set()
        for row in rows:
            if not isinstance(row, dict):
                continue
            tit = str(row.get("title") or "").strip()
            if len(tit) < 2:
                continue
            k = tit.lower()
            if k in seen or k == qa_norm:
                continue
            seen.add(k)
            out.append(tit)
            if len(out) >= lim:
                break
    except Exception as e:
        logger.error(f"ytmusicapi related artists error for {qa[:48]!r}: {e}")
    _related_artists_cache[cache_key] = (now, list(out))
    if len(_related_artists_cache) > _RELATED_ARTISTS_CACHE_MAX:
        drop = sorted(_related_artists_cache.items(), key=lambda kv: kv[1][0])[: len(_related_artists_cache) // 4]
        for k, _ in drop:
            _related_artists_cache.pop(k, None)
    return out


def youtube_radio_tracks_from_video_id(video_id: str, limit: int = 50) -> List[Dict[str, Any]]:
    """
    «Радио» YouTube Music: get_watch_playlist(..., radio=True) — похожие треки по одному videoId.
    Формат как у search_youtube_tracks (id = watch URL) для плеера/resolve.
    """
    raw_in = (video_id or "").strip()
    vid = raw_in if re.match(r"^[\w-]{11}$", raw_in) else (extract_video_id(raw_in) or "")
    if not vid:
        return []
    lim = max(20, min(100, int(limit)))
    out: List[Dict[str, Any]] = []
    try:
        ytm = _get_ytm()
        for radio_mode in (True, False):
            raw = ytm.get_watch_playlist(videoId=vid, radio=radio_mode, limit=lim)
            tracks = raw.get("tracks") or []
            for item in tracks:
                if not isinstance(item, dict):
                    continue
                v = item.get("videoId")
                if not v or not re.match(r"^[\w-]{11}$", str(v)):
                    continue
                title = str(item.get("title") or "Unknown").strip() or "Unknown"
                artists_data = item.get("artists") or []
                artist = ", ".join(
                    a.get("name", "") for a in artists_data if isinstance(a, dict)
                ).strip() or "Unknown"
                thumbs = item.get("thumbnail") or item.get("thumbnails") or []
                cover_url = None
                if isinstance(thumbs, list) and thumbs:
                    last = thumbs[-1] if isinstance(thumbs[-1], dict) else None
                    if last and last.get("url"):
                        cover_url = _make_highres_cover(last.get("url"))
                elif isinstance(thumbs, dict) and thumbs.get("url"):
                    cover_url = _make_highres_cover(thumbs.get("url"))
                dur = _ytm_duration_from_watch_item(item)
                if dur > 600:
                    continue
                out.append(
                    {
                        "id": f"https://www.youtube.com/watch?v={v}",
                        "title": title,
                        "artist": artist,
                        "duration": dur,
                        "cover_url": cover_url,
                    }
                )
            if len(out) >= min(12, lim // 2):
                break
    except Exception as e:
        logger.error(f"ytmusicapi radio/watch error: {e}")
    return out