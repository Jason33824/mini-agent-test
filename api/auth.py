"""JWT issue/verify + password hashing: the identity primitives, no I/O.

Design (ADR 0003): stateless HS256 tokens — the demo has no session store to
invalidate against, and a self-contained token keeps the worker out of the
auth path (it trusts the user_id/tenant the API already persisted on the task
row). Subject is the user id; `tenant` carries the quota namespace.

Secret: JWT_SECRET env, read at call time (compose/.env). No default — a
missing secret fails fast the same way OPENAI_API_KEY does in agents/llm.py.

Passwords: pbkdf2-hmac-sha256 (stdlib) with per-user random salt. Deliberately
not bcrypt/argon2: no new dependency, and the work factor is a parameter, so
the algorithm choice is swappable without touching call sites.
"""
import hashlib
import hmac
import os
import secrets
import time
from typing import NamedTuple

import jwt

ALGORITHM = "HS256"
TOKEN_TTL_SECONDS = 12 * 3600
PBKDF2_ITERATIONS = 120_000


class Principal(NamedTuple):
    """Authenticated identity resolved from a bearer token."""

    user_id: str
    tenant_id: str


def _secret() -> str:
    secret = os.environ.get("JWT_SECRET", "")
    if not secret or secret == "change-me-to-a-long-random-string":
        raise RuntimeError("JWT_SECRET is not set — export a long random string")
    return secret


def hash_password(password: str) -> str:
    """pbkdf2$<algo>$<iterations>$<salt-hex>$<hash-hex> — self-describing so
    verification needs no out-of-band knowledge of the parameters used."""
    salt = secrets.token_hex(16)
    digest = _pbkdf2(password, salt)
    return f"pbkdf2$sha256${PBKDF2_ITERATIONS}${salt}${digest}"


def _pbkdf2(password: str, salt_hex: str, iterations: int = PBKDF2_ITERATIONS) -> str:
    return hashlib.pbkdf2_hmac(
        "sha256", password.encode(), bytes.fromhex(salt_hex), iterations
    ).hex()


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, algo, iters, salt_hex, digest = stored.split("$")
        if scheme != "pbkdf2" or algo != "sha256":
            return False
        candidate = _pbkdf2(password, salt_hex, int(iters))
    except ValueError:
        return False
    # constant-time: a mismatched length must not return faster than a
    # same-length wrong digest
    return hmac.compare_digest(candidate, digest)


def create_token(user_id: str, tenant_id: str, ttl_seconds: int = TOKEN_TTL_SECONDS) -> str:
    now = int(time.time())
    return jwt.encode(
        {"sub": user_id, "tenant": tenant_id, "iat": now, "exp": now + ttl_seconds},
        _secret(),
        algorithm=ALGORITHM,
    )


def decode_token(token: str) -> Principal:
    """Raises jwt.InvalidTokenError (expired/signature/claims included) on
    anything that should not reach the route layer."""
    claims = jwt.decode(token, _secret(), algorithms=[ALGORITHM])
    return Principal(user_id=claims["sub"], tenant_id=claims["tenant"])
