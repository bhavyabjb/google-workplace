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
- An OpenAI API key with billing enabled - or any OpenAI-compatible provider (e.g.
  Gemini's OpenAI-compatible endpoint); see "Using a different LLM provider" below

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
```

If you're filling in `.env` yourself, or you received one from someone else with
some values already set (e.g. `GOOGLE_CLIENT_ID`/`GOOGLE_CLIENT_SECRET`), here's what
needs to be **your own** value versus what's safe to leave as-is:

| Variable | Required | Notes |
|---|---|---|
| `OPENAI_API_KEY` | Yes, your own | Create one at [platform.openai.com/api-keys](https://platform.openai.com/api-keys), then add a payment method at [platform.openai.com/settings/organization/billing](https://platform.openai.com/settings/organization/billing) - a syntactically valid key with **no credits** still fails every request with a 429 `insufficient_quota` error. Never reuse someone else's key; it bills their account for your usage. (Want to avoid OpenAI billing entirely? See "Using a different LLM provider" below for Gemini's free tier instead.) |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | Yes | Either create your own via "Google OAuth setup" above, **or** reuse someone else's *if* they add your Google account under their OAuth consent screen's **Test users** list in Google Cloud Console - the app stays in "Testing" mode, so only whitelisted accounts can complete login, regardless of whose client credentials are configured. |
| `APP_SECRET_KEY` | Yes, your own | Signs session JWTs. Generate with `python -c "import secrets; print(secrets.token_hex(32))"`. Don't reuse someone else's value - anyone holding it could forge a session token your instance would accept as valid. |
| `FERNET_KEY` | Recommended, your own | Encrypts Google OAuth tokens at rest. If left blank/invalid, `app/security.py` auto-generates a random one at startup - fine on a fresh database, but encrypted tokens won't survive a process restart. Generate a stable one with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. |

Everything else (`DATABASE_URL`, `REDIS_URL`, `CELERY_BROKER_URL`/`CELERY_RESULT_BACKEND`,
`GOOGLE_REDIRECT_URI`, `GOOGLE_SCOPES`, model names, etc.) is either a local
docker-compose address or a plain default - safe to leave as given unless you've
changed ports or want a different LLM provider (see "Using a different LLM provider"
below).

```bash
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

Or skip the curl commands entirely and use the local UI:

```bash
streamlit run streamlit_app.py   # http://localhost:8501
```

`streamlit_app.py` drives the same API over HTTP - Google OAuth login (no manual
token copy-pasting needed if `FRONTEND_REDIRECT_URL` is set in `.env`, see
`.env.example`), a sync trigger/status panel, and a chat interface over
`POST /api/v1/query`. It holds no orchestration logic of its own.

## Using a different LLM provider (Gemini, etc.)

`app/llm_client.py` centralizes every OpenAI SDK client construction and JSON-response
parsing in the app, so pointing at any OpenAI-compatible provider instead of real
OpenAI is an `.env` change, not a code change. For Gemini:

1. Get a free API key from [aistudio.google.com/apikey](https://aistudio.google.com/apikey)
   (sign in with any Google account, click "Create API key").
2. Set the following in `.env` (verify the exact chat model id at
   [ai.google.dev/gemini-api/docs/models](https://ai.google.dev/gemini-api/docs/models) -
   Gemini's model lineup changes faster than this file gets updated):

```
OPENAI_API_KEY=<your Gemini API key>
OPENAI_BASE_URL=https://generativelanguage.googleapis.com/v1beta/openai/
OPENAI_CHAT_MODEL=<a current Gemini flash model id>
OPENAI_EMBEDDING_MODEL=gemini-embedding-001
EMBEDDING_DIMENSIONS=1536   # keep this - see the comment in app/embeddings/embedder.py
```

Then run `alembic upgrade head` to apply
`alembic/versions/0002_reset_embedding_cache_for_provider_switch.py`, which clears
cached embeddings (a vector from one model isn't comparable to another, even at the
same dimension) and forces a full resync.

## Tests

```bash
pytest                       # unit tests only (no external services needed)
docker compose up -d postgres
alembic upgrade head
pytest -m integration        # adds the real-Postgres/pgvector hybrid-search test
```

31 unit tests mock every external dependency (OpenAI, Google APIs, Redis via
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
  embeddings/              embedding wrapper (OpenAI or any OpenAI-compatible provider) + pgvector hybrid search
  llm_client.py            shared OpenAI SDK client factory (OPENAI_BASE_URL) + tolerant JSON parsing
  cache/                    Redis: rate limiting, conversation context, embedding cache
  orchestrator/             intent classifier, planner, executor, synthesizer, agents/
  tasks/                     Celery app + background sync job
  api/                       FastAPI routes + auth dependency
alembic/                   migrations
tests/                      unit + one integration test
docs/                        sample queries, ER diagram, Postman collection
scripts/                     Precision@5 evaluation script
streamlit_app.py            local UI (OAuth login, sync panel, query chat) - talks to the API over HTTP, no logic of its own
DESIGN.md, API.md            scaling strategy + API reference (kept at repo root per the assignment brief)
```
