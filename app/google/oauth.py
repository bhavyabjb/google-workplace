"""Google OAuth2 "Authorization Code" flow.

Flow, end to end:
1. Frontend/user hits GET /api/v1/auth/google -> we redirect them to Google's consent
   screen (build_auth_url).
2. Google redirects back to GOOGLE_REDIRECT_URI with a `code` query param.
3. Our callback route calls exchange_code_for_tokens(code) to trade that one-time code
   for an access token + refresh token, then upserts a User row with those tokens
   (encrypted) and returns a session JWT (app/security.py) to the client.
4. On every later request, get_credentials_for_user() loads the encrypted tokens,
   decrypts them, and hands back a google.oauth2.Credentials object. If the access
   token is expired, google-auth transparently uses the refresh token to get a new one
   - we then re-encrypt and persist the refreshed access token so we don't re-auth
   the user every hour.
"""

from datetime import datetime, timezone
# datetime/timezone: used to compute/compare Credentials.expiry (Google tokens are short-lived).

from google.oauth2.credentials import Credentials
# Credentials: google-auth's representation of a token pair the API clients accept.

from google_auth_oauthlib.flow import Flow
# Flow: drives the OAuth2 authorization-code exchange (auth URL -> code -> tokens).

from sqlalchemy.orm import Session
# Session: used to look up/update the User row holding this user's tokens.

from app.config import get_settings
from app.db.models import User
from app.security import decrypt_token, encrypt_token

settings = get_settings()


def _build_flow(state: str | None = None) -> Flow:
    """Construct a Flow using our OAuth client config (no client_secret.json file needed)."""
    client_config = {
        "web": {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [settings.google_redirect_uri],
        }
    }
    return Flow.from_client_config(
        client_config,
        scopes=settings.google_scopes_list,
        state=state,
        redirect_uri=settings.google_redirect_uri,
    )


def build_auth_url() -> str:
    """Return the Google consent-screen URL to redirect the user to."""
    flow = _build_flow()
    # access_type="offline" is what makes Google issue a refresh_token (not just an
    # access token) - without it we'd lose access again after ~1 hour.
    # prompt="consent" forces the consent screen every time, which guarantees a
    # refresh_token is returned even if the user has authorized this app before
    # (Google only returns it on the *first* consent by default).
    auth_url, _state = flow.authorization_url(access_type="offline", prompt="consent", include_granted_scopes="true")
    return auth_url


def exchange_code_for_tokens(code: str) -> Credentials:
    """Trade the one-time `code` from Google's redirect for real access/refresh tokens."""
    flow = _build_flow()
    flow.fetch_token(code=code)
    return flow.credentials


def upsert_user_from_credentials(db: Session, email: str, creds: Credentials) -> User:
    """Create or update the User row for `email` with the latest (encrypted) tokens."""
    user = db.query(User).filter(User.email == email).one_or_none()
    if user is None:
        user = User(email=email)
        db.add(user)

    user.google_access_token = encrypt_token(creds.token)
    # creds.refresh_token is only present on the *first* consent (see prompt="consent"
    # above) - don't overwrite an existing refresh token with None on subsequent logins.
    if creds.refresh_token:
        user.google_refresh_token = encrypt_token(creds.refresh_token)
    user.google_token_expiry = creds.expiry.replace(tzinfo=timezone.utc) if creds.expiry else None

    db.commit()
    db.refresh(user)
    return user


def get_credentials_for_user(db: Session, user: User) -> Credentials:
    """Build a live Credentials object for `user`, refreshing the access token if expired.

    Callers (the Gmail/GCal/Drive clients) use the returned Credentials to construct
    an authorized googleapiclient service - they never see the raw stored token.
    """
    if not user.google_refresh_token:
        raise ValueError(f"User {user.email} has no Google refresh token - they must complete OAuth login")

    creds = Credentials(
        token=decrypt_token(user.google_access_token) if user.google_access_token else None,
        refresh_token=decrypt_token(user.google_refresh_token),
        token_uri="https://oauth2.googleapis.com/token",
        client_id=settings.google_client_id,
        client_secret=settings.google_client_secret,
        scopes=settings.google_scopes_list,
    )

    # If the access token is missing/expired, google-auth's Request() transport call
    # inside refresh() will use the refresh_token to mint a new access token.
    expired = user.google_token_expiry is None or user.google_token_expiry <= datetime.now(timezone.utc)
    if expired or creds.token is None:
        from google.auth.transport.requests import Request

        creds.refresh(Request())
        # Persist the newly refreshed access token so we don't refresh on every call.
        user.google_access_token = encrypt_token(creds.token)
        user.google_token_expiry = creds.expiry.replace(tzinfo=timezone.utc) if creds.expiry else None
        db.commit()

    return creds
