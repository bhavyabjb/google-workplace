"""Common interface every service agent (Gmail/GCal/Drive) implements.

Matches the brief's "Service Agents" section exactly:
    search(): Semantic search using embeddings
    execute(): Perform write operations (send, create, delete)
    get_context(): Retrieve full content for LLM reasoning

Agent methods are plain (synchronous) functions, not `async def` - the underlying
work is either a blocking Postgres query (SQLAlchemy's sync API) or a blocking
googleapiclient HTTP call (google-api-python-client has no first-class async
client). The executor (app/orchestrator/executor.py) is what provides concurrency:
it runs each node's synchronous call inside `asyncio.to_thread(...)`, so independent
nodes still execute in parallel from the caller's point of view without every agent
needing its own async HTTP stack.
"""

from abc import ABC, abstractmethod
# ABC/abstractmethod: enforces that every concrete agent implements all three methods -
# a missing override fails at class-definition time, not at first use in production.

from typing import Any

from sqlalchemy.orm import Session

from app.db.models import User


class ServiceAgent(ABC):
    """Base class for GmailAgent / GCalAgent / DriveAgent."""

    def __init__(self, db: Session, user: User):
        self.db = db
        self.user = user

    @abstractmethod
    def search(self, entities: dict[str, Any], intent: str) -> list[dict]:
        """Hybrid (metadata + vector) search over this user's cached data for this service.
        Returns a list of lightweight result dicts (id + key fields), not full content."""
        raise NotImplementedError

    @abstractmethod
    def get_context(self, search_results: list[dict]) -> dict:
        """Given search() results, fetch full content for the most relevant item(s)
        (live from Google, not just the cached preview) for the LLM to reason over."""
        raise NotImplementedError

    @abstractmethod
    def execute(self, verb: str, entities: dict[str, Any], upstream_context: dict[str, Any]) -> dict:
        """Perform a write operation (send/draft/create/update/delete/share/move).
        `upstream_context` holds the results of every node this one depends_on, so e.g.
        drafting a cancellation email can read the booking reference and calendar event
        details gathered by earlier nodes in the plan."""
        raise NotImplementedError
