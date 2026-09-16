"""OAuth routes: GET /api/v1/auth/google (start login) and its callback.

The brief only lists `GET /api/v1/auth/google -> OAuth flow` as a single endpoint,
but a real Authorization Code flow needs two legs - the initial redirect to Google,
and the callback Google redirects back to with a `code`. We expose both under the
`/auth/google` prefix so the pair reads as one logical "OAuth flow" from outside.
"""

from fastapi import APIRouter, Depends
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app.db.session import get_db
from app.google.oauth import build_auth_url, exchange_code_for_tokens, upsert_user_from_credentials
from app.security import create_session_token

router = APIRouter(prefix="/api/v1/auth/google", tags=["auth"])


@router.get("")
def start_google_oauth() -> RedirectResponse:
    """Redirect the browser to Google's consent screen."""
    return RedirectResponse(build_auth_url())


@router.get("/callback")
def google_oauth_callback(code: str, db: Session = Depends(get_db)) -> dict:
    """Google redirects here with `?code=...` after the user grants consent.

    Exchanges the code for tokens, upserts the User row (tokens encrypted at rest -
    see app/security.py), and returns a session JWT the client uses as a Bearer
    token on every subsequent /api/v1/* request.
    """
    credentials = exchange_code_for_tokens(code)

    # The Gmail/Calendar/Drive scopes we request don't include an explicit "identity"
    # scope, so we ask Google's userinfo endpoint (covered implicitly by the default
    # `openid`/profile grant most Google OAuth clients receive) for the account email.
    # In a from-scratch client like this, the simplest reliable source is the ID token
    # if present; fall back to decoding it via google.oauth2.id_token if needed.
    email = _extract_email(credentials)

    user = upsert_user_from_credentials(db, email, credentials)
    session_token = create_session_token(str(user.id))

    return {"session_token": session_token, "user_id": str(user.id), "email": user.email}


def _extract_email(credentials) -> str:
    """Best-effort extraction of the authenticated user's email from the ID token
    Google includes alongside the access/refresh token pair. Requires the 'openid'
    and 'email' scopes (see GOOGLE_SCOPES in .env) - without them Google omits the
    id_token entirely."""
    from google.oauth2 import id_token as google_id_token
    from google.auth.transport.requests import Request

    from app.config import get_settings

    if not credentials.id_token:
        raise ValueError("Google did not return an id_token - ensure the 'openid' and 'email' scopes are granted")

    # Passing audience=our client_id verifies this token was actually issued for OUR
    # app (not replayed from a token minted for some other OAuth client).
    claims = google_id_token.verify_oauth2_token(credentials.id_token, Request(), audience=get_settings().google_client_id)
    return claims["email"]
