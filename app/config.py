# Centralized, typed application settings. Every other module reads configuration
# through `get_settings()` instead of calling `os.environ` directly, so there is one
# place that knows how env vars map to Python values and one place to validate them.

from functools import lru_cache
# lru_cache: memoizes get_settings() so the .env file is parsed once per process,
# not on every request - Settings() becomes a cheap singleton lookup after the first call.

from pydantic_settings import BaseSettings, SettingsConfigDict
# BaseSettings: a Pydantic model that automatically populates its fields from
# environment variables (and a .env file) instead of constructor arguments.


class Settings(BaseSettings):
    """Typed view over the process environment / .env file.

    Field names map to env vars by upper-casing (e.g. `database_url` <- DATABASE_URL).
    Pydantic validates types and raises at startup if a required var is missing,
    which is much safer than discovering a typo'd env var deep inside a request.
    """

    # Tell pydantic-settings where to look for a local .env file, and to silently
    # ignore any extra env vars present in the environment that aren't declared below.
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # --- App / security ---
    app_secret_key: str      # signs session JWTs (app/security.py)
    fernet_key: str          # encrypts Google OAuth tokens at rest (app/security.py)
    environment: str = "development"

    # --- Datastores ---
    database_url: str        # SQLAlchemy connection string (Postgres + pgvector)
    redis_url: str           # cache + rate limiter

    # --- OpenAI (or an OpenAI-compatible provider) ---
    openai_api_key: str
    # If set, points the OpenAI SDK at a different OpenAI-compatible endpoint instead
    # of api.openai.com - e.g. Gemini's https://ai.google.dev/gemini-api/docs/openai
    # (base_url "https://generativelanguage.googleapis.com/v1beta/openai/"). See
    # app/llm_client.py for where this is actually used and why it's centralized there.
    openai_base_url: str | None = None
    openai_chat_model: str = "gpt-4o-mini"                 # intent classification + response synthesis
    openai_embedding_model: str = "text-embedding-3-small"  # email/event/file embeddings
    embedding_dimensions: int = 1536                        # must match the `vector(N)` columns in app/db/models.py - keep this at 1536 (embedder.py requests truncated output at this size from any provider) unless you also write a migration to resize the columns, and note pgvector's ivfflat/hnsw indexes cap out at 2000 dims regardless

    # --- Google OAuth ---
    google_client_id: str
    google_client_secret: str
    google_redirect_uri: str
    google_scopes: str       # raw comma-separated string as stored in the env var

    # --- Rate limiting ---
    queries_per_user_per_hour: int = 100

    # --- Frontend (optional) ---
    # If set, the OAuth callback redirects here with the session token as query
    # params instead of returning it as raw JSON - lets a UI (e.g. the Streamlit
    # app) complete login without the user copy-pasting a token by hand. Leave unset
    # and the callback keeps returning JSON directly, unchanged for API/Swagger use.
    frontend_redirect_url: str | None = None

    # --- Celery / background sync ---
    celery_broker_url: str
    celery_result_backend: str
    sync_interval_minutes: int = 15

    @property
    def google_scopes_list(self) -> list[str]:
        """Split the comma-separated GOOGLE_SCOPES env var into a list for the OAuth flow."""
        return [s.strip() for s in self.google_scopes.split(",") if s.strip()]


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide Settings instance, constructing it only on first call."""
    return Settings()
