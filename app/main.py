"""FastAPI application entrypoint. Run with:
    uvicorn app.main:app --reload
(or via `docker compose up`, which runs exactly this command in the `api` service).

This module only wires things together - route logic lives in app/api/routes_*.py,
orchestration logic lives in app/orchestrator/, so this file stays small and stable.
"""

from fastapi import FastAPI

from app.api import routes_auth, routes_query, routes_sync

app = FastAPI(
    title="Agentic Google Workspace Orchestrator",
    description="Natural-language orchestration across Gmail, Calendar, and Drive.",
    version="0.1.0",
    # FastAPI auto-generates OpenAPI/Swagger docs from the route type hints below -
    # this satisfies the brief's "OpenAPI spec" submission requirement for free,
    # served live at /docs (Swagger UI) and /openapi.json (raw spec).
)

app.include_router(routes_auth.router)
app.include_router(routes_query.router)
app.include_router(routes_sync.router)


@app.get("/health", tags=["meta"])
def health() -> dict:
    """Liveness check for load balancers / docker-compose healthchecks."""
    return {"status": "ok"}
