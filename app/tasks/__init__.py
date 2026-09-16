# Marks `app.tasks` as a package.
# celery_app.py: the Celery application + periodic (beat) schedule.
# sync_tasks.py: the actual background job that pulls fresh Gmail/Calendar/Drive
#                data, embeds it, and upserts it into the *_cache tables.
