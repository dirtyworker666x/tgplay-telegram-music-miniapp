-- claim_head.lua (v2)
--
-- Атомарный лимитер для одного token_id:
-- 1) Token Bucket (rate:bucket:{token_id})
-- 2) Sliding Window (rate:window:{token_id})
-- 3) Freeze / penalty / risk (упрощённо: только freeze здесь)
-- 4) FIFO LPOP из очереди vk:q:{token_id}
--
-- KEYS:
--   [1] queue_key         = "vk:q:{token_id}"
--   [2] bucket_key        = "rate:bucket:{token_id}"  (HASH)
--   [3] window_key        = "rate:window:{token_id}" (ZSET)
--   [4] freeze_key        = "vk:freeze_until_ms:{token_id}"
--   [5] risk_key          = "rate:risk:{token_id}"   (HASH, пока только для совместимости)
--   [6] metrics_key       = "vk:m:{token_id}"        (HASH)
--   [7] tokens_avail_key  = "vk:tokens:avail"        (ZSET)
--
-- ARGV:
--   [1] now_ms            - unix time в миллисекундах
--   [2] capacity          - max tokens (обычно 3)
--   [3] refill_per_ms     - базовая скорость пополнения (3/1000)
--   [4] window_ms         - размер окна (обычно 1000)
--   [5] window_limit      - макс. событий в окне (обычно 3)
--   [6] token_id          - строковый идентификатор токена (16 hex)
--
-- Ответ (JSON):
--   {
--     allowed = true/false,
--     job = string|nil,
--     delay_ms = number,         -- рекомендованная задержка до следующей попытки
--     bucket_tokens = number,
--     freeze_until_ms = number
--   }

local queue_key        = KEYS[1]
local bucket_key       = KEYS[2]
local window_key       = KEYS[3]
local freeze_key       = KEYS[4]
local risk_key         = KEYS[5]
local metrics_key      = KEYS[6]
local tokens_avail_key = KEYS[7]

local now_ms        = tonumber(ARGV[1])
local capacity      = tonumber(ARGV[2])
local refill_per_ms = tonumber(ARGV[3])
local window_ms     = tonumber(ARGV[4])
local window_limit  = tonumber(ARGV[5])
local token_id      = ARGV[6]

if capacity <= 0 then capacity = 3 end
if refill_per_ms <= 0 then refill_per_ms = 3.0 / 1000.0 end
if window_ms <= 0 then window_ms = 1000 end
if window_limit <= 0 then window_limit = 3 end

-- Jitter helper (взвешенный)
local function pick_jitter_ms()
  -- 40% → 20–70ms
  -- 30% → 70–150ms
  -- 20% → 150–300ms
  -- 10% → 0–10ms
  local r = math.random()
  if r < 0.4 then
    return 20 + math.random(0, 50)   -- 20–70
  elseif r < 0.7 then
    return 70 + math.random(0, 80)   -- 70–150
  elseif r < 0.9 then
    return 150 + math.random(0, 150) -- 150–300
  else
    return math.random(0, 10)        -- 0–10
  end
end

-- Freeze state
local freeze_until_ms = tonumber(redis.call("GET", freeze_key) or "0")
if now_ms < freeze_until_ms then
  local delay_ms = freeze_until_ms - now_ms
  if delay_ms < 5 then delay_ms = 5 end
  delay_ms = delay_ms + pick_jitter_ms()
  return cjson.encode({
    allowed = false,
    job = cjson.null,
    delay_ms = delay_ms,
    bucket_tokens = 0,
    freeze_until_ms = freeze_until_ms,
  })
end

-- Token Bucket
local bucket = redis.call("HMGET", bucket_key, "tokens", "last_refill_ts", "refill_rate", "penalty_multiplier")
local tokens          = tonumber(bucket[1])
local last_refill_ts  = tonumber(bucket[2])
local refill_rate     = tonumber(bucket[3])
local penalty_mult    = tonumber(bucket[4])

