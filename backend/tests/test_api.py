"""
API tests for server_lite: auth, search, resolve, admin.
VK/Redis are mocked so tests run without network.
"""
import json
import os
import time
import pytest
from unittest.mock import AsyncMock, patch
from fastapi.testclient import TestClient

# Import after conftest has set env and mocked shutil.which
import server_lite


@pytest.fixture
def client():
    return TestClient(server_lite.app)


@pytest.fixture
def admin_key():
    return os.environ.get("ANALYTICS_ADMIN_KEY", "test_admin_key_pytest_789")


def test_admin_bot_incoming_forbidden_without_key(client):
    r = client.get("/api/admin/bot-incoming")
    assert r.status_code == 403


def test_admin_bot_incoming_list_ok_with_key(client, admin_key):
    r = client.get(f"/api/admin/bot-incoming?key={admin_key}&limit=5")
    assert r.status_code == 200
    data = r.json()
    assert "items" in data
    assert isinstance(data["items"], list)


# ─── Auth ─────────────────────────────────────────────────────────

def test_login_missing_init_data(client):
    r = client.post("/api/auth/login", json={"initData": ""})
    assert r.status_code == 400
    assert "initData" in r.text or "Missing" in r.text


def test_login_invalid_init_data(client):
    r = client.post("/api/auth/login", json={"initData": "invalid"})
    assert r.status_code == 401
    assert "Invalid" in r.text or "expired" in r.text or "401" in r.text


def test_auth_logout_ok(client):
    r = client.post("/api/auth/logout")
    assert r.status_code == 200
    assert r.json().get("ok") is True


def test_auth_telegram_missing_id_token(client):
    r = client.post("/api/auth/telegram", json={})
    assert r.status_code == 400
    assert "id_token" in r.text or "Missing" in r.text


def _telegram_webhook_start_payload():
    return {
        "update_id": 91001,
        "message": {
            "message_id": 1,
            "chat": {"id": 424242, "type": "private"},
            "from": {"id": 424242, "is_bot": False, "first_name": "Test"},
            "text": "/start",
        },
    }


def test_telegram_webhook_post_returns_200_and_runs_start_handler(client, monkeypatch):
    """Регрессия: /start должен ставить фоновую задачу _handle_telegram_update (см. /api/telegram-webhook)."""
    calls = []

    async def capture(update):
        calls.append(update)

    monkeypatch.setattr(server_lite, "_handle_telegram_update", capture)
    r = client.post("/api/telegram-webhook", json=_telegram_webhook_start_payload())
    assert r.status_code == 200
    assert len(calls) == 1
    assert calls[0]["message"]["text"] == "/start"


def test_telegram_webhook_playlist_runs_handler(client, monkeypatch):
    calls = []

    async def capture(update):
        calls.append(update)

    monkeypatch.setattr(server_lite, "_handle_telegram_update", capture)
    body = _telegram_webhook_start_payload()
    body["message"]["text"] = "/playlist"
    r = client.post("/api/telegram-webhook", json=body)
    assert r.status_code == 200
    assert len(calls) == 1


def test_telegram_webhook_inline_query_skips_message_handler(client, monkeypatch):
    calls_update = []
    calls_inline = []

    async def cap_u(update):
        calls_update.append(update)

    async def cap_i(update):
        calls_inline.append(update)

    monkeypatch.setattr(server_lite, "_handle_telegram_update", cap_u)
    monkeypatch.setattr(server_lite, "_handle_inline_query", cap_i)
    body = {"update_id": 91002, "inline_query": {"id": "iq1", "from": {"id": 1}, "query": "x"}}
    r = client.post("/api/telegram-webhook", json=body)
    assert r.status_code == 200
    assert len(calls_update) == 0
    assert len(calls_inline) == 1


def test_telegram_webhook_secret_token_enforced(client, monkeypatch):
    monkeypatch.setattr(server_lite, "TELEGRAM_WEBHOOK_SECRET", "expected-secret")
    r = client.post("/api/telegram-webhook", json=_telegram_webhook_start_payload())
    assert r.status_code == 403
    r2 = client.post(
        "/api/telegram-webhook",
        json=_telegram_webhook_start_payload(),
        headers={"X-Telegram-Bot-Api-Secret-Token": "expected-secret"},
    )
    assert r2.status_code == 200


def test_telegram_webhook_users_shared_routes_to_shared_handler_only(client, monkeypatch):
    calls_u = []
    calls_s = []

    async def cap_u(update):
        calls_u.append(update)

    async def cap_s(update):
        calls_s.append(update)

    monkeypatch.setattr(server_lite, "_handle_telegram_update", cap_u)
    monkeypatch.setattr(server_lite, "_handle_users_shared", cap_s)
    body = {
        "update_id": 91003,
        "message": {
            "chat": {"id": 1},
            "from": {"id": 1},
            "users_shared": {"users": [{"user_id": 2}], "request_id": 0},
        },
    }
    r = client.post("/api/telegram-webhook", json=body)
    assert r.status_code == 200
    assert len(calls_u) == 0
    assert len(calls_s) == 1


def test_auth_telegram_not_configured(client):
    with patch.object(server_lite, "TELEGRAM_OAUTH_CLIENT_ID", ""):
        with patch.object(server_lite, "TGPLAY_WEB_SESSION_SECRET", ""):
            r = client.post("/api/auth/telegram", json={"id_token": "x.y.z"})
            assert r.status_code == 503


@patch.object(server_lite, "verify_telegram_oidc_id_token")
def test_auth_telegram_ok(mock_verify, client):
    now = int(time.time())
    mock_verify.return_value = {
        "iss": "https://oauth.telegram.org",
        "sub": "999888777001",
        "id": 999888777,
        "name": "Test User",
        "preferred_username": "testuser",
        "aud": "123456789",
        "iat": now,
        "exp": now + 3600,
    }
    r = client.post("/api/auth/telegram", json={"id_token": "header.payload.sig"})
    assert r.status_code == 200
    data = r.json()
    assert data.get("token_type") == "Bearer"
    assert "access_token" in data
    assert data["user"]["id"] == 999888777
    assert data["user"]["first_name"] == "Test"


