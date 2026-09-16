# Agentic Google Workspace Orchestrator

Natural-language orchestration across Gmail, Google Calendar, and Google Drive.
Classifies intent, plans a small execution DAG, runs Gmail/Calendar/Drive agents in
parallel where possible, and synthesizes one coherent natural-language answer.

See also: [DESIGN.md](DESIGN.md) (architecture + scaling to 1M users), [API.md](API.md)
(endpoint reference), [docs/SAMPLE_QUERIES.md](docs/SAMPLE_QUERIES.md) (worked examples
including the assignment's "hard cases"), [docs/ER_DIAGRAM.md](docs/ER_DIAGRAM.md).

## Architecture at a glance

```
User Query
  -> Intent Classifier      (app/orchestrator/intent_classifier.py)  - LLM call #1
  -> Query Planner          (app/orchestrator/planner.py)            - builds a DAG
  -> Service Orchestrator   (app/orchestrator/executor.py)           - runs the DAG
       |-> Gmail Agent      (app/orchestrator/agents/gmail_agent.py)
       |-> GCal Agent       (app/orchestrator/agents/gcal_agent.py)
       `-> Drive Agent      (app/orchestrator/agents/drive_agent.py)
  -> Embedding & Search     (app/embeddings/) - pgvector hybrid search backing every agent's search()
  -> Response Synthesizer   (app/orchestrator/synthesizer.py)        - LLM call #2
```

`app/orchestrator/pipeline.py:run_query` is the single function that wires all of the
above together for one request; `app/api/routes_query.py` is its only caller.

Background sync (`app/tasks/sync_tasks.py`, run by Celery) keeps Postgres's cached
copy of each user's Gmail/Calendar/Drive data - and its embeddings - fresh, so the
live query path never calls Google APIs on the hot path except to fetch full detail
for the top hit (`get_context`) or to perform a write (`execute`).

## Prerequisites

- Python 3.12 (3.13 currently has no prebuilt `psycopg[binary]` wheel)
- Docker + Docker Compose (for Postgres+pgvector, Redis, and optionally the whole app)
- A Google Cloud project with the Gmail, Calendar, and Drive APIs enabled, and an
  OAuth 2.0 Client ID (see "Google OAuth setup" below)
- An OpenAI API key with billing enabled

## Google OAuth setup

1. console.cloud.google.com -> create a project -> enable **Gmail API**, **Google
   Calendar API**, **Google Drive API**.
2. APIs & Services -> OAuth consent screen -> User type **External** -> stay in
   **Testing** mode -> add your own Google account under **Test users** (this avoids
   Google's app-verification process entirely for personal/demo use).
3. Add scopes: `openid`, `.../auth/userinfo.email`, `.../auth/gmail.readonly`,
   `.../auth/gmail.send`, `.../auth/gmail.compose`, `.../auth/gmail.modify`,
   `.../auth/calendar`, `.../auth/drive` (the exact URLs are in `.env.example`).
4. APIs & Services -> Credentials -> Create Credentials -> **OAuth client ID** ->
   type **Web application** -> Authorized redirect URI:
   `http://localhost:8000/api/v1/auth/google/callback`.
5. Copy the Client ID/Secret into `.env` (see below).

## Local setup

```bash
cp .env.example .env
# fill in .env: OPENAI_API_KEY, GOOGLE_CLIENT_ID, GOOGLE_CLIENT_SECRET,
# and generate real APP_SECRET_KEY / FERNET_KEY (commands are in .env.example's comments)

python3.12 -m venv .venv
source .venv/bin/activate      # or .venv\Scripts\activate on Windows
pip install -r requirements.txt

docker compose up -d postgres redis   # just the datastores; run the API on your host
alembic upgrade head                   # creates the schema (see alembic/versions/0001_initial.py)

uvicorn app.main:app --reload          # API at http://localhost:8000, docs at /docs

# in separate terminals, for background sync:
celery -A app.tasks.celery_app worker --loglevel=info
celery -A app.tasks.celery_app beat --loglevel=info
```

Or run the entire stack (API + both Celery processes + datastores) in Docker:

```bash
docker compose up -d --build
alembic upgrade head   # from the host, against the exposed localhost:5432 port
```

## Using it

1. Visit `http://localhost:8000/api/v1/auth/google` in a browser, sign in, grant
   consent. The callback returns `{"session_token": "...", "user_id": "...", "email": "..."}`.
2. Trigger an initial sync so there's data to search:
   `curl -X POST localhost:8000/api/v1/sync/trigger -H "Authorization: Bearer <session_token>"`
3. Ask a question:
   ```bash
   curl -X POST localhost:8000/api/v1/query \
     -H "Authorization: Bearer <session_token>" -H "Content-Type: application/json" \
     -d '{"query": "What'\''s on my calendar next week?"}'
   ```

See [docs/SAMPLE_QUERIES.md](docs/SAMPLE_QUERIES.md) for 10+ worked examples.

## Tests

```bash
pytest                       # unit tests only (no external services needed)
docker compose up -d postgres
alembic upgrade head
pytest -m integration        # adds the real-Postgres/pgvector hybrid-search test
```

27 unit tests mock every external dependency (OpenAI, Google APIs, Redis via
`fakeredis`) and cover the planner's DAG construction, the executor's parallel/
dependency/failure-cascade behavior, intent classification + caching, response
synthesis, rate limiting, conversation context, token encryption, and one full
agent (Gmail). One integration test runs the actual `ORDER BY embedding <=> ...`
pgvector query against a real database - it's skipped automatically if Postgres
isn't reachable.

## Embedding search quality (Precision@5)

`scripts/eval_precision.py` runs a small labeled query set against a user's synced
data and reports Precision@5 per query + the average, and the p50/p95 search
latency. See that file's docstring for how to run it and how the labels work.

## Project layout

```
app/
  config.py            typed settings (env vars)
  security.py           token-at-rest encryption + session JWTs
  schemas.py             shared Pydantic models (Intent, ExecutionPlan, ...)
  db/                    SQLAlchemy models + session/engine setup
  google/                 OAuth flow + Gmail/GCal/Drive API clients + retry policy
  embeddings/              OpenAI embedding wrapper + pgvector hybrid search
  cache/                    Redis: rate limiting, conversation context, embedding cache
  orchestrator/             intent classifier, planner, executor, synthesizer, agents/
  tasks/                     Celery app + background sync job
  api/                       FastAPI routes + auth dependency
alembic/                   migrations
tests/                      unit + one integration test
docs/                        sample queries, ER diagram, Postman collection
scripts/                     Precision@5 evaluation script
DESIGN.md, API.md            scaling strategy + API reference (kept at repo root per the assignment brief)
```
