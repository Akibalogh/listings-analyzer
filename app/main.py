"""FastAPI app for the Listings Analyzer."""

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app import db
from app.auth import (
    create_session_cookie,
    is_allowed_email,
    verify_google_id_token,
    verify_session_cookie,
)
from app.config import settings
from app.poller import poll_once

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    logger.info("Database initialized")
    yield


app = FastAPI(
    title="Listings Analyzer",
    description="Paste listing alert text -> extract data -> score against goals",
    version="0.1.0",
    lifespan=lifespan,
)


# --- Auth helpers ---


def _get_current_user(request: Request) -> str | None:
    """Get the current authenticated user email from session cookie."""
    session = request.cookies.get("session")
    if not session:
        return None
    return verify_session_cookie(session)


def _require_auth(request: Request) -> str:
    """Raise 401 if not authenticated, otherwise return email."""
    email = _get_current_user(request)
    if not email:
        raise HTTPException(status_code=401, detail="Not authenticated")
    return email


# --- Auth endpoints ---


@app.get("/auth/config")
def auth_config():
    """Return Google client ID for the sign-in button."""
    return {"client_id": settings.google_client_id}


@app.get("/auth/me")
def auth_me(request: Request):
    """Return the current user or 401."""
    email = _require_auth(request)
    return {"email": email}


@app.post("/auth/login")
async def auth_login(request: Request):
    """Verify Google ID token and create session."""
    body = await request.json()
    credential = body.get("credential", "")
    if not credential:
        raise HTTPException(status_code=400, detail="Missing credential")

    email = verify_google_id_token(credential)
    if not email:
        raise HTTPException(status_code=401, detail="Invalid Google token")

    if not is_allowed_email(email):
        raise HTTPException(
            status_code=403,
            detail="Access denied. This app is restricted to authorized users.",
        )

    session_value = create_session_cookie(email)
    response = JSONResponse({"email": email, "status": "ok"})
    response.set_cookie(
        key="session",
        value=session_value,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        max_age=7 * 24 * 3600,
    )
    return response


@app.post("/auth/logout")
def auth_logout():
    """Clear the session cookie."""
    response = JSONResponse({"status": "ok"})
    response.delete_cookie("session")
    return response


# --- Dashboard ---


@app.get("/", response_class=HTMLResponse)
def dashboard():
    """Serve the mobile-friendly dashboard (auth handled client-side)."""
    html_path = TEMPLATES_DIR / "dashboard.html"
    return HTMLResponse(content=html_path.read_text())


# --- API endpoints (protected) ---


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/poll")
def trigger_poll(request: Request):
    """Trigger a Gmail poll cycle. Returns processed listings."""
    _require_auth(request)
    results = poll_once()
    return {
        "listings_processed": len(results),
        "results": results,
    }


@app.get("/listings")
def list_listings(request: Request):
    """Get all scored listings."""
    _require_auth(request)
    listings = db.get_all_listings()
    return {"count": len(listings), "listings": listings}


@app.get("/listings/{mls_id}")
def get_listing(request: Request, mls_id: str):
    """Get a single listing by MLS ID."""
    _require_auth(request)
    listing = db.get_listing_by_mls(mls_id)
    if not listing:
        raise HTTPException(status_code=404, detail=f"Listing MLS #{mls_id} not found")
    return listing
