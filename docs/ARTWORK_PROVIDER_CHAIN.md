# Artwork Provider Chain

## Why

Некоторые VK токены/профили User-Agent отдают урезанные `audio`-объекты без `album/thumb/covers`.
В этом режиме `audio.search` и `audio.getById` дают `cover_url=null`, даже если аудио URL резолвится.

## Provider order

1. `vk_api` (основной путь): обложка из `audio.search/getById`.
2. `external_itunes` (fallback): быстрый поиск без API key.
3. `external_deezer` (fallback): открытый API без ключа.
4. `spotify_api` -> `spotify_page`.
5. `lastfm_api` -> `lastfm_page`.
6. `bandcamp` (web parsing).
7. `soundcloud` (web parsing).
8. `yandex_music` (web parsing).
9. `boom` (web parsing).
10. `vk_mobile_web` (`m.vk.com` search parsing).
11. `vk_web_music` (`vk.com` search parsing).
12. `coverartarchive` (MusicBrainz + CAA).

## Config flags

- `SEARCH_ARTWORK_FALLBACK_ENABLED=1` - включить fallback-цепочку.
- `SEARCH_ARTWORK_FALLBACK_MAX=10` - максимум треков на запрос, для которых будет fallback.
- `SEARCH_ARTWORK_FALLBACK_CACHE_MAX=4000` - размер LRU-кэша.
- `SEARCH_ARTWORK_AGGRESSIVE_MODE=1` - перебираем источники до результата или конца цепочки.
- `SEARCH_ARTWORK_MIN_CONFIDENCE=70` - порог confidence матчинга по `artist+title`.
- `LASTFM_API_KEY=...` - ключ для Last.fm.
- `SPOTIFY_BEARER_TOKEN=...` - Bearer токен Spotify Web API.

## Observability

`/api/admin/tokens` и `/api/admin/stats/overview` теперь включают `cache.artwork_providers`:

- `requests`, `tracks_examined`, `tracks_enriched`
- `cache_hit`, `cache_miss`, `cache_size`
- `provider_hit`, `provider_error`
- `providers_enabled`

## Staged rollout

1. Stage: `SEARCH_ARTWORK_FALLBACK_ENABLED=1`, `..._MAX=5`.
2. Verify via `scripts/artwork_source_probe.py` and admin metrics.
3. Increase `..._MAX` to 10-15 if p95 search latency remains stable.
4. При необходимости повышать/понижать `SEARCH_ARTWORK_MIN_CONFIDENCE`.
5. Для снижения нагрузки использовать `SEARCH_ARTWORK_STAGE_THRESHOLD_PCT`:
   если после VK + Redis-cache покрытие уже выше порога, внешние источники не запускаются.

## Rollback

Быстрый откат без релиза:

- `SEARCH_ARTWORK_FALLBACK_ENABLED=0` (полностью выключает внешние провайдеры).
- Для более консервативного режима: увеличить `SEARCH_ARTWORK_MIN_CONFIDENCE`.
- Для снижения нагрузки: уменьшить `SEARCH_ARTWORK_FALLBACK_MAX` и/или увеличить `SEARCH_ARTWORK_STAGE_THRESHOLD_PCT`.

