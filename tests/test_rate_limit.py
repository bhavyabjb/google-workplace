"""Tests for the per-user rate limiter (app/cache/rate_limit.py)."""

import pytest

from app.cache.rate_limit import RateLimitExceeded, check_and_increment
from app.config import get_settings


def test_allows_requests_under_the_limit():
    # Should not raise for any call count at or below the configured cap.
    for _ in range(get_settings().queries_per_user_per_hour):
        check_and_increment("user-a")


def test_raises_once_over_the_limit():
    limit = get_settings().queries_per_user_per_hour
    for _ in range(limit):
        check_and_increment("user-b")

    with pytest.raises(RateLimitExceeded):
        check_and_increment("user-b")


def test_limits_are_tracked_per_user_independently():
    limit = get_settings().queries_per_user_per_hour
    for _ in range(limit):
        check_and_increment("user-c")

    # A different user's counter is untouched by user-c's usage.
    check_and_increment("user-d")
