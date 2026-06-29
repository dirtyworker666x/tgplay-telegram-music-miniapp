"""
Аналитика: SQLite‑хранилище событий и агрегатов.

Важно:
- Всё время храним в UTC как unix‑timestamp (целое число секунд).
- Сырые события лежат в events_* таблицах, суточные и месячные агрегаты — в *_aggregates.
- Старые таблицы events / user_sessions остаются для обратной совместимости
  и могут использоваться миграционным скриптом.
"""
from __future__ import annotations
import json
import sqlite3
import threading
import time
from datetime import datetime, timezone, timedelta
import os
from pathlib import Path
from typing import AbstractSet, Any, Dict, List, Optional, Tuple

# Для тестов и sanity-скриптов можно переопределить через env: ANALYTICS_DB=/path/to/db
DB_PATH = Path(os.environ.get("ANALYTICS_DB", "") or str(Path(__file__).parent / "analytics.db"))

# init_db() гоняет большой executescript: раньше вызывался с десятков мест на каждый запрос —
# синхронный SQLite в asyncio-блоке глушил весь процесс (таймауты в т.ч. к /api/telegram-webhook).
_init_db_lock = threading.Lock()
_init_db_completed_for: Optional[Path] = None


def _get_conn():
    return sqlite3.connect(str(DB_PATH), timeout=10)


def init_db() -> None:
    global _init_db_completed_for
    target = DB_PATH
    if _init_db_completed_for == target:
        return
    with _init_db_lock:
        if _init_db_completed_for == target:
            return
        _init_db_impl()
        _init_db_completed_for = target


