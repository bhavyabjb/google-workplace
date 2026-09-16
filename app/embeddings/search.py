"""Hybrid search: cheap metadata filtering first, then vector similarity ranking.

The brief's hint is explicit: "Metadata filtering > pure vector search for speed."
Concretely, that means every search here does:
  1. A `WHERE user_id = ... AND <indexed metadata columns>` filter first (uses the
     b-tree indexes on user_id/sender/received_at/start_time/etc from app/db/models.py) -
     this narrows an unbounded table down to a small candidate set cheaply.
  2. Only THEN computes cosine distance against the (much smaller) candidate set and
     orders by it - this is what actually uses the ivfflat vector index / does the
     expensive part, but on far fewer rows than a naive "rank everything by distance"
     query would touch.

This ordering is what keeps us under the <500ms target as data grows: without step 1,
a semantic search over a user with 50k emails would need to rank all 50k vectors.
"""

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import GCalCache, GDriveCache, GmailCache


def search_gmail(
    db: Session,
    user_id: str,
    query_embedding: list[float],
    sender: str | None = None,
    received_after: datetime | None = None,
    received_before: datetime | None = None,
    limit: int = 5,
) -> list[GmailCache]:
    """Semantic + metadata search over a user's cached emails."""
    stmt = select(GmailCache).where(GmailCache.user_id == user_id)

    # --- metadata filters (cheap, indexed) applied before ranking ---
    if sender:
        stmt = stmt.where(GmailCache.sender.ilike(f"%{sender}%"))
    if received_after:
        stmt = stmt.where(GmailCache.received_at >= received_after)
    if received_before:
        stmt = stmt.where(GmailCache.received_at <= received_before)

    # --- vector similarity ranking (expensive, done last, on the filtered set) ---
    # cosine_distance() is provided by pgvector.sqlalchemy's Vector column type and
    # compiles to Postgres' `<=>` operator, which the ivfflat index accelerates.
    stmt = stmt.order_by(GmailCache.embedding.cosine_distance(query_embedding)).limit(limit)

    return list(db.execute(stmt).scalars().all())


def search_gcal(
    db: Session,
    user_id: str,
    query_embedding: list[float] | None,
    time_min: datetime | None = None,
    time_max: datetime | None = None,
    attendee_email: str | None = None,
    limit: int = 20,
) -> list[GCalCache]:
    """Semantic + metadata search over a user's cached calendar events.

    `query_embedding` is optional here: a query like "what's on my calendar next
    week" has no semantic content to rank by - it's pure metadata filtering (date
    range), so we skip the vector ordering entirely in that case and just sort by
    start_time. This matters for the "single-service, no ambiguity" query type -
    running an unnecessary embedding+ANN search would be pure overhead.
    """
    stmt = select(GCalCache).where(GCalCache.user_id == user_id)

    if time_min:
        stmt = stmt.where(GCalCache.start_time >= time_min)
    if time_max:
        stmt = stmt.where(GCalCache.start_time <= time_max)
    if attendee_email:
        # JSONB containment: attendees is a JSON array of email strings, e.g. ["john@company.com"].
        # `@>` (contains) is index-friendly under a GIN index if one is added later; for
        # this assignment's data volume a sequential scan over the already-date-filtered
        # rows is fine.
        stmt = stmt.where(GCalCache.attendees.op("@>")([attendee_email]))

    if query_embedding is not None:
        stmt = stmt.order_by(GCalCache.embedding.cosine_distance(query_embedding))
    else:
        stmt = stmt.order_by(GCalCache.start_time)

    stmt = stmt.limit(limit)
    return list(db.execute(stmt).scalars().all())


def search_gdrive(
    db: Session,
    user_id: str,
    query_embedding: list[float] | None,
    mime_type: str | None = None,
    modified_after: datetime | None = None,
    limit: int = 5,
) -> list[GDriveCache]:
    """Semantic + metadata search over a user's cached Drive files."""
    stmt = select(GDriveCache).where(GDriveCache.user_id == user_id)

    if mime_type:
        stmt = stmt.where(GDriveCache.mime_type == mime_type)
    if modified_after:
        stmt = stmt.where(GDriveCache.modified_at >= modified_after)

    if query_embedding is not None:
        stmt = stmt.order_by(GDriveCache.embedding.cosine_distance(query_embedding))
    else:
        stmt = stmt.order_by(GDriveCache.modified_at.desc())

    stmt = stmt.limit(limit)
    return list(db.execute(stmt).scalars().all())