@patch.object(server_lite, "verify_telegram_oidc_id_token")
def test_auth_telegram_nonce_ok(mock_verify, client):
    now = int(time.time())
    mock_verify.return_value = {
        "iss": "https://oauth.telegram.org",
        "sub": "999888777002",
        "id": 999888778,
        "name": "Nonce User",
        "preferred_username": "nonceuser",
        "aud": "123456789",
        "iat": now,
        "exp": now + 3600,
        "nonce": "same-nonce-abc",
    }
    r = client.post(
        "/api/auth/telegram",
        json={"id_token": "header.payload.sig", "nonce": "same-nonce-abc"},
    )
    assert r.status_code == 200
    assert r.json().get("token_type") == "Bearer"


@patch.object(server_lite, "verify_telegram_oidc_id_token")
def test_auth_telegram_nonce_mismatch(mock_verify, client):
    now = int(time.time())
    mock_verify.return_value = {
        "iss": "https://oauth.telegram.org",
        "sub": "1",
        "id": 1,
        "name": "X",
        "aud": "123456789",
        "iat": now,
        "exp": now + 3600,
        "nonce": "from-token",
    }
    r = client.post("/api/auth/telegram", json={"id_token": "dummy", "nonce": "from-client"})
    assert r.status_code == 401


@patch.object(server_lite, "verify_telegram_oidc_id_token")
def test_auth_telegram_nonce_missing_in_token_allowed(mock_verify, client):
    """Клиент прислал nonce, в JWT нет claim nonce — вход не блокируем (как в рекомендации docs, не «обязательно»)."""
    now = int(time.time())
    mock_verify.return_value = {
        "iss": "https://oauth.telegram.org",
        "sub": "2",
        "id": 2,
        "name": "Y",
        "aud": "123456789",
        "iat": now,
        "exp": now + 3600,
    }
    r = client.post("/api/auth/telegram", json={"id_token": "dummy", "nonce": "client-wants"})
    assert r.status_code == 200
    assert r.json().get("token_type") == "Bearer"
    assert r.json()["user"]["id"] == 2


def test_auth_telegram_code_missing_fields(client):
    r = client.post("/api/auth/telegram/code", json={"code": "x"})
    assert r.status_code == 400


def test_auth_telegram_code_requires_secret(client):
    with patch.object(server_lite, "TELEGRAM_OAUTH_CLIENT_SECRET", ""):
        r = client.post(
            "/api/auth/telegram/code",
            json={
                "code": "abc",
                "redirect_uri": "https://tgplay.fun/auth/telegram/callback",
                "code_verifier": "x" * 43,
            },
        )
        assert r.status_code == 503


@patch.object(server_lite, "get_session")
@patch.object(server_lite, "verify_telegram_oidc_id_token")
def test_auth_telegram_code_ok(mock_verify, mock_get_session, client):
    now = int(time.time())
    mock_verify.return_value = {
        "iss": "https://oauth.telegram.org",
        "sub": "111",
        "id": 111222333,
        "name": "Code Flow",
        "preferred_username": "codeuser",
        "aud": "123456789",
        "iat": now,
        "exp": now + 3600,
    }

    class FakeResp:
        status = 200

        async def text(self):
            return json.dumps({"id_token": "a.b.c", "access_token": "fromtg"})

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

    class FakeSession:
        def post(self, url, data=None, headers=None, timeout=None):
            return FakeResp()

    async def fake_get():
        return FakeSession()

    mock_get_session.side_effect = fake_get

    r = client.post(
        "/api/auth/telegram/code",
        json={
            "code": "authcode",
            "redirect_uri": "https://example.com/auth/telegram/callback",
            "code_verifier": "v" * 43,
        },
    )
    assert r.status_code == 200
    data = r.json()
    assert data["token_type"] == "Bearer"
    assert data["user"]["id"] == 111222333


@patch.object(server_lite, "verify_telegram_oidc_id_token")
def test_playlist_with_bearer_web_session(mock_verify, client):
    now = int(time.time())
    mock_verify.return_value = {
        "iss": "https://oauth.telegram.org",
        "sub": "42",
        "id": 42424242,
        "name": "Bearer Test",
        "aud": "123456789",
        "iat": now,
        "exp": now + 3600,
    }
    r = client.post("/api/auth/telegram", json={"id_token": "dummy"})
    assert r.status_code == 200
    token = r.json()["access_token"]
    r2 = client.get("/api/playlist", headers={"Authorization": f"Bearer {token}"})
    assert r2.status_code == 200
    body = r2.json()
    assert "items" in body


def test_playlist_invalid_bearer(client):
    r = client.get("/api/playlist", headers={"Authorization": "Bearer not.a.valid.jwt"})
    assert r.status_code == 401


@patch.object(server_lite, "verify_telegram_oidc_id_token")
def test_playlist_post_accepts_youtube_video_id(mock_verify, client):
    """После YT-fallback id трека — 11 символов; избранное не должно отвечать 400."""
    now = int(time.time())
    mock_verify.return_value = {
        "iss": "https://oauth.telegram.org",
        "sub": "99",
        "id": 99999099,
        "name": "YT Fav",
        "aud": "123456789",
        "iat": now,
        "exp": now + 3600,
    }
    r = client.post("/api/auth/telegram", json={"id_token": "dummy"})
    assert r.status_code == 200
    token = r.json()["access_token"]
    # Уникальный 11-симв. id (повторный прогон тестов оставляет данные в user_data).
    yt_id = ("u" + f"{int(time.time() * 1000) % 10**10:010d}")[:11]
    r2 = client.post(
        "/api/playlist",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "id": yt_id,
            "title": "Never Gonna Give You Up",
            "artist": "Rick Astley",
            "duration": 212,
            "vk_legacy": False,
        },
    )
    assert r2.status_code == 200, r2.text
    assert r2.json().get("status") == "saved"
    r3 = client.get("/api/playlist", headers={"Authorization": f"Bearer {token}"})
    assert r3.status_code == 200
    ids = [x["id"] for x in r3.json().get("items", [])]
    assert yt_id in ids


