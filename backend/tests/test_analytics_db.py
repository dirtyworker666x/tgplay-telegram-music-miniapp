"""
Unit tests for analytics_db: init_db, log events, get_summary.
Uses a temporary SQLite file so the real analytics.db is not touched.
"""
import os
import tempfile
import pytest


@pytest.fixture
def temp_db():
    """Create a temporary DB path and patch analytics_db to use it."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    try:
        import analytics_db
        original_path = analytics_db.DB_PATH
        analytics_db.DB_PATH = path
        yield path
    finally:
        analytics_db.DB_PATH = original_path
        try:
            os.unlink(path)
        except Exception:
            pass


def test_init_db_does_not_raise(temp_db):
    import analytics_db
    analytics_db.init_db()
    # init_db creates tables; no exception means success


def test_log_user_activity_and_summary(temp_db):
    import analytics_db
    analytics_db.init_db()
    analytics_db.log_user_event(
        telegram_user_id=12345,
        username="testuser",
        country_code=None,
        city_region=None,
        event_type="open_app",
        event_source="miniapp",
        extra=None,
    )
    analytics_db.log_user_event(
        telegram_user_id=12345,
        username="testuser",
        country_code=None,
        city_region=None,
        event_type="search",
        event_source="miniapp",
        extra=None,
    )
    summary = analytics_db.get_summary()
    assert summary["unique_users"] >= 1
    assert summary["visits"] >= 1
    assert summary["search_count"] >= 1


def test_log_button_click_and_summary(temp_db):
    import analytics_db
    analytics_db.init_db()
    analytics_db.log_button_click(
        telegram_user_id=999,
        username="btnuser",
        button_id="button_download",
        context="main",
        extra=None,
    )
    analytics_db.log_button_click(
        telegram_user_id=999,
        username="btnuser",
        button_id="button_download",
        context="main",
        extra=None,
    )
    summary = analytics_db.get_summary()
    assert summary["by_button"].get("button_download", 0) >= 2


def test_get_summary_date_boundaries(temp_db):
    """Summary uses current time for day_ago/month_ago; after inserting events, counts should reflect them."""
    import analytics_db
    analytics_db.init_db()
    analytics_db.log_user_event(
        telegram_user_id=111,
        username="u1",
        country_code=None,
        city_region=None,
        event_type="open_app",
        event_source="miniapp",
        extra=None,
    )
    summary = analytics_db.get_summary()
    assert "unique_users" in summary
    assert "unique_users_today" in summary
    assert "unique_users_month" in summary
    assert "by_button" in summary
    assert "visits" in summary


def test_track_usage_has_meta_columns_after_init(temp_db):
    import analytics_db

    analytics_db.init_db()
    conn = analytics_db._get_conn()
    try:
        cur = conn.execute("PRAGMA table_info(events_track_usage)")
        cols = {row[1] for row in cur.fetchall()}
        assert "genre_id" in cols
        assert "release_year" in cols
        assert "lang_bucket" in cols
    finally:
        conn.close()


def test_log_track_usage_with_meta_columns(temp_db):
    import analytics_db

    analytics_db.init_db()
    analytics_db.log_track_usage(
        telegram_user_id=1,
        username="u",
        track_id="1_2",
        action="play",
        duration_sec=12.0,
        from_cache=False,
        region=None,
        genre_id=5,
        release_year=2011,
        lang_bucket="ru",
        extra={"x": 1},
    )
    conn = analytics_db._get_conn()
    try:
        cur = conn.execute(
            "SELECT track_id, genre_id, release_year, lang_bucket FROM events_track_usage WHERE telegram_user_id = 1 LIMIT 1"
        )
        row = cur.fetchone()
        assert row is not None
        assert row[0] == "1_2"
        assert row[1] == 5
        assert row[2] == 2011
        assert row[3] == "ru"
    finally:
        conn.close()


def test_user_taste_aggregates_from_sqlite(temp_db):
    import analytics_db

    analytics_db.init_db()
    # play
    analytics_db.log_track_usage(
        telegram_user_id=7,
        username="u7",
        track_id="10_1",
        action="play",
        genre_id=1,
        release_year=2000,
        lang_bucket="ru",
    )
    # complete (weight > play)
    analytics_db.log_track_usage(
        telegram_user_id=7,
        username="u7",
        track_id="10_2",
        action="complete",
        genre_id=1,
        release_year=2001,
        lang_bucket="ru",
    )
    agg = analytics_db.get_user_taste_aggregates(7, days=365)
    assert float(agg["genre"].get("1", 0)) > 1.0
    assert float(agg["lang"].get("ru", 0)) > 1.0
    assert float(agg["year"].get("2000", 0)) > 0.0


def test_bot_private_chat_column_and_summary(temp_db):
    import analytics_db

    analytics_db.init_db()
    conn = analytics_db._get_conn()
    try:
        cur = conn.execute("PRAGMA table_info(bot_subscribers)")
        cols = {row[1] for row in cur.fetchall()}
        assert "private_chat_ok" in cols
    finally:
        conn.close()

    analytics_db.log_user_event(
        telegram_user_id=4242,
        username="pcuser",
        country_code=None,
        city_region=None,
        event_type="open_app",
        event_source="miniapp",
        extra=None,
    )
    analytics_db.upsert_bot_subscriber(4242, "pcuser", "api_test")
    s0 = analytics_db.get_summary()
    row0 = next((r for r in s0["users_list"] if r["telegram_user_id"] == 4242), None)
    assert row0 is not None
    assert row0.get("bot_private_chat_ok") is False

    analytics_db.mark_bot_private_chat_open(4242)
    s1 = analytics_db.get_summary()
    row1 = next((r for r in s1["users_list"] if r["telegram_user_id"] == 4242), None)
    assert row1 is not None
    assert row1.get("bot_private_chat_ok") is True
    assert s1.get("bot_private_chat_users_total", 0) >= 1
    assert s1.get("analytics_users_with_bot_dm", 0) >= 1


def test_get_user_taste_aggregates_only_track_ids(temp_db):
    import analytics_db

    analytics_db.init_db()
    uid = 5001
    analytics_db.log_track_usage(
        telegram_user_id=uid,
        username="u",
        track_id="1_1",
        action="play",
        genre_id=10,
        release_year=2020,
        lang_bucket="ru",
    )
    analytics_db.log_track_usage(
        telegram_user_id=uid,
        username="u",
        track_id="2_2",
        action="play",
        genre_id=20,
        release_year=2021,
        lang_bucket="en",
    )
    all_agg = analytics_db.get_user_taste_aggregates(uid, days=365)
    assert float(all_agg["genre"].get("10", 0)) > 0
    assert float(all_agg["genre"].get("20", 0)) > 0

    filtered = analytics_db.get_user_taste_aggregates(uid, days=365, only_track_ids={"1_1"})
    assert float(filtered["genre"].get("10", 0)) > 0
    assert float(filtered["genre"].get("20", 0)) == 0

    empty = analytics_db.get_user_taste_aggregates(uid, days=365, only_track_ids=set())
    assert empty["genre"] == {} and empty["lang"] == {} and empty["year"] == {}


def test_get_recent_search_q_norms(temp_db):
    import analytics_db

    analytics_db.init_db()
    uid = 6002
    analytics_db.log_user_event(
        telegram_user_id=uid,
        username="s",
        country_code=None,
        city_region=None,
        event_type="search",
        event_source="miniapp",
        extra={"q_norm": "artist one"},
    )
    analytics_db.log_user_event(
        telegram_user_id=uid,
        username="s",
        country_code=None,
        city_region=None,
        event_type="search",
        event_source="miniapp",
        extra={"q_norm": "artist two"},
    )
    qs = analytics_db.get_recent_search_q_norms(uid, limit=10, days=30)
    assert "artist one" in qs and "artist two" in qs


def test_dislike_track_artist_genre_roundtrip(temp_db):
    import analytics_db

    analytics_db.init_db()
    uid = 7001
    analytics_db.record_track_dislike(
        uid,
        "123_456",
        artist_key="test artist",
        genre_id=99,
    )
    assert "123_456" in analytics_db.get_disliked_track_ids(uid)
    assert analytics_db.get_rec_artist_show_penalties(uid).get("test artist") == analytics_db.REC_SHOW_PENALTY_STEP
    assert analytics_db.get_rec_genre_show_penalties(uid).get(99) == analytics_db.REC_SHOW_PENALTY_STEP
    assert "test artist" not in analytics_db.get_disliked_artist_keys(uid)
    assert 99 not in analytics_db.get_disliked_genre_ids(uid)


def test_removed_library_track_ids_roundtrip(temp_db):
    import analytics_db

    analytics_db.init_db()
    uid = 7005
    assert analytics_db.get_removed_library_track_ids(uid) == []
    analytics_db.record_removed_library_track_ids(uid, ["10_1", "10_2"])
    got = set(analytics_db.get_removed_library_track_ids(uid))
    assert got == {"10_1", "10_2"}
    analytics_db.record_removed_library_track_ids(uid, ["10_3"])
    assert "10_3" in analytics_db.get_removed_library_track_ids(uid)


def test_dislike_without_genre_id_skips_genre_penalty(temp_db):
    import analytics_db

    analytics_db.init_db()
    uid = 7002
    # Как API после дизлайка: жанр «Другое»/без id не передаётся в record_track_dislike.
    analytics_db.record_track_dislike(uid, "1_1", artist_key="solo artist", genre_id=None)
    assert analytics_db.get_rec_artist_show_penalties(uid).get("solo artist") == analytics_db.REC_SHOW_PENALTY_STEP
    assert analytics_db.get_rec_genre_show_penalties(uid) == {}


def test_favorite_relief_reduces_penalty(temp_db):
    import analytics_db

    analytics_db.init_db()
    uid = 7003
    analytics_db.record_track_dislike(uid, "2_2", artist_key="rel a", genre_id=2)
    analytics_db.record_track_dislike(uid, "2_3", artist_key="rel a", genre_id=2)
    assert analytics_db.get_rec_artist_show_penalties(uid)["rel a"] == 2 * analytics_db.REC_SHOW_PENALTY_STEP
    assert analytics_db.get_rec_genre_show_penalties(uid)[2] == 2 * analytics_db.REC_SHOW_PENALTY_STEP
    analytics_db.bump_rec_penalties_on_favorite(uid, artist_key="rel a", genre_id=2)
    assert analytics_db.get_rec_artist_show_penalties(uid)["rel a"] == analytics_db.REC_SHOW_PENALTY_STEP
    assert analytics_db.get_rec_genre_show_penalties(uid)[2] == analytics_db.REC_SHOW_PENALTY_STEP


def test_collaborative_library_track_ids(temp_db):
    import analytics_db

    analytics_db.init_db()
    analytics_db.replace_user_library_tracks(8001, ["a_1", "a_2"])
    analytics_db.replace_user_library_tracks(8002, ["a_1", "b_1", "b_2"])
    analytics_db.replace_user_library_tracks(8003, ["a_1", "c_9"])
    out = analytics_db.get_collaborative_library_track_ids(
        ["a_1", "a_2"],
        8001,
        {"a_1", "a_2"},
        limit=10,
    )
    assert "a_1" not in out
    assert set(out) & {"b_1", "b_2", "c_9"}, out


def test_backfill_ubad_from_track_usage(temp_db):
    import analytics_db

    analytics_db.init_db()
    analytics_db.log_track_usage(
        telegram_user_id=4242,
        username="u",
        track_id="9_876543",
        action="download_to_bot",
    )
    r = analytics_db.backfill_user_bot_audio_delivered_from_history(force=True)
    assert not r.get("skipped")
    assert r.get("sqlite_changes", 0) >= 1
    ids = analytics_db.get_bot_audio_delivered_track_ids(4242)
    assert "9_876543" in ids
    assert analytics_db.get_bot_audio_delivered_verified_live_track_ids(4242) == []


def test_record_bot_audio_delivered_sets_verified_live(temp_db):
    import analytics_db

    analytics_db.init_db()
    analytics_db.record_bot_audio_delivered(9001, "3_999")
    assert "3_999" in analytics_db.get_bot_audio_delivered_track_ids(9001)
    assert "3_999" in analytics_db.get_bot_audio_delivered_verified_live_track_ids(9001)


def test_backfill_ubad_from_button_click_extra_track_id(temp_db):
    import analytics_db

    analytics_db.init_db()
    analytics_db.log_button_click(
        telegram_user_id=5151,
        username="u2",
        button_id="button_download",
        context="player",
        extra={"track_id": "1_222333"},
    )
    r = analytics_db.backfill_user_bot_audio_delivered_from_history(force=True)
    assert not r.get("skipped")
    ids = analytics_db.get_bot_audio_delivered_track_ids(5151)
    assert "1_222333" in ids
    assert analytics_db.get_bot_audio_delivered_verified_live_track_ids(5151) == []


def test_backfill_ubad_skips_second_run_without_force(temp_db):
    import analytics_db

    analytics_db.init_db()
    r1 = analytics_db.backfill_user_bot_audio_delivered_from_history(force=True)
    assert not r1.get("skipped")
    r2 = analytics_db.backfill_user_bot_audio_delivered_from_history(force=False)
    assert r2.get("skipped") is True