def _init_db_impl() -> None:
    """
    Инициализация схемы analytics.db.

    Схема нового поколения:
    - events_user_activity      — общие действия пользователя (экраны, high‑level события)
    - events_button_clicks      — клики по конкретным кнопкам
    - events_errors             — ошибки (VK, Telegram, фронтенд и пр.)
    - events_captcha            — капчи, кулдауны и баны токенов
    - events_track_usage        — прослушивания и скачивания треков
    - events_playlists          — операции с плейлистами
    - daily_aggregates          — агрегаты по дням (UTC)
    - monthly_aggregates        — агрегаты по месяцам (UTC)

    Плюс оставляем старые таблицы events / user_sessions как есть,
    чтобы не ломать уже записанные данные и дать возможность миграции.
    """

    conn = _get_conn()
    try:
        conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            PRAGMA foreign_keys=OFF;

            -- ─── Новые события: пользователи / кнопки / ошибки / капчи / треки / плейлисты ─

            CREATE TABLE IF NOT EXISTS events_user_activity (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_utc          INTEGER NOT NULL,           -- unix timestamp (UTC)
                telegram_user_id INTEGER,
                username        TEXT,
                country_code    TEXT,
                city_region     TEXT,
                event_type      TEXT NOT NULL,              -- open_app, open_profile, etc.
                event_source    TEXT,                       -- miniapp / bot
                extra_json      TEXT                        -- JSON с деталями
            );
            CREATE INDEX IF NOT EXISTS idx_eua_ts      ON events_user_activity(ts_utc);
            CREATE INDEX IF NOT EXISTS idx_eua_user    ON events_user_activity(telegram_user_id);
            CREATE INDEX IF NOT EXISTS idx_eua_type    ON events_user_activity(event_type);

            CREATE TABLE IF NOT EXISTS events_button_clicks (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_utc          INTEGER NOT NULL,
                telegram_user_id INTEGER,
                username        TEXT,
                button_id       TEXT NOT NULL,              -- логическое имя кнопки
                context         TEXT,                       -- экран / раздел
                extra_json      TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_ebc_ts      ON events_button_clicks(ts_utc);
            CREATE INDEX IF NOT EXISTS idx_ebc_button  ON events_button_clicks(button_id);

            CREATE TABLE IF NOT EXISTS events_errors (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_utc          INTEGER NOT NULL,
                telegram_user_id INTEGER,
                username        TEXT,
                error_key       TEXT NOT NULL,              -- vk_captcha, vk_rate_limit, tg_timeout, ...
                message         TEXT,
                stack           TEXT,
                country_code    TEXT,
                city_region     TEXT,
                extra_json      TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_err_ts      ON events_errors(ts_utc);
            CREATE INDEX IF NOT EXISTS idx_err_key     ON events_errors(error_key);

            CREATE TABLE IF NOT EXISTS events_captcha (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_utc          INTEGER NOT NULL,
                token_id        TEXT NOT NULL,              -- hash токена, не сам токен
                event_type      TEXT NOT NULL,              -- captcha_shown, cooldown_start, cooldown_end, token_banned
                cooldown_seconds INTEGER,
                rucaptcha_used  INTEGER DEFAULT 0,          -- 0/1
                result          TEXT,                       -- success / fail / None
                extra_json      TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_cpt_ts      ON events_captcha(ts_utc);
            CREATE INDEX IF NOT EXISTS idx_cpt_token   ON events_captcha(token_id);

            CREATE TABLE IF NOT EXISTS events_track_usage (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_utc          INTEGER NOT NULL,
                telegram_user_id INTEGER,
                username        TEXT,
                track_id        TEXT NOT NULL,
                action          TEXT NOT NULL,              -- play, complete, download_to_bot
                duration_sec    REAL,                       -- фактически прослушанное
                from_cache      INTEGER DEFAULT 0,          -- 0/1
                region          TEXT,
                extra_json      TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_etu_ts      ON events_track_usage(ts_utc);
            CREATE INDEX IF NOT EXISTS idx_etu_track   ON events_track_usage(track_id);
            CREATE INDEX IF NOT EXISTS idx_etu_action  ON events_track_usage(action);

            CREATE TABLE IF NOT EXISTS events_playlists (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ts_utc          INTEGER NOT NULL,
                telegram_user_id INTEGER,
                username        TEXT,
                playlist_id     TEXT,
                action          TEXT NOT NULL,              -- create, rename, delete, add_track, remove_track
                extra_json      TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_epl_ts      ON events_playlists(ts_utc);
            CREATE INDEX IF NOT EXISTS idx_epl_action  ON events_playlists(action);

            -- ─── Агрегаты по дням / месяцам (UTC) ─────────────────────────────────────

            CREATE TABLE IF NOT EXISTS daily_aggregates (
                date_utc            TEXT PRIMARY KEY,       -- YYYY-MM-DD (UTC)
                users_active_24h    INTEGER NOT NULL DEFAULT 0,
                users_new_24h       INTEGER NOT NULL DEFAULT 0,
                plays_24h           INTEGER NOT NULL DEFAULT 0,
                downloads_24h       INTEGER NOT NULL DEFAULT 0,
                playlists_created_24h INTEGER NOT NULL DEFAULT 0,
                errors_total_24h    INTEGER NOT NULL DEFAULT 0,
                errors_by_key_json  TEXT,                   -- JSON: {error_key: count}
                captcha_stats_json  TEXT,                   -- JSON: агрегаты по капчам/кулдаунам
                button_clicks_json  TEXT,                   -- JSON: {button_id: count}
                retention_24h       REAL                    -- D1 retention для этого дня (%)
            );

            CREATE TABLE IF NOT EXISTS monthly_aggregates (
                month_utc           TEXT PRIMARY KEY,       -- YYYY-MM (UTC)
                users_active_month  INTEGER NOT NULL DEFAULT 0,
                plays_month         INTEGER NOT NULL DEFAULT 0,
                downloads_month     INTEGER NOT NULL DEFAULT 0,
                playlists_created_month INTEGER NOT NULL DEFAULT 0,
                errors_by_key_json  TEXT,
                captcha_stats_json  TEXT,
                retention_metrics_json TEXT                 -- расширенные метрики удержания
            );

            -- ─── Старые таблицы, сохраняем без изменений для совместимости ────────────

            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts REAL NOT NULL,
                event_type TEXT NOT NULL,
                payload TEXT,
                user_hash TEXT,
                session_id TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
            CREATE INDEX IF NOT EXISTS idx_events_type ON events(event_type);
            CREATE INDEX IF NOT EXISTS idx_events_user ON events(user_hash);
            CREATE INDEX IF NOT EXISTS idx_events_session ON events(session_id);

            CREATE TABLE IF NOT EXISTS user_sessions (
                user_hash TEXT PRIMARY KEY,
                first_seen_ts REAL NOT NULL,
                last_seen_ts REAL NOT NULL
            );

            -- Кто писал боту (/start, /playlist и т.д.) — для рассылок «всем, кто запускал бота»
            CREATE TABLE IF NOT EXISTS bot_subscribers (
                telegram_user_id INTEGER PRIMARY KEY NOT NULL,
                username            TEXT,
                first_contact_ts    INTEGER NOT NULL,
                last_contact_ts     INTEGER NOT NULL,
                last_source         TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_bot_sub_last ON bot_subscribers(last_contact_ts);
            """
        )
        conn.commit()
        _ensure_track_usage_columns(conn)
        _ensure_bot_subscribers_private_chat_column(conn)
        _ensure_user_dislikes_table(conn)
        _ensure_user_dislike_signals_tables(conn)
        _ensure_user_rec_show_penalty_tables(conn)
        _migrate_legacy_dislikes_to_rec_penalties(conn)
        _ensure_user_library_tracks_table(conn)
        _ensure_user_bot_audio_delivered_table(conn)
    finally:
        conn.close()


# Персональные рекомендации: «видимость» артиста/жанра 0–100 (шаг 20 за дизлайк/лайк в избранное).
REC_SHOW_PENALTY_STEP = 20
REC_SHOW_PENALTY_MAX = 100


def _ensure_user_rec_show_penalty_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_rec_artist_show_penalty (
            telegram_user_id INTEGER NOT NULL,
            artist_key TEXT NOT NULL,
            penalty INTEGER NOT NULL,
            ts_utc INTEGER NOT NULL,
            PRIMARY KEY (telegram_user_id, artist_key)
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_rec_genre_show_penalty (
            telegram_user_id INTEGER NOT NULL,
            genre_id INTEGER NOT NULL,
            penalty INTEGER NOT NULL,
            ts_utc INTEGER NOT NULL,
            PRIMARY KEY (telegram_user_id, genre_id)
        );
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_rec_sp_a_user ON user_rec_artist_show_penalty(telegram_user_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_rec_sp_g_user ON user_rec_genre_show_penalty(telegram_user_id)"
    )
    conn.commit()


def _migrate_legacy_dislikes_to_rec_penalties(conn: sqlite3.Connection) -> None:
    """Одноразово: старые user_disliked_* → штраф 100 (как раньше полный отсев)."""
    _ensure_user_rec_show_penalty_tables(conn)
    try:
        conn.execute(
            """
            INSERT INTO user_rec_artist_show_penalty (telegram_user_id, artist_key, penalty, ts_utc)
            SELECT a.telegram_user_id, a.artist_key, ?, a.ts_utc
            FROM user_disliked_artists a
            WHERE NOT EXISTS (
                SELECT 1 FROM user_rec_artist_show_penalty p
                WHERE p.telegram_user_id = a.telegram_user_id AND p.artist_key = a.artist_key
            )
            """,
            (REC_SHOW_PENALTY_MAX,),
        )
        conn.execute(
            """
            INSERT INTO user_rec_genre_show_penalty (telegram_user_id, genre_id, penalty, ts_utc)
            SELECT g.telegram_user_id, g.genre_id, ?, g.ts_utc
            FROM user_disliked_genres g
            WHERE NOT EXISTS (
                SELECT 1 FROM user_rec_genre_show_penalty p
                WHERE p.telegram_user_id = g.telegram_user_id AND p.genre_id = g.genre_id
            )
            """,
            (REC_SHOW_PENALTY_MAX,),
        )
        conn.commit()
    except Exception:
        conn.rollback()


def _bump_rec_artist_penalty_conn(
    conn: sqlite3.Connection, telegram_user_id: int, artist_key: str, delta: int, ts: int
) -> None:
    ak = (artist_key or "").strip()[:200]
    if not ak:
        return
    cur = conn.execute(
        """
        SELECT penalty FROM user_rec_artist_show_penalty
        WHERE telegram_user_id = ? AND artist_key = ?
        """,
        (int(telegram_user_id), ak),
    )
    row = cur.fetchone()
    cur_pen = int(row[0]) if row and row[0] is not None else 0
    new_pen = max(0, min(REC_SHOW_PENALTY_MAX, cur_pen + int(delta)))
    if new_pen <= 0:
        conn.execute(
            """
            DELETE FROM user_rec_artist_show_penalty
            WHERE telegram_user_id = ? AND artist_key = ?
            """,
            (int(telegram_user_id), ak),
        )
    else:
        conn.execute(
            """
            INSERT OR REPLACE INTO user_rec_artist_show_penalty
            (telegram_user_id, artist_key, penalty, ts_utc) VALUES (?, ?, ?, ?)
            """,
            (int(telegram_user_id), ak, new_pen, ts),
        )


def _bump_rec_genre_penalty_conn(
    conn: sqlite3.Connection, telegram_user_id: int, genre_id: int, delta: int, ts: int
) -> None:
    try:
        gid = int(genre_id)
    except (TypeError, ValueError):
        return
    cur = conn.execute(
        """
        SELECT penalty FROM user_rec_genre_show_penalty
        WHERE telegram_user_id = ? AND genre_id = ?
        """,
        (int(telegram_user_id), gid),
    )
    row = cur.fetchone()
    cur_pen = int(row[0]) if row and row[0] is not None else 0
    new_pen = max(0, min(REC_SHOW_PENALTY_MAX, cur_pen + int(delta)))
    if new_pen <= 0:
        conn.execute(
            """
            DELETE FROM user_rec_genre_show_penalty
            WHERE telegram_user_id = ? AND genre_id = ?
            """,
            (int(telegram_user_id), gid),
        )
    else:
        conn.execute(
            """
            INSERT OR REPLACE INTO user_rec_genre_show_penalty
            (telegram_user_id, genre_id, penalty, ts_utc) VALUES (?, ?, ?, ?)
            """,
            (int(telegram_user_id), gid, new_pen, ts),
        )


def get_rec_artist_show_penalties(telegram_user_id: int, *, limit: int = 400) -> Dict[str, int]:
    init_db()
    cap = max(1, min(int(limit), 800))
    conn = _get_conn()
    try:
        _ensure_user_rec_show_penalty_tables(conn)
        cur = conn.execute(
            """
            SELECT artist_key, penalty FROM user_rec_artist_show_penalty
            WHERE telegram_user_id = ?
            ORDER BY ts_utc DESC
            LIMIT ?
            """,
            (int(telegram_user_id), cap),
        )
        out: Dict[str, int] = {}
        for ak_raw, pen_raw in cur.fetchall():
            ak = str(ak_raw or "").strip()
            if not ak:
                continue
            try:
                out[ak] = int(pen_raw)
            except (TypeError, ValueError):
                continue
        return out
    finally:
        conn.close()


def get_rec_genre_show_penalties(telegram_user_id: int, *, limit: int = 80) -> Dict[int, int]:
    init_db()
    cap = max(1, min(int(limit), 200))
    conn = _get_conn()
    try:
        _ensure_user_rec_show_penalty_tables(conn)
        cur = conn.execute(
            """
            SELECT genre_id, penalty FROM user_rec_genre_show_penalty
            WHERE telegram_user_id = ?
            ORDER BY ts_utc DESC
            LIMIT ?
            """,
            (int(telegram_user_id), cap),
        )
        out: Dict[int, int] = {}
        for gid_raw, pen_raw in cur.fetchall():
            try:
                gid = int(gid_raw)
            except (TypeError, ValueError):
                continue
            try:
                out[gid] = int(pen_raw)
            except (TypeError, ValueError):
                continue
        return out
    finally:
        conn.close()


def bump_rec_penalties_on_favorite(
    telegram_user_id: int,
    *,
    artist_key: Optional[str] = None,
    genre_id: Optional[int] = None,
) -> None:
    """Лайк в избранное: −шаг к штрафу показа артиста/жанра в рекомендациях."""
    ak = (artist_key or "").strip()[:200]
    ts = _now_utc_ts()
    init_db()
    conn = _get_conn()
    try:
        _ensure_user_rec_show_penalty_tables(conn)
        if ak:
            _bump_rec_artist_penalty_conn(conn, telegram_user_id, ak, -REC_SHOW_PENALTY_STEP, ts)
        if genre_id is not None:
            try:
                gid = int(genre_id)
            except (TypeError, ValueError):
                gid = None
            if gid is not None:
                _bump_rec_genre_penalty_conn(conn, telegram_user_id, gid, -REC_SHOW_PENALTY_STEP, ts)
        conn.commit()
    finally:
        conn.close()


def _ensure_user_dislike_signals_tables(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_disliked_artists (
            telegram_user_id INTEGER NOT NULL,
            artist_key TEXT NOT NULL,
            ts_utc INTEGER NOT NULL,
            PRIMARY KEY (telegram_user_id, artist_key)
        );
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_disliked_genres (
            telegram_user_id INTEGER NOT NULL,
            genre_id INTEGER NOT NULL,
            ts_utc INTEGER NOT NULL,
            PRIMARY KEY (telegram_user_id, genre_id)
        );
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_uda_user ON user_disliked_artists(telegram_user_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_udg_user ON user_disliked_genres(telegram_user_id)"
    )
    conn.commit()


def _ensure_user_dislikes_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_disliked_tracks (
            telegram_user_id INTEGER NOT NULL,
            track_id TEXT NOT NULL,
            ts_utc INTEGER NOT NULL,
            PRIMARY KEY (telegram_user_id, track_id)
        );
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_user_dislikes_user ON user_disliked_tracks(telegram_user_id)"
    )
    conn.commit()


def record_track_dislike(
    telegram_user_id: int,
    track_id: str,
    *,
    artist_key: Optional[str] = None,
    genre_id: Optional[int] = None,
) -> None:
    """Дизлайк: трек в список исключений; артист/жанр — +шаг к штрафу показа (0–100) в персональной ленте."""
    tid = (track_id or "").strip()[:96]
    if not tid:
        return
    ts = _now_utc_ts()
    ak = (artist_key or "").strip()[:200]
    init_db()
    conn = _get_conn()
    try:
        _ensure_user_dislikes_table(conn)
        _ensure_user_dislike_signals_tables(conn)
        _ensure_user_rec_show_penalty_tables(conn)
        conn.execute(
            """
            INSERT OR REPLACE INTO user_disliked_tracks (telegram_user_id, track_id, ts_utc)
            VALUES (?, ?, ?)
            """,
            (int(telegram_user_id), tid, ts),
        )
        if ak:
            _bump_rec_artist_penalty_conn(conn, int(telegram_user_id), ak, REC_SHOW_PENALTY_STEP, ts)
        if genre_id is not None:
            try:
                gid = int(genre_id)
            except (TypeError, ValueError):
                gid = None
            if gid is not None:
                _bump_rec_genre_penalty_conn(conn, int(telegram_user_id), gid, REC_SHOW_PENALTY_STEP, ts)
        conn.commit()
    finally:
        conn.close()


def get_disliked_track_ids(telegram_user_id: int, *, limit: int = 800) -> List[str]:
    init_db()
    cap = max(1, min(int(limit), 2000))
    conn = _get_conn()
    try:
        _ensure_user_dislikes_table(conn)
        cur = conn.execute(
            """
            SELECT track_id FROM user_disliked_tracks
            WHERE telegram_user_id = ?
            ORDER BY ts_utc DESC
            LIMIT ?
            """,
            (int(telegram_user_id), cap),
        )
        return [str(r[0]).strip() for r in cur.fetchall() if r[0]]
    finally:
        conn.close()


def _ensure_removed_library_tracks_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_removed_library_tracks (
            telegram_user_id INTEGER NOT NULL,
            track_id TEXT NOT NULL,
            ts_utc INTEGER NOT NULL,
            PRIMARY KEY (telegram_user_id, track_id)
        );
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_urlt_user ON user_removed_library_tracks(telegram_user_id)"
    )
    conn.commit()


def record_removed_library_track_ids(telegram_user_id: int, track_ids: List[str]) -> None:
    """Трек убран из избранного — не использовать как seed и не подбирать «похожие» по нему."""
    if not track_ids:
        return
    ts = _now_utc_ts()
    init_db()
    conn = _get_conn()
    try:
        _ensure_removed_library_tracks_table(conn)
        for raw in track_ids:
            tid = (raw or "").strip()[:96]
            if not tid:
                continue
            conn.execute(
                """
                INSERT OR REPLACE INTO user_removed_library_tracks (telegram_user_id, track_id, ts_utc)
                VALUES (?, ?, ?)
                """,
                (int(telegram_user_id), tid, ts),
            )
        conn.commit()
    finally:
        conn.close()


def get_removed_library_track_ids(telegram_user_id: int, *, limit: int = 2500) -> List[str]:
    init_db()
    cap = max(1, min(int(limit), 5000))
    conn = _get_conn()
    try:
        _ensure_removed_library_tracks_table(conn)
        cur = conn.execute(
            """
            SELECT track_id FROM user_removed_library_tracks
            WHERE telegram_user_id = ?
            ORDER BY ts_utc DESC
            LIMIT ?
            """,
            (int(telegram_user_id), cap),
        )
        return [str(r[0]).strip() for r in cur.fetchall() if r[0]]
    finally:
        conn.close()


def get_disliked_artist_keys(telegram_user_id: int, *, limit: int = 400) -> List[str]:
    """Артисты с максимальным штрафом показа (редко нужно; рекомендации читают get_rec_artist_show_penalties)."""
    init_db()
    cap = max(1, min(int(limit), 800))
    conn = _get_conn()
    try:
        _ensure_user_rec_show_penalty_tables(conn)
        cur = conn.execute(
            """
            SELECT artist_key FROM user_rec_artist_show_penalty
            WHERE telegram_user_id = ? AND penalty >= ?
            ORDER BY ts_utc DESC
            LIMIT ?
            """,
            (int(telegram_user_id), REC_SHOW_PENALTY_MAX, cap),
        )
        return [str(r[0]).strip() for r in cur.fetchall() if r[0]]
    finally:
        conn.close()


def get_disliked_genre_ids(telegram_user_id: int, *, limit: int = 80) -> List[int]:
    """Жанры с максимальным штрафом показа."""
    init_db()
    cap = max(1, min(int(limit), 200))
    conn = _get_conn()
    try:
        _ensure_user_rec_show_penalty_tables(conn)
        cur = conn.execute(
            """
            SELECT genre_id FROM user_rec_genre_show_penalty
            WHERE telegram_user_id = ? AND penalty >= ?
            ORDER BY ts_utc DESC
            LIMIT ?
            """,
            (int(telegram_user_id), REC_SHOW_PENALTY_MAX, cap),
        )
        out: List[int] = []
        for (raw,) in cur.fetchall():
            try:
                out.append(int(raw))
            except (TypeError, ValueError):
                continue
        return out
    finally:
        conn.close()


def _table_has_column(conn: sqlite3.Connection, table: str, col: str) -> bool:
    try:
        cur = conn.execute(f"PRAGMA table_info({table})")
        return any((row[1] == col) for row in cur.fetchall())
    except Exception:
        return False


def _ensure_track_usage_columns(conn: sqlite3.Connection) -> None:
    """
    Мягкая миграция: добавляем колонки метаданных трека в events_track_usage.
    Нужны для обучения рекомендаций по plays без внешних запросов.
    """
    if not _table_has_column(conn, "events_track_usage", "genre_id"):
        conn.execute("ALTER TABLE events_track_usage ADD COLUMN genre_id INTEGER")
    if not _table_has_column(conn, "events_track_usage", "release_year"):
        conn.execute("ALTER TABLE events_track_usage ADD COLUMN release_year INTEGER")
    if not _table_has_column(conn, "events_track_usage", "lang_bucket"):
        conn.execute("ALTER TABLE events_track_usage ADD COLUMN lang_bucket TEXT")
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_etu_genre ON events_track_usage(genre_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_etu_year  ON events_track_usage(release_year)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_etu_lang  ON events_track_usage(lang_bucket)")
    except Exception:
        pass
    conn.commit()


def _ensure_bot_subscribers_private_chat_column(conn: sqlite3.Connection) -> None:
    """Флаг: пользователь открыл приватный чат с ботом (нужно для рассылки в ЛС)."""
    if not _table_has_column(conn, "bot_subscribers", "private_chat_ok"):
        conn.execute("ALTER TABLE bot_subscribers ADD COLUMN private_chat_ok INTEGER NOT NULL DEFAULT 0")
    try:
        conn.execute("CREATE INDEX IF NOT EXISTS idx_bot_sub_private ON bot_subscribers(private_chat_ok)")
    except Exception:
        pass
    conn.commit()


def mark_bot_private_chat_open(telegram_user_id: int) -> None:
    """
    Пользователь имеет приватный диалог с ботом (например /start, сообщение в ЛС).
    Отличается от записи в bot_subscribers только по API мини‑аппа: без этого Bot API не шлёт ЛС.
    """
    init_db()
    uid = int(telegram_user_id)
    ts = _now_utc_ts()
    conn = _get_conn()
    try:
        cur = conn.execute("SELECT 1 FROM bot_subscribers WHERE telegram_user_id = ?", (uid,))
        if cur.fetchone():
            conn.execute(
                """
                UPDATE bot_subscribers
                SET private_chat_ok = 1, last_contact_ts = ?, last_source = ?
                WHERE telegram_user_id = ?
                """,
                (ts, "private_chat", uid),
            )
        else:
            conn.execute(
                """
                INSERT INTO bot_subscribers (
                    telegram_user_id, username, first_contact_ts, last_contact_ts, last_source, private_chat_ok
                )
                VALUES (?, NULL, ?, ?, ?, 1)
                """,
                (uid, ts, ts, "private_chat"),
            )
        conn.commit()
    finally:
        conn.close()


def get_user_taste_aggregates(
    telegram_user_id: int,
    *,
    days: int = 180,
    only_track_ids: Optional[AbstractSet[str]] = None,
) -> Dict[str, Any]:
    """
    Агрегаты вкуса пользователя из events_track_usage (play/complete) по уже сохранённым метаданным.
    Возвращает словарь:
    - genre: {genre_id: count}
    - lang: {lang_bucket: count}
    - year: {release_year: count}

    only_track_ids: если задан (не None), учитываются только события по этим track_id
    (например только треки из текущего избранного — удалённые из плейлиста не влияют).
    Пустой набор → пустые агрегаты.
    """
    init_db()
    cutoff = _now_utc_ts() - int(days) * 86400
    conn = _get_conn()
    try:
        _ensure_track_usage_columns(conn)
        has_genre = _table_has_column(conn, "events_track_usage", "genre_id")
        has_year = _table_has_column(conn, "events_track_usage", "release_year")
        has_lang = _table_has_column(conn, "events_track_usage", "lang_bucket")
        if not (has_genre or has_year or has_lang):
            return {"genre": {}, "lang": {}, "year": {}}

        if only_track_ids is not None and len(only_track_ids) == 0:
            return {"genre": {}, "lang": {}, "year": {}}

        track_filter = ""
        track_params: Tuple[Any, ...] = ()
        if only_track_ids is not None:
            ids = sorted({str(x).strip() for x in only_track_ids if str(x).strip()})[:500]
            if not ids:
                return {"genre": {}, "lang": {}, "year": {}}
            ph = ",".join("?" * len(ids))
            track_filter = f" AND track_id IN ({ph})"
            track_params = tuple(ids)

        weights = "SUM(CASE WHEN action='complete' THEN 1.7 ELSE 1.0 END) AS w"
        base_where = """
            FROM events_track_usage
            WHERE telegram_user_id = ?
              AND ts_utc >= ?
              AND action IN ('play','complete')
        """ + track_filter

        base_params: Tuple[Any, ...] = (int(telegram_user_id), cutoff) + track_params

        out_genre: Dict[str, float] = {}
        out_year: Dict[str, float] = {}
        out_lang: Dict[str, float] = {}

        if has_genre:
            cur = conn.execute(
                f"SELECT genre_id, {weights} {base_where} AND genre_id IS NOT NULL GROUP BY genre_id",
                base_params,
            )
            out_genre = {str(r[0]): float(r[1] or 0.0) for r in cur.fetchall() if r[0] is not None}

        if has_year:
            cur = conn.execute(
                f"SELECT release_year, {weights} {base_where} AND release_year IS NOT NULL GROUP BY release_year",
                base_params,
            )
            out_year = {str(r[0]): float(r[1] or 0.0) for r in cur.fetchall() if r[0] is not None}

        if has_lang:
            cur = conn.execute(
                f"SELECT lang_bucket, {weights} {base_where} AND lang_bucket IS NOT NULL AND lang_bucket != '' GROUP BY lang_bucket",
                base_params,
            )
            out_lang = {str(r[0]): float(r[1] or 0.0) for r in cur.fetchall() if r[0] is not None}

        return {"genre": out_genre, "lang": out_lang, "year": out_year}
    finally:
        conn.close()


def _now_utc_ts() -> int:
    """Текущее время в UTC в виде целого unix‑timestamp (секунды)."""
    return int(time.time())


def get_distinct_telegram_user_ids() -> List[int]:
    """
    Все уникальные telegram_user_id из аналитики (для рассылок и т.п.).
    Объединяет таблицы, где может быть id пользователя.
    Пользователи только с /start без событий в мини‑аппе могут отсутствовать.
    """
    init_db()
    conn = _get_conn()
    try:
        cur = conn.execute(
            """
            SELECT telegram_user_id FROM (
                SELECT DISTINCT telegram_user_id AS telegram_user_id
                FROM events_user_activity WHERE telegram_user_id IS NOT NULL
                UNION
                SELECT DISTINCT telegram_user_id
                FROM events_button_clicks WHERE telegram_user_id IS NOT NULL
                UNION
                SELECT DISTINCT telegram_user_id
                FROM events_errors WHERE telegram_user_id IS NOT NULL
                UNION
                SELECT DISTINCT telegram_user_id
                FROM events_track_usage WHERE telegram_user_id IS NOT NULL
                UNION
                SELECT DISTINCT telegram_user_id
                FROM events_playlists WHERE telegram_user_id IS NOT NULL
            )
            ORDER BY telegram_user_id
            """
        )
        return [int(row[0]) for row in cur.fetchall()]
    finally:
        conn.close()


def upsert_bot_subscriber(telegram_user_id: int, username: Optional[str], source: str) -> None:
    """Известный numeric id (мини‑апп, webhook и т.д.). Не означает, что открыт ЛС с ботом — см. private_chat_ok."""
    init_db()
    ts = _now_utc_ts()
    uname = ((username or "").strip()[:64] or None)
    src = (source or "unknown").strip()[:48] or "unknown"
    conn = _get_conn()
    try:
        has_pc = _table_has_column(conn, "bot_subscribers", "private_chat_ok")
        if has_pc:
            conn.execute(
                """
                INSERT INTO bot_subscribers (
                    telegram_user_id, username, first_contact_ts, last_contact_ts, last_source, private_chat_ok
                )
                VALUES (?, ?, ?, ?, ?, 0)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    username = COALESCE(NULLIF(excluded.username, ''), bot_subscribers.username),
                    last_contact_ts = excluded.last_contact_ts,
                    last_source = excluded.last_source
                """,
                (int(telegram_user_id), uname, ts, ts, src),
            )
        else:
            conn.execute(
                """
                INSERT INTO bot_subscribers (telegram_user_id, username, first_contact_ts, last_contact_ts, last_source)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(telegram_user_id) DO UPDATE SET
                    username = COALESCE(NULLIF(excluded.username, ''), bot_subscribers.username),
                    last_contact_ts = excluded.last_contact_ts,
                    last_source = excluded.last_source
                """,
                (int(telegram_user_id), uname, ts, ts, src),
            )
        conn.commit()
    finally:
        conn.close()


def get_bot_subscriber_ids() -> List[int]:
    """Все chat_id из таблицы bot_subscribers (кто хоть раз написал боту после деплоя этой фичи)."""
    init_db()
    conn = _get_conn()
    try:
        cur = conn.execute("SELECT telegram_user_id FROM bot_subscribers ORDER BY telegram_user_id")
        return [int(row[0]) for row in cur.fetchall()]
    finally:
        conn.close()


def get_broadcast_recipient_ids() -> List[int]:
    """
    Рассылка: объединение подписчиков бота и всех id из аналитики.
    Так не теряются старые пользователи до появления bot_subscribers.
    """
    seen: set[int] = set()
    out: List[int] = []
    for uid in get_bot_subscriber_ids():
        if uid not in seen:
            seen.add(uid)
            out.append(uid)
    for uid in get_distinct_telegram_user_ids():
        if uid not in seen:
            seen.add(uid)
            out.append(uid)
    out.sort()
    return out


def count_hash_only_user_events() -> Dict[str, int]:
    """
    События аналитики без telegram_user_id (остался только user_hash в extra).
    Личное сообщение от бота таким пользователям отправить нельзя — нет numeric chat_id.
    """
    init_db()
    conn = _get_conn()
    out: Dict[str, int] = {"user_activity_rows": 0, "distinct_user_hash_approx": 0}
    try:
        cur = conn.execute(
            """
            SELECT COUNT(*) FROM events_user_activity
            WHERE telegram_user_id IS NULL
              AND json_extract(extra_json, '$.user_hash') IS NOT NULL
              AND length(trim(json_extract(extra_json, '$.user_hash'))) > 0
            """
        )
        out["user_activity_rows"] = int(cur.fetchone()[0] or 0)
        cur = conn.execute(
            """
            SELECT COUNT(DISTINCT json_extract(extra_json, '$.user_hash'))
            FROM events_user_activity
            WHERE telegram_user_id IS NULL
              AND json_extract(extra_json, '$.user_hash') IS NOT NULL
              AND length(trim(json_extract(extra_json, '$.user_hash'))) > 0
            """
        )
        out["distinct_user_hash_approx"] = int(cur.fetchone()[0] or 0)
    finally:
        conn.close()
    return out


def get_best_usernames_map(user_ids: List[int]) -> Dict[int, str]:
    """
    Возвращает best-effort имя пользователя для приветствия в рассылке.
    Приоритет:
    1) bot_subscribers.username
    2) последнее username из events_* таблиц
    """
    ids = [int(x) for x in user_ids if isinstance(x, int) or str(x).lstrip("-").isdigit()]
    if not ids:
        return {}
    init_db()
    conn = _get_conn()
    try:
        ph = ",".join("?" * len(ids))
        out: Dict[int, str] = {}
        # 1) bot_subscribers
        cur = conn.execute(
            f"SELECT telegram_user_id, COALESCE(username,'') FROM bot_subscribers WHERE telegram_user_id IN ({ph})",
            tuple(ids),
        )
        for uid, uname in cur.fetchall():
            if uid is None:
                continue
            s = (uname or "").strip()
            if s:
                out[int(uid)] = s

        # 2) latest from events tables (union)
        cur2 = conn.execute(
            f"""
            SELECT telegram_user_id, username FROM (
              SELECT telegram_user_id, username, ts_utc FROM events_user_activity WHERE telegram_user_id IN ({ph}) AND username IS NOT NULL AND username != ''
              UNION ALL
              SELECT telegram_user_id, username, ts_utc FROM events_button_clicks WHERE telegram_user_id IN ({ph}) AND username IS NOT NULL AND username != ''
              UNION ALL
              SELECT telegram_user_id, username, ts_utc FROM events_errors WHERE telegram_user_id IN ({ph}) AND username IS NOT NULL AND username != ''
              UNION ALL
              SELECT telegram_user_id, username, ts_utc FROM events_track_usage WHERE telegram_user_id IN ({ph}) AND username IS NOT NULL AND username != ''
              UNION ALL
              SELECT telegram_user_id, username, ts_utc FROM events_playlists WHERE telegram_user_id IN ({ph}) AND username IS NOT NULL AND username != ''
            )
            ORDER BY ts_utc DESC
            """,
            tuple(ids) * 5,
        )
        for uid, uname in cur2.fetchall():
            if uid is None or int(uid) in out:
                continue
            s = (uname or "").strip()
            if s:
                out[int(uid)] = s
        return out
    finally:
        conn.close()
    return out


# ─── Низкоуровневые функции логирования событий ──────────────────────────────

def log_user_event(
    *,
    telegram_user_id: Optional[int],
    username: Optional[str],
    country_code: Optional[str],
    city_region: Optional[str],
    event_type: str,
    event_source: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    conn = _get_conn()
    try:
        conn.execute(
            """
            INSERT INTO events_user_activity (
                ts_utc, telegram_user_id, username, country_code, city_region,
                event_type, event_source, extra_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _now_utc_ts(),
                telegram_user_id,
                username,
                country_code,
                city_region,
                event_type,
                event_source,
                json.dumps(extra or {}, ensure_ascii=False)[:4000],
            ),
        )
        conn.commit()
    finally:
        conn.close()


def log_button_click(
    *,
    telegram_user_id: Optional[int],
    username: Optional[str],
    button_id: str,
    context: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    conn = _get_conn()
    try:
        conn.execute(
            """
            INSERT INTO events_button_clicks (
                ts_utc, telegram_user_id, username, button_id, context, extra_json
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                _now_utc_ts(),
                telegram_user_id,
                username,
                button_id,
                context,
                json.dumps(extra or {}, ensure_ascii=False)[:4000],
            ),
        )
        conn.commit()
    finally:
        conn.close()


def log_error_event(
    *,
    telegram_user_id: Optional[int],
    username: Optional[str],
    error_key: str,
    message: Optional[str] = None,
    stack: Optional[str] = None,
    country_code: Optional[str] = None,
    city_region: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    conn = _get_conn()
    try:
        conn.execute(
            """
            INSERT INTO events_errors (
                ts_utc, telegram_user_id, username, error_key, message,
                stack, country_code, city_region, extra_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _now_utc_ts(),
                telegram_user_id,
                username,
                error_key,
                message,
                stack,
                country_code,
                city_region,
                json.dumps(extra or {}, ensure_ascii=False)[:4000],
            ),
        )
        conn.commit()
    finally:
        conn.close()


def log_captcha_event(
    *,
    token_id: str,
    event_type: str,
    cooldown_seconds: Optional[int] = None,
    rucaptcha_used: bool = False,
    result: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    conn = _get_conn()
    try:
        conn.execute(
            """
            INSERT INTO events_captcha (
                ts_utc, token_id, event_type, cooldown_seconds,
                rucaptcha_used, result, extra_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                _now_utc_ts(),
                token_id,
                event_type,
                cooldown_seconds,
                1 if rucaptcha_used else 0,
                result,
                json.dumps(extra or {}, ensure_ascii=False)[:4000],
            ),
        )
        conn.commit()
    finally:
        conn.close()


def clear_captcha_events() -> int:
    """Удалить все записи из events_captcha (если события были ошибочно залогированы, напр. ошибка 9 как капча). Возвращает число удалённых строк."""
    conn = _get_conn()
    try:
        cur = conn.execute("SELECT COUNT(*) FROM events_captcha")
        n = cur.fetchone()[0] or 0
        conn.execute("DELETE FROM events_captcha")
        conn.commit()
        return n
    finally:
        conn.close()


def log_track_usage(
    *,
    telegram_user_id: Optional[int],
    username: Optional[str],
    track_id: str,
    action: str,
    duration_sec: Optional[float] = None,
    from_cache: bool = False,
    region: Optional[str] = None,
    genre_id: Optional[int] = None,
    release_year: Optional[int] = None,
    lang_bucket: Optional[str] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    conn = _get_conn()
    try:
        # На всякий случай: мягкая миграция, если init_db ещё не вызывали.
        _ensure_track_usage_columns(conn)
        if _table_has_column(conn, "events_track_usage", "genre_id"):
            conn.execute(
                """
                INSERT INTO events_track_usage (
                    ts_utc, telegram_user_id, username, track_id, action,
                    duration_sec, from_cache, region,
                    genre_id, release_year, lang_bucket,
                    extra_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _now_utc_ts(),
                    telegram_user_id,
                    username,
                    track_id,
                    action,
                    duration_sec,
                    1 if from_cache else 0,
                    region,
                    genre_id,
                    release_year,
                    (lang_bucket or None),
                    json.dumps(extra or {}, ensure_ascii=False)[:4000],
                ),
            )
        else:
            conn.execute(
                """
                INSERT INTO events_track_usage (
                    ts_utc, telegram_user_id, username, track_id, action,
                    duration_sec, from_cache, region, extra_json
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _now_utc_ts(),
                    telegram_user_id,
                    username,
                    track_id,
                    action,
                    duration_sec,
                    1 if from_cache else 0,
                    region,
                    json.dumps(extra or {}, ensure_ascii=False)[:4000],
                ),
            )
        conn.commit()
    finally:
        conn.close()


def log_playlist_event(
    *,
    telegram_user_id: Optional[int],
    username: Optional[str],
    playlist_id: Optional[str],
    action: str,
    extra: Optional[Dict[str, Any]] = None,
) -> None:
    conn = _get_conn()
    try:
        conn.execute(
            """
            INSERT INTO events_playlists (
                ts_utc, telegram_user_id, username, playlist_id, action, extra_json
            )
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                _now_utc_ts(),
                telegram_user_id,
                username,
                playlist_id,
                action,
                json.dumps(extra or {}, ensure_ascii=False)[:4000],
            ),
        )
        conn.commit()
    finally:
        conn.close()


def get_recent_playlist_events(limit: int = 200) -> List[Dict[str, Any]]:
    """
    Последние события по плейлистам: create/rename/delete/add_track/remove_track.
    Возвращает список с временем (UTC), user_id, username, действием и данными.
    """
    conn = _get_conn()
    try:
        cur = conn.execute(
            """
            SELECT ts_utc, telegram_user_id, username, playlist_id, action, extra_json
            FROM events_playlists
            ORDER BY ts_utc DESC
            LIMIT ?
            """,
            (int(limit),),
        )
        rows: List[Dict[str, Any]] = []
        for ts_utc, telegram_user_id, username, playlist_id, action, extra_json in cur.fetchall():
            try:
                extra = json.loads(extra_json) if extra_json else {}
            except Exception:
                extra = {"raw": str(extra_json)[:200]}
            rows.append(
                {
                    "time": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts_utc)),
                    "telegram_user_id": telegram_user_id,
                    "username": username or "",
                    "playlist_id": playlist_id,
                    "action": action,
                    "extra": extra,
                }
            )
        return rows
    finally:
        conn.close()


def get_user_timeline(telegram_user_id: int, limit: int = 100) -> List[Dict[str, Any]]:
    """
    Лента событий пользователя: активность, ошибки, воспроизведения — в одном списке по времени (новые сверху).
    Для разбора сценария «что делал и почему ошибки».
    """
    conn = _get_conn()
    try:
        uid = int(telegram_user_id)
    except (TypeError, ValueError):
        return []
    try:
        rows: List[Dict[str, Any]] = []
        # Активность: open_app, search, open_profile и т.д.
        cur = conn.execute(
            """
            SELECT ts_utc, event_type, event_source
            FROM events_user_activity
            WHERE telegram_user_id = ?
            ORDER BY ts_utc DESC
            LIMIT ?
            """,
            (uid, int(limit)),
        )
        for ts_utc, event_type, event_source in cur.fetchall():
            rows.append({
                "time_utc": ts_utc,
                "time": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts_utc)),
                "kind": "activity",
                "detail": event_type or "",
                "extra": event_source or "",
            })
        # Ошибки
        cur = conn.execute(
            """
            SELECT ts_utc, error_key, message
            FROM events_errors
            WHERE telegram_user_id = ?
            ORDER BY ts_utc DESC
            LIMIT ?
            """,
            (uid, int(limit)),
        )
        for ts_utc, error_key, message in cur.fetchall():
            rows.append({
                "time_utc": ts_utc,
                "time": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts_utc)),
                "kind": "error",
                "detail": error_key or "",
                "extra": (message or "")[:200],
            })
        # Воспроизведения и скачивания
        cur = conn.execute(
            """
            SELECT ts_utc, action, track_id
            FROM events_track_usage
            WHERE telegram_user_id = ?
            ORDER BY ts_utc DESC
            LIMIT ?
            """,
            (uid, int(limit)),
        )
        for ts_utc, action, track_id in cur.fetchall():
            rows.append({
                "time_utc": ts_utc,
                "time": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts_utc)),
                "kind": "track",
                "detail": action or "",
                "extra": (track_id or "")[:80],
            })
        rows.sort(key=lambda x: x["time_utc"], reverse=True)
        return rows[: int(limit)]
    finally:
        conn.close()


