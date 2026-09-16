"""Tests for GmailAgent (app/orchestrator/agents/gmail_agent.py).

Everything that would touch Postgres, Redis/OpenAI, or the real Gmail API is
mocked at the names gmail_agent.py imports them under - this test is only checking
OUR glue logic (how search results get turned into get_context/execute calls),
not the correctness of pgvector or the Gmail API itself.
"""

from datetime import datetime, timezone
from types import SimpleNamespace

from app.orchestrator.agents.gmail_agent import GmailAgent


class _FakeGmailCacheRow:
    """Stands in for a GmailCache ORM row - just needs the attributes GmailAgent.search() reads."""

    def __init__(self):
        self.email_id = "msg-123"
        self.thread_id = "thread-123"
        self.subject = "Your Turkish Airlines booking TK1234"
        self.sender = "noreply@turkishairlines.com"
        self.body_preview = "Your booking is confirmed..."
        self.received_at = datetime(2026, 8, 15, tzinfo=timezone.utc)


def _make_agent(mocker):
    fake_db = mocker.Mock()
    fake_user = SimpleNamespace(id="user-123")
    return GmailAgent(fake_db, fake_user)


def test_search_maps_cache_rows_to_lightweight_dicts(mocker):
    mocker.patch("app.orchestrator.agents.gmail_agent.embed_text", return_value=[0.0] * 1536)
    mock_search = mocker.patch("app.orchestrator.agents.gmail_agent.search_gmail", return_value=[_FakeGmailCacheRow()])

    agent = _make_agent(mocker)
    results = agent.search(entities={"airline": "Turkish Airlines"}, intent="cancel_flight")

    assert results == [
        {
            "email_id": "msg-123",
            "thread_id": "thread-123",
            "subject": "Your Turkish Airlines booking TK1234",
            "sender": "noreply@turkishairlines.com",
            "body_preview": "Your booking is confirmed...",
            "received_at": "2026-08-15T00:00:00+00:00",
        }
    ]
    mock_search.assert_called_once()


def test_get_context_fetches_full_message_for_top_hit(mocker):
    mocker.patch("app.orchestrator.agents.gmail_agent.get_credentials_for_user", return_value=object())
    fake_client = mocker.Mock()
    fake_client.get_message.return_value = {
        "threadId": "thread-123",
        "snippet": "Your booking is confirmed...",
        "payload": {"headers": [{"name": "From", "value": "noreply@turkishairlines.com"}, {"name": "Subject", "value": "Booking TK1234"}]},
    }
    mocker.patch("app.orchestrator.agents.gmail_agent.GmailClient", return_value=fake_client)

    agent = _make_agent(mocker)
    context = agent.get_context([{"email_id": "msg-123"}])

    assert context["from"] == "noreply@turkishairlines.com"
    assert context["subject"] == "Booking TK1234"
    fake_client.get_message.assert_called_once_with("msg-123")


def test_get_context_returns_empty_dict_for_no_search_results(mocker):
    agent = _make_agent(mocker)
    assert agent.get_context([]) == {}


def test_execute_draft_creates_a_draft_and_records_audit_log(mocker):
    mocker.patch("app.orchestrator.agents.gmail_agent.get_credentials_for_user", return_value=object())
    mock_record_action = mocker.patch("app.orchestrator.agents.gmail_agent.record_action")

    fake_openai_response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="We would like to cancel booking TK1234."))]
    )
    mocker.patch("app.orchestrator.agents.gmail_agent._openai.chat.completions.create", return_value=fake_openai_response)

    fake_client = mocker.Mock()
    fake_client.create_draft.return_value = {"id": "draft-456"}
    mocker.patch("app.orchestrator.agents.gmail_agent.GmailClient", return_value=fake_client)

    agent = _make_agent(mocker)
    result = agent.execute(
        verb="draft",
        entities={"airline": "Turkish Airlines", "recipient": "support@turkishairlines.com"},
        upstream_context={"context_gmail": {"subject": "Booking TK1234"}},
    )

    assert result["draft_id"] == "draft-456"
    assert result["to"] == "support@turkishairlines.com"
    fake_client.create_draft.assert_called_once()
    mock_record_action.assert_called_once()
    # record_action(db, user_id, action, service, resource_id, status, details)
    assert mock_record_action.call_args.args[5] == "success"


def test_execute_unsupported_verb_raises(mocker):
    agent = _make_agent(mocker)
    try:
        agent.execute(verb="send", entities={}, upstream_context={})
        assert False, "expected NotImplementedError"
    except NotImplementedError:
        pass
