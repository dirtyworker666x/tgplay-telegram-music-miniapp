import asyncio
import json
import os
import random
import time
from typing import Any, Dict, Optional

import redis.asyncio as aioredis

from .vk_client import VKClient


REDIS_URL = os.getenv("REDIS_URL", "").strip()
WORKER_TOKEN_ID = (os.getenv("WORKER_TOKEN_ID") or "").strip()
if not WORKER_TOKEN_ID:
    # Derive a short stable id from token hash as a fallback.
    vk_token = (os.getenv("VK_TOKEN") or "").strip()
    if vk_token:
        import hashlib

        WORKER_TOKEN_ID = hashlib.sha256(vk_token.encode()).hexdigest()[:16]
    else:
        raise RuntimeError("WORKER_TOKEN_ID or VK_TOKEN must be set for worker")

INTERVAL_MS = max(50, min(1000, int(os.getenv("WORKER_INTERVAL_MS", "334"))))
POLL_MIN_MS = 10
POLL_MAX_MS = 20


class RedisWorker:
    """Single-token worker: pulls jobs for WORKER_TOKEN_ID, calls VK, writes results back."""

    def __init__(self) -> None:
        if not REDIS_URL:
            raise RuntimeError("REDIS_URL must be set for worker")
        self._redis: Optional[aioredis.Redis] = None
        self._vk = VKClient()
        self._claim_sha: Optional[str] = None
        self._stopping = False

    async def get_redis(self) -> aioredis.Redis:
        if self._redis is not None:
            return self._redis
        self._redis = aioredis.from_url(REDIS_URL, encoding="utf-8", decode_responses=True)
        # simple ping to fail fast
        await self._redis.ping()
        return self._redis

    async def _load_claim_script(self) -> str:
        if self._claim_sha:
            return self._claim_sha
        redis = await self.get_redis()
        path = os.path.join(os.path.dirname(__file__), "redis_scripts", "claim_head.lua")
        with open(path, "r", encoding="utf-8") as f:
            lua_src = f.read()
        self._claim_sha = await redis.script_load(lua_src)
        return self._claim_sha

    async def _set_freeze(self, redis: aioredis.Redis, freeze_ms: int) -> None:
        now_ms = int(time.time() * 1000)
        until = now_ms + max(0, freeze_ms)
        freeze_key = f"vk:freeze_until_ms:{WORKER_TOKEN_ID}"
        await redis.set(freeze_key, str(until))
        # also bump availability index
        await redis.zadd("vk:tokens:avail", {WORKER_TOKEN_ID: float(until)})
        await redis.hset(f"vk:m:{WORKER_TOKEN_ID}", mapping={"last_freeze_until_ms": until})

    async def _store_result(self, redis: aioredis.Redis, job_id: str, result: Dict[str, Any]) -> None:
        res_key = f"vk:res:{job_id}"
        resq_key = f"vk:resq:{job_id}"
        payload = json.dumps(result, ensure_ascii=False)
        # store result with TTL (e.g. 60s) and notify waiter via list push
        ttl = int(os.getenv("WORKER_RESULT_TTL_SEC", "60"))
        pipe = redis.pipeline()
        pipe.set(res_key, payload, ex=ttl)
        pipe.rpush(resq_key, "1")
        pipe.expire(resq_key, ttl)
        await pipe.execute()

    async def _set_penalty(
        self, redis: aioredis.Redis, factor: float, duration_ms: int, now_ms: int
    ) -> None:
        """Уменьшить refill_rate на factor и задать период штрафа."""
        if factor <= 0 or factor >= 1:
            return
        bucket_key = f"rate:bucket:{WORKER_TOKEN_ID}"
        risk_key = f"rate:risk:{WORKER_TOKEN_ID}"

        bucket = await redis.hgetall(bucket_key)
        # Текущий refill_rate или базовый по умолчанию
        try:
            current_refill = float(bucket.get("refill_rate") or 0.0)
        except (TypeError, ValueError):
            current_refill = 0.0
        if current_refill <= 0:
            current_refill = 3.0 / 1000.0

        risk = await redis.hgetall(risk_key)
        try:
            base_refill = float(risk.get("base_refill_rate") or 0.0)
        except (TypeError, ValueError):
            base_refill = 0.0
        if base_refill <= 0:
            base_refill = current_refill

        new_refill = base_refill * factor
        penalty_until_ms = now_ms + max(0, duration_ms)

        await redis.hset(
            bucket_key,
            mapping={
                "refill_rate": new_refill,
            },
        )
        await redis.hset(
            risk_key,
            mapping={
                "base_refill_rate": base_refill,
                "penalty_until_ms": penalty_until_ms,
            },
        )

    async def _apply_error_policy(self, redis: aioredis.Redis, code: int, now_ms: int) -> None:
        """Применить правила freeze/penalty/risk_score для ошибок VK."""
        risk_key = f"rate:risk:{WORKER_TOKEN_ID}"

        # 6: Too many requests per second → -30% refill на 10s
        if code == 6:
            await self._set_penalty(redis, factor=0.7, duration_ms=10_000, now_ms=now_ms)
            await redis.hincrby(risk_key, "risk_score", 1)
            return

        # 9: Flood control → freeze 15–45s, penalty 0.6 на 60s, risk_score++
        if code == 9:
            freeze_ms = random.randint(15_000, 45_000)
            await self._set_freeze(redis, freeze_ms)
            await self._set_penalty(redis, factor=0.6, duration_ms=60_000, now_ms=now_ms)
            await redis.hincrby(risk_key, "risk_score", 1)
            return

        # 14: Captcha needed → freeze 5–15 min, risk_score +=3; 2 подряд → disable 1h
        if code == 14:
            risk = await redis.hgetall(risk_key)
            consec = int(risk.get("captcha_consecutive") or 0) + 1
            await redis.hset(risk_key, mapping={"captcha_consecutive": consec})
            await redis.hincrby(risk_key, "risk_score", 3)
            if consec >= 2:
                # disable на 1 час
                freeze_ms = 60 * 60 * 1000
            else:
                freeze_ms = random.randint(5 * 60 * 1000, 15 * 60 * 1000)
            await self._set_freeze(redis, freeze_ms)
            return

        # 29: Rate limit reached → freeze 30–60s, refill_rate * 0.5
        if code == 29:
            freeze_ms = random.randint(30_000, 60_000)
            await self._set_freeze(redis, freeze_ms)
            await self._set_penalty(redis, factor=0.5, duration_ms=60_000, now_ms=now_ms)
            return

        # 10: Internal error → если >5% за минуту, soft-throttle (refill_rate * 0.8)
        if code == 10:
            window_ms = 60_000
            window_start_key = f"{risk_key}:err10_window_start_ms"
            cnt_key = f"{risk_key}:err10_count"
            total_key = f"{risk_key}:total_count"

            # Инициализация окна
            window_start_raw = await redis.get(window_start_key)
            window_start = int(window_start_raw or 0)
            if now_ms - window_start > window_ms or window_start == 0:
                window_start = now_ms
                await redis.set(window_start_key, str(window_start))
                await redis.set(cnt_key, "0")
                await redis.set(total_key, "0")

            await redis.incr(cnt_key)
            await redis.incr(total_key)

            err_cnt = int(await redis.get(cnt_key) or "0")
            total_cnt = int(await redis.get(total_key) or "0")
            if total_cnt >= 20 and err_cnt * 100 / max(total_cnt, 1) > 5:
                await self._set_penalty(redis, factor=0.8, duration_ms=60_000, now_ms=now_ms)
            return


    async def run_forever(self) -> None:
        redis = await self.get_redis()
        sha = await self._load_claim_script()
        print(f"🔧 Worker started for token_id={WORKER_TOKEN_ID}, redis={REDIS_URL}")

        # Базовые параметры bucket/sliding window
        capacity = 3
        refill_per_ms = 3.0 / 1000.0
        window_ms = 1000
        window_limit = 3

        while not self._stopping:
            now_ms = int(time.time() * 1000)
            try:
                raw = await redis.evalsha(
                    sha,
                    7,
                    f"vk:q:{WORKER_TOKEN_ID}",
                    f"rate:bucket:{WORKER_TOKEN_ID}",
                    f"rate:window:{WORKER_TOKEN_ID}",
                    f"vk:freeze_until_ms:{WORKER_TOKEN_ID}",
                    f"rate:risk:{WORKER_TOKEN_ID}",
                    f"vk:m:{WORKER_TOKEN_ID}",
                    "vk:tokens:avail",
                    now_ms,
                    capacity,
                    refill_per_ms,
                    window_ms,
                    window_limit,
                    WORKER_TOKEN_ID,
                )
            except aioredis.RedisError as e:
                print(f"⚠️ Redis error in worker loop: {e}")
                await asyncio.sleep(0.1)
                continue

            if not raw:
                await asyncio.sleep(0.01)
                continue

            try:
                data = json.loads(raw)
            except Exception:
                print(f"⚠️ Bad JSON from claim_head: {raw}")
                await asyncio.sleep(0.05)
                continue

            if not data.get("allowed"):
                # deny: simple jittered retry 5–20 ms
                delay_ms = int(data.get("delay_ms") or 10)
                delay_ms = max(POLL_MIN_MS, min(POLL_MAX_MS, delay_ms))
                await asyncio.sleep(delay_ms / 1000.0)
                continue

            job_raw = data.get("job")
            if not job_raw:
                # no job but allowed; short sleep
                await asyncio.sleep(0.01)
                continue

            try:
                job = json.loads(job_raw)
            except Exception:
                print(f"⚠️ Bad job payload for {WORKER_TOKEN_ID}: {job_raw}")
                continue

            job_id = str(job.get("job_id") or "")
            method = str(job.get("method") or "")
            params = job.get("params") or {}
            post = bool(job.get("post"))

            if not job_id or not method or not isinstance(params, dict):
                print(f"⚠️ Invalid job fields: {job}")
                continue

            t0 = time.time()
            vk_result = await self._vk.call(method, params, post=post)
            elapsed_ms = int((time.time() - t0) * 1000)

            # Error handling / freeze / penalty / risk_score
            err = vk_result.get("error")
            if isinstance(err, dict):
                code = int(err.get("error_code", 0) or 0)
                msg = str(err.get("error_msg", ""))[:200]
                await redis.hset(
                    f"vk:m:{WORKER_TOKEN_ID}",
                    mapping={"last_error_code": code, "last_error_msg": msg, "last_latency_ms": elapsed_ms},
                )
                await self._apply_error_policy(redis, code, now_ms)
            else:
                # Успешный ответ: сброс ошибок и счётчика капч
                await redis.hset(
                    f"vk:m:{WORKER_TOKEN_ID}",
                    mapping={"last_error_code": 0, "last_error_msg": "", "last_latency_ms": elapsed_ms},
                )
                await redis.hset(f"rate:risk:{WORKER_TOKEN_ID}", mapping={"captcha_consecutive": 0})

            await self._store_result(redis, job_id, vk_result)

    async def stop(self) -> None:
        self._stopping = True
        await self._vk.close()
        if self._redis is not None:
            await self._redis.close()


async def _main() -> None:
    worker = RedisWorker()
    try:
        await worker.run_forever()
    finally:
        await worker.stop()


def run() -> None:
    asyncio.run(_main())


if __name__ == "__main__":
    run()

