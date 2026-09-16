"""Tests for the "last 5 queries" conversation-context store (app/cache/conversation.py)."""

from app.cache.conversation import MAX_CONTEXT_TURNS, get_recent_context, record_turn


def test_empty_context_for_new_user():
    assert get_recent_context("brand-new-user") == []


def test_records_and_returns_most_recent_first():
    record_turn("user-1", "first query", {"intent": "a"}, "first response")
    record_turn("user-1", "second query", {"intent": "b"}, "second response")

    context = get_recent_context("user-1")

    assert len(context) == 2
    assert context[0]["query"] == "second query"  # most recent turn is index 0
    assert context[1]["query"] == "first query"


def test_caps_at_max_context_turns():
    for i in range(MAX_CONTEXT_TURNS + 3):
        record_turn("user-2", f"query {i}", {}, f"response {i}")

    context = get_recent_context("user-2")

    assert len(context) == MAX_CONTEXT_TURNS
    # The newest MAX_CONTEXT_TURNS entries should be kept, oldest ones dropped.
    assert context[0]["query"] == f"query {MAX_CONTEXT_TURNS + 2}"
