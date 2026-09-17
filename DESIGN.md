# DESIGN.md - Architecture & Scaling to 1M Users

## 1. What's actually built vs. what's a documented tradeoff

This assignment explicitly rewards "thoughtful architectural decisions over perfect
implementations." Three deliberate scope decisions, so the rest of this document
doesn't read as if they were overlooked:

1. **Google API integration is real** (OAuth2, live Gmail/Calendar/Drive calls,
   retry-with-backoff), not mocked - see `app/google/`. The tradeoff this bought is
   time spent on Google Cloud Console setup / OAuth plumbing instead of more
   orchestration edge cases.
2. **Sync is time-window-based, not cursor-based.** We re-list "what changed since
   last sync" per service on a timer (`app/tasks/sync_tasks.py`) instead of using
   Gmail's `historyId`, Calendar's `syncToken`, or Drive's `changes.list` cursors.
   Those cursor APIs are the *correct* production approach - they tell you exactly
   what changed instead of re-scanning a time window - but they add real
   per-user/per-service cursor-persistence complexity. `SyncStatus.last_synced_at`
   already gives us the seam to swap this in later without changing any call site.
3. **A single Postgres instance**, not the sharded-by-user_id fleet described in
   section 3 below. The schema and query patterns are written to make sharding
   straightforward (every table's rows are naturally partitionable by `user_id`,
   every foreign key is a UUID that doesn't depend on a specific shard's sequence),
   but actually standing up multiple Postgres instances is out of scope for a
   single-user take-home demo.

## 2. Request-time architecture (as built)

```
Client
  -> FastAPI (app/main.py)
       -> app/api/deps.py: get_current_user   (JWT -> User row, multi-tenant boundary)
       -> app/cache/rate_limit.py               (100 req/user/hour, Redis fixed-window)
       -> app/orchestrator/pipeline.py: run_query
            1. app/cache/conversation.py:get_recent_context   (Redis, last 5 turns)
            2. app/orchestrator/intent_classifier.py            (OpenAI call #1, Redis-cached)
            3. app/orchestrator/planner.py:build_plan            (pure function -> DAG)
            4. app/orchestrator/executor.py:execute_plan         (asyncio.gather per DAG "wave")
                 -> GmailAgent / GCalAgent / DriveAgent
                      .search()       Postgres + pgvector (app/embeddings/search.py)
                      .get_context()  live Google API call for the top hit
                      .execute()      live Google API write + app/orchestrator/audit.py
            5. app/orchestrator/synthesizer.py                    (OpenAI call #2)
            6. persist Conversation row + Redis conversation-context update
```

Two LLM calls per query (classify, synthesize) plus 0-2 Google API calls per touched
service (one for `get_context`, one for `execute` if the intent is a write) - search
itself never calls Google live, only Postgres.

## 3. Scaling to 1M users

```
                              Load Balancer
                                    |
                    API Servers (FastAPI) x N   <-- stateless; scale horizontally
                                    |
              +---------------------+----------------------+
              |                                            |
      Redis Cluster                                 Postgres (sharded by user_id)
   (cache + rate limit + Celery broker/backend)      + pgvector per shard
              |                                            |
              +---------------------+----------------------+
                                    |
                        Celery workers (autoscaled, queue-separated)
                                    |
                        Google Workspace APIs + OpenAI API
```

**API servers** are already stateless in this codebase (all state lives in
Postgres/Redis, auth is a self-contained JWT) - scaling this tier is just adding
uvicorn processes/containers behind the load balancer. No code change needed.

