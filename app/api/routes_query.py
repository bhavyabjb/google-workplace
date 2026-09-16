"""POST /api/v1/query - the main orchestration endpoint from the brief:

    {"query": "Cancel my Turkish Airlines flight", "conversation_id": "uuid"}
    -> Response: Natural language + actions_taken

`conversation_id` in the request is accepted for API-shape compatibility with the
brief but isn't required to look anything up - conversation CONTEXT is resolved
automatically per-user from the last 5 turns (app/cache/conversation.py), so the
client doesn't need to manage/pass any session state beyond the auth token itself.
"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.cache.rate_limit import RateLimitExceeded, check_and_increment
from app.db.models import User
from app.db.session import get_db
from app.orchestrator.pipeline import run_query
from app.schemas import QueryRequest, QueryResponse

router = APIRouter(prefix="/api/v1", tags=["query"])


@router.post("/query", response_model=QueryResponse)
async def query(
    request: QueryRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
) -> QueryResponse:
    # Rate limit BEFORE any LLM/Google API work - fail fast and cheap (brief: "100
    # queries/user/hour").
    try:
        check_and_increment(str(user.id))
    except RateLimitExceeded as exc:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, str(exc)) from exc

    return await run_query(db, user, request.query)