@patch.object(server_lite, "verify_telegram_oidc_id_token")
def test_playlist_post_rejects_non_vk_non_youtube_id(mock_verify, client):
    now = int(time.time())
    mock_verify.return_value = {
        "iss": "https://oauth.telegram.org",
        "sub": "100",
        "id": 100000100,
        "name": "Bad Id",
        "aud": "123456789",
        "iat": now,
        "exp": now + 3600,
    }
    r = client.post("/api/auth/telegram", json={"id_token": "dummy"})
    assert r.status_code == 200
    token = r.json()["access_token"]
    r2 = client.post(
        "/api/playlist",
        headers={"Authorization": f"Bearer {token}"},
        json={"id": "not_a_valid_id", "title": "x", "artist": "y", "duration": 1},
    )
    assert r2.status_code == 400


def test_favorite_track_ids_ordered_includes_youtube():
    """Рекомендации/волна: якоря из избранного не должны терять YouTube id."""
    fav = [
        {"id": "1_2", "title": "a", "artist": "b"},
        {"id": "dQw4w9WgXcQ", "title": "Yt", "artist": "Artist"},
    ]
    out = server_lite._favorite_track_ids_ordered(fav)
    assert "1_2" in out and "dQw4w9WgXcQ" in out


def test_me_photo_requires_auth(client):
    r = client.get("/api/me/photo")
    assert r.status_code == 401


def test_me_dislike_requires_auth(client):
    r = client.post("/api/me/dislike", json={"track_id": "123_456"})
    assert r.status_code == 401


def test_me_bot_audio_delivered_requires_auth(client):
    r = client.get("/api/me/bot-audio-delivered")
    assert r.status_code == 401


@patch.object(server_lite, "verify_telegram_oidc_id_token")
def test_me_bot_audio_delivered_empty_with_bearer(mock_verify, client):
    now = int(time.time())
    mock_verify.return_value = {
        "iss": "https://oauth.telegram.org",
        "sub": "77",
        "id": 77777007,
        "name": "DL Test",
        "aud": "123456789",
        "iat": now,
        "exp": now + 3600,
    }
    r = client.post("/api/auth/telegram", json={"id_token": "dummy"})
    assert r.status_code == 200
    token = r.json()["access_token"]
    r2 = client.get("/api/me/bot-audio-delivered", headers={"Authorization": f"Bearer {token}"})
    assert r2.status_code == 200
    body = r2.json()
    assert body.get("track_ids") == []
    assert body.get("verified_live_track_ids") == []


@patch("analytics_db.get_bot_audio_delivered_verified_live_track_ids", return_value=["9_1", "9_2"])
@patch("analytics_db.get_bot_audio_delivered_track_ids", return_value=[])
@patch.object(server_lite, "verify_telegram_oidc_id_token")
def test_me_bot_audio_delivered_merges_verified_into_track_ids(
    mock_verify,
    _mock_track_ids,
    _mock_verified,
    client,
):
    """Старые verified_live могли не попадать в track_ids из‑за LIMIT — ответ API всё равно должен содержать их."""
    now = int(time.time())
    mock_verify.return_value = {
        "iss": "https://oauth.telegram.org",
        "sub": "88",
        "id": 88888008,
        "name": "Merge Test",
        "aud": "123456789",
        "iat": now,
        "exp": now + 3600,
    }
    r = client.post("/api/auth/telegram", json={"id_token": "dummy"})
    assert r.status_code == 200
    token = r.json()["access_token"]
    r2 = client.get("/api/me/bot-audio-delivered", headers={"Authorization": f"Bearer {token}"})
    assert r2.status_code == 200
    body = r2.json()
    assert body.get("verified_live_track_ids") == ["9_1", "9_2"]
    assert body.get("track_ids") == ["9_1", "9_2"]


# ─── Admin ────────────────────────────────────────────────────────

def test_admin_overview_forbidden_without_key(client):
    r = client.get("/api/admin/stats/overview")
    assert r.status_code == 403


def test_admin_overview_forbidden_wrong_key(client):
    r = client.get("/api/admin/stats/overview", params={"key": "wrong_key"})
    assert r.status_code == 403


def test_admin_overview_ok_with_key(client, admin_key):
    if not admin_key:
        pytest.skip("ANALYTICS_ADMIN_KEY not set")
    r = client.get("/api/admin/stats/overview", params={"key": admin_key})
    assert r.status_code == 200
    data = r.json()
    assert "unique_users" in data or "visits" in data or "by_button" in data


# ─── Search ───────────────────────────────────────────────────────

def test_search_query_too_short(client):
    r = client.get("/api/music/search", params={"q": "ab", "limit": 50})
    assert r.status_code == 400
    assert "short" in r.text.lower() or "3" in r.text


