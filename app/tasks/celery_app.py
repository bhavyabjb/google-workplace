"""Celery application: the task-queue layer from the brief's scaling architecture
("Task Queue (Celery workers)" between the API servers and the Google/LLM APIs).

Two things run against this app (see docker-compose.yml):
- `celery_worker`: executes tasks pulled off the broker queue (app/tasks/sync_tasks.py).
- `celery_beat`: the scheduler process that enqueues the periodic sync task on a timer.
"""

from celery import Celery
# Celery: the distributed task queue client/worker library.

from celery.schedules import crontab
# crontab: lets us express "every N minutes" declaratively for the beat schedule below.

from app.config import get_settings

settings = get_settings()

celery_app = Celery(
    "orchestrator",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    # Auto-discover @celery_app.task-decorated functions in this module so Celery
    # doesn't need every task path listed by hand.
    include=["app.tasks.sync_tasks"],
)

celery_app.conf.update(
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
)

# Background sync: "Pre-computation: Background sync every 15 mins, index new
# emails/events/files" from the brief's scaling strategy. The interval is
# configurable via SYNC_INTERVAL_MINUTES rather than hardcoded.
celery_app.conf.beat_schedule = {
    "sync-all-users": {
        "task": "app.tasks.sync_tasks.sync_all_users",
        "schedule": crontab(minute=f"*/{settings.sync_interval_minutes}"),
    }
}
