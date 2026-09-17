"""Query Planner: Intent -> ExecutionPlan (a small DAG of PlanNodes).

Encodes the two rules from the brief's "Query Planner" section:
  - Parallel operations: independent searches (e.g. Gmail + Calendar) have no
    `depends_on` between them, so the executor (app/orchestrator/executor.py) can
    run them concurrently with asyncio.gather.
  - Sequential dependencies: a write action depends_on the search/get_context nodes
    that gather the information it needs first (e.g. "extract booking reference ->
    draft email" from the brief).

Earlier versions of this planner tried to *infer* write actions - which verb, which
service, what it depends on - by keyword-matching the intent label/step names (e.g.
detecting "cancel" or "move" in free text). That approach fundamentally couldn't
scale: the classifier's possible phrasing is unbounded, the same word means different
things per service ("cancel" -> draft an email for a booking, but delete the event
for a plain meeting), and every new phrasing needed a new hand-added rule here.

Instead, the Intent Classifier now decides all of that directly - see `Intent.actions`
in app/schemas.py. The LLM already resolves the query's semantics when it reads it;
this planner's only job is to mechanically turn that structured decision into a DAG.
No text is inspected here at all.
"""

from app.schemas import ExecutionPlan, Intent, PlanNode


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

    nodes: list[PlanNode] = []

    # 1. One parallel search+context pair per service the classifier flagged.
    for service in intent.services:
        nodes.append(_search_node(service, intent))
        nodes.append(_get_context_node(service))

    # 2. One execute node per action the classifier decided on - service, verb, and
    # what context it needs are all the classifier's decision, not inferred here.
    seen_ids: set[str] = set()
    for action in intent.actions:
        if action.service not in intent.services:
            # The classifier named an action for a service it never flagged as
            # touched by this query - don't act on a service it didn't confirm.
            continue

        node_id = f"execute_{action.verb}_{action.service}"
        if node_id in seen_ids:
            continue  # same (service, verb) action listed more than once - one node covers it
        seen_ids.add(node_id)

        depends_on = [f"context_{s}" for s in action.needs_context_from if s in intent.services]
        nodes.append(
            PlanNode(
                id=node_id,
                agent=action.service,
                action="execute",
                params={"verb": action.verb, "intent": intent.intent, "entities": intent.entities},
                depends_on=depends_on,
            )
        )

    return ExecutionPlan(nodes=nodes)
