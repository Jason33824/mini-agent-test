"""Auth primitives: password hashing, JWT issue/verify, secret fail-fast."""
import jwt as pyjwt
import pytest

from api import auth


@pytest.fixture(autouse=True)
def _secret_env(monkeypatch):
    monkeypatch.setenv("JWT_SECRET", "unit-test-secret-not-for-prod-00")


def test_password_roundtrip_and_mismatch():
    stored = auth.hash_password("hunter2")
    assert auth.verify_password("hunter2", stored)
    assert not auth.verify_password("hunter3", stored)


def test_hash_is_salted_per_user():
    # same password, different rows — the digests must not collide
    assert auth.hash_password("same") != auth.hash_password("same")


def test_verify_rejects_malformed_storage():
    assert not auth.verify_password("x", "not-a-hash")
    assert not auth.verify_password("x", "pbkdf2$md5$1$salt$digest")


def test_token_roundtrip_carries_principal():
    token = auth.create_token("alice", "acme")
    who = auth.decode_token(token)
    assert who == auth.Principal(user_id="alice", tenant_id="acme")


def test_expired_token_rejected():
    token = auth.create_token("alice", "acme", ttl_seconds=-10)
    with pytest.raises(pyjwt.ExpiredSignatureError):
        auth.decode_token(token)


def test_foreign_signed_token_rejected():
    forged = pyjwt.encode({"sub": "admin", "tenant": "acme"}, "other-secret-not-for-decoding-00000000",
                          algorithm="HS256")
    with pytest.raises(pyjwt.InvalidSignatureError):
        auth.decode_token(forged)


def test_missing_or_placeholder_secret_fails_fast(monkeypatch):
    monkeypatch.delenv("JWT_SECRET", raising=False)
    with pytest.raises(RuntimeError):
        auth.create_token("alice", "acme")
    monkeypatch.setenv("JWT_SECRET", "change-me-to-a-long-random-string")
    with pytest.raises(RuntimeError):
        auth.create_token("alice", "acme")
