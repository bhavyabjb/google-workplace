"""Query Planner: Intent -> ExecutionPlan (a small DAG of PlanNodes).

Encodes the two rules from the brief's "Query Planner" section:
  - Parallel operations: independent searches (e.g. Gmail + Calendar) have no
    `depends_on` between them, so the executor (app/orchestrator/executor.py) can
    run them concurrently with asyncio.gather.
  - Sequential dependencies: a write action (send/create/delete/...) depends_on the
    search/get_context nodes that gather the information it needs first (e.g.
    "extract booking reference -> draft email" from the brief).

Rather than hand-writing a bespoke DAG for every possible intent label the LLM might
produce (which doesn't scale - the classifier is free-text), the planner has:
  1. A small library of named templates for the exact flows the brief specifies
     (cancel_flight, prepare_for_meeting, list_events_by_attendee) where we know the
     precise shape of the DAG.
  2. A generic fallback for any other intent, built mechanically from
     `intent.services` (one search node per service, all parallel) plus one execute
     node per detected write verb, wired to depend on every search node so it has
     full context before acting.
This mirrors how a real system would evolve: start with the well-understood cases,
fall back to a general strategy for the long tail instead of guessing wrong.
"""

from app.schemas import ExecutionPlan, Intent, PlanNode

# Verbs that indicate a *write* (mutating) action rather than a read/search.
# Used by the generic fallback to decide whether an execute node is needed at all.
WRITE_VERBS = {"cancel", "draft", "send", "create", "update", "delete", "move", "share", "reschedule"}

# Maps a write verb to the service it actually mutates (a "cancel" surfaces as an
# email draft on Gmail; "reschedule"/"move" of a meeting mutates Calendar; etc).
WRITE_VERB_TO_SERVICE = {
    "cancel": "gmail",
    "draft": "gmail",
    "send": "gmail",
    "create": "gcal",
    "update": "gcal",
    "reschedule": "gcal",
    "delete": "gcal",
    "move": "gdrive",
    "share": "gdrive",
}


def _search_node(service: str, intent: Intent) -> PlanNode:
    """Build the read/search node for one service, seeded with whatever entities the
    classifier extracted (e.g. sender, attendee, date range, mime type)."""
    return PlanNode(
        id=f"search_{service}",
        agent=service,
        action="search",
        params={"intent": intent.intent, "entities": intent.entities},
        depends_on=[],
    )


def _get_context_node(service: str) -> PlanNode:
    """Fetch full detail for the top search hit (subject+body, full event, file content) -
    search() only returns lightweight metadata; get_context() is the "zoom in" step the
    response synthesizer needs for a detailed answer, not just a title/snippet."""
    return PlanNode(
        id=f"context_{service}",
        agent=service,
        action="get_context",
        params={},
        depends_on=[f"search_{service}"],
    )


def build_plan(intent: Intent) -> ExecutionPlan:
    if intent.needs_clarification:
        # The caller (app/orchestrator/executor.py's entrypoint) must check this and
        # short-circuit to a clarification question before ever calling build_plan.
        raise ValueError("Cannot build a plan for an intent that needs clarification")

    template = _TEMPLATES.get(intent.intent)
    if template:
        return template(intent)
    return _generic_plan(intent)


def _generic_plan(intent: Intent) -> ExecutionPlan:
    nodes: list[PlanNode] = []

    # 1. One parallel search node per service the classifier flagged.
    search_ids = []
    for service in intent.services:
        nodes.append(_search_node(service, intent))
        nodes.append(_get_context_node(service))
        search_ids.append(f"context_{service}")

    # 2. Detect write verbs in the intent label/steps and add a dependent execute node.
    haystack = " ".join([intent.intent, *intent.steps]).lower()
    detected_verbs = [v for v in WRITE_VERBS if v in haystack]

    for verb in detected_verbs:
        target_service = WRITE_VERB_TO_SERVICE[verb]
        if target_service not in intent.services:
            continue  # classifier didn't actually flag this service as involved - skip
        nodes.append(
            PlanNode(
                id=f"execute_{verb}_{target_service}",
                agent=target_service,
                action="execute",
                params={"verb": verb, "intent": intent.intent, "entities": intent.entities},
                # Depends on ALL gathered context, not just its own service's, since
                # e.g. drafting a cancellation email needs the calendar event details too.
                depends_on=list(search_ids),
            )
        )

    return ExecutionPlan(nodes=nodes)


def _plan_cancel_flight(intent: Intent) -> ExecutionPlan:
    """"Cancel my Turkish Airlines flight" -> search Gmail for booking + search Calendar
    for the flight event (parallel), then draft the cancellation email (depends on both,
    since the email needs the booking reference AND confirms against the calendar event)."""
    return ExecutionPlan(
        nodes=[
            PlanNode(id="search_gmail", agent="gmail", action="search", params={"intent": intent.intent, "entities": intent.entities}, depends_on=[]),
            PlanNode(id="search_gcal", agent="gcal", action="search", params={"intent": intent.intent, "entities": intent.entities}, depends_on=[]),
            PlanNode(id="context_gmail", agent="gmail", action="get_context", params={}, depends_on=["search_gmail"]),
            PlanNode(
                id="draft_cancellation_email",
                agent="gmail",
                action="execute",
                params={"verb": "draft", "intent": intent.intent, "entities": intent.entities},
                depends_on=["context_gmail", "search_gcal"],
            ),
        ]
    )


def _plan_prepare_for_meeting(intent: Intent) -> ExecutionPlan:
    """"Prepare for tomorrow's client meeting with Acme Corp" -> find the calendar event,
    then (in parallel, once we know who/when) search emails with the client and pull
    Drive documents. Purely read-only - nothing to execute, just gather + synthesize."""
    return ExecutionPlan(
        nodes=[
            PlanNode(id="find_calendar_event", agent="gcal", action="search", params={"intent": intent.intent, "entities": intent.entities}, depends_on=[]),
            PlanNode(id="context_gcal", agent="gcal", action="get_context", params={}, depends_on=["find_calendar_event"]),
            PlanNode(
                id="search_emails_with_client",
                agent="gmail",
                action="search",
                params={"intent": intent.intent, "entities": intent.entities},
                # Depends on the calendar lookup so we know exactly who the client
                # contacts are (attendee emails) rather than guessing from the query alone.
                depends_on=["context_gcal"],
            ),
            PlanNode(
                id="pull_drive_documents",
                agent="gdrive",
                action="search",
                params={"intent": intent.intent, "entities": intent.entities},
                depends_on=["context_gcal"],
            ),
        ]
    )


def _plan_list_events_by_attendee(intent: Intent) -> ExecutionPlan:
    """"What's on my calendar next week where john@company.com is invited?" -> a single
    filtered search, no writes, no other services."""
    return ExecutionPlan(
        nodes=[
            PlanNode(id="search_calendar", agent="gcal", action="search", params={"intent": intent.intent, "entities": intent.entities}, depends_on=[]),
        ]
    )


_TEMPLATES = {
    "cancel_flight": _plan_cancel_flight,
    "prepare_for_meeting": _plan_prepare_for_meeting,
    "list_events_by_attendee": _plan_list_events_by_attendee,
}
