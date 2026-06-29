# Архитектура TGPlay (актуальное состояние)

**Один источник правды** по лимитам, кэшу и VK-слою. Ориентируйся на этот документ и код, не на устаревшие заметки.

---

## Режимы работы с VK API

1. **Без воркеров** — бэкенд держит пул токенов (`VK_TOKEN` / `VK_TOKENS` в `.env`), сам ходит в `api.vk.com`. Лимит: **3 запроса/с на один исходящий IP** (token bucket внутри пула). Throttle (`VK_THROTTLE_DURATION`) по умолчанию **выключен** (= 0); токены в пуле дросселируют сами через token bucket + round-robin.

2. **С Redis-воркерами** — `VK_USE_REDIS_WORKERS=1`, `REDIS_URL` задан, запущены процессы `backend/vk_worker`. Бэкенд кладёт job в Redis (`vk:q:{token_id}`), воркер забирает через Lua (`claim_head.lua`), дёргает VK, пишет результат в `vk:res:{job_id}`. Лимиты и анти-буст **только в Redis** (Lua): 3 RPS, sliding window, freeze при ошибках. Один воркер = один ключ = один `WORKER_TOKEN_ID`. Масштабирование: до 50+ воркеров, stateless.

---

## Лимиты (текущие)

### VK и пул токенов

| Что | Значение (дефолт) | Env |
|-----|-------------------|-----|
| VK запросов/с на ключ (token bucket) | 3 | `VK_MAX_RPS_PER_TOKEN` |
| Глобальный RPS с сервера к VK (без воркеров) | 3 (при 7 токенах → 21) | `RATE_VK_GLOBAL_RPS` |
| Throttle при достижении RPS | **0 (выкл)** | `VK_THROTTLE_DURATION` |
| Глобальный почасовой лимит сервера к VK | 0 (выкл) | `RATE_VK_GLOBAL_PER_HOUR` |
| Одновременных запросов к бэкенду | 10 000 | `LIMIT_CONCURRENCY` |

> **Важно**: `VK_THROTTLE_DURATION` по умолчанию равен 0 — throttle выключен. Устанавливай `VK_THROTTLE_DURATION=1.5` в `.env` только если нужен дополнительный дросель поверх token bucket. В обычном режиме token bucket достаточен.

### Клиентские лимиты (per-user / per-IP)

Лимиты сначала ищут Telegram user_id из заголовка `Authorization: tma …`; если его нет — используют реальный IP клиента (из `X-Real-IP` / `X-Forwarded-For`, проставляемых nginx).

| Что | Значение (дефолт) | Env |
|-----|-------------------|-----|
| Почасовой лимит (resolve/download) | **500** в час на user_id или IP | `RATE_VK_PER_IP_PER_HOUR` |
| Дневной лимит поиска (бесплатный) | 50 live-поисков | `RATE_VK_PER_USER_PER_DAY` |
| Дневной лимит поиска (подписчик) | 100 live-поисков | `RATE_VK_PER_USER_SUB_PER_DAY` |

Подписчик определяется списком `SUBSCRIBER_TG_IDS`; Telegram Premium не влияет.

### Воспроизведение в Mini App (без `/api/music/pipe`)

Плеер **не** проксирует аудио через наш сервер: `resolve` → прямой URL VK CDN в `<audio>` / HLS.js; при ошибке — fallback `GET /api/music/download/{track_id}` (302 на MP3 или ffmpeg для HLS). Эндпоинта **`/api/music/pipe` нет и не добавлять** — прошлые попытки давали долгую буферизацию, обрывы на ~5 с, поломанный seek и лишнюю нагрузку на VPS.

| Что | Значение |
|-----|----------|
| Одновременных тяжёлых загрузок MP3 на сервере (кэш/отправка в Telegram и т.п.) | 5 (`_get_mp3_semaphore`) |
| Одновременных VK API-запросов (глобальный семафор) | `N_tokens × 3` |

**Прямой MP3 у VK**: браузер сам шлёт `Range` на CDN — нативный seek.

**HLS**: в плеере — поток с CDN через HLS.js; на сервере ffmpeg используется в `/api/music/download` и при подготовке файлов для Telegram, не как основной путь воспроизведения.

---

## Поиск (live-походы к VK)

- Лимитер считает **только живые запросы к VK**; ответы из кэша не лимитируются.
- Динамический лимит на пользователя:
  - `R_safe` — безопасный суммарный RPS по всем токенам.
  - `p_hit` — hit-rate поиска (кэш).
  - `U` — число активных пользователей live-поиска.
  - Целевая скорость: `q_user = clamp(R_safe / (U × (1 − p_hit)), 0.2, 2.0)`.
  - Лимит на окно (60 с): `S_live = max(8, int(q_user × 60))`.

### Три типа ответов на 429

1. **Мягкий per-user (live-поиск → VK)**  
   `_check_live_search_limit` → `429` с `retry_after_sec`.  
   Фронт показывает «Подбираем лучшие совпадения…», добавляет взвешенный jitter (0–300 ms), **однократно** автоповторяет поиск.

2. **Мягкий глобальный (сервер без воркеров, RPS к VK превышен)**  
   `RateLimitMiddleware` + `_vk_global_rps_under_limit()` → `429` с `retry_after_sec`.  
   Фронт обрабатывает так же, как п. 1.

