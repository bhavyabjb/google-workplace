# Single shared Redis connection (connection-pooled internally by redis-py), reused
# across the embedding cache, rate limiter, and conversation-context store so we don't
# open a new connection per call.

from functools import lru_cache
# lru_cache: makes get_redis() a memoized singleton, same pattern as app.config.get_settings.

import redis
# redis: redis-py client library.

from app.config import get_settings


@lru_cache
def get_redis() -> redis.Redis:
    """Return the process-wide Redis client, constructing it only on first call."""
    settings = get_settings()
    # decode_responses=True: get back `str` instead of `bytes` from GET/HGET/etc,
    # since almost everything we store is JSON-encoded text.
    return redis.Redis.from_url(settings.redis_url, decode_responses=True)
