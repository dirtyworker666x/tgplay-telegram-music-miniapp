# Artwork Provider Research (April 2026)

## VK-native checks

Проверено на боевом окружении с текущим токеном и несколькими User-Agent профилями:

- `audio.search`
- `audio.getById`
- `audio.getById` с `extended=1`, `need_blocks=1`, `https=1`, `device_id`
- `execute`-варианты с инъекцией `https/device_id`
- API версии `5.131` и `5.199`

Результат: объекты `audio` приходят урезанными, без `album/thumb/covers/main_artists`.
Это подтверждено для Kate UA и VKAndroid UA.

## VK-web-derived checks

`m.vk.com` / web-derived parsing потенциально может дать обложки, но требует user-web session (`remixsid`) и отдельного потока авторизации.
Для production backend без пользовательских cookie этот путь не включён по умолчанию.

Вывод: путь возможен только как отдельный opt-in модуль (с юридической/продуктовой валидацией), не как default backend strategy.

## External fallback checks

Практически протестированные источники:

- Apple iTunes Search API: быстрый, стабильно доступный, без ключа.
- Deezer Search API: без ключа, хорошее покрытие по популярным трекам.
- MusicBrainz + CoverArtArchive: рабочий, но медленнее и хуже для fuzzy-matching.
- Spotify Search API: высокое качество обложек, но нужен Bearer token.
- Last.fm Track Search API: полезен для редких треков, лучше с API key.
- Bandcamp / SoundCloud / Yandex Music web search parsing: best-effort, не гарантирует стабильный парсинг.

## Production decision

Текущий рабочий компромисс:

- VK-first (если есть `cover_url` из VK, используем его).
- fallback provider-chain: iTunes -> Deezer -> Spotify -> Last.fm -> Bandcamp -> SoundCloud -> Yandex -> (опционально) CoverArtArchive.
- строгие лимиты по числу fallback-кандидатов + LRU cache + админ-метрики.

