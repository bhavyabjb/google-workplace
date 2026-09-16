# Marks `app.google` as a package. Contains everything that talks to real Google APIs:
# - oauth.py: the OAuth2 login flow + token refresh
# - retry.py: shared retry/backoff wrapper for flaky Google API calls
# - gmail_client.py / gcal_client.py / drive_client.py: thin per-service API wrappers
#
# Nothing outside this package should import `googleapiclient` directly - agents in
# app/orchestrator/agents/ call through these clients so the retry/auth logic is
# centralized in one place.