def test_rec_apply_artist_feature_caps_solo_two_feat_three():
    solo = [
        {"id": "1", "artist": "Alpha", "title": "a1"},
        {"id": "2", "artist": "Alpha", "title": "a2"},
        {"id": "3", "artist": "Alpha", "title": "a3"},
        {"id": "4", "artist": "Beta", "title": "b1"},
    ]
    capped = server_lite._rec_apply_artist_feature_caps(solo)
    assert len(capped) == 3
    assert [t["id"] for t in capped] == ["1", "2", "4"]
    feat_mix = [
        {"id": "10", "artist": "Gamma feat Delta", "title": "g1"},
        {"id": "11", "artist": "Delta", "title": "d1"},
        {"id": "12", "artist": "Gamma", "title": "g2"},
        {"id": "13", "artist": "Gamma", "title": "g3"},
        {"id": "14", "artist": "Gamma", "title": "g4"},
    ]
    capped2 = server_lite._rec_apply_artist_feature_caps(feat_mix)
    assert len(capped2) == 3
    assert [t["id"] for t in capped2] == ["10", "11", "12"]


@patch.object(server_lite, "_vk_enrich_tracks_album_covers_via_get_by_id", new_callable=AsyncMock)
@patch.object(server_lite, "vk_audio_search", new_callable=AsyncMock)
def test_search_ok_mocked(mock_vk_search, _mock_enrich_covers, client):
    """HTTP-поиск зовёт vk_audio_search → _vk_search_for_http; мок без реального VK."""
    mock_vk_search.return_value = [
        {"id": "123_456", "title": "Test Track", "artist": "Test Artist", "duration": 180, "cover_url": None},
    ]
    r = client.get("/api/music/search", params={"q": "test query", "limit": 50})
    assert r.status_code == 200
    data = r.json()
    assert "items" in data
    assert len(data["items"]) == 1
    assert data["items"][0]["id"] == "123_456"


@patch.object(server_lite, "_vk_enrich_tracks_album_covers_via_get_by_id", new_callable=AsyncMock)
@patch.object(server_lite, "_vk_search_artist_catalog", new_callable=AsyncMock)
def test_search_artist_catalog_mocked(mock_cat, _mock_enrich, client):
    """Каталог исполнителя: несколько страниц VK объединяются на бэкенде."""
    mock_cat.return_value = [
        {"owner_id": -1, "id": 1, "title": "Hit", "artist": "Star Name", "duration": 200},
        {"owner_id": -1, "id": 2, "title": "Rare", "artist": "Star Name", "duration": 100},
    ]
    r = client.get(
        "/api/music/search",
        params={"q": "Star Name", "artist_catalog": 1, "limit": 100},
    )
    assert r.status_code == 200
    data = r.json()
    assert len(data["items"]) == 2
    assert data["items"][0]["title"] == "Hit"


@patch.object(server_lite, "_vk_enrich_tracks_album_covers_via_get_by_id", new_callable=AsyncMock)
@patch.object(server_lite, "_vk_search_artist_catalog", new_callable=AsyncMock)
def test_search_artist_catalog_min_two_chars(mock_cat, _mock_enrich, client):
    mock_cat.return_value = []
    r = client.get("/api/music/search", params={"q": "OK", "artist_catalog": 1, "limit": 10})
    assert r.status_code == 200


@patch.object(server_lite, "_vk_enrich_tracks_album_covers_via_get_by_id", new_callable=AsyncMock)
@patch.object(server_lite, "_vk_search_artist_catalog", new_callable=AsyncMock)
def test_search_artist_catalog_limit_600_no_422(mock_cat, _mock_enrich, client):
    """Раньше limit>300 давал 422 до входа в обработчик."""
    mock_cat.return_value = []
    r = client.get(
        "/api/music/search",
        params={"q": "Some Artist", "artist_catalog": 1, "limit": 600},
    )
    assert r.status_code == 200


# ─── Resolve ──────────────────────────────────────────────────────

def test_resolve_invalid_track_id(client):
    r = client.get("/api/music/resolve/invalid")
    assert r.status_code == 400
    assert "Invalid" in r.text or "format" in r.text.lower()


@patch.object(server_lite, "vk_get_audio_url", new_callable=AsyncMock)
def test_resolve_not_found(mock_vk_url, client):
    mock_vk_url.return_value = None
    r = client.get("/api/music/resolve/123_456")
    assert r.status_code == 404


@patch.object(server_lite, "vk_get_audio_url", new_callable=AsyncMock)
def test_resolve_ok(mock_vk_url, client):
    mock_vk_url.return_value = "https://example.com/audio.mp3"
    r = client.get("/api/music/resolve/123_456")
    assert r.status_code == 200
    data = r.json()
    assert data.get("url") == "https://example.com/audio.mp3"
    assert "hls" in data


@patch.object(server_lite, "_redis_get_vk_yt_fallback_video_id", new_callable=AsyncMock)
@patch.object(server_lite, "vk_get_audio_url", new_callable=AsyncMock)
def test_resolve_legacy_favorites_redis_youtube_skips_vk(mock_vk_url, mock_redis_yt, client):
    """Кэш vk_yt_fb: без query тоже сразу YouTube, без вызова VK (как до строгого gating)."""
    mock_redis_yt.return_value = "dQw4w9WgXcQ"
    mock_vk_url.return_value = "https://vk.example/audio.mp3"
    r = client.get("/api/music/resolve/123_456")
    assert r.status_code == 200
    data = r.json()
    assert data.get("url") == "/api/music/youtube-direct/dQw4w9WgXcQ"
    mock_vk_url.assert_not_called()


@patch.object(server_lite, "vk_batch_get_audio_urls", new_callable=AsyncMock)
@patch.object(server_lite, "_redis_get_vk_yt_fallback_video_id", new_callable=AsyncMock)
def test_resolve_batch_redis_youtube_skips_vk(mock_redis_yt, mock_vk_batch, client):
    """resolve-batch совпадает с одиночным resolve: vk_yt_fb → youtube-direct, без vk_batch_get_audio_urls."""
    mock_redis_yt.return_value = "dQw4w9WgXcQ"
    mock_vk_batch.return_value = {"123_456": "https://vk.example/wrong.mp3"}
    r = client.post("/api/music/resolve-batch", json={"ids": ["123_456"]})
    assert r.status_code == 200
    data = r.json()
    assert data["123_456"]["url"] == "/api/music/youtube-direct/dQw4w9WgXcQ"
    assert data["123_456"]["hls"] is False
    mock_vk_batch.assert_not_called()


