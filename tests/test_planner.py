"""Tests for the Query Planner (app/orchestrator/planner.py) - pure logic, no
network/DB calls, since build_plan only transforms an Intent into an ExecutionPlan.

The planner no longer infers write actions from text - it just turns whatever
`Intent.actions` the classifier decided on into execute nodes. So these tests
construct Intents with explicit `actions` rather than relying on keyword detection.
"""

import pytest

from app.orchestrator.planner import build_plan
from app.schemas import Action, Intent


def test_needs_clarification_intent_raises():
    intent = Intent(services=["gcal"], intent="reschedule_event", needs_clarification=True, clarification_question="Which John?")

    with pytest.raises(ValueError):
        build_plan(intent)


def test_builds_search_and_context_nodes_for_every_service_in_parallel():
    intent = Intent(services=["gmail", "gdrive"], intent="some_unrecognized_intent", entities={})
    plan = build_plan(intent)

    node_ids = {n.id for n in plan.nodes}
    assert node_ids == {"search_gmail", "context_gmail", "search_gdrive", "context_gdrive"}

    search_gmail = next(n for n in plan.nodes if n.id == "search_gmail")
    search_gdrive = next(n for n in plan.nodes if n.id == "search_gdrive")
    assert search_gmail.depends_on == []
    assert search_gdrive.depends_on == []

    # No actions given -> no execute node, regardless of what the intent label says.
    assert all(n.action != "execute" for n in plan.nodes)


def test_execute_node_built_directly_from_action_with_multi_service_dependency():
    intent = Intent(
        services=["gmail", "gcal"],
        intent="cancel_flight",
        entities={"airline": "Turkish Airlines"},
        actions=[Action(service="gmail", verb="draft", needs_context_from=["gmail", "gcal"])],
    )
    plan = build_plan(intent)

    execute_nodes = [n for n in plan.nodes if n.action == "execute"]
    assert len(execute_nodes) == 1
    execute_node = execute_nodes[0]

    assert execute_node.agent == "gmail"
    assert execute_node.params["verb"] == "draft"
    assert set(execute_node.depends_on) == {"context_gmail", "context_gcal"}


def test_calendar_only_cancellation_resolves_to_gcal_delete():
    # The exact ambiguity that used to break: "cancel" with no email/booking in play
    # must delete the calendar event, not draft a Gmail message - the classifier
    # decides this now, so the planner just builds whatever it's told.
    intent = Intent(
        services=["gcal"],
        intent="cancel_event",
        entities={"date": "2026-09-22"},
        actions=[Action(service="gcal", verb="delete", needs_context_from=["gcal"])],
    )
    plan = build_plan(intent)

    execute_nodes = [n for n in plan.nodes if n.action == "execute"]
    assert len(execute_nodes) == 1
    assert execute_nodes[0].agent == "gcal"
    assert execute_nodes[0].params["verb"] == "delete"


def test_action_skipped_if_its_service_was_not_flagged():
    # Constructing this Intent directly (bypassing the classifier) to simulate an
    # inconsistent LLM output - the planner must not act on a service it wasn't told
    # is actually involved, even if an action names it.
    intent = Intent(services=["gmail"], intent="delete_something", actions=[Action(service="gcal", verb="delete", needs_context_from=["gcal"])])
    plan = build_plan(intent)

    assert all(n.action != "execute" for n in plan.nodes)


def test_duplicate_actions_produce_one_execute_node():
    intent = Intent(
        services=["gdrive"],
        intent="share_file",
        actions=[
            Action(service="gdrive", verb="share", needs_context_from=["gdrive"]),
            Action(service="gdrive", verb="share", needs_context_from=["gdrive"]),
        ],
    )
    plan = build_plan(intent)

    execute_nodes = [n for n in plan.nodes if n.action == "execute"]
    assert len(execute_nodes) == 1


def test_needs_context_from_filtered_to_services_actually_in_plan():
    # The action asks for gmail context, but gmail was never flagged as a service -
    # the dependency must be dropped rather than pointing at a node that doesn't exist.
    intent = Intent(services=["gdrive"], intent="move_file", actions=[Action(service="gdrive", verb="move", needs_context_from=["gdrive", "gmail"])])
    plan = build_plan(intent)

    execute_node = next(n for n in plan.nodes if n.action == "execute")
    assert execute_node.depends_on == ["context_gdrive"]


def test_action_verb_must_be_supported_by_its_service():
    with pytest.raises(ValueError):
        Action(service="gmail", verb="delete")  # GmailAgent only ever implements "draft"


def test_single_service_read_only_query_has_no_execute_node():
    intent = Intent(services=["gcal"], intent="list_events_by_attendee", entities={"attendee": "john@company.com"}, steps=["search_calendar", "filter_by_attendee", "return_formatted_list"])
    plan = build_plan(intent)

    assert all(n.action != "execute" for n in plan.nodes)
    assert {n.agent for n in plan.nodes} == {"gcal"}
