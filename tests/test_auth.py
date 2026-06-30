from __future__ import annotations

from glitch_core.config import GlitchEnv
from glitch_core.web.auth import hash_password, verify_password


def test_hash_and_verify_roundtrip():
    h = hash_password("hunter2")
    env = GlitchEnv(admin_username="admin", admin_password_hash=h, session_secret="x" * 40)
    assert verify_password(env, "admin", "hunter2")
    assert not verify_password(env, "admin", "wrong")
    assert not verify_password(env, "someone", "hunter2")


def test_plaintext_dev_fallback():
    env = GlitchEnv(
        admin_username="admin", admin_password="secret", admin_password_hash="",
        session_secret="x" * 40,
    )
    assert verify_password(env, "admin", "secret")
    assert not verify_password(env, "admin", "nope")


def test_no_credentials_configured_denies():
    env = GlitchEnv(admin_username="admin", admin_password_hash="", session_secret="x" * 40)
    assert not verify_password(env, "admin", "anything")
