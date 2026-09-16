# Single image reused by all four app-level services in docker-compose.yml (api,
# celery_worker, celery_beat) - they differ only in the `command:` they run, not the
# environment, so one Dockerfile is enough.
FROM python:3.12-slim

WORKDIR /app

# Copy requirements first (before the rest of the source) so Docker's layer cache
# skips the pip install step on rebuilds where only application code changed.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

EXPOSE 8000

# Overridden by each service's `command:` in docker-compose.yml; this default is what
# runs if the image is started standalone (`docker run`) without a compose override.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
