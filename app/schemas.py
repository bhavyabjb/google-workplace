"""Pydantic models shared across the API layer and the orchestration core.

Keeping these in one module (rather than scattering request/response shapes across
route files) means the intent classifier, planner, executor, and API routes all agree
on exactly one definition of "what does an Intent/PlanNode/etc look like."
"""

from datetime import datetime
from typing import Any, Literal
# Literal: constrains fields like `service`/`intent` to a known, closed set of string
# values instead of an unrestricted `str` - catches typos from the LLM output at
# validation time rather than deep inside the executor.

from pydantic import BaseModel, Field
# BaseModel: base class for all schemas below - gives us automatic validation,
# JSON (de)serialization, and the shapes FastAPI uses to build the OpenAPI spec.
# Field: lets us attach defaults/descriptions to individual fields.

Service = Literal["gmail", "gcal", "gdrive"]


class Intent(BaseModel):
    """Structured output of the Intent Classifier (see app/orchestrator/intent_classifier.py).

    Mirrors the JSON shape given in the assignment brief:
    {"services": [...], "intent": "...", "entities": {...}, "steps": [...]}
    """

    services: list[Service] = Field(description="Which Google service(s) this query touches")
    intent: str = Field(description="Short machine-readable intent label, e.g. 'cancel_flight'")
    entities: dict[str, Any] = Field(default_factory=dict, description="Extracted entities, e.g. {'airline': 'Turkish Airlines'}")
    steps: list[str] = Field(default_factory=list, description="Human-readable step names, e.g. ['search_gmail_for_booking', ...]")
    needs_clarification: bool = Field(default=False, description="True if the query is too ambiguous to safely execute (e.g. 'move the meeting with John')")
    clarification_question: str | None = Field(default=None, description="Question to ask the user when needs_clarification is True")


class PlanNode(BaseModel):
    """One node in the Query Planner's execution DAG (see app/orchestrator/planner.py)."""

    id: str = Field(description="Unique id for this step within the plan, e.g. 'search_gmail'")
    agent: Service = Field(description="Which service agent executes this node")
    action: str = Field(description="Method name on the agent, e.g. 'search', 'execute', 'get_context'")
    params: dict[str, Any] = Field(default_factory=dict, description="Arguments passed to the agent method")
    depends_on: list[str] = Field(default_factory=list, description="Node ids that must complete before this one runs")
    optional: bool = Field(default=False, description="If True, a failure here doesn't fail the whole plan (graceful degradation)")


class ExecutionPlan(BaseModel):
    """The full DAG produced by the planner and consumed by the orchestrator/executor."""

    nodes: list[PlanNode]


class NodeResult(BaseModel):
    """Outcome of executing one PlanNode, collected by the executor and fed to the synthesizer."""

    node_id: str
    status: Literal["success", "error", "skipped"]
    data: Any = None
    error: str | None = None


class QueryRequest(BaseModel):
    """Body of POST /api/v1/query."""

    query: str
    conversation_id: str | None = None


class QueryResponse(BaseModel):
    """Response of POST /api/v1/query."""

    response: str
    actions_taken: list[str]
    intent: Intent
    conversation_id: str


class SyncStatusEntry(BaseModel):
    service: Service
    last_synced_at: datetime | None
    status: Literal["pending", "ok", "error"]
    error: str | None = None
