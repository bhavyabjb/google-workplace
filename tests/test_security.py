"""Tests for token encryption and session JWTs (app/security.py)."""

from app.security import create_session_token, decode_session_token, decrypt_token, encrypt_token


def test_encrypt_decrypt_roundtrip():
    raw = "ya29.some-fake-google-access-token"
    encrypted = encrypt_token(raw)

    assert encrypted != raw  # never stored/transmitted in plaintext
    assert decrypt_token(encrypted) == raw


def test_session_token_roundtrip():
    user_id = "11111111-1111-1111-1111-111111111111"
    token = create_session_token(user_id)

    assert decode_session_token(token) == user_id


def test_invalid_session_token_returns_none():
    assert decode_session_token("not-a-real-jwt") is None
