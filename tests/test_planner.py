"""Tests for the Query Planner (app/orchestrator/planner.py) - pure logic, no
network/DB calls, since build_plan only transforms an Intent into an ExecutionPlan.
"""

import pytest

from app.orchestrator.planner import build_plan
from app.schemas import Intent


def test_needs_clarification_intent_raises():
    intent = Intent(services=["gcal"], intent="reschedule_event", needs_clarification=True, clarification_question="Which John?")

    with pytest.raises(ValueError):
        build_plan(intent)


def test_cancel_flight_template_shape():
    intent = Intent(services=["gmail", "gcal"], intent="cancel_flight", entities={"airline": "Turkish Airlines"})
    plan = build_plan(intent)

    node_ids = {n.id for n in plan.nodes}
    assert {"search_gmail", "search_gcal", "context_gmail", "draft_cancellation_email"} == node_ids

    # The two searches are independent - the executor's parallelism depends on this.
    search_gmail = next(n for n in plan.nodes if n.id == "search_gmail")
    search_gcal = next(n for n in plan.nodes if n.id == "search_gcal")
    assert search_gmail.depends_on == []
    assert search_gcal.depends_on == []

    # The write action waits on both branches (needs the booking AND the calendar event).
    draft_node = next(n for n in plan.nodes if n.id == "draft_cancellation_email")
    assert set(draft_node.depends_on) == {"context_gmail", "search_gcal"}
    assert draft_node.agent == "gmail"
    assert draft_node.action == "execute"


def test_prepare_for_meeting_template_fans_out_from_calendar():
    intent = Intent(services=["gcal", "gmail", "gdrive"], intent="prepare_for_meeting", entities={"client": "Acme Corp"})
    plan = build_plan(intent)

    calendar_search = next(n for n in plan.nodes if n.id == "find_calendar_event")
    assert calendar_search.depends_on == []

    email_search = next(n for n in plan.nodes if n.id == "search_emails_with_client")
    drive_search = next(n for n in plan.nodes if n.id == "pull_drive_documents")
    # Both downstream searches wait on the calendar context (need attendee emails first).
    assert email_search.depends_on == ["context_gcal"]
    assert drive_search.depends_on == ["context_gcal"]


def test_list_events_by_attendee_is_a_single_read_node():
    intent = Intent(services=["gcal"], intent="list_events_by_attendee", entities={"attendee": "john@company.com"})
    plan = build_plan(intent)

    assert len(plan.nodes) == 1
    assert plan.nodes[0].agent == "gcal"
    assert plan.nodes[0].action == "search"


def test_generic_fallback_builds_search_nodes_for_every_service():
    intent = Intent(services=["gmail", "gdrive"], intent="some_unrecognized_intent", entities={})
    plan = build_plan(intent)

    agents_present = {n.agent for n in plan.nodes}
    assert agents_present == {"gmail", "gdrive"}
    # No write verb in "some_unrecognized_intent" -> no execute node.
    assert all(n.action != "execute" for n in plan.nodes)


def test_generic_fallback_adds_execute_node_for_detected_write_verb():
    intent = Intent(services=["gdrive"], intent="share_file_with_client", entities={"share_with": "a@b.com"})
    plan = build_plan(intent)

    execute_nodes = [n for n in plan.nodes if n.action == "execute"]
    assert len(execute_nodes) == 1
    assert execute_nodes[0].agent == "gdrive"
    assert execute_nodes[0].params["verb"] == "share"
    # The execute node depends on every gathered-context node so it has full info before acting.
    assert execute_nodes[0].depends_on == ["context_gdrive"]
