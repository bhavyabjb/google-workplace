"""Tests for the Intent Classifier (app/orchestrator/intent_classifier.py).

We never call the real OpenAI API in tests - `mocker.patch` replaces the chat
completion call with a canned response, so these tests check OUR prompt-building
and response-parsing/caching logic, not OpenAI's model behavior (which would be
flaky and cost money to test against).
"""

import json
from types import SimpleNamespace

from app.orchestrator import intent_classifier


def _fake_openai_response(payload: dict):
    """Build an object shaped like the OpenAI SDK's ChatCompletion response, just
    deep enough for our code's `response.choices[0].message.content` access pattern."""
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))])


def test_classify_intent_parses_llm_json_into_intent_model(mocker):
    canned = {
        "services": ["gmail", "gcal"],
        "intent": "cancel_flight",
        "entities": {"airline": "Turkish Airlines"},
        "steps": ["search_gmail_for_booking", "find_calendar_event"],
        "needs_clarification": False,
        "clarification_question": None,
    }
    mock_create = mocker.patch.object(
        intent_classifier._client.chat.completions, "create", return_value=_fake_openai_response(canned)
    )

    intent = intent_classifier.classify_intent(
        "Cancel my Turkish Airlines flight", conversation_context=[], user_timezone="UTC", now_iso="2026-09-17T00:00:00Z"
    )

    assert intent.services == ["gmail", "gcal"]
    assert intent.intent == "cancel_flight"
    assert intent.entities == {"airline": "Turkish Airlines"}
    assert mock_create.call_count == 1


def test_classify_intent_is_cached_for_identical_query_and_context(mocker):
    canned = {"services": ["gcal"], "intent": "list_events", "entities": {}, "steps": [], "needs_clarification": False, "clarification_question": None}
    mock_create = mocker.patch.object(
        intent_classifier._client.chat.completions, "create", return_value=_fake_openai_response(canned)
    )

    intent_classifier.classify_intent("What's on my calendar?", [], "UTC", "2026-09-17T00:00:00Z")
    intent_classifier.classify_intent("What's on my calendar?", [], "UTC", "2026-09-17T00:00:00Z")

    # Second call with identical (query, context) hit the Redis cache - OpenAI called only once.
    assert mock_create.call_count == 1


def test_needs_clarification_flows_through(mocker):
    canned = {
        "services": ["gcal"],
        "intent": "reschedule_event",
        "entities": {"attendee_hint": "John"},
        "steps": [],
        "needs_clarification": True,
        "clarification_question": "Which meeting with John - the 2pm or the 4pm?",
    }
    mocker.patch.object(intent_classifier._client.chat.completions, "create", return_value=_fake_openai_response(canned))

    intent = intent_classifier.classify_intent("Move the meeting with John", [], "UTC", "2026-09-17T00:00:00Z")

    assert intent.needs_clarification is True
    assert "John" in intent.clarification_question
