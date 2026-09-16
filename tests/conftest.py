"""Shared pytest fixtures.

The most important one is `use_fake_redis`: it's autouse (applies to every test
automatically) and swaps the real Redis client for fakeredis's in-memory
implementation, so the whole test suite runs without a real Redis server. Every
module that calls `get_redis()` (rate_limit.py, conversation.py, embedder.py,
intent_classifier.py) goes through `redis.Redis.from_url`, so patching that one
classmethod on the `redis` library itself - rather than patching each call site -
is what makes this cover all of them with one fixture.
"""

import fakeredis
# fakeredis: a pure-Python, in-memory implementation of the Redis protocol/API
# surface, used only in tests so we don't need `docker compose up redis` to run them.
import pytest
import redis

from app.cache import redis_client


@pytest.fixture(autouse=True)
def use_fake_redis(monkeypatch):
    fake = fakeredis.FakeRedis(decode_responses=True)

    # get_redis() is memoized with @lru_cache (see app/cache/redis_client.py) - clear
    # it first so the next call re-executes the (now-patched) constructor logic
    # instead of returning a real client cached from an earlier test.
    redis_client.get_redis.cache_clear()
    monkeypatch.setattr(redis.Redis, "from_url", classmethod(lambda cls, *args, **kwargs: fake))

    yield fake

    redis_client.get_redis.cache_clear()
