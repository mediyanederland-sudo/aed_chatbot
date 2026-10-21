"""Admin authentication using bcrypt password verification, expiring in-memory
sessions, HTTP-only cookies, and optional legacy Bearer tokens."""

import hmac
import logging
import os
import time
import uuid

import bcrypt
from fastapi import HTTPException, Request

log = logging.getLogger("admin_auth")

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME")
_admin_password_plain = os.getenv("ADMIN_PASSWORD")
LEGACY_ADMIN_TOKEN = os.getenv("ADMIN_TOKEN", "")

_DASHBOARD_AUTH_ENABLED = bool(ADMIN_USERNAME and _admin_password_plain)

if _DASHBOARD_AUTH_ENABLED:
    # Hash the configured password once for subsequent login checks.

    _ADMIN_PASSWORD_HASH = bcrypt.hashpw(
        _admin_password_plain.encode("utf-8"), bcrypt.gensalt()
    )
    _admin_password_plain = None  # Release the temporary local plaintext reference.
else:
    _ADMIN_PASSWORD_HASH = None
    log.warning(
        "ADMIN_USERNAME / ADMIN_PASSWORD not set — dashboard login disabled. "
        "The legacy ADMIN_TOKEN bearer is still accepted if configured."
    )

# In-memory token store: {token_string: expiry_timestamp}
_admin_tokens: dict[str, float] = {}
TOKEN_TTL = 8 * 3600  # 8 hours


def login(username: str, password: str) -> str | None:
    """Validate configured credentials and issue an expiring dashboard token."""
    if not _DASHBOARD_AUTH_ENABLED:
        return None
    if not isinstance(username, str) or not isinstance(password, str):
        log.warning("Failed admin login: non-string credentials")
        return None
    username_ok = hmac.compare_digest(username, ADMIN_USERNAME)
    try:
        password_ok = bcrypt.checkpw(password.encode("utf-8"), _ADMIN_PASSWORD_HASH)
    except (ValueError, TypeError):
        password_ok = False
    if username_ok and password_ok:
        token = str(uuid.uuid4())
        _admin_tokens[token] = time.time() + TOKEN_TTL
        _cleanup_expired_tokens()
        log.info(f"Admin login successful for '{username}'")
        return token
    log.warning(f"Failed admin login attempt for '{username}'")
    return None


def _cleanup_expired_tokens():
    now = time.time()
    expired = [t for t, exp in _admin_tokens.items() if now > exp]
    for t in expired:
        del _admin_tokens[t]


def validate_token(token: str) -> bool:
    if not token:
        return False
    exp = _admin_tokens.get(token)
    if not exp:
        return False
    if time.time() > exp:
        del _admin_tokens[token]
        return False
    return True


def logout(token: str):
    _admin_tokens.pop(token, None)


def require_admin(request: Request):
    """FastAPI dependency: accept either the dashboard cookie/Bearer OR the
    legacy ADMIN_TOKEN env-var Bearer. Raises 401 if neither matches.

    Cookie name `aed_admin_token` is namespaced so it doesn't collide with
    other Mediya admin panels on the same host.
    """
    # 1. Dashboard cookie
    cookie_token = request.cookies.get("aed_admin_token")
    if cookie_token and validate_token(cookie_token):
        return cookie_token

    # 2. Authorization header — try dashboard token first, then legacy
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        bearer = auth[7:]
        if validate_token(bearer):
            return bearer
        if LEGACY_ADMIN_TOKEN and hmac.compare_digest(bearer, LEGACY_ADMIN_TOKEN):
            return "legacy"

    raise HTTPException(status_code=401, detail="Unauthorized")