def test_vk_batch_get_audio_urls_retries_like_single_resolve(monkeypatch):
    """Регрессия: батч (prewarm избранного) не должен ставить negative до пути с access_key из Redis meta."""
    import asyncio

    calls: list[list[str]] = []

    async def fake_batch(ids: list[str]):
        calls.append(list(ids))
        if any("akxx" in x for x in ids):
            return [{"owner_id": 1, "id": 2, "url": "https://vk.cdn/track.mp3"}]
        return [{"owner_id": 1, "id": 2}]

    monkeypatch.setattr(server_lite, "_vk_batch_get_by_id", fake_batch)
    monkeypatch.setattr(server_lite, "_redis_get_track_negative", AsyncMock(return_value=False))
    monkeypatch.setattr(server_lite, "_redis_get_track_source", AsyncMock(return_value=None))
    monkeypatch.setattr(server_lite, "_url_cache_fallback_get", lambda _canon: None)

    async def fake_meta(canon: str):
        if canon == "1_2":
            return {"access_key": "akxx"}
        return None

    monkeypatch.setattr(server_lite, "_redis_get_track_meta", fake_meta)
    monkeypatch.setattr(server_lite, "_redis_set_track_source", AsyncMock())
    monkeypatch.setattr(server_lite, "_redis_set_track_negative", AsyncMock())
    monkeypatch.setattr(server_lite, "_redis_delete_track_negative", AsyncMock())
    monkeypatch.setattr(server_lite, "_url_cache_fallback_set", lambda *_a, **_k: None)
    monkeypatch.setattr(server_lite, "get_redis", AsyncMock(return_value=None))

    out = asyncio.run(server_lite.vk_batch_get_audio_urls(["1_2"]))
    assert out.get("1_2") == "https://vk.cdn/track.mp3"
    assert len(calls) >= 2
    assert any("akxx" in x for c in calls for x in c)