def get_user_vk_activity_summary(
    telegram_user_id: int,
    date_utc: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Сводка по пользователю за день (UTC): поиски, воспроизведения из кэша vs из VK, ошибки.
    date_utc: YYYY-MM-DD или None (сегодня UTC).
    """
    now = _now_utc_ts()
    if date_utc:
        day_start = int(datetime.strptime(date_utc, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp())
    else:
        day_start = int(datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0).timestamp())
    day_end = day_start + 86400
    uid = int(telegram_user_id)

    conn = _get_conn()
    try:
        # Поиски за день
        cur = conn.execute(
            "SELECT COUNT(*) FROM events_user_activity WHERE telegram_user_id = ? AND event_type = 'search' AND ts_utc >= ? AND ts_utc < ?",
            (uid, day_start, day_end),
        )
        search_count = cur.fetchone()[0] or 0

        # Воспроизведения за день: всего, из кэша, из VK (не из кэша)
        cur = conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN from_cache = 1 THEN 1 ELSE 0 END) FROM events_track_usage WHERE telegram_user_id = ? AND action = 'play' AND ts_utc >= ? AND ts_utc < ?",
            (uid, day_start, day_end),
        )
        row = cur.fetchone()
        play_count = row[0] or 0
        play_from_cache = row[1] or 0
        play_from_vk = play_count - play_from_cache

        # Ошибки за день: daily_limit (search/search_load_more), play_failed
        cur = conn.execute(
            """SELECT error_key, message, COUNT(*) FROM events_errors
               WHERE telegram_user_id = ? AND ts_utc >= ? AND ts_utc < ?
               GROUP BY error_key, message""",
            (uid, day_start, day_end),
        )
        errors = {f"{row[0]}:{row[1]}": row[2] for row in cur.fetchall()}

        return {
            "telegram_user_id": uid,
            "date_utc": date_utc or datetime.utcnow().strftime("%Y-%m-%d"),
            "search_count": search_count,
            "play_count": play_count,
            "play_from_cache": play_from_cache,
            "play_from_vk": play_from_vk,
            "errors": errors,
        }
    finally:
        conn.close()


def get_user_search_count_in_interval(
    telegram_user_id: int,
    start_ts: int,
    end_ts: int,
) -> int:
    """
    Количество поисков (event_type = 'search') пользователя в интервале [start_ts, end_ts) UTC (unix).
    """
    conn = _get_conn()
    try:
        cur = conn.execute(
            """SELECT COUNT(*) FROM events_user_activity
               WHERE telegram_user_id = ? AND event_type = 'search' AND ts_utc >= ? AND ts_utc < ?""",
            (int(telegram_user_id), start_ts, end_ts),
        )
        return cur.fetchone()[0] or 0
    finally:
        conn.close()


def get_summary() -> Dict[str, Any]:
    init_db()
    conn = _get_conn()
    try:
        now = _now_utc_ts()
        day_ago = now - 86400
        month_ago = now - 30 * 86400

        # Учёт только по telegram_user_id + отдельно те, кто учтён только по user_hash (старые логи)
        # Так не дублируем одного человека (id и hash), но не теряем тех, у кого нет id
        _where_id = "telegram_user_id IS NOT NULL"
        _where_hash_only = "telegram_user_id IS NULL AND json_extract(extra_json, '$.user_hash') IS NOT NULL AND json_extract(extra_json, '$.user_hash') != ''"
        _hash_expr = "json_extract(extra_json, '$.user_hash')"

        cur = conn.execute(
            f"SELECT COUNT(DISTINCT telegram_user_id) FROM events_user_activity WHERE {_where_id}"
        )
        by_id = cur.fetchone()[0] or 0
        cur = conn.execute(
            f"SELECT COUNT(DISTINCT {_hash_expr}) FROM events_user_activity WHERE {_where_hash_only}"
        )
        by_hash_only = cur.fetchone()[0] or 0
        unique_users = by_id + by_hash_only

        cur = conn.execute(
            f"SELECT COUNT(DISTINCT telegram_user_id) FROM events_user_activity WHERE {_where_id} AND ts_utc >= ?",
            (day_ago,),
        )
        by_id_today = cur.fetchone()[0] or 0
        cur = conn.execute(
            f"SELECT COUNT(DISTINCT {_hash_expr}) FROM events_user_activity WHERE {_where_hash_only} AND ts_utc >= ?",
            (day_ago,),
        )
        unique_users_today = by_id_today + (cur.fetchone()[0] or 0)

        cur = conn.execute(
            f"SELECT COUNT(DISTINCT telegram_user_id) FROM events_user_activity WHERE {_where_id} AND ts_utc >= ?",
            (month_ago,),
        )
        by_id_month = cur.fetchone()[0] or 0
        cur = conn.execute(
            f"SELECT COUNT(DISTINCT {_hash_expr}) FROM events_user_activity WHERE {_where_hash_only} AND ts_utc >= ?",
            (month_ago,),
        )
        unique_users_month = by_id_month + (cur.fetchone()[0] or 0)

        online_cutoff = now - 15 * 60
        cur = conn.execute(
            f"SELECT COUNT(DISTINCT telegram_user_id) FROM events_user_activity WHERE {_where_id} AND ts_utc >= ?",
            (online_cutoff,),
        )
        by_id_online = cur.fetchone()[0] or 0
        cur = conn.execute(
            f"SELECT COUNT(DISTINCT {_hash_expr}) FROM events_user_activity WHERE {_where_hash_only} AND ts_utc >= ?",
            (online_cutoff,),
        )
        users_online = by_id_online + (cur.fetchone()[0] or 0)

        # Визиты (open_app) как аналог сессий
        cur = conn.execute(
            "SELECT COUNT(*) FROM events_user_activity WHERE event_type = 'open_app'"
        )
        visits = cur.fetchone()[0] or 0

        # Треки
        cur = conn.execute(
            "SELECT COUNT(*) FROM events_track_usage WHERE action = 'play'"
        )
        track_plays = cur.fetchone()[0] or 0
        cur = conn.execute(
            "SELECT COUNT(*) FROM events_track_usage WHERE action = 'complete'"
        )
        track_finishes = cur.fetchone()[0] or 0
        cur = conn.execute(
            "SELECT COUNT(*) FROM events_track_usage WHERE action = 'download_to_bot'"
        )
        downloads_total = cur.fetchone()[0] or 0

        # Поиски — как отдельный event_type в user_activity
        cur = conn.execute(
            "SELECT COUNT(*) FROM events_user_activity WHERE event_type = 'search'"
        )
        search_count = cur.fetchone()[0] or 0

        # Кнопки (шеринг «пользователям» и «в личные сообщения» считаем одним)
        cur = conn.execute(
            "SELECT button_id, COUNT(*) FROM events_button_clicks GROUP BY button_id"
        )
        by_button = {row[0]: int(row[1]) for row in cur.fetchall()}
        share_users = by_button.get("button_share_to_users", 0) + by_button.get("button_share_track", 0) + by_button.get("button_share_chat_direct", 0)
        if share_users:
            by_button["button_share_to_users"] = share_users
        for k in ("button_share_track", "button_share_chat_direct"):
            by_button.pop(k, None)

        # Ошибки
        cur = conn.execute(
            "SELECT error_key, COUNT(*) FROM events_errors GROUP BY error_key"
        )
        errors_by_key = {row[0]: int(row[1]) for row in cur.fetchall()}

        cur = conn.execute(
            """
            SELECT ts_utc, telegram_user_id, username, error_key, message, country_code, city_region, extra_json
            FROM events_errors
            ORDER BY ts_utc DESC
            LIMIT 100
            """
        )
        recent_errors: List[Dict[str, Any]] = []
        for ts_utc, telegram_user_id, username, error_key, message, country_code, city_region, extra_json in cur.fetchall():
            try:
                extra = json.loads(extra_json) if extra_json else {}
            except Exception:
                extra = {"raw": str(extra_json)[:200]}
            user_hash = (extra.get("user_hash") or "").strip() if isinstance(extra, dict) else ""
            recent_errors.append(
                {
                    "time": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts_utc)),
                    "telegram_user_id": telegram_user_id,
                    "username": username or "",
                    "user_display": str(telegram_user_id) if telegram_user_id is not None else (user_hash[:12] + "…" if len(user_hash) > 12 else user_hash),
                    "error_key": error_key,
                    "message": message or "",
                    "country": country_code or "",
                    "region": city_region or "",
                    "extra": extra,
                }
            )

        # Капчи и кулдауны (всего и по типам)
        cur = conn.execute(
            "SELECT event_type, COUNT(*) FROM events_captcha GROUP BY event_type"
        )
        captcha_stats = {row[0]: int(row[1]) for row in cur.fetchall()}
        cur = conn.execute("SELECT COUNT(*) FROM events_captcha")
        captcha_total = cur.fetchone()[0] or 0

        # Капчи/кулдауны за последние 24 ч (чтобы не путать «сейчас только ошибка 9» с накопленными капчами)
        cutoff_24h = _now_utc_ts() - 86400
        cur = conn.execute(
            "SELECT event_type, COUNT(*) FROM events_captcha WHERE ts_utc >= ? GROUP BY event_type",
            (cutoff_24h,),
        )
        captcha_stats_24h = {row[0]: int(row[1]) for row in cur.fetchall()}
        cur = conn.execute("SELECT COUNT(*) FROM events_captcha WHERE ts_utc >= ?", (cutoff_24h,))
        captcha_total_24h = cur.fetchone()[0] or 0

        # Список пользователей: по одному разу на telegram_user_id,
        # с датой первой активности (регистрация), последнего захода и регионом.
        cur = conn.execute(
            """
            WITH per_user AS (
                SELECT
                    ua.telegram_user_id AS telegram_user_id,
                    MIN(ua.ts_utc) AS first_ts,
                    MAX(ua.ts_utc) AS last_ts,
                    COALESCE(MAX(CASE WHEN trim(ua.username) != '' THEN ua.username END), MAX(ua.username), '') AS username,
                    MAX(ua.country_code) AS country_code,
                    MAX(ua.city_region) AS city_region
                FROM events_user_activity ua
                WHERE ua.telegram_user_id IS NOT NULL
                GROUP BY ua.telegram_user_id
            ),
            with_ord AS (
                SELECT
                    ROW_NUMBER() OVER (ORDER BY first_ts ASC) AS ordinal,
                    telegram_user_id,
                    first_ts,
                    last_ts,
                    username,
                    country_code,
                    city_region
                FROM per_user
            ),
            with_flags AS (
                SELECT
                    w.*,
                    COALESCE(bs.private_chat_ok, 0) AS bot_private_chat_ok
                FROM with_ord w
                LEFT JOIN bot_subscribers bs ON bs.telegram_user_id = w.telegram_user_id
            )
            SELECT
                telegram_user_id,
                username,
                last_ts,
                first_ts,
                country_code,
                city_region,
                ordinal,
                bot_private_chat_ok
            FROM with_flags
            ORDER BY last_ts DESC
            LIMIT 5000
            """
        )
        users_list: List[Dict[str, Any]] = []
        for tid, uname, last_ts, first_ts, country_code, city_region, ordinal, pc_ok in cur.fetchall():
            users_list.append(
                {
                    "telegram_user_id": tid,
                    "username": (uname or "").strip(),
                    "last_seen_utc": int(last_ts or 0),
                    "registered_utc": int(first_ts or 0),
                    "ordinal": int(ordinal),
                    "country_code": (country_code or "").strip() if country_code else None,
                    "city_region": (city_region or "").strip() if city_region else None,
                    "bot_private_chat_ok": bool(int(pc_ok or 0)),
                }
            )

        cur = conn.execute("SELECT COUNT(*) FROM bot_subscribers WHERE private_chat_ok = 1")
        bot_private_chat_users_total = int(cur.fetchone()[0] or 0)
        cur = conn.execute(
            """
            SELECT COUNT(DISTINCT ua.telegram_user_id) FROM events_user_activity ua
            WHERE ua.telegram_user_id IS NOT NULL
              AND EXISTS (
                SELECT 1 FROM bot_subscribers bs
                WHERE bs.telegram_user_id = ua.telegram_user_id AND bs.private_chat_ok = 1
              )
            """
        )
        analytics_users_with_bot_dm = int(cur.fetchone()[0] or 0)

        # Удержание: по той же схеме (id или hash-only) — активность в более чем один день
        cur = conn.execute(
            """
            WITH days AS (
                SELECT
                    CASE WHEN telegram_user_id IS NOT NULL THEN CAST(telegram_user_id AS TEXT) ELSE json_extract(extra_json, '$.user_hash') END AS user_key,
                    DATE(ts_utc, 'unixepoch') AS d
                FROM events_user_activity
                WHERE telegram_user_id IS NOT NULL
                   OR (telegram_user_id IS NULL AND json_extract(extra_json, '$.user_hash') IS NOT NULL AND json_extract(extra_json, '$.user_hash') != '')
            ),
            grouped AS (
                SELECT user_key, MIN(d) AS first_day, MAX(d) AS last_day
                FROM days
                WHERE user_key IS NOT NULL AND user_key != ''
                GROUP BY user_key
            )
            SELECT COUNT(*) FROM grouped WHERE julianday(last_day) - julianday(first_day) >= 1.0
            """
        )
        returned_next_day = cur.fetchone()[0] or 0
        retention_pct = round(100.0 * returned_next_day / unique_users, 1) if unique_users else 0

        return {
            "total_events": track_plays + track_finishes + downloads_total + search_count,
            "users_online": users_online,
            "by_button": by_button,
            "errors_by_key": errors_by_key,
            "unique_users": unique_users,
            "unique_users_today": unique_users_today,
            "unique_users_month": unique_users_month,
            "visits": visits,
            "track_plays": track_plays,
            "track_finishes": track_finishes,
            "downloads_total": downloads_total,
            "search_count": search_count,
            "errors_count": sum(errors_by_key.values()),
            "recent_errors": recent_errors[:100],
            "retention_returned_next_day": returned_next_day,
            "retention_pct": retention_pct,
            "captcha_stats": captcha_stats,
            "captcha_total": captcha_total,
            "captcha_stats_24h": captcha_stats_24h,
            "captcha_total_24h": captcha_total_24h,
            "users_list": users_list,
            "bot_private_chat_users_total": bot_private_chat_users_total,
            "analytics_users_with_bot_dm": analytics_users_with_bot_dm,
        }
    finally:
        conn.close()


def get_metric_series(metric: str, days: int = 30) -> Dict[str, Any]:
    """
    Временной ряд по метрике за N дней (по суткам, UTC).

    Для простоты пока считаем по сырым событиям, а не по агрегатам:
    - visits        -> events_user_activity.event_type = 'open_app'
    - search_count  -> events_user_activity.event_type = 'search'
    - track_plays   -> events_track_usage.action = 'play'
    - track_finishes-> events_track_usage.action = 'complete'
    - downloads     -> events_track_usage.action = 'download_to_bot'
    - errors_count  -> events_errors
    """
    conn = _get_conn()
    try:
        since = _now_utc_ts() - days * 86400
        if metric == "visits":
            sql = """
                SELECT strftime('%Y-%m-%d', ts_utc, 'unixepoch') AS d, COUNT(*)
                FROM events_user_activity
                WHERE event_type = 'open_app' AND ts_utc >= ?
                GROUP BY d
                ORDER BY d
            """
            params = (since,)
        elif metric == "search_count":
            sql = """
                SELECT strftime('%Y-%m-%d', ts_utc, 'unixepoch') AS d, COUNT(*)
                FROM events_user_activity
                WHERE event_type = 'search' AND ts_utc >= ?
                GROUP BY d
                ORDER BY d
            """
            params = (since,)
        elif metric == "track_plays":
            sql = """
                SELECT strftime('%Y-%m-%d', ts_utc, 'unixepoch') AS d, COUNT(*)
                FROM events_track_usage
                WHERE action = 'play' AND ts_utc >= ?
                GROUP BY d
                ORDER BY d
            """
            params = (since,)
        elif metric == "track_finishes":
            sql = """
                SELECT strftime('%Y-%m-%d', ts_utc, 'unixepoch') AS d, COUNT(*)
                FROM events_track_usage
                WHERE action = 'complete' AND ts_utc >= ?
                GROUP BY d
                ORDER BY d
            """
            params = (since,)
        elif metric == "downloads":
            sql = """
                SELECT strftime('%Y-%m-%d', ts_utc, 'unixepoch') AS d, COUNT(*)
                FROM events_track_usage
                WHERE action = 'download_to_bot' AND ts_utc >= ?
                GROUP BY d
                ORDER BY d
            """
            params = (since,)
        elif metric == "errors_count":
            sql = """
                SELECT strftime('%Y-%m-%d', ts_utc, 'unixepoch') AS d, COUNT(*)
                FROM events_errors
                WHERE ts_utc >= ?
                GROUP BY d
                ORDER BY d
            """
            params = (since,)
        else:
            # неизвестная метрика — пустой ряд
            return {"metric": metric, "days": days, "points": []}

        cur = conn.execute(sql, params)
        points = [{"date": row[0], "count": int(row[1])} for row in cur.fetchall()]
        return {
            "metric": metric,
            "days": days,
            "points": points,
        }
    finally:
        conn.close()


# ─── Агрегаторы по дням и месяцам (выполняются фоново раз в сутки) ───────────

def recompute_daily_aggregate(date_utc: str) -> None:
    """
    Пересчитать агрегаты для конкретного дня UTC (формат YYYY-MM-DD) и
    записать/обновить строку в daily_aggregates.
    """
    conn = _get_conn()
    try:
        # границы дня в unix‑timestamp (UTC)
        cur = conn.execute(
            "SELECT strftime('%s', ? || ' 00:00:00', 'utc'), strftime('%s', ? || ' 23:59:59', 'utc')",
            (date_utc, date_utc),
        )
        start_ts, end_ts = cur.fetchone()
        start_ts = int(start_ts)
        end_ts = int(end_ts)

        # активные пользователи за день (telegram_user_id или user_hash для мигрированных)
        cur = conn.execute(
            """
            SELECT COUNT(DISTINCT COALESCE(CAST(telegram_user_id AS TEXT), json_extract(extra_json, '$.user_hash')))
            FROM events_user_activity
            WHERE ts_utc BETWEEN ? AND ?
              AND (telegram_user_id IS NOT NULL OR (json_extract(extra_json, '$.user_hash') IS NOT NULL AND json_extract(extra_json, '$.user_hash') != ''))
            """,
            (start_ts, end_ts),
        )
        users_active_24h = cur.fetchone()[0] or 0

        # новые пользователи: первый раз в базе именно в этот день
        cur = conn.execute(
            """
            WITH first_seen AS (
                SELECT COALESCE(CAST(telegram_user_id AS TEXT), json_extract(extra_json, '$.user_hash')) AS user_key,
                       MIN(ts_utc) AS first_ts
                FROM events_user_activity
                WHERE telegram_user_id IS NOT NULL OR (json_extract(extra_json, '$.user_hash') IS NOT NULL AND json_extract(extra_json, '$.user_hash') != '')
                GROUP BY user_key
            )
            SELECT COUNT(*) FROM first_seen WHERE user_key IS NOT NULL AND user_key != '' AND first_ts BETWEEN ? AND ?
            """,
            (start_ts, end_ts),
        )
        users_new_24h = cur.fetchone()[0] or 0

        # треки и плейлисты
        cur = conn.execute(
            """
            SELECT
                SUM(CASE WHEN action = 'play' THEN 1 ELSE 0 END),
                SUM(CASE WHEN action = 'download_to_bot' THEN 1 ELSE 0 END)
            FROM events_track_usage
            WHERE ts_utc BETWEEN ? AND ?
            """,
            (start_ts, end_ts),
        )
        row = cur.fetchone() or (0, 0)
        plays_24h = int(row[0] or 0)
        downloads_24h = int(row[1] or 0)

        cur = conn.execute(
            """
            SELECT COUNT(*)
            FROM events_playlists
            WHERE action = 'create' AND ts_utc BETWEEN ? AND ?
            """,
            (start_ts, end_ts),
        )
        playlists_created_24h = cur.fetchone()[0] or 0

        # ошибки за день
        cur = conn.execute(
            """
            SELECT error_key, COUNT(*)
            FROM events_errors
            WHERE ts_utc BETWEEN ? AND ?
            GROUP BY error_key
            """,
            (start_ts, end_ts),
        )
        errors_by_key = {row[0]: int(row[1]) for row in cur.fetchall()}
        errors_total_24h = sum(errors_by_key.values())

        # капчи / кулдауны
        cur = conn.execute(
            """
            SELECT event_type, COUNT(*)
            FROM events_captcha
            WHERE ts_utc BETWEEN ? AND ?
            GROUP BY event_type
            """,
            (start_ts, end_ts),
        )
        captcha_stats = {row[0]: int(row[1]) for row in cur.fetchall()}

        # клики по кнопкам
        cur = conn.execute(
            """
            SELECT button_id, COUNT(*)
            FROM events_button_clicks
            WHERE ts_utc BETWEEN ? AND ?
            GROUP BY button_id
            """,
            (start_ts, end_ts),
        )
        button_clicks = {row[0]: int(row[1]) for row in cur.fetchall()}

        # retention по дню: день и предыдущий день (date_utc уже YYYY-MM-DD)
        day_str = date_utc
        try:
            d = datetime.strptime(date_utc, "%Y-%m-%d").replace(tzinfo=timezone.utc) - timedelta(days=1)
            prev_day_str = d.strftime("%Y-%m-%d")
        except Exception:
            prev_day_str = date_utc

        cur = conn.execute(
            """
            WITH by_day AS (
                SELECT COALESCE(CAST(telegram_user_id AS TEXT), json_extract(extra_json, '$.user_hash')) AS user_key,
                       DATE(ts_utc, 'unixepoch') AS d
                FROM events_user_activity
                WHERE (telegram_user_id IS NOT NULL OR (json_extract(extra_json, '$.user_hash') IS NOT NULL AND json_extract(extra_json, '$.user_hash') != ''))
                  AND DATE(ts_utc, 'unixepoch') IN (?, ?)
            )
            SELECT d, COUNT(DISTINCT user_key) FROM by_day WHERE user_key IS NOT NULL AND user_key != '' GROUP BY d
            """,
            (day_str, prev_day_str),
        )
        counts = {row[0]: int(row[1]) for row in cur.fetchall()}
        active_today = counts.get(day_str, 0)
        active_yesterday = counts.get(prev_day_str, 0)

        cur = conn.execute(
            """
            WITH today AS (
                SELECT DISTINCT COALESCE(CAST(telegram_user_id AS TEXT), json_extract(extra_json, '$.user_hash')) AS user_key
                FROM events_user_activity
                WHERE (telegram_user_id IS NOT NULL OR (json_extract(extra_json, '$.user_hash') IS NOT NULL AND json_extract(extra_json, '$.user_hash') != ''))
                  AND DATE(ts_utc, 'unixepoch') = ?
            ),
            yesterday AS (
                SELECT DISTINCT COALESCE(CAST(telegram_user_id AS TEXT), json_extract(extra_json, '$.user_hash')) AS user_key
                FROM events_user_activity
                WHERE (telegram_user_id IS NOT NULL OR (json_extract(extra_json, '$.user_hash') IS NOT NULL AND json_extract(extra_json, '$.user_hash') != ''))
                  AND DATE(ts_utc, 'unixepoch') = ?
            )
            SELECT COUNT(*) FROM today WHERE user_key IN (SELECT user_key FROM yesterday) AND user_key IS NOT NULL AND user_key != ''
            """,
            (day_str, prev_day_str),
        )
        returned = cur.fetchone()[0] or 0
        retention_24h = round(100.0 * returned / active_yesterday, 1) if active_yesterday else 0.0

        conn.execute(
            """
            INSERT INTO daily_aggregates (
                date_utc, users_active_24h, users_new_24h,
                plays_24h, downloads_24h, playlists_created_24h,
                errors_total_24h, errors_by_key_json,
                captcha_stats_json, button_clicks_json, retention_24h
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(date_utc) DO UPDATE SET
                users_active_24h = excluded.users_active_24h,
                users_new_24h = excluded.users_new_24h,
                plays_24h = excluded.plays_24h,
                downloads_24h = excluded.downloads_24h,
                playlists_created_24h = excluded.playlists_created_24h,
                errors_total_24h = excluded.errors_total_24h,
                errors_by_key_json = excluded.errors_by_key_json,
                captcha_stats_json = excluded.captcha_stats_json,
                button_clicks_json = excluded.button_clicks_json,
                retention_24h = excluded.retention_24h
            """,
            (
                date_utc,
                users_active_24h,
                users_new_24h,
                plays_24h,
                downloads_24h,
                playlists_created_24h,
                errors_total_24h,
                json.dumps(errors_by_key, ensure_ascii=False),
                json.dumps(captcha_stats, ensure_ascii=False),
                json.dumps(button_clicks, ensure_ascii=False),
                retention_24h,
            ),
        )
        conn.commit()
    finally:
        conn.close()


def recompute_monthly_aggregate(month_utc: str) -> None:
    """
    Пересчитать агрегаты для конкретного месяца UTC (формат YYYY-MM) и
    записать/обновить строку в monthly_aggregates.
    """
    conn = _get_conn()
    try:
        # границы месяца
        cur = conn.execute(
            "SELECT strftime('%s', ? || '-01 00:00:00', 'utc')",
            (month_utc,),
        )
        start_ts = int(cur.fetchone()[0])
        cur = conn.execute(
            "SELECT strftime('%s', datetime(? || '-01', '+1 month', '-1 second'), 'utc')",
            (month_utc,),
        )
        end_ts = int(cur.fetchone()[0])

        cur = conn.execute(
            """
            SELECT COUNT(DISTINCT telegram_user_id)
            FROM events_user_activity
            WHERE telegram_user_id IS NOT NULL
              AND ts_utc BETWEEN ? AND ?
            """,
            (start_ts, end_ts),
        )
        users_active_month = cur.fetchone()[0] or 0

        cur = conn.execute(
            """
            SELECT
                SUM(CASE WHEN action = 'play' THEN 1 ELSE 0 END),
                SUM(CASE WHEN action = 'download_to_bot' THEN 1 ELSE 0 END)
            FROM events_track_usage
            WHERE ts_utc BETWEEN ? AND ?
            """,
            (start_ts, end_ts),
        )
        row = cur.fetchone() or (0, 0)
        plays_month = int(row[0] or 0)
        downloads_month = int(row[1] or 0)

        cur = conn.execute(
            """
            SELECT COUNT(*)
            FROM events_playlists
            WHERE action = 'create' AND ts_utc BETWEEN ? AND ?
            """,
            (start_ts, end_ts),
        )
        playlists_created_month = cur.fetchone()[0] or 0

        cur = conn.execute(
            """
            SELECT error_key, COUNT(*)
            FROM events_errors
            WHERE ts_utc BETWEEN ? AND ?
            GROUP BY error_key
            """,
            (start_ts, end_ts),
        )
        errors_by_key = {row[0]: int(row[1]) for row in cur.fetchall()}

        cur = conn.execute(
            """
            SELECT event_type, COUNT(*)
            FROM events_captcha
            WHERE ts_utc BETWEEN ? AND ?
            GROUP BY event_type
            """,
            (start_ts, end_ts),
        )
        captcha_stats = {row[0]: int(row[1]) for row in cur.fetchall()}

        # для простоты retention по месяцу считаем как отношение пользователей,
        # у которых gap между первой и последней активностью >= 30 дней
        cur = conn.execute(
            """
            WITH spans AS (
                SELECT telegram_user_id,
                       MIN(ts_utc) AS first_ts,
                       MAX(ts_utc) AS last_ts
                FROM events_user_activity
                WHERE telegram_user_id IS NOT NULL
                GROUP BY telegram_user_id
            )
            SELECT COUNT(*)
            FROM spans
            WHERE (last_ts - first_ts) >= 30 * 86400
            """,
        )
        long_lived = cur.fetchone()[0] or 0
        retention_month_pct = round(100.0 * long_lived / users_active_month, 1) if users_active_month else 0.0

        retention_metrics = {
            "long_lived_users": long_lived,
            "retention_month_pct": retention_month_pct,
        }

        conn.execute(
            """
            INSERT INTO monthly_aggregates (
                month_utc, users_active_month, plays_month,
                downloads_month, playlists_created_month,
                errors_by_key_json, captcha_stats_json, retention_metrics_json
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(month_utc) DO UPDATE SET
                users_active_month = excluded.users_active_month,
                plays_month = excluded.plays_month,
                downloads_month = excluded.downloads_month,
                playlists_created_month = excluded.playlists_created_month,
                errors_by_key_json = excluded.errors_by_key_json,
                captcha_stats_json = excluded.captcha_stats_json,
                retention_metrics_json = excluded.retention_metrics_json
            """,
            (
                month_utc,
                users_active_month,
                plays_month,
                downloads_month,
                playlists_created_month,
                json.dumps(errors_by_key, ensure_ascii=False),
                json.dumps(captcha_stats, ensure_ascii=False),
                json.dumps(retention_metrics, ensure_ascii=False),
            ),
        )
        conn.commit()
    finally:
        conn.close()


# ─── Рекомендации: коллаборатив + поиск (агрегаты по SQLite) ─


def _ensure_user_library_tracks_table(conn: sqlite3.Connection) -> None:
    """Избранное + кастомные плейлисты (только библиотека TGPlay, не plays / не подборки VK)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_library_tracks (
            telegram_user_id INTEGER NOT NULL,
            track_id TEXT NOT NULL,
            ts_utc INTEGER NOT NULL,
            PRIMARY KEY (telegram_user_id, track_id)
        );
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ult_track ON user_library_tracks(track_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ult_user ON user_library_tracks(telegram_user_id)"
    )
    conn.commit()


def _ensure_user_bot_audio_delivered_table(conn: sqlite3.Connection) -> None:
    """Успешная отправка аудиофайла в личный чат с ботом (после ok от Telegram sendAudio)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS user_bot_audio_delivered (
            telegram_user_id INTEGER NOT NULL,
            track_id TEXT NOT NULL,
            ts_utc INTEGER NOT NULL,
            verified_live INTEGER NOT NULL DEFAULT 0,
            PRIMARY KEY (telegram_user_id, track_id)
        );
        """
    )
    if not _table_has_column(conn, "user_bot_audio_delivered", "verified_live"):
        conn.execute(
            "ALTER TABLE user_bot_audio_delivered ADD COLUMN verified_live INTEGER NOT NULL DEFAULT 0"
        )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ubad_user ON user_bot_audio_delivered(telegram_user_id)"
    )
    conn.commit()


def record_bot_audio_delivered(telegram_user_id: int, track_id: str) -> None:
    """Пометить трек как доставленный в чат пользователя с ботом (verified_live=1 — реальный sendAudio, без повторной кнопки в UI)."""
    tid = (track_id or "").strip()[:96]
    if not tid:
        return
    ts = _now_utc_ts()
    init_db()
    conn = _get_conn()
    try:
        _ensure_user_bot_audio_delivered_table(conn)
        conn.execute(
            """
            INSERT INTO user_bot_audio_delivered (telegram_user_id, track_id, ts_utc, verified_live)
            VALUES (?, ?, ?, 1)
            ON CONFLICT(telegram_user_id, track_id) DO UPDATE SET
              ts_utc = excluded.ts_utc,
              verified_live = 1
            """,
            (int(telegram_user_id), tid, ts),
        )
        conn.commit()
    finally:
        conn.close()


def get_bot_audio_delivered_track_ids(telegram_user_id: int, *, limit: int = 20000) -> List[str]:
    """Все track_id, по которым sendAudio в чат пользователя уже завершился успешно."""
    init_db()
    cap = max(1, min(int(limit), 50000))
    conn = _get_conn()
    try:
        _ensure_user_bot_audio_delivered_table(conn)
        cur = conn.execute(
            """
            SELECT track_id FROM user_bot_audio_delivered
            WHERE telegram_user_id = ?
            ORDER BY ts_utc DESC
            LIMIT ?
            """,
            (int(telegram_user_id), cap),
        )
        return [str(r[0]).strip() for r in cur.fetchall() if r[0]]
    finally:
        conn.close()


def get_bot_audio_delivered_verified_live_track_ids(telegram_user_id: int, *, limit: int = 20000) -> List[str]:
    """track_id с verified_live=1 (после правки: реальная доставка; UI не предлагает повтор без необходимости)."""
    init_db()
    cap = max(1, min(int(limit), 50000))
    conn = _get_conn()
    try:
        _ensure_user_bot_audio_delivered_table(conn)
        cur = conn.execute(
            """
            SELECT track_id FROM user_bot_audio_delivered
            WHERE telegram_user_id = ? AND COALESCE(verified_live, 0) = 1
            ORDER BY ts_utc DESC
            LIMIT ?
            """,
            (int(telegram_user_id), cap),
        )
        return [str(r[0]).strip() for r in cur.fetchall() if r[0]]
    finally:
        conn.close()


# Одноразовое восстановление «скачано в бота» из истории аналитики (до таблицы user_bot_audio_delivered).
UBAD_BACKFILL_META_KEY = "ubad_from_analytics_v1"


def _ensure_analytics_meta_table(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS analytics_meta (
            k TEXT PRIMARY KEY NOT NULL,
            v TEXT NOT NULL,
            ts_utc INTEGER NOT NULL
        );
        """
    )
    conn.commit()


def backfill_user_bot_audio_delivered_from_history(*, force: bool = False) -> Dict[str, Any]:
    """
    Восстановить user_bot_audio_delivered по:
    - events_track_usage.action = download_to_bot (раньше писалось при постановке в очередь, сейчас — при успехе);
    - events_button_clicks с button_id про скачивание и extra.track_id (если был).

    Массово прочитать историю чатов Telegram у всех пользователей через Bot API нельзя — это максимум по SQLite.
    Повтор без force не выполняется (флаг в analytics_meta).
    """
    init_db()
    conn = _get_conn()
    out: Dict[str, Any] = {"skipped": False, "ran": True}
    try:
        _ensure_user_bot_audio_delivered_table(conn)
        _ensure_analytics_meta_table(conn)
        if not force:
            cur = conn.execute("SELECT 1 FROM analytics_meta WHERE k = ?", (UBAD_BACKFILL_META_KEY,))
            if cur.fetchone():
                out["skipped"] = True
                out["ran"] = False
                return out

        tc0 = conn.total_changes

        # 1) События download_to_bot (основной источник); verified_live=0 — не блокируем повтор в UI
        conn.execute(
            """
            INSERT INTO user_bot_audio_delivered (telegram_user_id, track_id, ts_utc, verified_live)
            SELECT tu.telegram_user_id, substr(trim(tu.track_id), 1, 96) AS tid, MAX(tu.ts_utc) AS mx, 0
            FROM events_track_usage tu
            WHERE tu.action = 'download_to_bot'
              AND tu.telegram_user_id IS NOT NULL
              AND tu.track_id IS NOT NULL
              AND trim(tu.track_id) != ''
              AND length(trim(tu.track_id)) <= 96
            GROUP BY tu.telegram_user_id, tid
            ON CONFLICT(telegram_user_id, track_id) DO UPDATE SET
              ts_utc = CASE
                WHEN excluded.ts_utc > user_bot_audio_delivered.ts_utc THEN excluded.ts_utc
                ELSE user_bot_audio_delivered.ts_utc
              END,
              verified_live = MAX(
                COALESCE(user_bot_audio_delivered.verified_live, 0),
                COALESCE(excluded.verified_live, 0)
              )
            """
        )
        changes_after_track_usage = conn.total_changes - tc0

        # 2) Клики «скачать» с track_id в extra
        conn.execute(
            """
            INSERT INTO user_bot_audio_delivered (telegram_user_id, track_id, ts_utc, verified_live)
            SELECT
                bc.telegram_user_id,
                substr(trim(CAST(json_extract(bc.extra_json, '$.track_id') AS TEXT)), 1, 96) AS tid,
                MAX(bc.ts_utc) AS mx,
                0
            FROM events_button_clicks bc
            WHERE bc.telegram_user_id IS NOT NULL
              AND bc.extra_json IS NOT NULL
              AND length(trim(bc.extra_json)) > 2
              AND json_extract(bc.extra_json, '$.track_id') IS NOT NULL
              AND trim(CAST(json_extract(bc.extra_json, '$.track_id') AS TEXT)) != ''
              AND (
                bc.button_id = 'button_download'
                OR bc.button_id LIKE '%download%'
              )
            GROUP BY bc.telegram_user_id, tid
            ON CONFLICT(telegram_user_id, track_id) DO UPDATE SET
              ts_utc = CASE
                WHEN excluded.ts_utc > user_bot_audio_delivered.ts_utc THEN excluded.ts_utc
                ELSE user_bot_audio_delivered.ts_utc
              END,
              verified_live = MAX(
                COALESCE(user_bot_audio_delivered.verified_live, 0),
                COALESCE(excluded.verified_live, 0)
              )
            """
        )

        out["sqlite_changes"] = conn.total_changes - tc0
        out["changes_from_track_usage"] = changes_after_track_usage
        out["changes_from_button_clicks"] = out["sqlite_changes"] - changes_after_track_usage
        conn.execute(
            "INSERT OR REPLACE INTO analytics_meta (k, v, ts_utc) VALUES (?, ?, ?)",
            (UBAD_BACKFILL_META_KEY, "1", _now_utc_ts()),
        )
        conn.commit()
        return out
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def replace_user_library_tracks(telegram_user_id: int, track_ids: List[str]) -> None:
    """Полная замена индекса библиотеки пользователя (из JSON избранного + кастомных плейлистов)."""
    tid = int(telegram_user_id)
    ts = _now_utc_ts()
    seen: set = set()
    rows: List[Tuple[int, str, int]] = []
    for x in track_ids:
        s = str(x or "").strip()[:96]
        if not s or s in seen:
            continue
        seen.add(s)
        rows.append((tid, s, ts))
        if len(rows) >= 8000:
            break
    init_db()
    conn = _get_conn()
    try:
        _ensure_user_library_tracks_table(conn)
        conn.execute("DELETE FROM user_library_tracks WHERE telegram_user_id = ?", (tid,))
        if rows:
            conn.executemany(
                "INSERT INTO user_library_tracks (telegram_user_id, track_id, ts_utc) VALUES (?, ?, ?)",
                rows,
            )
        conn.commit()
    finally:
        conn.close()


def count_user_library_tracks(telegram_user_id: int) -> int:
    init_db()
    conn = _get_conn()
    try:
        _ensure_user_library_tracks_table(conn)
        cur = conn.execute(
            "SELECT COUNT(*) FROM user_library_tracks WHERE telegram_user_id = ?",
            (int(telegram_user_id),),
        )
        row = cur.fetchone()
        return int(row[0] or 0) if row else 0
    finally:
        conn.close()


def get_collaborative_library_track_ids(
    anchor_track_ids: List[str],
    current_user_id: int,
    exclude_track_ids: AbstractSet[str],
    *,
    limit: int = 28,
    anchor_sample: int = 40,
    max_peer_users: int = 320,
) -> List[str]:
    """
    Треки из библиотек других пользователей (только индекс избранного + кастомных плейлистов),
    у кого есть пересечение с якорными track_id. Не использует events_track_usage (plays / волна / подборки).
    """
    ft = [x.strip() for x in anchor_track_ids if isinstance(x, str) and x.strip()]
    if not ft:
        return []
    sample = _evenly_sample_str_ids(ft, min(int(anchor_sample), len(ft)))
    if not sample:
        return []
    init_db()
    conn = _get_conn()
    try:
        _ensure_user_library_tracks_table(conn)
        q_marks = ",".join("?" * len(sample))
        cur = conn.execute(
            f"""
            SELECT DISTINCT telegram_user_id
            FROM user_library_tracks
            WHERE track_id IN ({q_marks})
              AND telegram_user_id != ?
            LIMIT ?
            """,
            (*sample, int(current_user_id), int(max_peer_users)),
        )
        peer_users = [int(row[0]) for row in cur.fetchall() if row[0] is not None]
        if not peer_users:
            return []
        excl: set = {str(x).strip() for x in exclude_track_ids if x and str(x).strip()}
        for x in ft:
            excl.add(x)
        ph = ",".join("?" * len(peer_users))
        cur2 = conn.execute(
            f"""
            SELECT track_id, COUNT(*) AS cnt
            FROM user_library_tracks
            WHERE telegram_user_id IN ({ph})
            GROUP BY track_id
            ORDER BY cnt DESC
            LIMIT ?
            """,
            (*peer_users, int(max(80, limit * 8))),
        )
        out: List[str] = []
        for row in cur2.fetchall():
            tid = str(row[0] or "").strip()
            if not tid or tid in excl:
                continue
            out.append(tid)
            if len(out) >= int(limit):
                break
        return out
    finally:
        conn.close()


def _evenly_sample_str_ids(ids: List[str], k: int) -> List[str]:
    """Равномерные индексы по списку id (для IN-запросов к аналитике)."""
    if not ids or k <= 0:
        return []
    if len(ids) <= k:
        return list(dict.fromkeys(ids))
    out: List[str] = []
    n = len(ids)
    for i in range(k):
        idx = int(round(i * (n - 1) / max(1, k - 1)))
        tid = ids[idx]
        if tid not in out:
            out.append(tid)
    return out


def get_user_track_play_weights(
    telegram_user_id: int,
    *,
    limit: int = 48,
    days: int = 120,
) -> List[Tuple[str, float]]:
    """
    Частоты play по трекам пользователя (для профиля жанров/года в рекомендациях).
    Возвращает (track_id, count) по убыванию count.
    """
    cutoff = _now_utc_ts() - int(days) * 86400
    conn = _get_conn()
    try:
        cur = conn.execute(
            """
            SELECT track_id, COUNT(*) AS c
            FROM events_track_usage
            WHERE telegram_user_id = ?
              AND action = 'play'
              AND ts_utc >= ?
            GROUP BY track_id
            ORDER BY c DESC
            LIMIT ?
            """,
            (int(telegram_user_id), cutoff, int(limit)),
        )
        return [(str(row[0]), float(row[1])) for row in cur.fetchall() if row[0]]
    finally:
        conn.close()


def get_collaborative_track_ids(
    favorite_track_ids: List[str],
    current_user_id: int,
    *,
    limit: int = 24,
    days: int = 90,
    fav_sample: int = 28,
    max_peer_users: int = 400,
) -> List[str]:
    """
    Legacy: коллаборатив по events_track_usage (plays). Не используется в персональной ленте —
    там get_collaborative_library_track_ids (только избранное + кастомные плейлисты на диске).
    """
    ft = [x.strip() for x in favorite_track_ids if isinstance(x, str) and x.strip()]
    if not ft:
        return []
    sample = _evenly_sample_str_ids(ft, min(fav_sample, len(ft)))
    cutoff = _now_utc_ts() - days * 86400
    conn = _get_conn()
    try:
        q_marks = ",".join("?" * len(sample))
        cur = conn.execute(
            f"""
            SELECT DISTINCT telegram_user_id
            FROM events_track_usage
            WHERE action = 'play'
              AND ts_utc >= ?
              AND telegram_user_id IS NOT NULL
              AND telegram_user_id != ?
              AND track_id IN ({q_marks})
            LIMIT ?
            """,
            (cutoff, int(current_user_id), *sample, int(max_peer_users)),
        )
        peer_users = [row[0] for row in cur.fetchall() if row[0] is not None]
        if not peer_users:
            return []
        excl = set(ft)
        ph = ",".join("?" * len(peer_users))
        cur2 = conn.execute(
            f"""
            SELECT track_id, COUNT(*) AS cnt
            FROM events_track_usage
            WHERE action = 'play'
              AND ts_utc >= ?
              AND telegram_user_id IN ({ph})
            GROUP BY track_id
            ORDER BY cnt DESC
            LIMIT ?
            """,
            (cutoff, *peer_users, int(limit * 4)),
        )
        out: List[str] = []
        for tid, _cnt in cur2.fetchall():
            if tid and tid not in excl and tid not in out:
                out.append(tid)
            if len(out) >= limit:
                break
        return out
    finally:
        conn.close()


def _parse_q_norm_from_extra(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    try:
        d = json.loads(raw)
        q = d.get("q_norm")
        if isinstance(q, str):
            s = q.strip().lower()
            if len(s) >= 2:
                return s[:120]
    except Exception:
        pass
    return None


def get_recent_search_q_norms(
    telegram_user_id: int,
    *,
    limit: int = 12,
    days: int = 90,
) -> List[str]:
    """Недавние нормализованные поисковые строки пользователя (для VK audio.search в рекомендациях)."""
    cutoff = _now_utc_ts() - int(days) * 86400
    conn = _get_conn()
    try:
        cur = conn.execute(
            """
            SELECT extra_json FROM events_user_activity
            WHERE telegram_user_id = ?
              AND event_type = 'search'
              AND ts_utc >= ?
            ORDER BY ts_utc DESC
            LIMIT 120
            """,
            (int(telegram_user_id), cutoff),
        )
        out: List[str] = []
        seen: set = set()
        for (raw,) in cur.fetchall():
            qn = _parse_q_norm_from_extra(raw)
            if qn and qn not in seen:
                seen.add(qn)
                out.append(qn)
            if len(out) >= int(limit):
                break
        return out
    finally:
        conn.close()


def get_search_affinity_track_ids(
    telegram_user_id: int,
    exclude_track_ids: set,
    *,
    limit: int = 16,
    days: int = 60,
    max_q: int = 8,
    max_peers_per_q: int = 120,
    max_peer_users: int = 400,
) -> List[str]:
    """
    Недавние q_norm этого пользователя → те же запросы у других → их частые plays.
    """
    cutoff = _now_utc_ts() - days * 86400
    conn = _get_conn()
    try:
        cur = conn.execute(
            """
            SELECT extra_json FROM events_user_activity
            WHERE telegram_user_id = ?
              AND event_type = 'search'
              AND ts_utc >= ?
            ORDER BY ts_utc DESC
            LIMIT 50
            """,
            (int(telegram_user_id), cutoff),
        )
        q_list: List[str] = []
        seen_q = set()
        for (raw,) in cur.fetchall():
            qn = _parse_q_norm_from_extra(raw)
            if qn and qn not in seen_q:
                seen_q.add(qn)
                q_list.append(qn)
            if len(q_list) >= max_q:
                break
        if not q_list:
            return []

        peer_users: set = set()
        for qn in q_list:
            try:
                cur2 = conn.execute(
                    """
                    SELECT DISTINCT telegram_user_id
                    FROM events_user_activity
                    WHERE event_type = 'search'
                      AND ts_utc >= ?
                      AND telegram_user_id IS NOT NULL
                      AND telegram_user_id != ?
                      AND json_extract(extra_json, '$.q_norm') = ?
                    LIMIT ?
                    """,
                    (cutoff, int(telegram_user_id), qn, int(max_peers_per_q)),
                )
            except sqlite3.OperationalError:
                return []
            for (uid,) in cur2.fetchall():
                if uid is not None:
                    peer_users.add(uid)

        peer_list = list(peer_users)[: int(max_peer_users)]
        if not peer_list:
            return []
        ph = ",".join("?" * len(peer_list))
        cur3 = conn.execute(
            f"""
            SELECT track_id, COUNT(*) AS cnt
            FROM events_track_usage
            WHERE action = 'play'
              AND ts_utc >= ?
              AND telegram_user_id IN ({ph})
            GROUP BY track_id
            ORDER BY cnt DESC
            LIMIT ?
            """,
            (cutoff, *peer_list, int(limit * 4)),
        )
        out: List[str] = []
        for tid, _cnt in cur3.fetchall():
            if tid and tid not in exclude_track_ids and tid not in out:
                out.append(tid)
            if len(out) >= limit:
                break
        return out
    finally:
        conn.close()


def get_global_trending_track_ids(*, limit: int = 24, days: int = 14) -> List[str]:
    """Самые частые play по всем пользователям (холодный старт без избранного)."""
    cutoff = _now_utc_ts() - days * 86400
    conn = _get_conn()
    try:
        cur = conn.execute(
            """
            SELECT track_id, COUNT(*) AS cnt
            FROM events_track_usage
            WHERE action = 'play'
              AND ts_utc >= ?
              AND track_id IS NOT NULL
              AND track_id != ''
            GROUP BY track_id
            ORDER BY cnt DESC
            LIMIT ?
            """,
            (cutoff, int(limit * 2)),
        )
        return [row[0] for row in cur.fetchall() if row[0]][: int(limit)]
    finally:
        conn.close()
