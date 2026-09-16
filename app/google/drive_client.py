"""Thin wrapper around the Google Drive API (v3).

Same pattern as gmail_client.py/gcal_client.py: direct, retried calls only.
app/orchestrator/agents/drive_agent.py owns search/cache/business logic on top of this.
"""

import io
# io.BytesIO: in-memory buffer we download exported file content into (avoids writing
# temp files to disk just to read a few KB of text for embedding).

from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
# MediaIoBaseDownload: handles Drive's chunked download protocol for exported content.

from app.google.retry import google_api_retry

# Google Docs/Sheets/Slides are not "files" with downloadable bytes - they must be
# *exported* to a concrete format. We export them to plain text so we have something
# to embed. Arbitrary uploaded files (PDFs, images, etc.) are left content_preview=None;
# extracting text from arbitrary binary formats is out of scope for this assignment,
# so those are only searchable by name/mime_type metadata, not full-text semantic search.
EXPORTABLE_GOOGLE_APPS_MIME_TYPES = {
    "application/vnd.google-apps.document": "text/plain",
    "application/vnd.google-apps.spreadsheet": "text/csv",
    "application/vnd.google-apps.presentation": "text/plain",
}


class DriveClient:
    def __init__(self, credentials: Credentials):
        self.service = build("drive", "v3", credentials=credentials, cache_discovery=False)

    @google_api_retry
    def list_files(self, query: str, page_size: int = 25) -> list[dict]:
        """List file metadata matching a Drive query string (Drive's own `q` syntax,
        e.g. "mimeType='application/pdf' and modifiedTime > '2026-08-01T00:00:00'").
        """
        result = (
            self.service.files()
            .list(
                q=query,
                pageSize=page_size,
                fields="files(id, name, mimeType, modifiedTime, webViewLink, parents)",
            )
            .execute()
        )
        return result.get("files", [])

    @google_api_retry
    def get_file(self, file_id: str) -> dict:
        """Fetch full metadata for one file, for LLM reasoning / display."""
        return (
            self.service.files()
            .get(fileId=file_id, fields="id, name, mimeType, modifiedTime, webViewLink, parents, description")
            .execute()
        )

    def get_file_content(self, file_id: str, mime_type: str) -> str | None:
        """Best-effort plain-text content for embedding. Returns None for non-exportable
        (i.e. non-Google-Apps) file types - see EXPORTABLE_GOOGLE_APPS_MIME_TYPES above.
        """
        export_mime = EXPORTABLE_GOOGLE_APPS_MIME_TYPES.get(mime_type)
        if not export_mime:
            return None

        request = self.service.files().export_media(fileId=file_id, mimeType=export_mime)
        buffer = io.BytesIO()
        downloader = MediaIoBaseDownload(buffer, request)
        done = False
        while not done:
            _status, done = downloader.next_chunk()
        return buffer.getvalue().decode("utf-8", errors="ignore")

    @google_api_retry
    def share_file(self, file_id: str, email: str, role: str = "reader") -> dict:
        """Grant `email` access to a file (role: reader | writer | commenter)."""
        permission = {"type": "user", "role": role, "emailAddress": email}
        return self.service.permissions().create(fileId=file_id, body=permission, sendNotificationEmail=True).execute()

    @google_api_retry
    def move_file(self, file_id: str, new_parent_id: str, old_parent_id: str | None = None) -> dict:
        """Move a file into a different folder by swapping Drive `parents`."""
        return self.service.files().update(
            fileId=file_id,
            addParents=new_parent_id,
            removeParents=old_parent_id,
            fields="id, parents",
        ).execute()

    @google_api_retry
    def create_folder(self, name: str, parent_id: str | None = None) -> dict:
        """Create a new Drive folder, optionally nested under `parent_id`."""
        body = {"name": name, "mimeType": "application/vnd.google-apps.folder"}
        if parent_id:
            body["parents"] = [parent_id]
        return self.service.files().create(body=body, fields="id, name, parents").execute()
