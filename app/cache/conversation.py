"""Conversation-context store: "store last 5 queries for conversation context" (brief's hint),
used to resolve references like "that email about the proposal" or "move the meeting with John"
where the query on its own is ambiguous without recent history.

Two-tier storage:
- Redis list `conversation:{user_id}` (capped at 5, most-recent-first) - fast path the
  intent classifier reads on every request.
- Postgres `conversations` table (app/db/models.py) - the durable, unbounded log used
  for anything beyond the last 5 (analytics, debugging, audit).

Both are written together in `record_turn`; Redis is a cache of the same data, not a
separate source of truth, so if it's ever flushed we simply fall back to the DB.
"""

import json
# json: Redis stores strings, so each conversation turn is JSON-encoded before LPUSH.

from app.cache.redis_client import get_redis

MAX_CONTEXT_TURNS = 5
CONTEXT_TTL_SECONDS = 24 * 3600  # a day is plenty; conversation context is meant to be short-lived


def _key(user_id: str) -> str:
    return f"conversation:{user_id}"


def record_turn(user_id: str, query: str, intent: dict, response: str) -> None:
    """Push the latest query/intent/response onto this user's recent-context list.

    Call this from the /query endpoint AFTER the DB write to `conversations` succeeds,
    so Redis and Postgres never disagree about what happened.
    """
    redis = get_redis()
    key = _key(user_id)
    entry = json.dumps({"query": query, "intent": intent, "response": response})

    redis.lpush(key, entry)          # most recent turn ends up at index 0
    redis.ltrim(key, 0, MAX_CONTEXT_TURNS - 1)  # keep only the newest 5
    redis.expire(key, CONTEXT_TTL_SECONDS)


def get_recent_context(user_id: str) -> list[dict]:
    """Return up to the last 5 turns, most-recent-first, for this user."""
    redis = get_redis()
    raw_entries = redis.lrange(_key(user_id), 0, MAX_CONTEXT_TURNS - 1)
    return [json.loads(e) for e in raw_entries]