@patch.object(server_lite, "search_youtube_tracks")
@patch.object(server_lite, "_redis_get_vk_yt_fallback_video_id", new_callable=AsyncMock)
@patch.object(server_lite, "vk_get_audio_url", new_callable=AsyncMock)
def test_resolve_legacy_vk_youtube_fallback(mock_vk_url, mock_redis_fb, mock_yt_search, client):
    """Старые VK id: без URL от VK, но с title/artist — подбор через YouTube Music."""
    mock_redis_fb.return_value = None
    mock_vk_url.return_value = None
    mock_yt_search.return_value = [
        {
            "id": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "title": "Never Gonna Give You Up",
            "artist": "Rick Astley",
            "duration": 212,
            "cover_url": None,
        },
    ]
    r = client.get(
        "/api/music/resolve/123_456",
        params={"title": "Never Gonna Give You Up", "artist": "Rick Astley"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data.get("url") == "/api/music/youtube-direct/dQw4w9WgXcQ"
    assert data.get("hls") is False


# ─── Status ───────────────────────────────────────────────────────

def test_status(client):
    r = client.get("/api/status")
    assert r.status_code == 200
    data = r.json()
    assert data.get("status") == "online"


# ─── Recommendations ──────────────────────────────────────────────

def test_recommendations_invalid_seed(client):
    r = client.get("/api/music/recommendations", params={"seed": "bad", "limit": 10})
    assert r.status_code == 400


def test_recommendations_missing_seed(client):
    r = client.get("/api/music/recommendations", params={"limit": 10})
    assert r.status_code == 400


def test_recommendations_personal_requires_auth(client):
    r = client.get("/api/music/recommendations/personal", params={"limit": 10})
    assert r.status_code == 401


@patch.object(server_lite, "_rec_get_recent_served_ids", new_callable=AsyncMock, return_value=[])
@patch.object(server_lite, "get_user_from_header", return_value={"id": 424242})
@patch.object(server_lite, "_rec_ensure_popular_parsed", new_callable=AsyncMock, return_value=[])
@patch.object(server_lite, "_vk_api_call", new_callable=AsyncMock)
@patch.object(server_lite, "search_youtube_tracks")
def test_recommendations_personal_youtube_when_vk_empty(
    mock_yt, mock_vk, _mock_pop, _mock_user, _mock_served, client, monkeypatch
):
    """Без выдачи VK: персональная лента собирается из YTM (cold start / якоря)."""
    import analytics_db

    monkeypatch.setattr(analytics_db, "get_recent_search_q_norms", lambda *a, **k: [])
    monkeypatch.setattr(analytics_db, "get_user_track_play_weights", lambda *a, **k: [])
    monkeypatch.setattr(analytics_db, "get_disliked_track_ids", lambda *a, **k: [])
    monkeypatch.setattr(analytics_db, "get_removed_library_track_ids", lambda *a, **k: [])
    monkeypatch.setattr(analytics_db, "get_rec_artist_show_penalties", lambda *a, **k: {})
    monkeypatch.setattr(analytics_db, "get_rec_genre_show_penalties", lambda *a, **k: {})
    monkeypatch.setattr(analytics_db, "count_user_library_tracks", lambda *a, **k: 1)
    monkeypatch.setattr(analytics_db, "get_collaborative_library_track_ids", lambda *a, **k: [])
    monkeypatch.setattr(server_lite, "load_playlist", lambda uid: [])
    monkeypatch.setattr(server_lite, "load_custom_playlists", lambda uid: [])

    async def vk_fail(*a, **k):
        return {"error": {"error_code": 3, "error_msg": "unknown method"}}

    mock_vk.side_effect = vk_fail
    mock_yt.side_effect = lambda q, limit=10: [
        {
            "id": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
            "title": "Never",
            "artist": "Rick",
            "duration": 212,
        }
    ] * max(1, min(6, limit))

    r = client.get(
        "/api/music/recommendations/personal",
        params={"limit": 8},
        headers={"Authorization": "tma stub"},
    )
    assert r.status_code == 200
    data = r.json()
    assert "items" in data
    assert len(data["items"]) >= 1
    assert "youtube.com" in str(data["items"][0].get("id") or "")
    src = data.get("source") or ""
    assert "youtube" in src


@patch.object(server_lite, "_rec_get_recent_served_ids", new_callable=AsyncMock, return_value=[])
@patch.object(server_lite, "get_user_from_header", return_value={"id": 424244})
@patch.object(server_lite, "_rec_ensure_popular_parsed", new_callable=AsyncMock, return_value=[])
@patch.object(server_lite, "_vk_api_call", new_callable=AsyncMock)
@patch.object(server_lite, "youtube_radio_tracks_from_video_id")
@patch.object(server_lite, "search_youtube_tracks")
def test_recommendations_personal_yt_radio_uses_last_favorite_youtube_seed(
    mock_yt, mock_radio, mock_vk, _mock_pop, _mock_user, _mock_served, client, monkeypatch
):
    """Сид радио YTM — последний в списке избранного YouTube-id; пул мержится в выдачу."""
    import analytics_db

    monkeypatch.setattr(analytics_db, "get_recent_search_q_norms", lambda *a, **k: [])
    monkeypatch.setattr(analytics_db, "get_user_track_play_weights", lambda *a, **k: [])
    monkeypatch.setattr(analytics_db, "get_disliked_track_ids", lambda *a, **k: [])
    monkeypatch.setattr(analytics_db, "get_removed_library_track_ids", lambda *a, **k: [])
    monkeypatch.setattr(analytics_db, "get_rec_artist_show_penalties", lambda *a, **k: {})
    monkeypatch.setattr(analytics_db, "get_rec_genre_show_penalties", lambda *a, **k: {})
    monkeypatch.setattr(analytics_db, "count_user_library_tracks", lambda *a, **k: 1)
    monkeypatch.setattr(analytics_db, "get_collaborative_library_track_ids", lambda *a, **k: [])
    monkeypatch.setattr(server_lite, "load_custom_playlists", lambda uid: [])
    monkeypatch.setattr(
        server_lite,
        "load_playlist",
        lambda uid: [
            {"id": "111_222", "title": "Older VK", "artist": "A"},
            {"id": "dQw4w9WgXcQ", "title": "Last YT fav", "artist": "Rick"},
        ],
    )

    async def vk_fail(*a, **k):
        return {"error": {"error_code": 3, "error_msg": "unknown method"}}

    mock_vk.side_effect = vk_fail
    mock_radio.return_value = [
        {
            "id": "https://www.youtube.com/watch?v=rAdioSeed11",
            "title": "Radio one",
            "artist": "Radio Artist",
            "duration": 180,
        },
    ]
    mock_yt.side_effect = lambda q, limit=10: [
        {
            "id": "https://www.youtube.com/watch?v=cOldOnly1",
            "title": "Cold",
            "artist": "Y",
            "duration": 200,
        }
    ] * max(1, min(6, limit))

    r = client.get(
        "/api/music/recommendations/personal",
        params={"limit": 8},
        headers={"Authorization": "tma stub"},
    )
    assert r.status_code == 200
    mock_radio.assert_called()
    args, _kw = mock_radio.call_args
    assert args[0] == "dQw4w9WgXcQ"
    assert args[1] == 55
    data = r.json()
    src = data.get("source") or ""
    assert "yt_radio_seed" in src
    ids_joined = " ".join(str(it.get("id") or "") for it in data.get("items") or [])
    assert "rAdioSeed11" in ids_joined


@patch.object(server_lite, "get_user_from_header", return_value={"id": 525252})
def test_recommendations_personal_soundcloud_seeds_last_five_and_excludes_favorites(
    _mock_user, client, monkeypatch
):
    """SC-ветка персональных: seed = последние 5 добавленных в избранное (новейшие первыми),
    уже добавленные треки не попадают в выдачу."""
    import analytics_db

    monkeypatch.setattr(server_lite, "_sc_ready", lambda: True)
    monkeypatch.setattr(server_lite, "_fire_cache_track_meta_items", lambda items: None)
    monkeypatch.setattr(server_lite, "load_custom_playlists", lambda uid: [])
    # один дизлайкнутый трек — не должен попасть в выдачу
    monkeypatch.setattr(analytics_db, "get_disliked_track_ids", lambda *a, **k: ["sc:9102"])

    # 7 избранных; append кладёт новые в конец, поэтому последние 5 — sc:102..sc:106.
    favs = [{"id": f"sc:{100 + i}", "title": f"T{i}", "artist": "A"} for i in range(7)]
    monkeypatch.setattr(server_lite, "load_playlist", lambda uid: favs)

    async def fake_session():
        return object()

    monkeypatch.setattr(server_lite, "get_session", fake_session)

    seen_seeds: list[int] = []

    async def fake_related(session, sid, *, limit):
        seen_seeds.append(int(sid))
        return [
            {"id": f"sc:{9000 + int(sid)}", "title": "rel", "artist": "R", "duration": 100},
            # один уже-избранный — должен быть отфильтрован из выдачи
            {"id": "sc:106", "title": "alreadyfav", "artist": "R", "duration": 100},
        ]

    monkeypatch.setattr(server_lite.SC_CLIENT, "related_tracks", fake_related)

    r = client.get(
        "/api/music/recommendations/personal",
        params={"limit": 50},
        headers={"Authorization": "tma stub"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data.get("source") == "soundcloud_personal_related"
    # seed = последние 5 добавленных (sc:102..sc:106), старые sc:100/sc:101 не используются
    assert sorted(seen_seeds) == [102, 103, 104, 105, 106]
    ids = [str(it.get("id")) for it in (data.get("items") or [])]
    assert "sc:106" not in ids  # уже в избранном — исключаем
    assert "sc:100" not in ids and "sc:101" not in ids  # сами seed тоже не дублируем
    assert "sc:9102" not in ids  # дизлайкнутый — исключаем (related для seed 102)
    assert len(ids) >= 1


def test_recommendations_personal_soundcloud_empty_without_favorites(client, monkeypatch):
    """SC-ветка: без избранного — пустая лента, без вызовов related."""
    monkeypatch.setattr(server_lite, "_sc_ready", lambda: True)
    monkeypatch.setattr(server_lite, "get_user_from_header", lambda *a, **k: {"id": 777})
    monkeypatch.setattr(server_lite, "load_playlist", lambda uid: [])
    monkeypatch.setattr(server_lite, "load_custom_playlists", lambda uid: [])
    r = client.get(
        "/api/music/recommendations/personal",
        params={"limit": 20},
        headers={"Authorization": "tma stub"},
    )
    assert r.status_code == 200
    data = r.json()
    assert data.get("source") == "soundcloud_personal_empty"
    assert data.get("items") == []


def test_search_soundcloud_multiword_keeps_recall(client, monkeypatch):
    """Регрессия «пасош каждый день → 0»: многословный SC-поиск НЕ должен терять треки
    (ранжируем, но не отбрасываем). Целевой трек обязан присутствовать в выдаче."""
    monkeypatch.setattr(server_lite, "_sc_ready", lambda: True)
    monkeypatch.setattr(server_lite, "_fire_cache_track_meta_items", lambda items: None)

    async def fake_session():
        return object()

    async def fake_redis():
        return None

    monkeypatch.setattr(server_lite, "get_session", fake_session)
    monkeypatch.setattr(server_lite, "get_redis", fake_redis)

    target = {"id": "sc:777", "artist": "пасош", "title": "каждый день", "duration": 200, "cover_url": None}
    noise = {"id": "sc:888", "artist": "other", "title": "song", "duration": 100, "cover_url": None}

    async def fake_search(session, query, *, limit, offset=0):
        # Возвращаем целевой трек на любой из под-запросов (имитация шумной выдачи SC).
        return [target, noise]

    async def fake_catalog(session, artist_query, *, limit):
        return []

    async def fake_resolve_user(session, artist_query):
        return None

    monkeypatch.setattr(server_lite.SC_CLIENT, "search_tracks", fake_search)
    monkeypatch.setattr(server_lite.SC_CLIENT, "artist_catalog_tracks", fake_catalog)
    monkeypatch.setattr(server_lite.SC_CLIENT, "resolve_artist_user_id", fake_resolve_user)

    r = client.get("/api/music/search", params={"q": "пасош каждый день", "limit": 50})
    assert r.status_code == 200
    data = r.json()
    ids = [str(it.get("id")) for it in (data.get("items") or [])]
    assert "sc:777" in ids, f"целевой трек потерян при многословном поиске: {ids}"


def test_token_pool_optional_without_vk_tokens(monkeypatch):
    """VK-токен больше не обязателен: пул стартует пустым, без SystemExit/exit(1)."""
    monkeypatch.delenv("VK_TOKEN", raising=False)
    monkeypatch.delenv("VK_TOKENS", raising=False)
    monkeypatch.delenv("VK_WORKER_URLS", raising=False)
    pool = server_lite.TokenPool()
    assert pool.count == 0


def test_vk_api_call_short_circuits_without_tokens(monkeypatch):
    """Без токенов VK API мгновенно отдаёт error (вызывающий код уходит в YouTube-фолбэк)."""
    import asyncio

    class _EmptyPool:
        count = 0

    monkeypatch.setattr(server_lite, "_token_pool", _EmptyPool())
    res = asyncio.run(server_lite._vk_api_call("audio.search", {"q": "x"}))
    assert isinstance(res, dict) and "error" in res


@patch.object(server_lite, "_vk_api_call", new_callable=AsyncMock)
def test_recommendations_ok_mocked(mock_vk, client):
    async def side(method, params, post=False):
        if method == "audio.getRecommendations":
            return {
                "response": {
                    "items": [
                        {
                            "owner_id": 1,
                            "id": 99,
                            "title": "Rec",
                            "artist": "A",
                            "duration": 120,
                        },
                    ]
                }
            }
        if method == "audio.getPopular":
            return {
                "response": {
                    "items": [
                        {
                            "owner_id": 2,
                            "id": 3,
                            "title": "Pop",
                            "artist": "B",
                            "duration": 60,
                        },
                    ]
                }
            }
        return {"error": {"error_code": 1, "error_msg": "unknown"}}

    mock_vk.side_effect = side
    r = client.get("/api/music/recommendations", params={"seed": "-100_555", "limit": 10})
    assert r.status_code == 200
    data = r.json()
    assert "items" in data
    assert data.get("source") == "vk"
    assert len(data["items"]) == 1
    assert data["items"][0]["id"] == "1_99"


@patch.object(server_lite, "_vk_api_call", new_callable=AsyncMock)
def test_recommendations_fallback_popular_when_rec_empty(mock_vk, client):
    async def side(method, params, post=False):
        if method == "audio.getRecommendations":
            return {"error": {"error_code": 100, "error_msg": "fail"}}
        if method == "audio.getPopular":
            return {
                "response": {
                    "items": [
                        {
                            "owner_id": 2,
                            "id": 3,
                            "title": "Pop",
                            "artist": "B",
                            "duration": 60,
                        },
                    ]
                }
            }
        return {"error": {"error_code": 1, "error_msg": "unknown"}}

    mock_vk.side_effect = side
    r = client.get("/api/music/recommendations", params={"seed": "-100_777", "limit": 10})
    assert r.status_code == 200
    data = r.json()
    assert data.get("source") == "popular_fallback"
    assert len(data["items"]) == 1
    assert data["items"][0]["id"] == "2_3"


@patch.object(server_lite, "_vk_api_call", new_callable=AsyncMock)
def test_recommendations_merged_two_seeds(mock_vk, client):
    n = {"c": 0}

    async def side(method, params, post=False):
        if method == "audio.getRecommendations":
            n["c"] += 1
            if n["c"] == 1:
                return {
                    "response": {
                        "items": [
                            {"owner_id": 1, "id": 10, "title": "A", "artist": "X", "duration": 60},
                        ]
                    }
                }
            return {
                "response": {
                    "items": [
                        {"owner_id": 1, "id": 20, "title": "B", "artist": "Y", "duration": 60},
                    ]
                }
            }
        if method == "audio.getPopular":
            return {"response": {"items": []}}
        return {"error": {"error_code": 1, "error_msg": "unknown"}}

    mock_vk.side_effect = side
    r = client.get(
        "/api/music/recommendations",
        params={"seeds": "-100_501,-100_502", "limit": 10},
    )
    assert r.status_code == 200
    data = r.json()
    assert data.get("source") in ("vk_merged", "vk_merged_mixed")
    ids = [x["id"] for x in data["items"]]
    assert "1_10" in ids
    assert "1_20" in ids


# ─── Recommendations anchor (unique artists) ───────────────────────

def test_rec_genre_filter_keeps_unknown_when_enabled():
    allowed = {2}
    tracks = [
        {"id": "a", "genre_id": 2},
        {"id": "b", "genre_id": 99},
        {"id": "c"},
    ]
    out = server_lite._rec_filter_tracks_by_genre_allowlist(tracks, allowed, keep_unknown=True)
    ids = {t["id"] for t in out}
    assert "a" in ids and "c" in ids
    assert "b" not in ids


def test_rec_anchor_slot_split_60_30_10():
    a, b, c = server_lite._rec_anchor_slot_split(10, 60, 30)
    assert (a, b, c) == (6, 3, 1)


def test_rec_anchor_ids_weighted_fav_custom_search_order():
    """6/3/1 слотов: сначала избранное, затем кастом, затем поиск."""
    main = [
        {"id": "m1", "artist": "A", "title": "1"},
        {"id": "m2", "artist": "B", "title": "2"},
        {"id": "m3", "artist": "E", "title": "5"},
    ]
    main_ids = ["m1", "m2", "m3"]
    custom = [{"id": "c1", "artist": "C", "title": "3"}]
    custom_ids = ["c1"]
    search = [{"id": "s1", "artist": "D", "title": "4"}]
    search_ids = ["s1"]
    r = server_lite._rec_anchor_ids_weighted_fav_custom_search(
        main, main_ids, custom, custom_ids, search, search_ids, 5, 60, 30
    )
    assert len(r) == 5
    assert set(r) == {"m1", "m2", "m3", "c1", "s1"}


def test_rec_anchor_track_ids_prefers_distinct_artists():
    """10 треков одного артиста дают один якорный id; порядок — хронологический среди выбранных."""
    favorites = [
        {"id": "o1", "artist": "Old", "title": "a"},
        {"id": "x1", "artist": "X", "title": "b"},
        {"id": "x2", "artist": "X", "title": "c"},
        {"id": "y1", "artist": "Y", "title": "d"},
    ]
    fav_ids = ["o1", "x1", "x2", "y1"]
    r = server_lite._rec_anchor_track_ids_by_recent_artists(favorites, fav_ids, 10)
    assert r == ["o1", "x2", "y1"]

    same = ["a1", "a2", "a3", "a4", "a5", "a6", "a7", "a8", "a9", "a10"]
    favs10 = [{"id": i, "artist": "Same", "title": i} for i in same]
    r2 = server_lite._rec_anchor_track_ids_by_recent_artists(favs10, same, 10)
    assert r2 == ["a10"]


def test_rec_collab_heuristic():
    assert server_lite._rec_collab_heuristic("Artist", "Song feat. Guest")
    assert server_lite._rec_collab_heuristic("A, B", "Joint")
    assert not server_lite._rec_collab_heuristic("Artist", "Plain title")


def test_rec_pick_five_vk_catalog_slice_three_two():
    raw = [
        {"owner_id": 1, "id": 1, "title": "One", "artist": "PopStar", "duration": 100},
        {"owner_id": 1, "id": 2, "title": "Two", "artist": "PopStar", "duration": 100},
        {"owner_id": 1, "id": 3, "title": "Three", "artist": "PopStar", "duration": 100},
        {"owner_id": 1, "id": 4, "title": "Feat track", "artist": "PopStar feat. Other", "duration": 100},
        {"owner_id": 1, "id": 5, "title": "Another", "artist": "PopStar ft. X", "duration": 100},
    ]
    out = server_lite._rec_pick_five_vk_catalog_slice(raw, "PopStar", set())
    assert len(out) == 5
    assert [t["id"] for t in out[:3]] == ["1_1", "1_2", "1_3"]
    assert "feat" in out[3]["title"].lower() or "feat" in (out[3].get("artist") or "").lower()


# ─── Captcha (VK error 14) ────────────────────────────────────────

def test_token_report_error_14_puts_token_in_cooldown():
    """When report_error(state, 14, ...) is called, the token goes into cooldown (cooldown_until in future)."""
    import time
    pool = server_lite._token_pool
    state = pool._states[0]
    assert state.healthy
    pool.report_error(state, 14, "captcha")
    assert not state.healthy
    assert state.cooldown_until > time.time()
    assert state.last_error_code == 14
