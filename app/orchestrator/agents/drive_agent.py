"""Drive service agent: search/get_context/execute against the cached gdrive_cache
table and the live Drive API, per the brief's "Drive: search_files, get_file,
share_file, create_folder, move_file".
"""

from datetime import datetime
from typing import Any

from app.embeddings.embedder import embed_text
from app.embeddings.search import search_gdrive
from app.google.drive_client import DriveClient
from app.google.oauth import get_credentials_for_user
from app.orchestrator.agents.base import ServiceAgent
from app.orchestrator.audit import record_action

# Common shorthand -> MIME type mapping, so entities like {"file_type": "pdf"} from
# the classifier map onto Drive's actual mimeType metadata filter.
FILE_TYPE_TO_MIME = {
    "pdf": "application/pdf",
    "doc": "application/vnd.google-apps.document",
    "sheet": "application/vnd.google-apps.spreadsheet",
    "slide": "application/vnd.google-apps.presentation",
}


class DriveAgent(ServiceAgent):
    def search(self, entities: dict[str, Any], intent: str) -> list[dict]:
        mime_type = FILE_TYPE_TO_MIME.get(str(entities.get("file_type", "")).lower())

        modified_after = None
        if entities.get("modified_after"):
            modified_after = datetime.fromisoformat(entities["modified_after"])

        query_embedding = None
        semantic_hint = entities.get("client") or entities.get("topic") or entities.get("doc_name")
        if semantic_hint:
            query_embedding = embed_text(f"{intent} {semantic_hint}")

        results = search_gdrive(self.db, str(self.user.id), query_embedding, mime_type=mime_type, modified_after=modified_after, limit=5)
        return [
            {
                "file_id": r.file_id,
                "name": r.name,
                "mime_type": r.mime_type,
                "web_view_link": r.web_view_link,
                "modified_at": r.modified_at.isoformat() if r.modified_at else None,
            }
            for r in results
        ]

    def get_context(self, search_results: list[dict]) -> dict:
        """Fetch full metadata (+ text preview for Google Docs/Sheets/Slides) for the
        top hit, live from Drive."""
        if not search_results:
            return {}

        top = search_results[0]
        credentials = get_credentials_for_user(self.db, self.user)
        client = DriveClient(credentials)
        metadata = client.get_file(top["file_id"])
        content = client.get_file_content(top["file_id"], metadata.get("mimeType", ""))

        return {
            "file_id": metadata.get("id"),
            "name": metadata.get("name"),
            "mime_type": metadata.get("mimeType"),
            "web_view_link": metadata.get("webViewLink"),
            "content_preview": (content or "")[:1000] or None,
        }

    def execute(self, verb: str, entities: dict[str, Any], upstream_context: dict[str, Any]) -> dict:
        file_id = self._resolve_target_file_id(upstream_context)
        credentials = get_credentials_for_user(self.db, self.user)
        client = DriveClient(credentials)

        try:
            if verb == "share":
                email = entities.get("share_with") or entities.get("recipient")
                role = entities.get("role", "reader")
                result = client.share_file(file_id, email, role)
                record_action(self.db, str(self.user.id), "gdrive.share_file", "gdrive", file_id, "success", {"email": email, "role": role})
                return {"file_id": file_id, "shared_with": email, "permission_id": result.get("id")}

            if verb == "move":
                new_parent = entities.get("new_parent_id")
                old_parent = entities.get("old_parent_id")
                result = client.move_file(file_id, new_parent, old_parent)
                record_action(self.db, str(self.user.id), "gdrive.move_file", "gdrive", file_id, "success", {"new_parent": new_parent})
                return {"file_id": result.get("id"), "parents": result.get("parents")}

            raise NotImplementedError(f"DriveAgent.execute does not support verb={verb!r}")
        except Exception as exc:  # noqa: BLE001 - audit the failure, then re-raise so the executor marks this node as errored
            record_action(self.db, str(self.user.id), f"gdrive.{verb}", "gdrive", file_id, "error", {"error": str(exc)})
            raise

    @staticmethod
    def _resolve_target_file_id(upstream_context: dict[str, Any]) -> str:
        for result in upstream_context.values():
            if isinstance(result, dict) and result.get("file_id"):
                return result["file_id"]
            if isinstance(result, list) and result and isinstance(result[0], dict) and result[0].get("file_id"):
                return result[0]["file_id"]
        raise ValueError("Could not resolve which Drive file to act on from upstream context")
