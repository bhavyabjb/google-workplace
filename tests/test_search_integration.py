"""Integration test for the hybrid search layer (app/embeddings/search.py) against a
REAL Postgres+pgvector database - unlike every other test file, this one can't be
faked/mocked away, since the whole point is verifying the actual `ORDER BY
embedding <=> :query_vector` SQL pgvector compiles to behaves correctly.

Skipped automatically if DATABASE_URL isn't reachable (e.g. this sandbox has no
Postgres running) - run `docker compose up -d postgres` first, then
`pytest -m integration` to actually exercise this.
"""

import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db.models import GmailCache, User
from app.db.session import Base
from app.embeddings.search import search_gmail

settings = get_settings()


def _postgres_available() -> bool:
    try:
        engine = create_engine(settings.database_url)
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
        return True
    except OperationalError:
        return False


pytestmark = pytest.mark.skipif(not _postgres_available(), reason="requires a real Postgres+pgvector instance (see docker-compose.yml)")


@pytest.fixture
def db_session():
    """Yields a Session whose writes are rolled back at teardown, so this test never
    leaves rows behind - and, importantly, never drops tables. This fixture may run
    against the same Postgres instance `alembic upgrade head` has already set up
    (see docker-compose.yml); a `Base.metadata.drop_all()` here would rip out the
    real migration-managed schema out from under it (alembic_version would then
    claim migration 0001 is applied while its tables no longer exist) - exactly the
    kind of destructive-teardown mistake called out in this environment's safety
    guidelines. create_all is safe to call unconditionally (checkfirst=True by
    default: it no-ops on tables that already exist).
    """
    engine = create_engine(settings.database_url)
    with engine.connect() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.commit()
    Base.metadata.create_all(engine)

    session = Session(bind=engine)
    yield session
    session.rollback()
    session.close()


def _unit_vector(dim: int, hot_index: int) -> list[float]:
    """A simple one-hot-ish vector so we can construct emails that are provably
    closer/further from a query vector without needing real OpenAI embeddings."""
    vec = [0.01] * dim
    vec[hot_index] = 1.0
    return vec


@pytest.mark.integration
def test_search_gmail_orders_by_cosine_similarity_and_respects_sender_filter(db_session):
    dim = settings.embedding_dimensions
    user = User(id=uuid.uuid4(), email="tester@example.com")
    db_session.add(user)
    db_session.flush()

    close_match = GmailCache(
        user_id=user.id, email_id="close", subject="Turkish Airlines booking", sender="airline@turkish.com",
        embedding=_unit_vector(dim, 0),
    )
    far_match = GmailCache(
        user_id=user.id, email_id="far", subject="Unrelated newsletter", sender="news@example.com",
        embedding=_unit_vector(dim, 1),
    )
    db_session.add_all([close_match, far_match])
    db_session.flush()  # visible to subsequent queries in this session/transaction; rolled back at teardown, never committed

    query_vector = _unit_vector(dim, 0)  # identical direction to `close_match`
    results = search_gmail(db_session, str(user.id), query_vector, limit=5)

    assert [r.email_id for r in results] == ["close", "far"]  # closer vector ranked first

    filtered = search_gmail(db_session, str(user.id), query_vector, sender="turkish", limit=5)
    assert [r.email_id for r in filtered] == ["close"]  # metadata filter excludes the other row entirely