**Sharding by `user_id`** (the brief's own suggestion): every `*_cache` table's rows
belong to exactly one user, and every read/write in `app/embeddings/search.py` and
the agents already filters `WHERE user_id = ...` first. That means a shard router
(`shard = hash(user_id) % N`) can sit in front of a connection-pool selector in
`app/db/session.py` with no query-logic changes - the sharding key is already the
first predicate in every query. Cross-shard joins never happen because every
"related" table (conversations, gmail_cache, gcal_cache, ...) is keyed by the same
`user_id`.

**pgvector at scale:** `ivfflat` with `lists=100` (see the migration) is tuned for
tens-of-thousands of rows per shard. As a shard's row count grows, `lists` should
grow roughly with `sqrt(row_count)` - a periodic `REINDEX` job (or pgvector 0.7+'s
`HNSW` index, which trades build time for better recall/latency at scale) is the
next step once a shard has millions of cached emails/events/files. Since sharding is
by `user_id`, no single shard needs to hold more than one region's worth of active
users' data.

**Celery queue separation:** the brief lists two very different workloads under
"Task Queue" - the 15-minute background sync (bursty, backgroundable, retriable) and
"long-running orchestrations" (interactive, user is waiting). In production these
should be two Celery queues with separate worker pools (`celery -A ... worker -Q
sync` vs `-Q interactive`) so a burst of background syncs can never starve a user's
live query. This repo's `docker-compose.yml` runs one worker pool for simplicity;
splitting it is a one-line `-Q` flag change plus a second `celery_worker` service,
not a redesign.

**Multi-region:** each region gets its own API server pool + Postgres shard set +
Redis, with users routed to their nearest region by geo-DNS/LB rules; Google/OpenAI
API calls are regionless from our side (they're already the remote dependency).
Cross-region concerns (a user traveling, DR failover) are out of scope here but
don't require schema changes - `user_id` sharding composes with region routing.

## 4. Caching strategy

| What | Where | TTL | Why |
|---|---|---|---|
| Embeddings | Redis, keyed by `sha256(model + text)` | 1hr | Deterministic for a given (model, text) pair - avoids re-paying OpenAI cost when the same content re-syncs. `app/embeddings/embedder.py` |
| Intent classifications | Redis, keyed by `sha256(query + context)` | 1hr | Identical query + identical recent context always classifies the same way (temperature=0) - skip the LLM round-trip. `app/orchestrator/intent_classifier.py` |
| Conversation context (last 5 turns) | Redis list, capped+trimmed | 24hr | Fast path for "that email" style resolution; Postgres `conversations` table is the durable, unbounded backing store. `app/cache/conversation.py` |
| Rate-limit counters | Redis, fixed window per hour | ~1hr (auto-expire) | Atomic `INCR`, no extra locking needed. `app/cache/rate_limit.py` |

Cache invalidation is mostly non-issue by construction: embedding/intent caches are
keyed by content hash, so stale entries simply become unreferenced (never explicitly
invalidated) rather than needing a busting mechanism.

## 5. Database schema (see `app/db/models.py`, `alembic/versions/0001_initial.py`)

(`alembic/versions/0002_reset_embedding_cache_for_provider_switch.py` doesn't change
this schema - it just truncates the `*_cache` tables and clears `sync_status` when
switching the configured embedding model/provider, since an embedding from one model
isn't comparable to one from another even at the same vector dimension.)

Extends the brief's simplified schema with `gcal_cache`/`gdrive_cache` (the "similar
tables" it gestures at), `sync_status` (backs `GET /sync/status`), and `audit_log`
(backs the "Security: audit logging" requirement). Every `*_cache` table:
- has a `UNIQUE(user_id, external_id)` constraint so re-sync upserts instead of
  duplicating,
- has a `vector(1536)` column + an `ivfflat` cosine-distance index,
- has b-tree indexes on the columns `app/embeddings/search.py` filters on
  *before* ranking by vector distance (sender, start_time, modified_at) - per the
  brief's hint, "metadata filtering > pure vector search for speed."

An ER diagram is in [docs/ER_DIAGRAM.md](docs/ER_DIAGRAM.md).

## 6. API design

REST, versioned under `/api/v1`, matching the brief's exact endpoint list (see
[API.md](API.md) for full request/response shapes). Auth is a Bearer JWT issued
after Google OAuth login (`app/security.py:create_session_token`) - every other
route depends on `app/api/deps.py:get_current_user`, which is the single multi-tenant
isolation checkpoint: a request's `user_id` always comes from its verified token,
never from a client-supplied field, so one user's Bearer token can never be used to
read/act on another user's Gmail/Calendar/Drive data.

## 7. Security

- **Token encryption at rest:** Google OAuth access/refresh tokens are Fernet-encrypted
  (`app/security.py`) before being written to the `users` table; decrypted only
  in-memory, immediately before an API call. Production should source `FERNET_KEY`
  from a KMS with rotation, not a static env var.
- **Multi-tenant isolation:** every DB query in `app/embeddings/search.py` and every
  agent method takes `user_id` from the authenticated `User` object resolved by
  `get_current_user`, never from request input.
- **Audit logging:** every write action (send/draft/create/update/delete/share/move)
  is recorded in `audit_log` - including failed attempts - via
  `app/orchestrator/audit.py`, regardless of whether the action succeeded.
- **OAuth token refresh:** `app/google/oauth.py:get_credentials_for_user` transparently
  refreshes an expired access token using the stored refresh token and re-persists
  the new one, so a user never has to re-authenticate just because an hour passed.

## 8. Failure handling

Per the brief's "Cross-Service Dependencies" hard case: `app/orchestrator/executor.py`
runs the plan in dependency-respecting "waves"; a node whose dependency failed is
marked `skipped` rather than crashing the whole plan, so independent branches (e.g. a
successful Gmail search alongside a failed Calendar search) still complete and the
Response Synthesizer explicitly reports the partial result rather than erroring out.
Google API calls are retried with exponential backoff for transient errors (429/5xx/
network) and NOT retried for auth/permission/not-found errors (`app/google/retry.py`).

## 9. Metrics to monitor (brief's list, mapped to where each would be measured)

- **P99 latency <2s:** wrap `run_query` with request-duration histograms per stage
  (classify/plan/execute/synthesize) - the two LLM calls dominate; execute() is the
  next-largest contributor when a live Google API call is on the path.
- **Cache hit rate >80%:** track Redis `GET` hit/miss counts in
  `embedder.embed_text`/`embed_batch` and `intent_classifier.classify_intent`.
- **Google API errors <0.1%:** count `HttpError` occurrences by status code in
  `app/google/retry.py`'s retry predicate (both retried-and-recovered and
  ultimately-failed).
- **Embedding freshness <15min lag:** `SyncStatus.last_synced_at` per (user, service)
  directly measures this; alert if `now() - last_synced_at > sync_interval * 2`.
