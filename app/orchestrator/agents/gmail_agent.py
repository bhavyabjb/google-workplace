"""Gmail service agent: implements search/get_context/execute against the cached
gmail_cache table (search/get_context) and the live Gmail API (execute), per the
brief's "Gmail: search_emails, get_email, send_email, draft_email, update_labels".
"""

from typing import Any

from openai import OpenAI

from app.config import get_settings
from app.embeddings.embedder import embed_text
from app.embeddings.search import search_gmail
from app.google.gmail_client import GmailClient
from app.google.oauth import get_credentials_for_user
from app.orchestrator.agents.base import ServiceAgent
from app.orchestrator.audit import record_action

settings = get_settings()
_openai = OpenAI(api_key=settings.openai_api_key)


class GmailAgent(ServiceAgent):
    def search(self, entities: dict[str, Any], intent: str) -> list[dict]:
        """Search the local gmail_cache (synced by the background job) rather than
        calling the Gmail API live - keeps this on the fast, rate-limit-free path.
        """
        # Build the text we embed for semantic ranking from whatever the classifier
        # extracted (free-form entities), falling back to the intent label alone.
        query_text = f"{intent} " + " ".join(str(v) for v in entities.values())
        query_embedding = embed_text(query_text)

        # entities is free-form LLM output; we only pick out keys we recognize as
        # metadata filters and let everything else just influence the embedding above.
        sender = entities.get("sender") or entities.get("email")

        results = search_gmail(self.db, str(self.user.id), query_embedding, sender=sender, limit=5)
        return [
            {
                "email_id": r.email_id,
                "thread_id": r.thread_id,
                "subject": r.subject,
                "sender": r.sender,
                "body_preview": r.body_preview,
                "received_at": r.received_at.isoformat() if r.received_at else None,
            }
            for r in results
        ]

    def get_context(self, search_results: list[dict]) -> dict:
        """Fetch the full message body for the top search hit, live from Gmail, so the
        response synthesizer has more than just the cached preview to reason over."""
        if not search_results:
            return {}

        top = search_results[0]
        credentials = get_credentials_for_user(self.db, self.user)
        client = GmailClient(credentials)
        message = client.get_message(top["email_id"])

        headers = {h["name"]: h["value"] for h in message.get("payload", {}).get("headers", [])}
        return {
            "email_id": top["email_id"],
            "thread_id": message.get("threadId"),
            "from": headers.get("From"),
            "subject": headers.get("Subject"),
            "snippet": message.get("snippet"),
        }

    def execute(self, verb: str, entities: dict[str, Any], upstream_context: dict[str, Any]) -> dict:
        """Only "draft" is implemented as a write action for Gmail in this assignment -
        sending is a deliberate second step the user confirms (see the brief's sample
        response: "Would you like me to send it?"), so execute() never calls send_message
        directly for a draft-type verb.
        """
        if verb != "draft":
            raise NotImplementedError(f"GmailAgent.execute does not support verb={verb!r}")

        recipient, subject, body = self._compose_draft(entities, upstream_context)

        credentials = get_credentials_for_user(self.db, self.user)
        client = GmailClient(credentials)
        try:
            draft = client.create_draft(to=recipient, subject=subject, body=body)
            record_action(self.db, str(self.user.id), "gmail.draft_email", "gmail", draft.get("id"), "success", {"to": recipient, "subject": subject})
            return {"draft_id": draft.get("id"), "to": recipient, "subject": subject, "body": body}
        except Exception as exc:  # noqa: BLE001 - deliberately broad: we want to audit-log ANY failure, then re-raise
            record_action(self.db, str(self.user.id), "gmail.draft_email", "gmail", None, "error", {"to": recipient, "error": str(exc)})
            raise

    def _compose_draft(self, entities: dict[str, Any], upstream_context: dict[str, Any]) -> tuple[str, str, str]:
        """Use the LLM to draft short, professional email copy grounded in whatever the
        upstream search/get_context nodes found (booking reference, sender address, etc),
        instead of a rigid string template that would look robotic for every airline/vendor.
        """
        recipient = entities.get("recipient") or entities.get("vendor_email") or "support@example.com"

        prompt = (
            "Draft a short, polite email requesting cancellation, using only the facts "
            f"given below - do not invent details.\n\nEntities: {entities}\n"
            f"Context gathered so far: {upstream_context}\n\n"
            "Return ONLY the email body text, no subject line, no signature block."
        )
        response = _openai.chat.completions.create(
            model=settings.openai_chat_model,
            temperature=0.3,
            messages=[{"role": "user", "content": prompt}],
        )
        body = response.choices[0].message.content.strip()
        subject = f"Cancellation Request - {entities.get('airline') or entities.get('vendor') or entities.get('intent', 'Booking')}"
        return recipient, subject, body
