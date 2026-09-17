# ORM models = the actual Postgres schema, expressed as Python classes. This extends
# the "Database Schema (Simplified)" from the assignment brief: users/conversations/
# gmail_cache are as specified, and gcal_cache/gdrive_cache/sync_status/audit_log are
# the "similar tables" and security/observability pieces the brief calls out as needed
# but leaves simplified. Alembic migrations (alembic/versions/*.py) are generated from
# (and must stay in sync with) these classes.

import uuid
# uuid: primary keys are UUIDs (not auto-increment ints) so IDs are safe to generate
# client-side and never collide across shards - relevant for the "sharding by user_id"
# scaling strategy described in DESIGN.md.

from datetime import datetime
# datetime: type hint for all timestamp columns.

from pgvector.sqlalchemy import Vector
# Vector: the pgvector SQLAlchemy column type. Vector(1536) creates a Postgres
# `vector(1536)` column that supports cosine-distance queries.

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func
# DateTime: all timestamps are timezone-aware (DateTime(timezone=True)).
# ForeignKey: enforces referential integrity to users.id.
# Index / UniqueConstraint: declared per-table below (e.g. the ivfflat vector index).
# func: gives us func.now() for server-side default/onupdate timestamps.

from sqlalchemy.dialects.postgresql import JSONB, UUID
# UUID: Postgres-native UUID column type (stored as 16 bytes, not a 36-char string).
# JSONB (not plain JSON): stores semi-structured data (intent payloads, attendee
# lists) in Postgres' binary JSON format, which supports indexing and the `@>`
# containment operator - used by app/embeddings/search.py to filter events by
# attendee email (e.g. "calendar next week where john@company.com is invited").

from sqlalchemy.orm import Mapped, mapped_column, relationship
# Mapped/mapped_column: SQLAlchemy 2.0 typed ORM column declaration style.
# relationship: declares the Python-level link between User and its Conversations.

from app.config import get_settings
from app.db.session import Base

# Read once at import time so every embedding column below uses the same dimensionality
# as configured via OPENAI_EMBEDDING_MODEL / EMBEDDING_DIMENSIONS in .env.
EMBEDDING_DIM = get_settings().embedding_dimensions