-- Прочитаем базовый refill и текущий период penalty из risk_key
local risk = redis.call("HMGET", risk_key, "base_refill_rate", "penalty_until_ms")
local base_refill = tonumber(risk[1])
local penalty_until_ms = tonumber(risk[2])

if not base_refill or base_refill <= 0 then
  base_refill = refill_per_ms
end
if not penalty_until_ms then
  penalty_until_ms = 0
end

if not tokens or tokens < 0 then tokens = capacity end
if not last_refill_ts or last_refill_ts <= 0 then last_refill_ts = now_ms end
if not refill_rate or refill_rate <= 0 then refill_rate = base_refill end
if not penalty_mult or penalty_mult <= 0 then penalty_mult = 1.0 end

-- Если период штрафа истёк — сбросить refill_rate и penalty_mult
if penalty_until_ms > 0 and now_ms > penalty_until_ms then
  refill_rate = base_refill
  penalty_mult = 1.0
  redis.call("HMSET", risk_key,
    "base_refill_rate", base_refill,
    "penalty_until_ms", 0
  )
end

local delta_ms = now_ms - last_refill_ts
if delta_ms < 0 then delta_ms = 0 end
local refill = delta_ms * refill_rate * penalty_mult
tokens = tokens + refill
if tokens > capacity then tokens = capacity end

-- Sliding window clean-up
local window_start = now_ms - window_ms
redis.call("ZREMRANGEBYSCORE", window_key, 0, window_start)
local count = redis.call("ZCARD", window_key)

-- Проверка bucket + окно
if tokens < 1 or count >= window_limit then
  local delay_ms = pick_jitter_ms()
  redis.call("HMSET", bucket_key,
    "tokens", tokens,
    "last_refill_ts", now_ms,
    "refill_rate", refill_rate,
    "penalty_multiplier", penalty_mult
  )
  redis.call("HINCRBY", metrics_key, "denied_count", 1)
  return cjson.encode({
    allowed = false,
    job = cjson.null,
    delay_ms = delay_ms,
    bucket_tokens = tokens,
    freeze_until_ms = freeze_until_ms,
  })
end

-- Пытаемся снять job из головы очереди
local job = redis.call("LPOP", queue_key)
if not job then
  -- Нет задач, но лимиты позволяют — просто обновим состояние и вернём deny с малой задержкой
  redis.call("HMSET", bucket_key,
    "tokens", tokens,
    "last_refill_ts", now_ms,
    "refill_rate", refill_rate,
    "penalty_multiplier", penalty_mult
  )
  redis.call("ZADD", tokens_avail_key, now_ms, token_id)
  return cjson.encode({
    allowed = false,
    job = cjson.null,
    delay_ms = 10,
    bucket_tokens = tokens,
    freeze_until_ms = freeze_until_ms,
  })
end

-- Разрешаем запрос: списываем 1 токен и добавляем запись в окно
tokens = tokens - 1.0
if tokens < 0 then tokens = 0 end
redis.call("HMSET", bucket_key,
  "tokens", tokens,
  "last_refill_ts", now_ms,
  "refill_rate", refill_rate,
  "penalty_multiplier", penalty_mult
)
redis.call("ZADD", window_key, now_ms, tostring(now_ms))
redis.call("HINCRBY", metrics_key, "allowed_count", 1)
redis.call("HSET", metrics_key, "last_bucket_tokens", tokens)
redis.call("HSET", metrics_key, "last_window_count", count + 1)

-- Обновим индекс доступности: чем меньше токенов, тем дальше во времени выставляем score.
local next_score = now_ms
if tokens < 1 then
  next_score = now_ms + window_ms
end
redis.call("ZADD", tokens_avail_key, next_score, token_id)

return cjson.encode({
  allowed = true,
  job = job,
  delay_ms = 0,
  bucket_tokens = tokens,
  freeze_until_ms = freeze_until_ms,
})

