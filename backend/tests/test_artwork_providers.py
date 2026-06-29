from __future__ import annotations

import pytest

from artwork_providers import ArtworkResolver, ArtworkResolverConfig


@pytest.mark.asyncio
async def test_artwork_resolver_prefers_itunes_exact_match():
    resolver = ArtworkResolver(
        ArtworkResolverConfig(
            enabled=True,
            max_per_request=10,
            cache_max=100,
            aggressive_mode=True,
            min_confidence=50,
        )
    )

    async def fake_fetch(url: str):
        if "itunes.apple.com/search" in url:
            return {
                "results": [
                    {
                        "trackName": "Other Song",
                        "artistName": "Other Artist",
                        "artworkUrl100": "https://example.com/other.jpg",
                    },
                    {
                        "trackName": "Song A",
                        "artistName": "Artist A",
                        "artworkUrl100": "https://example.com/100x100bb.jpg",
                    },
                ]
            }, None
        if "api.deezer.com/search" in url:
            return {"data": []}, None
        return None, None

    tracks = [{"id": "1_1", "title": "Song A", "artist": "Artist A", "cover_url": None}]
    changed = await resolver.enrich_tracks(tracks, fake_fetch)
    assert changed == 1
    assert tracks[0]["cover_url"] == "https://example.com/1200x1200bb.jpg"
    metrics = resolver.metrics()
    assert metrics["provider_hit"].get("itunes", 0) >= 1


@pytest.mark.asyncio
async def test_artwork_resolver_uses_cache_and_negative_cache():
    resolver = ArtworkResolver(
        ArtworkResolverConfig(
            enabled=True,
            max_per_request=10,
            cache_max=100,
            min_confidence=70,
        )
    )
    calls = {"n": 0}

    async def fake_fetch(url: str):
        calls["n"] += 1
        return {"results": []}, None

    tracks1 = [{"id": "1_1", "title": "Song B", "artist": "Artist B", "cover_url": None}]
    changed1 = await resolver.enrich_tracks(tracks1, fake_fetch)
    assert changed1 == 0
    calls_after_first = calls["n"]
    assert calls_after_first > 0
    tracks2 = [{"id": "2_2", "title": "Song B", "artist": "Artist B", "cover_url": None}]
    changed2 = await resolver.enrich_tracks(tracks2, fake_fetch)
    assert changed2 == 0
    assert calls["n"] == calls_after_first
    metrics = resolver.metrics()
    assert metrics["cache_hit"] >= 1