3. **Жёсткий дневной (per-user per-day)**  
   `_check_rate_limit_vk_daily` → `429` **без** `retry_after_sec`.  
   Фронт: блокирует поиск (`searchHardBlocked`), показывает баннер «Поиск обновится в 00:00 UTC»; плейлисты и избранное продолжают работать.

---

## Кэш

| Кэш | TTL (дефолт) | Env | Где |
|-----|-------------|-----|-----|
| Результаты поиска | 7 дней (жёсткий) + 2 дня (soft revalidate) | `SEARCH_CACHE_SOFT_TTL_SEC` | Redis + in-memory fallback |
| URL трека (source) | **90 дней** (~сезон) | — | Redis `track:{id}:source` |
| Метаданные трека (getById) | **14 дней** | `TRACK_META_REDIS_TTL_SEC` | Redis + in-memory |
| Рекомендации | 24 ч | `RECOMMENDATIONS_CACHE_TTL_SEC` | Redis |
| Вкусовой профиль | 180 дней | `REC_TASTE_PROFILE_TTL_SEC` | Redis |
| In-memory fallback URL (недоступен Redis) | 1 ч | — | В памяти процесса |

> **Было → стало**: TTL метаданных трека изменён с 2 ч (историческое) на 14 дней — метаданные (title, artist, duration) не протухают быстро; длинный TTL снижает нагрузку на VK API.

---

## Entropy / анти-детект

### execute-батчи
Функция `_vk_get_by_id_with_entropy` намеренно нарушает регулярность запросов к VK:

- **Размеры батчей**: 5–10 (20%), 11–18 (40%), 19–25 (40%); с 10% шансом неполного батча вместо полного 25.
- **Случайный порядок ID**: `random.shuffle(ids)` перед разбивкой.
- **Случайный UUID job_id** (при воркерах): дополнительная энтропия на уровне Redis-очереди.

### User-Agent rotation
Каждый токен в пуле получает уникальный User-Agent:
- Явный список через `VK_USER_AGENTS` (запятая, порядок = порядок токенов).
- Авто-генерация: `VK_USER_AGENT;tgplay-{device_label}` где `device_label` ∈ `{pixel-6, samsung-s21, …, iphone-14}`.

### Singleflight (дедупликация)
Одновременные запросы на один и тот же ресурс (поиск, source URL, метаданные, рекомендации) объединяются в один реальный запрос к VK:

```
_search_singleflight, _source_singleflight, _meta_singleflight, _rec_singleflight
```

### Jitter при 429 (клиентский)
`rateLimitJitterSec()` в `src/lib/api.ts` добавляет случайную задержку к `retry_after_sec`, предотвращая одновременный шторм от сотен клиентов:

| Вероятность | Задержка |
|------------|---------|
| 5% | 0–10 ms |
| 40% | 20–70 ms |
| 40% | 70–150 ms |
| 15% | 150–300 ms |

---

## Обработка ошибок VK (воркеры и прямые вызовы)

Для кодов ошибок VK API 6, 9, 10, 14, 29 и сетевых ошибок (HTTP 5xx):

| Код | Действие |
|-----|---------|
| 6 (too many per second) | penalty refill_rate, risk_score++ |
| 9 (too many similar) | freeze 15–45 с, penalty, risk_score++ |
| 14 (captcha) | freeze 5–15 мин (2 подряд → 1 ч), risk_score+3 |
| 29 (rate limit) | freeze 30–60 с, refill_rate 0.5 |
| 10 (internal) | при >5% за минуту: soft-throttle |
| -1 / HTTP 5xx | freeze 60 с |

Redis-ключи (воркер): `rate:bucket`, `rate:window`, `rate:risk`, `vk:freeze_until_ms`.  
Прямые вызовы (без воркеров): аналогичный cooldown внутри `_TokenState`.

---

## IP-идентификация клиентов

`_request_ip()` читает реальный IP в порядке приоритета:
1. `X-Forwarded-For` (первый IP из цепочки) — проставляется nginx.
2. `X-Real-IP` — проставляется nginx как запасной вариант.
3. `request.client.host` — только если нет прокси (прямой запрос).

Для per-user лимитов Telegram user_id имеет приоритет над IP: `_check_rate_limit_vk_hourly` и `_get_vk_daily_limit_key` сначала читают `Authorization: tma …`.

---

## Транспортный слой

- **GZip**: все ответы ≥ 500 байт сжимаются (`GZipMiddleware`). Важно для VPN-пользователей с медленными каналами.
- **HTTP/2**: включён в nginx (`listen 443 ssl http2`).
- **HSTS**: `Strict-Transport-Security: max-age=31536000; includeSubDomains`.
- **Swagger/OpenAPI**: отключены в проде (`docs_url=None, redoc_url=None, openapi_url=None`).

---

## Что запускать в проде

- **Бэкенд:** `python3 server_lite.py` (или через systemd `tgplay-backend`). Не `app.main` и не MongoDB — это legacy.
- **Воркеры (опционально):** на каждой ноде со своим ключом — процесс из `backend/vk_worker` (`python -m vk_worker.worker` или через `app.py`). Обязательно: `REDIS_URL`, `WORKER_TOKEN_ID`, `VK_TOKEN`.

Подробнее по воркерам: [VK_WORKERS.md](VK_WORKERS.md). По ёмкости: [CAPACITY.md](CAPACITY.md).
