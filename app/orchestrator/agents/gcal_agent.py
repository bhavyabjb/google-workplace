"""Calendar service agent: search/get_context/execute against the cached gcal_cache
table and the live Calendar API, per the brief's "GCal: search_events, get_event,
create_event, update_event, delete_event".
"""

from datetime import datetime, timedelta, timezone
from typing import Any

from app.embeddings.embedder import embed_text
from app.embeddings.search import search_gcal
from app.google.gcal_client import GCalClient
from app.google.oauth import get_credentials_for_user
from app.orchestrator.agents.base import ServiceAgent
from app.orchestrator.audit import record_action


def _resolve_date_range(entities: dict[str, Any]) -> tuple[datetime, datetime]:
    """Turn whatever shape the intent classifier put in entities["date_range"] (or
    entities["date"]) into a concrete (start, end) UTC datetime pair. The classifier
    is prompted to resolve relative phrases ("next week") into ISO dates already; this
    is just a defensive parser since LLM output shape can vary, with a safe default
    (now -> +30 days) if nothing usable is present.
    """
    now = datetime.now(timezone.utc)
    date_range = entities.get("date_range")

    if isinstance(date_range, dict) and date_range.get("start") and date_range.get("end"):
        return datetime.fromisoformat(date_range["start"]), datetime.fromisoformat(date_range["end"])
    if isinstance(date_range, str) and "/" in date_range:
        start_str, end_str = date_range.split("/", 1)
        return datetime.fromisoformat(start_str), datetime.fromisoformat(end_str)

    single_date = entities.get("date")
    if isinstance(single_date, str):
        try:
            day = datetime.fromisoformat(single_date)
            return day, day + timedelta(days=1)
        except ValueError:
            pass

    return now, now + timedelta(days=30)


class GCalAgent(ServiceAgent):
    def search(self, entities: dict[str, Any], intent: str) -> list[dict]:
        time_min, time_max = _resolve_date_range(entities)
        attendee = entities.get("attendee") or entities.get("client")

        # Pure metadata queries (e.g. "what's on my calendar next week") have no useful
        # text to embed against - only pay for an embedding call when there's a
        # semantic hint (a client/company/topic name) worth ranking by.
        query_embedding = None
        semantic_hint = entities.get("client") or entities.get("airline") or entities.get("topic")
        if semantic_hint:
            query_embedding = embed_text(f"{intent} {semantic_hint}")

        results = search_gcal(
            self.db,
            str(self.user.id),
            query_embedding,
            time_min=time_min,
            time_max=time_max,
            attendee_email=attendee if attendee and "@" in str(attendee) else None,
            limit=20,
        )
        return [
            {
                "event_id": r.event_id,
                "title": r.title,
                "start_time": r.start_time.isoformat() if r.start_time else None,
                "end_time": r.end_time.isoformat() if r.end_time else None,
                "attendees": r.attendees,
                "location": r.location,
            }
            for r in results
        ]

    def get_context(self, search_results: list[dict]) -> dict:
        """Fetch full event detail (description, conferencing info, all attendees) live
        from Calendar for the top hit - cached rows only store the summary fields."""
        if not search_results:
            return {}

        top = search_results[0]
        credentials = get_credentials_for_user(self.db, self.user)
        client = GCalClient(credentials)
        event = client.get_event(top["event_id"])

        return {
            "event_id": event.get("id"),
            "title": event.get("summary"),
            "description": event.get("description"),
            "start": event.get("start"),
            "end": event.get("end"),
            "attendees": [a.get("email") for a in event.get("attendees", [])],
            "location": event.get("location"),
        }

    def execute(self, verb: str, entities: dict[str, Any], upstream_context: dict[str, Any]) -> dict:
        credentials = get_credentials_for_user(self.db, self.user)
        client = GCalClient(credentials)

        # The event this write targets: prefer whatever a prior search/get_context node
        # already resolved (upstream_context), rather than re-searching here.
        event_id = self._resolve_target_event_id(upstream_context)

        try:
            if verb == "delete":
                client.delete_event(event_id)
                record_action(self.db, str(self.user.id), "gcal.delete_event", "gcal", event_id, "success", {})
                return {"event_id": event_id, "deleted": True}

            if verb in ("update", "reschedule", "move"):
                body = self._build_update_body(entities)
                updated = client.update_event(event_id, body)
                record_action(self.db, str(self.user.id), "gcal.update_event", "gcal", event_id, "success", body)
                return {"event_id": updated.get("id"), "start": updated.get("start"), "end": updated.get("end")}

            if verb == "create":
                body = self._build_create_body(entities)
                created = client.create_event(body)
                record_action(self.db, str(self.user.id), "gcal.create_event", "gcal", created.get("id"), "success", body)
                return {"event_id": created.get("id")}

            raise NotImplementedError(f"GCalAgent.execute does not support verb={verb!r}")
        except Exception as exc:  # noqa: BLE001 - audit the failure, then re-raise so the executor marks this node as errored
            record_action(self.db, str(self.user.id), f"gcal.{verb}", "gcal", event_id, "error", {"error": str(exc)})
            raise

    @staticmethod
    def _resolve_target_event_id(upstream_context: dict[str, Any]) -> str:
        for result in upstream_context.values():
            if isinstance(result, dict) and result.get("event_id"):
                return result["event_id"]
            if isinstance(result, list) and result and isinstance(result[0], dict) and result[0].get("event_id"):
                return result[0]["event_id"]
        raise ValueError("Could not resolve which calendar event to act on from upstream context")

    @staticmethod
    def _build_update_body(entities: dict[str, Any]) -> dict:
        body: dict[str, Any] = {}
        if entities.get("new_start"):
            body["start"] = {"dateTime": entities["new_start"]}
        if entities.get("new_end"):
            body["end"] = {"dateTime": entities["new_end"]}
        return body

    @staticmethod
    def _build_create_body(entities: dict[str, Any]) -> dict:
        return {
            "summary": entities.get("title", "New Event"),
            "start": {"dateTime": entities.get("start")},
            "end": {"dateTime": entities.get("end")},
            "attendees": [{"email": e} for e in entities.get("attendees", [])],
        }
