"""Reset the embedding cache when switching LLM/embedding providers.

Swapping OPENAI_EMBEDDING_MODEL to a different provider's model (e.g. Gemini's
gemini-embedding-001 - see app/llm_client.py, app/config.py) does NOT require a
schema change here: app/embeddings/embedder.py explicitly requests
`dimensions=settings.embedding_dimensions` on every embed call (both OpenAI's
text-embedding-3-* and Gemini's gemini-embedding-001 support Matryoshka-truncated
output), so as long as EMBEDDING_DIMENSIONS stays 1536 the `vector(1536)` columns
never need to change.

What DOES need to happen: an embedding computed by one model is meaningless compared
against an embedding computed by a different model, even at the identical dimension
count - they're different vector spaces. So this migration truncates the *_cache
tables and clears sync_status, forcing a full resync under the new provider rather
than mixing old-model and new-model vectors in the same similarity search, or leaving
already-cached rows permanently stale because they're outside the next "incremental
since last sync" window.

(Earlier draft of this migration tried to resize the columns to Gemini's *native*
3072-dim output - don't do that: pgvector's ivfflat/hnsw indexes hard-cap at 2000
dimensions, so a 3072-dim column can never be indexed at all. Request a truncated
1536-dim embedding instead, as embedder.py now does.)

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-17
"""
from typing import Sequence, Union

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_TABLES = ("gmail_cache", "gcal_cache", "gdrive_cache")


def _reset() -> None:
    for table in _TABLES:
        op.execute(f"TRUNCATE TABLE {table}")
    # Force a full resync rather than leaving rows outside the next "since last sync"
    # incremental window permanently stuck with no re-fetch scheduled.
    op.execute("DELETE FROM sync_status")


def upgrade() -> None:
    _reset()


def downgrade() -> None:
    # Nothing to structurally revert - downgrading just means "you'll resync under
    # whatever provider is configured next" too.
    _reset()
