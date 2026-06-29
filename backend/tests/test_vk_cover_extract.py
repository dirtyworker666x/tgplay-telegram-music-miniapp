"""Cover URL extraction from raw VK audio objects."""
import asyncio

import server_lite


def test_cover_from_album_thumb_photo_270():
    item = {
        "owner_id": 1,
        "id": 2,
        "title": "t",
        "artist": "a",
        "duration": 60,
        "album": {"thumb": {"photo_270": "https://sun9.userapi.com/impg/x.jpg"}},
    }
    assert server_lite._vk_audio_cover_url_from_item(item) == "https://sun9.userapi.com/impg/x.jpg"


def test_cover_from_album_thumb_photo_135():
    item = {
        "owner_id": 1,
        "id": 2,
        "title": "t",
        "artist": "a",
        "duration": 60,
        "album": {"thumb": {"photo_135": "https://example.com/a.jpg"}},
    }
    assert server_lite._vk_audio_cover_url_from_item(item) == "https://example.com/a.jpg"


def test_cover_from_album_thumb_as_list():
    item = {
        "owner_id": 1,
        "id": 2,
        "title": "t",
        "artist": "a",
        "duration": 1,
        "album": {"thumb": [{"width": 200, "url": "https://vk.com/from-list.jpg"}]},
    }
    assert server_lite._vk_audio_cover_url_from_item(item) == "https://vk.com/from-list.jpg"


def test_cover_from_album_cover_url_string():
    item = {
        "owner_id": 1,
        "id": 2,
        "title": "t",
        "artist": "a",
        "duration": 1,
        "album": {"cover_url": "https://vk.com/album-cover.jpg"},
    }
    assert server_lite._vk_audio_cover_url_from_item(item) == "https://vk.com/album-cover.jpg"


def test_cover_from_sizes_picks_largest():
    item = {
        "owner_id": 1,
        "id": 2,
        "title": "t",
        "artist": "a",
        "duration": 1,
        "album": {
            "thumb": {
                "sizes": [
                    {"width": 75, "url": "https://vk.com/small.jpg"},
                    {"width": 300, "url": "https://vk.com/large.jpg"},
                ]
            }
        },
    }
    assert server_lite._vk_audio_cover_url_from_item(item) == "https://vk.com/large.jpg"


def test_parse_tracks_skips_items_without_owner_or_audio_id():
    items = [
        {"title": "orphan"},
        {"owner_id": 1, "id": 2, "title": "ok", "artist": "a", "duration": 1},
    ]
    out = server_lite._parse_tracks(items)
    assert len(out) == 1
    assert out[0]["id"] == "1_2"


def test_parse_tracks_preserves_access_key():
    items = [
        {
            "owner_id": 10,
            "id": 20,
            "title": "Song",
            "artist": "Band",
            "duration": 100,
            "access_key": "sekret",
        }
    ]
    out = server_lite._parse_tracks(items)
    assert out[0]["id"] == "10_20"
    assert out[0]["access_key"] == "sekret"


def test_parse_tracks_includes_cover():
    items = [
        {
            "owner_id": 10,
            "id": 20,
            "title": "Song",
            "artist": "Band",
            "duration": 100,
            "album": {"thumb": {"photo_270": "https://cdn.example/cover.jpg"}},
        }
    ]
    out = server_lite._parse_tracks(items)
    assert len(out) == 1
    assert out[0]["cover_url"] == "https://cdn.example/cover.jpg"


def test_enrich_passes_access_key_in_audios_param(monkeypatch):
    captured: list = []

    async def fake_batch(ids):
        captured.extend(ids)
        return [
            {
                "owner_id": 1,
                "id": 2,
                "title": "x",
                "artist": "y",
                "duration": 1,
                "album": {"thumb": {"photo_270": "https://vk.example/with-key.jpg"}},
            }
        ]

    monkeypatch.setattr(server_lite, "_vk_batch_get_by_id", fake_batch)
    tracks = [{"id": "1_2", "title": "x", "access_key": "abc"}]
    asyncio.run(server_lite._vk_enrich_tracks_album_covers_via_get_by_id(tracks))
    assert captured == ["1_2_abc"]
    assert tracks[0]["cover_url"] == "https://vk.example/with-key.jpg"


def test_enrich_tracks_covers_via_getbyid(monkeypatch):
    async def fake_batch(ids):
        assert "10_20" in ids
        return [
            {
                "owner_id": 10,
                "id": 20,
                "title": "Song",
                "artist": "Band",
                "duration": 100,
                "album": {"thumb": {"photo_270": "https://vk.example/from-getbyid.jpg"}},
            }
        ]

    monkeypatch.setattr(server_lite, "_vk_batch_get_by_id", fake_batch)
    tracks = [{"id": "10_20", "title": "Song", "artist": "Band", "duration": 100}]
    asyncio.run(server_lite._vk_enrich_tracks_album_covers_via_get_by_id(tracks))
    assert tracks[0]["cover_url"] == "https://vk.example/from-getbyid.jpg"


def test_enrich_respects_max_tracks(monkeypatch):
    calls = []

    async def fake_batch(ids):
        calls.append(ids)
        return []

    monkeypatch.setattr(server_lite, "_vk_batch_get_by_id", fake_batch)
    tracks = [
        {"id": "1_1", "title": "a"},
        {"id": "2_2", "title": "b"},
        {"id": "3_3", "title": "c"},
    ]
    asyncio.run(server_lite._vk_enrich_tracks_album_covers_via_get_by_id(tracks, max_tracks=2))
    assert len(calls) == 1
    assert len(calls[0]) == 2
