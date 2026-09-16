"""Service Orchestrator: executes an ExecutionPlan's DAG, running independent nodes
in parallel and respecting `depends_on` ordering, per the brief's "Service
Orchestrator (Parallel execution)" box in the architecture diagram.

Failure handling ("Cross-Service Dependencies: Failures must be handled gracefully -
Gmail succeeds, Calendar fails" from the brief's "What Makes This Hard" section):
a node whose dependency failed is marked "skipped" rather than crashing the whole
plan - every OTHER branch of the DAG that doesn't depend on the failed node still
runs to completion. The response synthesizer then reports what succeeded and what
didn't, instead of the whole request failing on one bad Google API call.
"""

import asyncio
# asyncio: drives the parallel execution. Agent methods are synchronous (blocking
# Postgres/Google API calls) - see app/orchestrator/agents/base.py's docstring for
# why we wrap each one in asyncio.to_thread() rather than making every agent async.

from app.orchestrator.agents.base import ServiceAgent
from app.schemas import ExecutionPlan, NodeResult, PlanNode


async def execute_plan(plan: ExecutionPlan, agents: dict[str, ServiceAgent]) -> dict[str, NodeResult]:
    """Run every node in `plan`, respecting dependencies, and return all NodeResults
    keyed by node id. `agents` maps service name ("gmail"/"gcal"/"gdrive") -> agent instance.
    """
    results: dict[str, NodeResult] = {}
    remaining: dict[str, PlanNode] = {n.id: n for n in plan.nodes}

    # Topological execution in "waves": each iteration runs every node whose
    # dependencies are already resolved, all concurrently, then moves to the next wave.
    # This is what gives us "Search Gmail + Calendar simultaneously" while still
    # letting "draft_cancellation_email" wait for both of them to finish first.
    while remaining:
        ready = [n for n in remaining.values() if all(dep in results for dep in n.depends_on)]

        if not ready:
            # Every remaining node is waiting on something that will never resolve
            # (a cycle, or a typo'd depends_on id from the planner) - fail loudly
            # rather than spin forever, and mark the stragglers as errored.
            for node in remaining.values():
                results[node.id] = NodeResult(node_id=node.id, status="error", error="Unresolvable dependency in plan")
            break

        node_results = await asyncio.gather(*(_run_node(node, agents, results) for node in ready))
        for node_result in node_results:
            results[node_result.node_id] = node_result
            del remaining[node_result.node_id]

    return results


async def _run_node(node: PlanNode, agents: dict[str, ServiceAgent], prior_results: dict[str, NodeResult]) -> NodeResult:
    # If any dependency failed outright (not just "skipped"), this node can't safely
    # run - propagate the skip rather than calling the agent with incomplete context.
    failed_deps = [d for d in node.depends_on if prior_results[d].status == "error"]
    if failed_deps:
        return NodeResult(node_id=node.id, status="skipped", error=f"Upstream dependency failed: {failed_deps}")

    upstream_context = {dep: prior_results[dep].data for dep in node.depends_on}
    agent = agents[node.agent]

    try:
        if node.action == "search":
            data = await asyncio.to_thread(agent.search, node.params.get("entities", {}), node.params.get("intent", ""))
        elif node.action == "get_context":
            # get_context takes the search results from its (single) dependency.
            search_results = next(iter(upstream_context.values()), [])
            data = await asyncio.to_thread(agent.get_context, search_results)
        elif node.action == "execute":
            data = await asyncio.to_thread(agent.execute, node.params.get("verb", ""), node.params.get("entities", {}), upstream_context)
        else:
            raise ValueError(f"Unknown plan node action: {node.action!r}")

        return NodeResult(node_id=node.id, status="success", data=data)

    except Exception as exc:  # noqa: BLE001 - deliberately broad: any agent failure (Google API error,
        # network timeout, bad LLM output) must degrade this one node, not crash the whole orchestration.
        if node.optional:
            return NodeResult(node_id=node.id, status="skipped", error=str(exc))
        return NodeResult(node_id=node.id, status="error", error=str(exc))
