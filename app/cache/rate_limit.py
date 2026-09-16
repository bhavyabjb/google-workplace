"""Per-user rate limiting: 100 queries/user/hour, as specified in the brief.

Implemented as a fixed-window counter in Redis: `ratelimit:{user_id}:{hour_bucket}`.
A fixed window is simpler than a sliding-window/token-bucket and good enough here -
its only weakness (allowing up to 2x the limit across a window boundary) doesn't
matter much for an anti-abuse-not-billing use case like this.
"""

import time
# time: used to compute the current hour bucket (epoch seconds // 3600).

from app.cache.redis_client import get_redis
from app.config import get_settings


class RateLimitExceeded(Exception):
    """Raised when a user has exceeded QUERIES_PER_USER_PER_HOUR."""


def check_and_increment(user_id: str) -> None:
    """Increment this user's counter for the current hour bucket; raise if over the cap.

    Called once at the top of the /api/v1/query handler, before any expensive LLM/
    Google API work happens - fail fast and cheap.
    """
    settings = get_settings()
    redis = get_redis()

    hour_bucket = int(time.time() // 3600)
    key = f"ratelimit:{user_id}:{hour_bucket}"

    # INCR is atomic - safe under concurrent requests from the same user without extra locking.
    current = redis.incr(key)
    if current == 1:
        # First request in this bucket: set the key to expire in a bit over an hour so
        # it's cleaned up automatically even if this bucket is never incremented again.
        redis.expire(key, 3700)

    if current > settings.queries_per_user_per_hour:
        raise RateLimitExceeded(
            f"User {user_id} exceeded {settings.queries_per_user_per_hour} queries/hour"
        )
