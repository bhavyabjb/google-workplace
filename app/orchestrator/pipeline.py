"""Top-level orchestration pipeline: wires together every stage in the brief's
architecture diagram (Intent Classifier -> Query Planner -> Service Orchestrator ->
Response Synthesizer) into the single function the /api/v1/query route calls.

This is deliberately the only place that knows the full pipeline order - route
handlers, tests, and the Celery task that might replay a query all call `run_query`
rather than re-assembling these stages themselves.
"""

from datetime import datetime, timezone
import uuid

from sqlalchemy.orm import Session

from app.cache.conversation import get_recent_context, record_turn
from app.db.models import Conversation, User
from app.orchestrator.agents.drive_agent import DriveAgent
from app.orchestrator.agents.gcal_agent import GCalAgent
from app.orchestrator.agents.gmail_agent import GmailAgent
from app.orchestrator.executor import execute_plan
from app.orchestrator.intent_classifier import classify_intent
from app.orchestrator.planner import build_plan
from app.orchestrator.synthesizer import synthesize_response
from app.schemas import QueryResponse

# Maps a service name to its agent class. Only instantiated for services the plan
# actually touches, not all three every time (no reason to construct a DriveAgent for
# a pure-calendar query).
AGENT_CLASSES = {"gmail": GmailAgent, "gcal": GCalAgent, "gdrive": DriveAgent}


async def run_query(db: Session, user: User, query: str) -> QueryResponse:
    """Classify -> plan -> execute -> synthesize -> persist, for one user query."""

    conversation_context = get_recent_context(str(user.id))
    now_iso = datetime.now(timezone.utc).isoformat()

    intent = classify_intent(query, conversation_context, user.timezone, now_iso)

    if intent.needs_clarification:
        # Ambiguity handling ("Move the meeting with John" -> which John?): stop here
        # rather than guessing. No plan is built, no Google API calls are made.
        response_text = intent.clarification_question or "Could you clarify your request?"
        actions_taken: list[str] = []
    else:
        plan = build_plan(intent)

        # Build only the agents this specific plan needs.
        needed_services = {node.agent for node in plan.nodes}
        agents = {service: AGENT_CLASSES[service](db, user) for service in needed_services}

        results = await execute_plan(plan, agents)
        response_text, actions_taken = synthesize_response(query, intent, results)

    conversation = Conversation(
        id=uuid.uuid4(),
        user_id=user.id,
        query=query,
        intent=intent.model_dump(),
        response=response_text,
        actions_taken=actions_taken,
    )
    db.add(conversation)
    db.commit()

    # Update the fast-path Redis context AFTER the durable Postgres write succeeds,
    # so the two never disagree about what happened (see app/cache/conversation.py).
    record_turn(str(user.id), query, intent.model_dump(), response_text)

    return QueryResponse(
        response=response_text,
        actions_taken=actions_taken,
        intent=intent,
        conversation_id=str(conversation.id),
    )
