"""Thin wrapper around the Google Calendar API (v3).

Same pattern as gmail_client.py: direct, retried calls only. app/orchestrator/agents/
gcal_agent.py owns search/cache/business logic on top of this.
"""

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build

from app.google.retry import google_api_retry


class GCalClient:
    def __init__(self, credentials: Credentials):
        self.service = build("calendar", "v3", credentials=credentials, cache_discovery=False)

    @google_api_retry
    def list_events(
        self,
        time_min: str,
        time_max: str,
        query: str | None = None,
        calendar_id: str = "primary",
        max_results: int = 50,
    ) -> list[dict]:
        """List events in [time_min, time_max) (RFC3339 strings), optionally full-text filtered.

        singleEvents=True expands recurring events into individual instances so "next
        week's standups" returns each occurrence rather than one recurrence rule.
        orderBy="startTime" requires singleEvents=True, which is why they're paired here.
        """
        result = (
            self.service.events()
            .list(
                calendarId=calendar_id,
                timeMin=time_min,
                timeMax=time_max,
                q=query,
                singleEvents=True,
                orderBy="startTime",
                maxResults=max_results,
            )
            .execute()
        )
        return result.get("items", [])

    @google_api_retry
    def get_event(self, event_id: str, calendar_id: str = "primary") -> dict:
        """Fetch full event detail (description, attendees, conferencing info) for LLM reasoning."""
        return self.service.events().get(calendarId=calendar_id, eventId=event_id).execute()

    @google_api_retry
    def create_event(self, body: dict, calendar_id: str = "primary") -> dict:
        """Create a new event. `body` follows the Calendar API Event resource shape
        (summary, description, start/end, attendees, ...) - built by gcal_agent.
        """
        return self.service.events().insert(calendarId=calendar_id, body=body).execute()

    @google_api_retry
    def update_event(self, event_id: str, body: dict, calendar_id: str = "primary") -> dict:
        """Patch an existing event (e.g. moving a meeting = updating start/end times)."""
        return self.service.events().patch(calendarId=calendar_id, eventId=event_id, body=body).execute()

    @google_api_retry
    def delete_event(self, event_id: str, calendar_id: str = "primary") -> None:
        """Delete/cancel an event (used by the "cancel my flight" -> calendar cleanup flow)."""
        self.service.events().delete(calendarId=calendar_id, eventId=event_id).execute()
