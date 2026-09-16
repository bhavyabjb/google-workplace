# Sets up the SQLAlchemy engine/session factory and the declarative Base that all
# ORM models (app/db/models.py) inherit from. Every DB-touching piece of the app
# depends on `get_db()` to obtain a request-scoped Session.

from collections.abc import Generator
# Generator: type hint for get_db(), which is a generator-based FastAPI dependency
# (yields a session, then closes it in the `finally` block after the request finishes).

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker
# create_engine: builds the connection pool to Postgres.
# sessionmaker: factory that produces new Session objects bound to that engine.
# DeclarativeBase: base class ORM models subclass to get table-mapping behavior.
# Session: type hint for the object get_db() yields.

from app.config import get_settings

settings = get_settings()

# The engine owns a connection pool. pool_pre_ping avoids handing out dead connections
# (e.g. after Postgres restarts); pool_size/max_overflow bound how many concurrent
# connections this process can open, which matters once we're running under multiple
# uvicorn workers / Celery workers hitting the same Postgres instance.
engine = create_engine(settings.database_url, pool_pre_ping=True, pool_size=20, max_overflow=10)

# autoflush/autocommit are both off so we're explicit about when SQL is sent and when
# transactions commit - avoids surprising half-committed state inside orchestration logic.
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    """Shared declarative base; every table model in app/db/models.py inherits this."""
    pass


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency: yields one Session per request and guarantees it's closed.

    Usage: `db: Session = Depends(get_db)` in a route function.
    """
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
