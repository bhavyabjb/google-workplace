"""Sync routes from the brief:

    POST /api/v1/sync/trigger -> Manually sync Gmail/GCal/Drive
    GET  /api/v1/sync/status  -> Last sync timestamps per service
"""

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.api.deps import get_current_user
from app.db.models import SyncStatus, User
from app.db.session import get_db
from app.schemas import SyncStatusEntry
from app.tasks.sync_tasks import sync_user_data

router = APIRouter(prefix="/api/v1/sync", tags=["sync"])


@router.post("/trigger")
def trigger_sync(user: User = Depends(get_current_user)) -> dict:
    """Enqueue an immediate sync for the current user rather than waiting for the
    next celery_beat tick (useful right after first login, or for the demo video)."""
    task = sync_user_data.delay(str(user.id))
    return {"task_id": task.id, "status": "queued"}


@router.get("/status", response_model=list[SyncStatusEntry])
def sync_status(user: User = Depends(get_current_user), db: Session = Depends(get_db)) -> list[SyncStatusEntry]:
    rows = db.query(SyncStatus).filter(SyncStatus.user_id == user.id).all()
    return [
        SyncStatusEntry(service=r.service, last_synced_at=r.last_synced_at, status=r.status, error=r.error)
        for r in rows
    ]
