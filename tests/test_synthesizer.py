"""Tests for the Response Synthesizer (app/orchestrator/synthesizer.py), again
mocking the OpenAI call so we're testing our own aggregation/parsing, not the model.
"""

import json
from types import SimpleNamespace

from app.orchestrator import synthesizer
from app.schemas import Intent, NodeResult


def _fake_openai_response(payload: dict):
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps(payload)))])


def test_synthesize_response_returns_text_and_actions(mocker):
    canned = {
        "response": "I found your Turkish Airlines booking and drafted a cancellation email.",
        "actions_taken": ["found booking email", "drafted cancellation email"],
    }
    mocker.patch.object(synthesizer._client.chat.completions, "create", return_value=_fake_openai_response(canned))

    intent = Intent(services=["gmail", "gcal"], intent="cancel_flight", entities={"airline": "Turkish Airlines"})
    results = {
        "search_gmail": NodeResult(node_id="search_gmail", status="success", data=[{"subject": "Your booking TK1234"}]),
        "search_gcal": NodeResult(node_id="search_gcal", status="error", error="Calendar API timeout"),
    }

    response_text, actions_taken = synthesizer.synthesize_response("Cancel my Turkish Airlines flight", intent, results)

    assert "Turkish Airlines" in response_text
    assert actions_taken == ["found booking email", "drafted cancellation email"]
