# ER Diagram

Generated from `app/db/models.py` / `alembic/versions/0001_initial.py`. Renders as a
diagram automatically on GitHub (Mermaid support in Markdown).

```mermaid
erDiagram
    USERS {
        uuid id PK
        varchar email
        text google_access_token "Fernet-encrypted"
        text google_refresh_token "Fernet-encrypted"
        timestamptz google_token_expiry
        varchar timezone
        timestamptz created_at
    }

    CONVERSATIONS {
        uuid id PK
        uuid user_id FK
        text query
        jsonb intent
        text response
        jsonb actions_taken
        timestamptz created_at
    }

    GMAIL_CACHE {
        uuid id PK
        uuid user_id FK
        varchar email_id
        varchar thread_id
        text subject
        varchar sender
        jsonb recipients
        text body_preview
        jsonb labels
        vector_1536 embedding
        timestamptz received_at
        timestamptz updated_at
    }

    GCAL_CACHE {
        uuid id PK
        uuid user_id FK
        varchar event_id
        varchar calendar_id
        text title
        text description
        text location
        jsonb attendees
        varchar status
        timestamptz start_time
        timestamptz end_time
        vector_1536 embedding
        timestamptz updated_at
    }

    GDRIVE_CACHE {
        uuid id PK
        uuid user_id FK
        varchar file_id
        text name
        varchar mime_type
        text content_preview
        text web_view_link
        jsonb parents
        vector_1536 embedding
        timestamptz modified_at
        timestamptz updated_at
    }

    SYNC_STATUS {
        uuid id PK
        uuid user_id FK
        varchar service
        timestamptz last_synced_at
        varchar status
        text error
    }

    AUDIT_LOG {
        uuid id PK
        uuid user_id FK
        varchar action
        varchar service
        varchar resource_id
        varchar status
        jsonb details
        timestamptz created_at
    }

    USERS ||--o{ CONVERSATIONS : "has"
    USERS ||--o{ GMAIL_CACHE : "has"
    USERS ||--o{ GCAL_CACHE : "has"
    USERS ||--o{ GDRIVE_CACHE : "has"
    USERS ||--o{ SYNC_STATUS : "has"
    USERS ||--o{ AUDIT_LOG : "has"
```

Notes:
- Every `*_CACHE` table has a `UNIQUE(user_id, <external_id>)` constraint (not shown
  above - Mermaid ER syntax doesn't render composite unique constraints) so a
  background re-sync upserts instead of duplicating rows.
- `vector_1536` is pgvector's `vector(1536)` type, each with an `ivfflat` cosine-
  distance index (see `alembic/versions/0001_initial.py`).
- There are no foreign keys *between* `*_CACHE` tables - by design, every table is
  independently partitionable by `user_id`, which is what makes sharding by
  `user_id` (see [DESIGN.md](../DESIGN.md) section 3) a schema-preserving change.
