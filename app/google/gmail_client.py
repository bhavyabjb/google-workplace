"""Thin wrapper around the Gmail API (v1).

Every method here is a direct, retried call to Google - no caching, no business
logic. app/orchestrator/agents/gmail_agent.py is the layer that decides *when* to
call these (e.g. "search cache first, fall back to a live call") and how results
feed into the orchestration DAG.
"""

import base64
# base64: Gmail's API requires the raw MIME message to be base64url-encoded before
# being sent as the `raw` field of a message/draft resource.

from email.mime.text import MIMEText
# MIMEText: builds a minimal valid RFC 2822 email (headers + plaintext body) that
# Gmail will accept as a `raw` message.

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
# build: constructs an authorized API client (a "service" object) for a given
# API name/version, using the Credentials obtained via app/google/oauth.py.

from app.google.retry import google_api_retry


class GmailClient:
    """One instance per request/task, scoped to a single user's credentials."""

    def __init__(self, credentials: Credentials):
        # cache_discovery=False avoids googleapiclient trying to write a discovery
        # doc cache file to disk, which is unnecessary overhead in a server process.
        self.service = build("gmail", "v1", credentials=credentials, cache_discovery=False)

    @google_api_retry
    def list_messages(self, query: str, max_results: int = 25) -> list[dict]:
        """List message stubs (id/threadId only) matching a Gmail search query string.

        `query` uses Gmail's own search syntax (e.g. "from:sarah@company.com after:2026/08/01")
        - the intent classifier / gmail_agent is responsible for building that string.
        """
        result = self.service.users().messages().list(userId="me", q=query, maxResults=max_results).execute()
        return result.get("messages", [])

    @google_api_retry
    def get_message(self, message_id: str, fmt: str = "full") -> dict:
        """Fetch a single message's full content (headers + body) for LLM reasoning."""
        return self.service.users().messages().get(userId="me", id=message_id, format=fmt).execute()

    @google_api_retry
    def send_message(self, to: str, subject: str, body: str, thread_id: str | None = None) -> dict:
        """Send an email immediately. Used for confirmed actions, not drafts."""
        raw = self._build_raw_message(to, subject, body)
        request_body = {"raw": raw}
        if thread_id:
            request_body["threadId"] = thread_id
        return self.service.users().messages().send(userId="me", body=request_body).execute()

    @google_api_retry
    def create_draft(self, to: str, subject: str, body: str, thread_id: str | None = None) -> dict:
        """Create a draft (not sent). This is what the "draft cancellation email" flow uses -
        the orchestrator drafts, then asks the user to confirm before send_message() runs.
        """
        raw = self._build_raw_message(to, subject, body)
        message = {"raw": raw}
        if thread_id:
            message["threadId"] = thread_id
        return self.service.users().drafts().create(userId="me", body={"message": message}).execute()

    @google_api_retry
    def update_labels(self, message_id: str, add_labels: list[str] | None = None, remove_labels: list[str] | None = None) -> dict:
        """Add/remove Gmail labels (e.g. mark read, archive) on a message."""
        body = {"addLabelIds": add_labels or [], "removeLabelIds": remove_labels or []}
        return self.service.users().messages().modify(userId="me", id=message_id, body=body).execute()

    @staticmethod
    def _build_raw_message(to: str, subject: str, body: str) -> str:
        """Build a base64url-encoded RFC 2822 message, as required by Gmail's `raw` field."""
        message = MIMEText(body)
        message["to"] = to
        message["subject"] = subject
        return base64.urlsafe_b64encode(message.as_bytes()).decode()
