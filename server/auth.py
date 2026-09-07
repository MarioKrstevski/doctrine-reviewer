"""Authentication primitives: hashing, tokens, session and throttle rules.

Deliberately free of HTTP and of the database. Everything here is a pure
function over its arguments, including the clock, so the security-critical
logic can be tested without a running server and without sleeping.
"""

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta, timezone

# scrypt cost parameters. n=2**14 keeps a single hash around 50-100ms on
# modest hardware -- slow enough to make guessing expensive, fast enough
# that a login does not feel stalled.
_N = 2 ** 14
_R = 8
_P = 1
_DKLEN = 32
_SALT_BYTES = 16

SESSION_DAYS = 14
THROTTLE_MAX_ATTEMPTS = 5
THROTTLE_WINDOW_MINUTES = 15


# ---------------------------------------------------------------- passwords

def hash_password(password: str) -> str:
    salt = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P, dklen=_DKLEN
    )
    return f"scrypt${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored) -> bool:
    """Total: any malformed stored value is a failed verification, not a crash."""
    if not isinstance(stored, str):
        return False
    parts = stored.split("$")
    if len(parts) != 3 or parts[0] != "scrypt":
        return False
    try:
        salt = bytes.fromhex(parts[1])
        expected = bytes.fromhex(parts[2])
    except ValueError:
        return False
    if not salt or not expected:
        return False
    try:
        candidate = hashlib.scrypt(
            password.encode("utf-8"), salt=salt, n=_N, r=_R, p=_P,
            dklen=len(expected),
        )
    except ValueError:
        return False
    return hmac.compare_digest(candidate, expected)


# ---------------------------------------------------------------- tokens

def new_token() -> str:
    return secrets.token_urlsafe(32)


def generate_password() -> str:
    """A password the admin reads off the screen once and hands over."""
    return secrets.token_urlsafe(12)


def csrf_token(session_token: str, secret: str) -> str:
    return hmac.new(
        secret.encode("utf-8"), session_token.encode("utf-8"), hashlib.sha256
    ).hexdigest()


def csrf_ok(supplied, session_token: str, secret: str) -> bool:
    if not isinstance(supplied, str) or not supplied:
        return False
    return hmac.compare_digest(supplied, csrf_token(session_token, secret))


# ---------------------------------------------------------------- sessions

def _parse(ts):
    if isinstance(ts, datetime):
        return ts
    try:
        return datetime.fromisoformat(ts)
    except (TypeError, ValueError):
        return None


def session_expiry(now: datetime) -> str:
    return (now + timedelta(days=SESSION_DAYS)).isoformat()


def is_expired(expires_at, now: datetime) -> bool:
    """An unparseable or missing expiry counts as expired."""
    parsed = _parse(expires_at)
    if parsed is None:
        return True
    return parsed <= now


# ---------------------------------------------------------------- throttle

def too_many_attempts(attempt_times, now: datetime) -> bool:
    """True when recent failures should block a further login attempt.

    Counted per username rather than per IP: campus and library NAT means
    many legitimate users share one address.
    """
    cutoff = now - timedelta(minutes=THROTTLE_WINDOW_MINUTES)
    recent = 0
    for ts in attempt_times:
        parsed = _parse(ts)
        if parsed is not None and parsed > cutoff:
            recent += 1
    return recent >= THROTTLE_MAX_ATTEMPTS


def utcnow() -> datetime:
    return datetime.now(timezone.utc)
