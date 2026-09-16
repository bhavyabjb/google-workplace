"""Token-at-rest encryption and session JWTs.

Google OAuth access/refresh tokens are the most sensitive thing this service stores
(they grant read/write access to a user's inbox, calendar, and drive). We encrypt them
with Fernet (symmetric, authenticated) before writing to Postgres, and only decrypt
in-memory right before calling the Google API client. In production FERNET_KEY should
come from a KMS/secrets manager with rotation, not a static .env value.

This module also issues the session JWT the API hands back after a successful Google
OAuth login - the client stores that JWT and sends it as a Bearer token on every
subsequent request, which is how app/api/deps.py:get_current_user knows which user
(and therefore which encrypted Google tokens) a request belongs to.
"""

from datetime import datetime, timedelta, timezone
# datetime/timedelta/timezone: used to compute the JWT's expiry timestamp in UTC.

from cryptography.fernet import Fernet
# Fernet: symmetric authenticated encryption (AES-128-CBC + HMAC under the hood).
# "Authenticated" matters here - it means a tampered/corrupted ciphertext fails to
# decrypt loudly instead of silently returning garbage bytes.

from jose import JWTError, jwt
# jwt: encodes/decodes/signs the session token (HS256, i.e. HMAC with our secret key).
# JWTError: raised on an invalid signature or expired token - caught in decode_session_token.

from app.config import get_settings

settings = get_settings()

# Fernet requires a 32-byte urlsafe-base64 key (44 chars once encoded). If FERNET_KEY
# in .env hasn't been set to a real generated key yet, fall back to a freshly generated
# one so local dev doesn't hard-crash - but note this means encrypted tokens become
# unreadable across process restarts until a real, stable FERNET_KEY is configured.
_fernet = Fernet(settings.fernet_key.encode() if len(settings.fernet_key) == 44 else Fernet.generate_key())

JWT_ALGORITHM = "HS256"     # HMAC-SHA256 - symmetric signing, fine since we're both issuer and verifier
JWT_EXPIRY_HOURS = 24        # session tokens are short-lived; the underlying Google refresh token is what's long-lived


def encrypt_token(raw: str) -> str:
    """Encrypt a plaintext Google OAuth token before it's written to the users table."""
    return _fernet.encrypt(raw.encode()).decode()


def decrypt_token(token: str) -> str:
    """Decrypt a Google OAuth token read from the users table, for use in an API call."""
    return _fernet.decrypt(token.encode()).decode()


def create_session_token(user_id: str) -> str:
    """Issue a signed JWT identifying `user_id`, returned to the client after OAuth login."""
    payload = {
        "sub": user_id,  # "subject" - standard JWT claim, holds our internal user UUID as a string
        "exp": datetime.now(timezone.utc) + timedelta(hours=JWT_EXPIRY_HOURS),  # standard "expiry" claim
    }
    return jwt.encode(payload, settings.app_secret_key, algorithm=JWT_ALGORITHM)


def decode_session_token(token: str) -> str | None:
    """Verify a session JWT and return the embedded user id, or None if invalid/expired."""
    try:
        payload = jwt.decode(token, settings.app_secret_key, algorithms=[JWT_ALGORITHM])
        return payload.get("sub")
    except JWTError:
        # Covers both a bad signature (tampered/forged token) and a naturally expired one.
        return None
