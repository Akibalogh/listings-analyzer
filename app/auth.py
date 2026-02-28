"""Google authentication and session management.

Uses Google Identity Services on the frontend. The backend verifies
ID tokens and issues signed session cookies (HMAC-SHA256, stateless).

Only emails in ALLOWED_EMAILS can access the dashboard.
"""

import base64
import hashlib
import hmac
import json
import logging
import time

from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

from app.config import settings

logger = logging.getLogger(__name__)

SESSION_DURATION = 7 * 24 * 3600  # 7 days


def verify_google_id_token(token: str) -> str | None:
    """Verify a Google ID token and return the email if valid.

    Returns None on any failure (bad token, unverified email, etc.).
    """
    try:
        idinfo = id_token.verify_oauth2_token(
            token, google_requests.Request(), settings.google_client_id
        )
        email = idinfo.get("email", "").lower()
        if email and idinfo.get("email_verified"):
            return email
        return None
    except Exception as e:
        logger.warning("Failed to verify Google ID token: %s", e)
        return None


def create_session_cookie(email: str) -> str:
    """Create a signed session cookie value.

    Format: base64(JSON payload) + "." + HMAC signature
    """
    payload = json.dumps({"email": email, "exp": int(time.time()) + SESSION_DURATION})
    payload_b64 = base64.urlsafe_b64encode(payload.encode()).decode()
    sig = hmac.new(
        settings.effective_session_secret.encode(),
        payload.encode(),
        hashlib.sha256,
    ).hexdigest()[:32]
    return f"{payload_b64}.{sig}"


def verify_session_cookie(cookie: str) -> str | None:
    """Verify a session cookie and return the email, or None if invalid."""
    try:
        parts = cookie.rsplit(".", 1)
        if len(parts) != 2:
            return None
        payload_b64, sig = parts
        payload = base64.urlsafe_b64decode(payload_b64).decode()
        expected_sig = hmac.new(
            settings.effective_session_secret.encode(),
            payload.encode(),
            hashlib.sha256,
        ).hexdigest()[:32]
        if not hmac.compare_digest(sig, expected_sig):
            return None
        info = json.loads(payload)
        if info.get("exp", 0) < time.time():
            return None
        email = info.get("email", "").lower()
        if email not in settings.allowed_email_list:
            return None
        return email
    except Exception:
        return None


def is_allowed_email(email: str) -> bool:
    """Check if an email is in the allowed list."""
    return email.lower() in settings.allowed_email_list