class User(Base):
    """One row per person who has connected their Google account via OAuth."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    # Google OAuth tokens - stored Fernet-encrypted (see app/security.py encrypt_token/
    # decrypt_token). NEVER read/write these columns with plaintext values.
    google_access_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    google_refresh_token: Mapped[str | None] = mapped_column(Text, nullable=True)
    google_token_expiry: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # User's IANA timezone (e.g. "America/New_York"), used for temporal reasoning like
    # "next week" / "tomorrow" in the intent classifier and query planner.
    timezone: Mapped[str] = mapped_column(String(64), default="UTC")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    # One-to-many: a user has many past conversations (used for the "last 5 queries" context).
    conversations: Mapped[list["Conversation"]] = relationship(back_populates="user")


class Conversation(Base):
    """One row per natural-language query the orchestrator has answered.

    Doubles as the conversation-context store: "that email about the proposal" is
    resolved by looking at this user's most recent rows (see app/cache/conversation.py).
    """

    __tablename__ = "conversations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), index=True)
    query: Mapped[str] = mapped_column(Text)                 # the raw user query
    intent: Mapped[dict] = mapped_column(JSONB)                # structured output of the intent classifier
    response: Mapped[str] = mapped_column(Text)                # final synthesized natural-language answer
    actions_taken: Mapped[dict] = mapped_column(JSONB, default=list)  # e.g. ["drafted email", "found event"]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)

    user: Mapped["User"] = relationship(back_populates="conversations")


class GmailCache(Base):
    """Local mirror of Gmail messages, enriched with an embedding for semantic search.

    We never query the Gmail API live on the hot path for search - we sync into this
    table on a schedule (app/tasks/sync_tasks.py) and search happens against Postgres,
    which is what keeps query latency low and respects Gmail's rate limits.
    """

    __tablename__ = "gmail_cache"
    __table_args__ = (
        # One cached row per (user, gmail message id) - re-syncing an email upserts, never duplicates.
        UniqueConstraint("user_id", "email_id", name="uq_gmail_user_email"),
        # Approximate-nearest-neighbor index for cosine similarity search over `embedding`.
        # ivfflat trades a little recall for large speedups at scale vs. exact scan.
        Index(
           "ix_gmail_embedding", "embedding",
            postgresql_using="ivfflat",
            postgresql_with={"lists": 100},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    ) 

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), index=True)
    email_id: Mapped[str] = mapped_column(String(255))            # Gmail API message id
    thread_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    subject: Mapped[str | None] = mapped_column(Text, nullable=True)
    sender: Mapped[str | None] = mapped_column(String(255), index=True, nullable=True)  # indexed: cheap metadata filter
    recipients: Mapped[list] = mapped_column(JSONB, default=list)
    body_preview: Mapped[str | None] = mapped_column(Text, nullable=True)   # truncated body used for embedding + display
    labels: Mapped[list] = mapped_column(JSONB, default=list)                # Gmail label ids (INBOX, IMPORTANT, ...)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)  # subject+body embedding
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True, nullable=True)  # indexed: date-range filter
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class GCalCache(Base):
    """Local mirror of Calendar events, enriched with an embedding for semantic search."""

    __tablename__ = "gcal_cache"
    __table_args__ = (
        UniqueConstraint("user_id", "event_id", name="uq_gcal_user_event"),
        Index(
            "ix_gcal_embedding", "embedding",
            postgresql_using="ivfflat",
            postgresql_with={"lists": 100},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), index=True)
    event_id: Mapped[str] = mapped_column(String(255))          # Calendar API event id
    calendar_id: Mapped[str] = mapped_column(String(255), default="primary")
    title: Mapped[str | None] = mapped_column(Text, nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    location: Mapped[str | None] = mapped_column(Text, nullable=True)
    attendees: Mapped[list] = mapped_column(JSONB, default=list)   # list of attendee emails, used for "where X is invited" queries
    status: Mapped[str] = mapped_column(String(32), default="confirmed")  # confirmed | tentative | cancelled
    start_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True, nullable=True)  # indexed: "next week" range filter
    end_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)  # title+description embedding
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class GDriveCache(Base):
    """Local mirror of Drive file metadata (+ text preview), enriched with an embedding."""

    __tablename__ = "gdrive_cache"
    __table_args__ = (
        UniqueConstraint("user_id", "file_id", name="uq_gdrive_user_file"),
        Index(
            "ix_gdrive_embedding", "embedding",
            postgresql_using="ivfflat",
            postgresql_with={"lists": 100},
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), index=True)
    file_id: Mapped[str] = mapped_column(String(255))          # Drive API file id
    name: Mapped[str | None] = mapped_column(Text, nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)   # e.g. application/pdf, used for "PDFs in Drive" filter
    content_preview: Mapped[str | None] = mapped_column(Text, nullable=True)     # extracted text snippet used for embedding
    web_view_link: Mapped[str | None] = mapped_column(Text, nullable=True)
    parents: Mapped[list] = mapped_column(JSONB, default=list)                     # parent folder ids
    embedding: Mapped[list[float] | None] = mapped_column(Vector(EMBEDDING_DIM), nullable=True)  # name+content embedding
    modified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True, nullable=True)  # indexed: "from last month" filter
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


class SyncStatus(Base):
    """Tracks the last successful background sync per (user, service).

    Backs GET /api/v1/sync/status and lets the sync task know how far back it needs
    to look (incremental sync) rather than re-fetching everything every 15 minutes.
    """

    __tablename__ = "sync_status"
    __table_args__ = (UniqueConstraint("user_id", "service", name="uq_sync_user_service"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), index=True)
    service: Mapped[str] = mapped_column(String(32))   # "gmail" | "gcal" | "gdrive"
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="pending")   # pending | ok | error
    error: Mapped[str | None] = mapped_column(Text, nullable=True)         # last error message, if status == "error"


class AuditLog(Base):
    """Immutable record of every write/side-effecting action the orchestrator takes.

    Covers the "Security: audit logging" requirement from the brief - e.g. every
    send_email/create_event/delete_event/share_file call is recorded here regardless
    of whether it succeeded, for compliance and debugging ("why did it email support@...").
    """

    __tablename__ = "audit_log"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), ForeignKey("users.id"), index=True)
    action: Mapped[str] = mapped_column(String(64))            # e.g. "gmail.send_email", "gcal.delete_event"
    service: Mapped[str] = mapped_column(String(32))            # "gmail" | "gcal" | "gdrive"
    resource_id: Mapped[str | None] = mapped_column(String(255), nullable=True)   # id of the email/event/file acted on
    status: Mapped[str] = mapped_column(String(32))              # "success" | "error" | "dry_run"
    details: Mapped[dict] = mapped_column(JSONB, default=dict)     # arbitrary extra context (request params, error text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
