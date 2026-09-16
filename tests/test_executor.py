"""Tests for the Service Orchestrator's DAG executor (app/orchestrator/executor.py),
using fake in-memory agents instead of real Gmail/Calendar/Drive/Postgres calls -
this module only cares about dependency ordering and failure propagation, which is
pure asyncio logic independent of what an agent actually does.
"""

import pytest

from app.orchestrator.agents.base import ServiceAgent
from app.orchestrator.executor import execute_plan
from app.schemas import ExecutionPlan, PlanNode


class FakeAgent(ServiceAgent):
    """A ServiceAgent whose behavior is fully scripted by the test, with a call log
    so tests can assert *what ran* and *in what order*, without a real DB/Google API."""

    def __init__(self, name: str, calls: list[str], should_fail: bool = False):
        self.name = name
        self.calls = calls
        self.should_fail = should_fail

    def search(self, entities, intent):
        self.calls.append(f"{self.name}.search")
        if self.should_fail:
            raise RuntimeError(f"{self.name} search failed")
        return [{"id": f"{self.name}-item"}]

    def get_context(self, search_results):
        self.calls.append(f"{self.name}.get_context")
        return {"detail": search_results}

    def execute(self, verb, entities, upstream_context):
        self.calls.append(f"{self.name}.execute")
        return {"verb": verb, "upstream": upstream_context}


@pytest.mark.asyncio
async def test_independent_nodes_both_run():
    calls: list[str] = []
    plan = ExecutionPlan(
        nodes=[
            PlanNode(id="search_gmail", agent="gmail", action="search", params={}, depends_on=[]),
            PlanNode(id="search_gcal", agent="gcal", action="search", params={}, depends_on=[]),
        ]
    )
    agents = {"gmail": FakeAgent("gmail", calls), "gcal": FakeAgent("gcal", calls)}

    results = await execute_plan(plan, agents)

    assert results["search_gmail"].status == "success"
    assert results["search_gcal"].status == "success"
    assert set(calls) == {"gmail.search", "gcal.search"}


@pytest.mark.asyncio
async def test_dependent_node_waits_for_and_receives_upstream_data():
    calls: list[str] = []
    plan = ExecutionPlan(
        nodes=[
            PlanNode(id="search_gmail", agent="gmail", action="search", params={}, depends_on=[]),
            PlanNode(id="execute_gmail", agent="gmail", action="execute", params={"verb": "draft"}, depends_on=["search_gmail"]),
        ]
    )
    agents = {"gmail": FakeAgent("gmail", calls)}

    results = await execute_plan(plan, agents)

    assert results["search_gmail"].status == "success"
    assert results["execute_gmail"].status == "success"
    # search ran before execute (dependency order respected).
    assert calls == ["gmail.search", "gmail.execute"]
    # The execute node actually received the search node's output as upstream context.
    assert results["execute_gmail"].data["upstream"] == {"search_gmail": [{"id": "gmail-item"}]}


@pytest.mark.asyncio
async def test_failed_node_marks_dependents_as_skipped_not_crashed():
    calls: list[str] = []
    plan = ExecutionPlan(
        nodes=[
            PlanNode(id="search_gcal", agent="gcal", action="search", params={}, depends_on=[]),
            PlanNode(id="search_gmail", agent="gmail", action="search", params={}, depends_on=[]),
            PlanNode(id="execute_gcal", agent="gcal", action="execute", params={"verb": "delete"}, depends_on=["search_gcal"]),
        ]
    )
    # gcal's search is scripted to fail; gmail's search is independent and unaffected.
    agents = {"gcal": FakeAgent("gcal", calls, should_fail=True), "gmail": FakeAgent("gmail", calls)}

    results = await execute_plan(plan, agents)

    assert results["search_gcal"].status == "error"
    assert results["search_gmail"].status == "success"  # unrelated branch still completes
    assert results["execute_gcal"].status == "skipped"  # depended on the failed node
    # gcal.search ran (and raised) but gcal.execute must never have been invoked, since
    # the executor short-circuits dependents of a failed node before calling the agent.
    assert "gcal.execute" not in calls
