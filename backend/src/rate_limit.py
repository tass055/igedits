"""
Proactive, distributed LLM rate limiting.

Every LLM request in igedits funnels through ``_run_agent_with_retry`` in
``ai.py``. Because transcript analysis runs in the *worker* process and Instagram
caption generation runs in the *backend* process, an in-process limiter cannot
enforce a global ceiling. This module implements a **Redis-backed token bucket**
(shared by all processes and concurrent jobs) so the provider's per-minute /
per-second thresholds are respected *before* a request is sent, rather than
reacting to 429s after the fact.

Three independent buckets are enforced (any set to 0 is skipped):

- RPM  — requests per minute   (``LLM_MAX_RPM``)
- RPS  — requests per second   (``LLM_MAX_RPS``)
- TPM  — tokens per minute      (``LLM_MAX_TPM``)

Buckets are keyed by the active model name so switching models uses a fresh
budget, matching how providers such as Groq scope their quotas.

If Redis is unreachable the limiter *fails open* (logs a warning and allows the
request) so a Redis outage degrades throughput protection rather than deadlocking
the pipeline.
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

import redis.asyncio as redis

from .config import Config

logger = logging.getLogger(__name__)

config = Config()

# Atomic token-bucket refill-and-consume in a single round trip.
# KEYS[1] = bucket key
# ARGV: capacity, refill_per_sec, now (float secs), requested tokens, ttl secs
# Returns: wait time in milliseconds until `requested` tokens are available
#          (0 means the tokens were consumed now).
_TOKEN_BUCKET_LUA = """
local key = KEYS[1]
local capacity = tonumber(ARGV[1])
local refill = tonumber(ARGV[2])
local now = tonumber(ARGV[3])
local requested = tonumber(ARGV[4])
local ttl = tonumber(ARGV[5])

local data = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(data[1])
local ts = tonumber(data[2])
if tokens == nil then
  tokens = capacity
  ts = now
end

-- Refill based on elapsed time.
local elapsed = now - ts
if elapsed < 0 then elapsed = 0 end
tokens = math.min(capacity, tokens + elapsed * refill)
ts = now

if requested > capacity then
  requested = capacity
end

local wait_ms = 0
if tokens >= requested then
  tokens = tokens - requested
else
  local deficit = requested - tokens
  wait_ms = math.ceil((deficit / refill) * 1000)
  -- Do not consume now; caller will sleep and retry.
end

redis.call('HMSET', key, 'tokens', tokens, 'ts', ts)
redis.call('PEXPIRE', key, ttl * 1000)
return wait_ms
"""

_redis_client: Optional[redis.Redis] = None
_script_sha: Optional[str] = None
_init_lock = asyncio.Lock()


def _model_key(suffix: str) -> str:
    model = (config.llm or "unknown").replace(" ", "")
    return f"llm_ratelimit:{model}:{suffix}"


async def _get_client() -> Optional[redis.Redis]:
    """Lazily create (and cache) the shared Redis client + loaded Lua script."""
    global _redis_client, _script_sha
    if _redis_client is not None:
        return _redis_client
    async with _init_lock:
        if _redis_client is not None:
            return _redis_client
        try:
            client = redis.Redis(
                host=config.redis_host,
                port=config.redis_port,
                password=config.redis_password,
                decode_responses=True,
            )
            _script_sha = await client.script_load(_TOKEN_BUCKET_LUA)
            _redis_client = client
        except Exception as e:  # pragma: no cover - depends on runtime Redis
            logger.warning(
                "Rate limiter could not connect to Redis (%s); "
                "LLM requests will not be throttled.",
                e,
            )
            return None
    return _redis_client


async def _wait_for_bucket(
    client: redis.Redis,
    suffix: str,
    capacity: int,
    window_seconds: float,
    requested: int,
) -> None:
    """Block until `requested` tokens can be consumed from the named bucket."""
    if capacity <= 0 or requested <= 0:
        return

    refill_per_sec = capacity / window_seconds
    key = _model_key(suffix)
    # Keep bucket state alive comfortably longer than one window.
    ttl = max(int(window_seconds * 2), 60)

    while True:
        try:
            wait_ms = await client.evalsha(
                _script_sha,
                1,
                key,
                capacity,
                refill_per_sec,
                time.time(),
                requested,
                ttl,
            )
        except redis.ResponseError:
            # Script cache was flushed (e.g. Redis restart) — reload and retry.
            await _reload_script(client)
            continue
        except Exception as e:  # pragma: no cover - runtime Redis failure
            logger.warning("Rate limiter check failed (%s); allowing request.", e)
            return

        wait_ms = int(wait_ms or 0)
        if wait_ms <= 0:
            return
        # Cap a single sleep so we re-check state periodically.
        await asyncio.sleep(min(wait_ms / 1000.0, window_seconds))


async def _reload_script(client: redis.Redis) -> None:
    global _script_sha
    try:
        _script_sha = await client.script_load(_TOKEN_BUCKET_LUA)
    except Exception as e:  # pragma: no cover
        logger.warning("Failed to reload rate-limit script: %s", e)


async def acquire_llm_slot(est_tokens: int = 0) -> None:
    """Block until an LLM request is permitted under all configured thresholds.

    Enforces the RPS, RPM and (optionally) TPM buckets in turn. Each bucket only
    engages when its limit is > 0. Fails open if Redis is unavailable.

    Args:
        est_tokens: Rough token count for this request, used for the TPM bucket.
    """
    client = await _get_client()
    if client is None:
        return

    # Per-second bucket first (smooths concurrent bursts), then per-minute.
    await _wait_for_bucket(client, "rps", config.llm_max_rps, 1.0, 1)
    await _wait_for_bucket(client, "rpm", config.llm_max_rpm, 60.0, 1)
    if config.llm_max_tpm > 0 and est_tokens > 0:
        await _wait_for_bucket(
            client, "tpm", config.llm_max_tpm, 60.0, est_tokens
        )


def estimate_tokens(text: str) -> int:
    """Conservative token estimate for TPM accounting and transcript chunk sizing.

    Uses ~2.5 chars/token (rather than the ~4 rule of thumb for prose) because
    igedits transcripts are timestamp-dense (e.g. "[01:23 - 01:45]"), which
    tokenizes far heavier. Over-estimating keeps requests safely under the
    provider's per-request / TPM ceiling instead of tripping a 413.
    """
    if not text:
        return 0
    return max(1, (len(text) * 2) // 5)
