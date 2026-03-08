"""FastAPI app for the Listings Analyzer."""

import json
import logging
import threading
import time
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
from app.scorer import ai_score_listing, build_batch_request, parse_batch_result

logger = logging.getLogger(__name__)

TEMPLATES_DIR = Path(__file__).parent / "templates"

# Number of listings per batch chunk to limit memory usage during rescore.
# Each listing can include ~6.6MB of base64-encoded images; on a 512MB
# Fly machine, 5 is safe even if a background poll is still in memory.
_BATCH_CHUNK_SIZE = 5


def _scheduled_poll_loop(interval_hours: int):
    """Background thread: poll Gmail and prune sold listings on a fixed interval."""
    interval_secs = interval_hours * 3600
    logger.info(f"Scheduled poller started (every {interval_hours}h)")
    while True:
        time.sleep(interval_secs)
        try:
            results = poll_once()
            logger.info(f"Scheduled poll complete: {len(results)} listing(s)")
        except Exception:
            logger.exception("Scheduled poll failed")

        # Prune sold/off-market listings
        try:
            report = _prune_sold_listings(fix=True)
            if report["sold_count"] > 0 or report.get("pending_count", 0) > 0:
                logger.info(
                    f"Scheduled prune: removed {report['sold_count']} sold, "
                    f"updated {report.get('pending_count', 0)} pending "
                    f"(checked {report['checked']}, errors {report['errors']})"
                )
            else:
                logger.info(f"Scheduled prune: no sold/pending found (checked {report['checked']})")
        except Exception:
            logger.exception("Scheduled prune-sold failed")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    logger.info("Database initialized")

    # Start background poller if configured
    interval = settings.poll_interval_hours
    if interval > 0:
        t = threading.Thread(target=_scheduled_poll_loop, args=(interval,), daemon=True)
        t.start()
    else:
        logger.info("Scheduled polling disabled (POLL_INTERVAL_HOURS=0)")

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


@app.post("/reprocess")
def reprocess_emails(request: Request):
    """Re-fetch and re-parse all processed emails to extract listing URLs + descriptions.

    Used when the parser has been updated (e.g., URL extraction added) and
    existing listings need their URLs/descriptions backfilled.
    """
    from app.gmail import fetch_email_by_id
    from app.parsers import parser_chain
    from app.parsers.onehome import scrape_listing_description

    _require_auth(request)

    gmail_ids = db.get_all_processed_gmail_ids()
    updated = 0
    scraped = 0
    images_found = 0

    for gmail_id in gmail_ids:
        email_data = fetch_email_by_id(gmail_id)
        if not email_data:
            continue

        # Re-parse with current parser (which now extracts URLs)
        listings = parser_chain.parse(
            html=email_data.get("html"),
            text=email_data.get("text"),
            subject=email_data.get("subject", ""),
        )

        for listing in listings:
            if not listing.mls_id or not listing.listing_url:
                continue

            # Scrape description + images from listing page
            description = None
            image_urls = []
            if listing.listing_url:
                description, image_urls = scrape_listing_description(
                    listing.listing_url,
                    address=listing.address,
                    town=listing.town,
                    state=listing.state,
                    zip_code=listing.zip_code,
                    mls_id=listing.mls_id,
                )
                if description:
                    scraped += 1

            db.update_listing_url_by_mls(listing.mls_id, listing.listing_url, description)

            # Backfill address fields from re-parsed data
            db.backfill_listing_address(
                listing.mls_id,
                listing.address,
                listing.town,
                listing.state,
                listing.zip_code,
            )

            # Attach scraped images if found
            if image_urls:
                listing_row = db.get_listing_by_mls(listing.mls_id)
                if listing_row:
                    db.add_listing_images(listing_row["id"], image_urls)
                    images_found += len(image_urls)

            updated += 1

    # Re-score all if criteria exist
    rescore_started = False
    if updated > 0:
        criteria = db.get_active_criteria()
        if criteria:
            _start_rescore(criteria["version"], criteria["instructions"])
            rescore_started = True

    logger.info(f"Reprocess complete: {updated} URLs updated, {scraped} descriptions scraped, {images_found} images")
    return {
        "emails_checked": len(gmail_ids),
        "urls_updated": updated,
        "descriptions_scraped": scraped,
        "images_found": images_found,
        "rescore_started": rescore_started,
    }


@app.get("/want-to-go", response_class=HTMLResponse)
@app.get("/toured", response_class=HTMLResponse)
@app.get("/passed", response_class=HTMLResponse)
@app.get("/non-reject", response_class=HTMLResponse)
def filtered_dashboard():
    """Serve the dashboard — JS reads the URL path to set the initial filter."""
    html_path = TEMPLATES_DIR / "dashboard.html"
    return HTMLResponse(content=html_path.read_text())


@app.get("/listings")
def list_listings():
    """Get all scored listings. Public — no auth required."""
    listings = db.get_all_listings()
    return {"count": len(listings), "listings": listings}


@app.get("/listings/{mls_id}")
def get_listing(mls_id: str):
    """Get a single listing by MLS ID. Public — no auth required."""
    listing = db.get_listing_by_mls(mls_id)
    if not listing:
        raise HTTPException(status_code=404, detail=f"Listing MLS #{mls_id} not found")
    return listing


# --- Evaluation Criteria ---


@app.get("/criteria")
def get_criteria():
    """Get the active evaluation criteria. Public — no auth required."""
    criteria = db.get_active_criteria()
    if not criteria:
        return {"instructions": "", "version": 0}
    return {
        "instructions": criteria["instructions"],
        "version": criteria["version"],
        "created_by": criteria.get("created_by"),
        "created_at": criteria.get("created_at"),
    }


@app.get("/criteria/history")
def get_criteria_history():
    """Get all saved criteria versions, newest first. Public — no auth required."""
    history = db.get_criteria_history()
    return [
        {
            "version": h["version"],
            "created_by": h.get("created_by"),
            "created_at": h.get("created_at"),
            "preview": (h["instructions"] or "")[:80],
            "instructions": h["instructions"],
        }
        for h in history
    ]


@app.put("/criteria")
async def update_criteria(request: Request):
    """Save new evaluation criteria and trigger background re-score."""
    email = _require_auth(request)
    body = await request.json()
    instructions = body.get("instructions", "").strip()
    if not instructions:
        raise HTTPException(status_code=400, detail="Instructions cannot be empty")

    new_version = db.save_criteria(instructions, created_by=email)
    logger.info(f"Saved criteria v{new_version} by {email}")

    # Start background re-score
    _start_rescore(new_version, instructions)

    return {"version": new_version, "rescore_started": True}


# --- Image Management ---


@app.post("/listings/{listing_id}/images")
async def add_images(request: Request, listing_id: int):
    """Attach image URLs to a listing."""
    _require_auth(request)
    body = await request.json()
    image_urls = body.get("image_urls", [])

    if not isinstance(image_urls, list):
        raise HTTPException(status_code=400, detail="image_urls must be a list")

    # Validate URLs (basic check)
    for url in image_urls:
        if not isinstance(url, str) or not url.startswith(("http://", "https://")):
            raise HTTPException(status_code=400, detail=f"Invalid URL: {url}")

    listing = db.get_listing_by_id(listing_id)
    if not listing:
        raise HTTPException(status_code=404, detail=f"Listing #{listing_id} not found")

    db.add_listing_images(listing_id, image_urls)
    return {"listing_id": listing_id, "image_count": len(image_urls)}


# --- Toured status ---


@app.post("/listings/{listing_id}/toured")
async def mark_toured(request: Request, listing_id: int):
    """Mark or un-mark a listing as toured. Requires auth."""
    _require_auth(request)
    listing = db.get_listing_by_id(listing_id)
    if not listing:
        raise HTTPException(status_code=404, detail=f"Listing #{listing_id} not found")

    body = await request.json()
    toured = bool(body.get("toured", True))
    db.mark_listing_toured(listing_id, toured)
    return {"listing_id": listing_id, "toured": toured}


# --- Tour request ---


@app.post("/listings/{listing_id}/tour-request")
async def toggle_tour_request(request: Request, listing_id: int):
    """Flag or un-flag a listing for tour request. Requires auth."""
    _require_auth(request)
    listing = db.get_listing_by_id(listing_id)
    if not listing:
        raise HTTPException(status_code=404, detail=f"Listing #{listing_id} not found")

    body = await request.json()
    tour_requested = bool(body.get("tour_requested", True))
    db.mark_listing_tour_requested(listing_id, tour_requested)
    return {"listing_id": listing_id, "tour_requested": tour_requested}


# --- Passed status ---


@app.post("/listings/{listing_id}/passed")
async def toggle_passed(request: Request, listing_id: int):
    """Flag or un-flag a listing as passed (chose not to pursue). Requires auth."""
    _require_auth(request)
    listing = db.get_listing_by_id(listing_id)
    if not listing:
        raise HTTPException(status_code=404, detail=f"Listing #{listing_id} not found")

    body = await request.json()
    passed = bool(body.get("passed", True))
    db.mark_listing_passed(listing_id, passed)
    return {"listing_id": listing_id, "passed": passed}


# --- Mark as sold (delete) ---


@app.post("/listings/{listing_id}/sold")
async def mark_sold(request: Request, listing_id: int):
    """Delete a listing that has been sold. Requires auth."""
    _require_auth(request)
    listing = db.get_listing_by_id(listing_id)
    if not listing:
        raise HTTPException(status_code=404, detail=f"Listing #{listing_id} not found")

    ph = db._placeholder()
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute(f"DELETE FROM scores WHERE listing_id = {ph}", (listing_id,))
        cur.execute(f"DELETE FROM listings WHERE id = {ph}", (listing_id,))
        conn.commit()

    logger.info("Listing #%s marked as sold and deleted", listing_id)
    return {"listing_id": listing_id, "deleted": True}


# --- Listing URL + description scraping ---


@app.post("/listings/{listing_id}/scrape")
async def scrape_listing(request: Request, listing_id: int):
    """Set a listing URL, scrape the description, and re-score."""
    from app.parsers.onehome import scrape_listing_description

    _require_auth(request)
    body = await request.json()
    url = (body.get("listing_url") or "").strip()
    if not url or not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Invalid listing URL")

    listing = db.get_listing_by_id(listing_id)
    if not listing:
        raise HTTPException(status_code=404, detail=f"Listing #{listing_id} not found")

    # Scrape description + images from the listing page
    logger.info(f"Scraping listing page for #{listing_id}: {url}")
    description, image_urls = scrape_listing_description(
        url,
        address=listing.get("address"),
        town=listing.get("town"),
        state=listing.get("state"),
        zip_code=listing.get("zip_code"),
        mls_id=listing.get("mls_id"),
    )

    # Update the listing in DB
    db.update_listing_description(listing_id, url, description)

    # Attach scraped images if found
    if image_urls:
        db.add_listing_images(listing_id, image_urls)

    # Auto re-score with new description
    criteria = db.get_active_criteria()
    result_info = {
        "listing_id": listing_id,
        "listing_url": url,
        "description_found": description is not None,
        "images_found": len(image_urls),
    }

    if criteria:
        # Re-fetch listing with updated description
        updated_listing = db.get_listing_by_id(listing_id)
        score = _rescore_one_listing(updated_listing, criteria)
        result_info["score"] = score.score
        result_info["verdict"] = score.verdict

    return result_info


# --- Add listing from URL ---


@app.post("/listings/add")
async def add_listing_from_url(request: Request):
    """Create a new listing from a URL. Scrapes, extracts data, and scores."""
    import re
    import httpx
    from app.parsers.onehome import scrape_listing_description, _extract_property_stats
    from app.parsers.plaintext import REDFIN_URL_ADDR_RE
    from app.enrichment import normalize_address, fetch_commute_time, fetch_school_data
    from app.models import ParsedListing, ScoringResult

    _require_auth(request)
    body = await request.json()
    url = (body.get("url") or "").strip()
    if not url or not url.startswith(("http://", "https://")):
        raise HTTPException(status_code=400, detail="Invalid URL")

    # Resolve short URLs (e.g. redf.in)
    try:
        with httpx.Client(timeout=10, follow_redirects=True) as client:
            resp = client.head(url)
            resolved_url = str(resp.url)
    except Exception:
        resolved_url = url

    # Extract address from Redfin URL path
    address = town = state = zip_code = None
    redfin_match = REDFIN_URL_ADDR_RE.search(resolved_url)
    if redfin_match:
        state = redfin_match.group(1).upper()
        town = redfin_match.group(2).replace("-", " ").title()
        addr_slug = redfin_match.group(3).replace("-", " ")
        if redfin_match.group(4):
            zip_code = redfin_match.group(4)
        address = addr_slug.title()

    # Check for duplicates by address
    if address and town:
        address_key = normalize_address(address, town, state)
        if address_key and db.is_listing_duplicate_by_address(address_key):
            raise HTTPException(status_code=409, detail=f"Listing already exists: {address}, {town}")
    else:
        address_key = None

    # Scrape the page for description + images + structured data
    description, image_urls = scrape_listing_description(
        resolved_url, address=address, town=town, state=state, zip_code=zip_code,
    )

    # Try to extract structured data from the page
    price = beds = baths = sqft = year_built = list_date = lot_acres = None
    try:
        with httpx.Client(timeout=10, follow_redirects=True, headers={
            "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        }) as client:
            page_resp = client.get(resolved_url)
            if page_resp.status_code == 200:
                stats = _extract_property_stats(page_resp.text)
                if stats:
                    price = stats.get("price")
                    beds = stats.get("bedrooms")
                    baths = stats.get("bathrooms")
                    sqft = stats.get("sqft")
                    year_built = stats.get("year_built")
                    list_date = stats.get("list_date")
                    lot_acres = stats.get("lot_acres")
    except Exception:
        pass

    # Create the listing
    listing = ParsedListing(
        source_format="manual",
        address=address,
        town=town,
        state=state,
        zip_code=zip_code,
        price=price,
        bedrooms=beds,
        bathrooms=baths,
        sqft=sqft,
        year_built=year_built,
        list_date=list_date,
        lot_acres=lot_acres,
        listing_url=resolved_url,
        description=description,
    )

    # Placeholder score — will rescore below
    placeholder_score = ScoringResult(
        score=0, verdict="Reject", hard_results=[], soft_points={},
        concerns=["Pending scoring"], confidence="low",
    )

    # Enrichment
    enrichment = {}
    if address_key:
        enrichment["address_key"] = address_key
    if zip_code:
        school_json = fetch_school_data(zip_code, state)
        if school_json:
            enrichment["school_data_json"] = school_json
    if address and town:
        commute = fetch_commute_time(address, town, state, zip_code)
        if commute:
            enrichment["commute_minutes"] = commute.get("commute_minutes")
            enrichment["commute_data_json"] = commute.get("commute_data_json")

    # Save a dummy email record
    email_id = db.save_processed_email(
        gmail_id=f"manual-{resolved_url}",
        message_id="",
        sender="manual",
        subject=f"Manual add: {address or resolved_url}",
        parser_used="manual",
        listings_found=1,
    )

    listing_id = db.save_listing(listing, placeholder_score, email_id, enrichment)
    if image_urls:
        db.add_listing_images(listing_id, image_urls)

    # Score with AI
    result_info = {
        "listing_id": listing_id,
        "address": address,
        "town": town,
        "url": resolved_url,
        "description_found": description is not None,
    }

    criteria = db.get_active_criteria()
    if criteria:
        saved = db.get_listing_by_id(listing_id)
        score = _rescore_one_listing(saved, criteria)
        result_info["score"] = score.score
        result_info["verdict"] = score.verdict

    return result_info


# --- Re-scoring ---


@app.post("/listings/{listing_id}/rescore")
def rescore_single(request: Request, listing_id: int):
    """Re-score a single listing with current criteria."""
    key = request.headers.get("x-manage-key", "")
    if not (settings.manage_key and key == settings.manage_key):
        _require_auth(request)
    listing = db.get_listing_by_id(listing_id)
    if not listing:
        raise HTTPException(status_code=404, detail=f"Listing #{listing_id} not found")

    criteria = db.get_active_criteria()
    if not criteria:
        raise HTTPException(status_code=400, detail="No evaluation criteria configured")

    result = _rescore_one_listing(listing, criteria)
    return {
        "listing_id": listing_id,
        "score": result.score,
        "verdict": result.verdict,
        "evaluation_method": result.evaluation_method,
    }


@app.get("/rescore/status")
def rescore_status():
    """Get the status of the background re-score operation. Public — no auth required."""
    state = dict(db.rescore_state)
    # Add human-readable elapsed time and stuck flag
    started = state.get("started_at")
    if started and state.get("in_progress"):
        elapsed = int(time.time() - started)
        state["elapsed_seconds"] = elapsed
        state["stuck"] = elapsed > 1800 and state.get("completed", 0) == 0
    state.pop("started_at", None)  # don't expose raw timestamp
    return state


# --- Background re-scoring ---


def _build_listing_data(listing_row: dict) -> dict:
    """Extract listing data dict from a DB row for AI scoring."""
    listing_data = {
        "address": listing_row.get("address"),
        "town": listing_row.get("town"),
        "state": listing_row.get("state"),
        "zip_code": listing_row.get("zip_code"),
        "mls_id": listing_row.get("mls_id"),
        "price": listing_row.get("price"),
        "sqft": listing_row.get("sqft"),
        "bedrooms": listing_row.get("bedrooms"),
        "bathrooms": listing_row.get("bathrooms"),
        "property_type": listing_row.get("property_type"),
        "listing_status": listing_row.get("listing_status"),
        "description": listing_row.get("description"),
        "year_built": listing_row.get("year_built"),
        "list_date": listing_row.get("list_date"),
        "lot_acres": listing_row.get("lot_acres"),
    }

    # Add enrichment data (school quality + commute time)
    if listing_row.get("school_data_json"):
        try:
            listing_data["school_data"] = json.loads(listing_row["school_data_json"])
        except (json.JSONDecodeError, TypeError):
            pass
    if listing_row.get("commute_minutes") is not None:
        listing_data["commute_minutes"] = listing_row["commute_minutes"]
        # Extract commute_mode from stored JSON (transit or drive)
        commute_json = listing_row.get("commute_data_json")
        if commute_json:
            try:
                cd = json.loads(commute_json)
                listing_data["commute_mode"] = cd.get("commute_mode", "transit")
            except (json.JSONDecodeError, TypeError):
                pass

    # Age/condition signal (computed on-the-fly, no DB storage needed)
    from app.enrichment import score_age_condition, get_price_per_sqft_signal
    age_cond = score_age_condition(
        listing_row.get("year_built"),
        listing_row.get("description"),
    )
    listing_data["age_condition"] = age_cond

    # Price/sqft benchmark vs Zillow median
    ppsf = get_price_per_sqft_signal(
        listing_row.get("price"),
        listing_row.get("sqft"),
        listing_row.get("zip_code"),
    )
    if ppsf:
        listing_data["price_per_sqft_signal"] = ppsf

    # Property tax (stored JSON if previously fetched)
    if listing_row.get("property_tax_json"):
        try:
            listing_data["property_tax"] = json.loads(listing_row["property_tax_json"])
        except (json.JSONDecodeError, TypeError):
            pass

    # Lot size (acres)
    if listing_row.get("lot_acres") is not None:
        listing_data["lot_acres"] = listing_row["lot_acres"]

    # Power line proximity (stored JSON if previously fetched)
    if listing_row.get("power_line_json"):
        try:
            pl = json.loads(listing_row["power_line_json"])
            if pl.get("nearest_distance_m") is not None:
                listing_data["power_line_proximity"] = pl
        except (json.JSONDecodeError, TypeError):
            pass

    # FEMA flood zone (stored JSON if previously fetched)
    if listing_row.get("flood_zone_json"):
        try:
            fz = json.loads(listing_row["flood_zone_json"])
            if fz.get("fld_zone"):
                listing_data["flood_zone"] = fz
        except (json.JSONDecodeError, TypeError):
            pass

    # Metro-North station proximity (stored JSON if previously fetched)
    if listing_row.get("station_json"):
        try:
            st = json.loads(listing_row["station_json"])
            if st.get("station"):
                listing_data["nearest_metro_north"] = st
        except (json.JSONDecodeError, TypeError):
            pass

    # Structured description signals (parsed from text, stored in DB)
    if listing_row.get("garage_count") is not None:
        listing_data["garage"] = {
            "count": listing_row["garage_count"],
            "type": listing_row.get("garage_type"),
        }
    if listing_row.get("hoa_monthly") is not None:
        listing_data["hoa_monthly"] = listing_row["hoa_monthly"]
    if listing_row.get("has_pool") is not None:
        listing_data["pool"] = {
            "has_pool": bool(listing_row["has_pool"]),
            "type": listing_row.get("pool_type"),
        }
    if listing_row.get("has_basement") is not None:
        listing_data["basement"] = {
            "has_basement": bool(listing_row["has_basement"]),
            "type": listing_row.get("basement_type"),
        }

    return listing_data


def _get_image_urls(listing_row: dict) -> list[str] | None:
    """Extract image URLs from a DB row."""
    raw_images = listing_row.get("image_urls_json")
    if raw_images:
        try:
            return json.loads(raw_images)
        except (json.JSONDecodeError, TypeError):
            pass
    return None


def _rescore_one_listing(listing_row: dict, criteria: dict) -> "ScoringResult":
    """Re-score a single listing dict using AI evaluation."""
    listing_data = _build_listing_data(listing_row)
    image_urls = _get_image_urls(listing_row)

    score, reasoning = ai_score_listing(
        listing_data=listing_data,
        instructions=criteria["instructions"],
        image_urls=image_urls,
    )
    score.criteria_version = criteria["version"]

    db.update_score(
        listing_id=listing_row["id"],
        score=score,
        method=score.evaluation_method,
        criteria_version=criteria["version"],
        reasoning=reasoning,
        property_summary=score.property_summary,
    )
    return score


def _should_skip(listing_row: dict, score_meta: dict | None, criteria_version: int) -> bool:
    """Determine if a listing can be skipped during rescore.

    Skip when: same criteria version AND no new enrichment since last score.
    """
    if not score_meta or score_meta.get("criteria_version") != criteria_version:
        return False  # different criteria → must rescore
    scored_at = score_meta.get("scored_at")
    enriched_at = listing_row.get("enriched_at")
    if enriched_at and scored_at and enriched_at > scored_at:
        return False  # enrichment after scoring → must rescore
    return True  # same criteria, no new enrichment → skip


def _rescore_all(criteria_version: int, instructions: str):
    """Background thread: re-score all listings using the Batch API (50% discount).

    Processes listings in chunks of _BATCH_CHUNK_SIZE to limit peak memory —
    each listing can include ~6.6MB of base64-encoded images, so building all
    batch requests at once causes OOM on 512MB machines.

    Falls back to sequential scoring if batch submission fails.
    """
    import anthropic

    try:
        listing_ids = db.get_all_listing_ids()
        score_metadata = db.get_all_score_metadata()

        # First pass: collect IDs that need rescoring (lightweight — no images loaded)
        ids_to_score = []
        skip_count = 0
        for lid in listing_ids:
            listing = db.get_listing_by_id(lid)
            if not listing:
                continue
            meta = score_metadata.get(lid)
            if _should_skip(listing, meta, criteria_version):
                skip_count += 1
            else:
                ids_to_score.append(lid)

        total_to_score = len(ids_to_score)
        db.rescore_state["total"] = total_to_score + skip_count
        db.rescore_state["completed"] = skip_count
        db.rescore_state["skipped"] = skip_count

        logger.info(
            f"Rescore: {total_to_score} to score, {skip_count} skipped "
            f"(criteria v{criteria_version})"
        )

        if total_to_score == 0:
            logger.info("Nothing to rescore — all listings up to date")
            return

        if not settings.anthropic_api_key:
            logger.error("ANTHROPIC_API_KEY not set, cannot rescore")
            return

        try:
            client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
            scored_so_far = 0

            # Process in chunks to keep peak memory low
            for chunk_start in range(0, total_to_score, _BATCH_CHUNK_SIZE):
                chunk_ids = ids_to_score[chunk_start : chunk_start + _BATCH_CHUNK_SIZE]
                chunk_num = chunk_start // _BATCH_CHUNK_SIZE + 1
                total_chunks = (total_to_score + _BATCH_CHUNK_SIZE - 1) // _BATCH_CHUNK_SIZE

                # Build batch requests for this chunk only
                batch_requests = []
                for lid in chunk_ids:
                    listing = db.get_listing_by_id(lid)
                    if not listing:
                        continue
                    listing_data = _build_listing_data(listing)
                    image_urls = _get_image_urls(listing)
                    req = build_batch_request(
                        custom_id=f"listing_{lid}",
                        listing_data=listing_data,
                        instructions=instructions,
                        image_urls=image_urls,
                    )
                    batch_requests.append(req)

                if not batch_requests:
                    continue

                batch = client.messages.batches.create(requests=batch_requests)
                batch_id = batch.id
                db.rescore_state["batch_id"] = batch_id
                logger.info(
                    f"Batch chunk {chunk_num}/{total_chunks} submitted: "
                    f"{batch_id} ({len(batch_requests)} requests)"
                )

                # Free memory before polling
                del batch_requests

                # Poll until batch completes (max 20 min per chunk)
                _BATCH_POLL_INTERVAL = 30
                _BATCH_MAX_POLLS = 40  # 40 × 30s = 20 minutes
                for _poll in range(_BATCH_MAX_POLLS):
                    time.sleep(_BATCH_POLL_INTERVAL)
                    batch = client.messages.batches.retrieve(batch_id)
                    status = batch.processing_status
                    logger.info(f"Batch {batch_id}: {status} (poll {_poll + 1}/{_BATCH_MAX_POLLS})")
                    if status == "ended":
                        break
                else:
                    logger.error(
                        f"Batch {batch_id} timed out after {_BATCH_MAX_POLLS} polls "
                        f"— falling back to sequential for remaining listings"
                    )
                    raise RuntimeError(f"Batch {batch_id} timed out")

                # Process results for this chunk
                for result in client.messages.batches.results(batch_id):
                    custom_id = result.custom_id
                    lid = int(custom_id.split("_")[1])

                    score_result, reasoning = parse_batch_result(result)
                    if score_result:
                        score_result.criteria_version = criteria_version
                        db.update_score(
                            listing_id=lid,
                            score=score_result,
                            method=score_result.evaluation_method,
                            criteria_version=criteria_version,
                            reasoning=reasoning,
                            property_summary=score_result.property_summary,
                        )
                    else:
                        logger.warning(f"Batch result for listing #{lid} could not be parsed")

                    scored_so_far += 1
                    db.rescore_state["completed"] = skip_count + scored_so_far

            logger.info(
                f"Batch rescore complete: {scored_so_far}/{total_to_score} scored, "
                f"{skip_count} skipped (criteria v{criteria_version})"
            )

        except Exception as e:
            logger.warning(f"Batch API failed ({e}), falling back to sequential")
            _rescore_all_sequential(criteria_version, instructions, listing_ids, score_metadata)

    except Exception:
        logger.exception("Background re-score failed")
    finally:
        db.rescore_state["in_progress"] = False


def _rescore_all_sequential(
    criteria_version: int,
    instructions: str,
    listing_ids: list[int],
    score_metadata: dict[int, dict],
):
    """Sequential fallback for rescoring (used when batch API fails)."""
    criteria = {"instructions": instructions, "version": criteria_version}
    skip_count = db.rescore_state.get("skipped", 0)
    completed = skip_count

    for lid in listing_ids:
        listing = db.get_listing_by_id(lid)
        if not listing:
            continue

        meta = score_metadata.get(lid)
        if _should_skip(listing, meta, criteria_version):
            continue

        try:
            _rescore_one_listing(listing, criteria)
        except Exception as e:
            logger.error(f"Failed to re-score listing #{lid}: {e}")

        completed += 1
        db.rescore_state["completed"] = completed

    logger.info(
        f"Sequential rescore complete: {completed - skip_count} scored, "
        f"{skip_count} skipped (criteria v{criteria_version})"
    )


def _start_rescore(criteria_version: int, instructions: str, sequential: bool = True):
    """Launch background re-score thread if not already running.

    If a rescore has been in_progress with 0 completed for over 30 minutes,
    it's considered stuck and will be cleared automatically.
    """
    if db.rescore_state["in_progress"]:
        started = db.rescore_state.get("started_at")
        completed = db.rescore_state.get("completed", 0)
        stuck = (
            started is not None
            and completed == 0
            and (time.time() - started) > 1800  # 30 minutes
        )
        if stuck:
            logger.warning("Rescore stuck (0 completed in 30+ min) — clearing and restarting")
            db.rescore_state["in_progress"] = False
        else:
            logger.warning("Re-score already in progress, skipping")
            return

    db.rescore_state["in_progress"] = True
    db.rescore_state["started_at"] = time.time()
    db.rescore_state["criteria_version"] = criteria_version
    db.rescore_state["skipped"] = 0
    db.rescore_state["batch_id"] = None
    target = _rescore_all_sequential_standalone if sequential else _rescore_all
    t = threading.Thread(
        target=target,
        args=(criteria_version, instructions),
        daemon=True,
    )
    t.start()


def _rescore_all_sequential_standalone(criteria_version: int, instructions: str):
    """Sequential rescore entry point (bypasses batch API entirely)."""
    try:
        listing_ids = db.get_all_listing_ids()
        score_metadata = db.get_all_score_metadata()
        ids_to_score = []
        skip_count = 0
        for lid in listing_ids:
            listing = db.get_listing_by_id(lid)
            if not listing:
                continue
            meta = score_metadata.get(lid)
            if _should_skip(listing, meta, criteria_version):
                skip_count += 1
            else:
                ids_to_score.append(lid)
        db.rescore_state["total"] = len(ids_to_score) + skip_count
        db.rescore_state["skipped"] = skip_count
        criteria = {"instructions": instructions, "version": criteria_version}
        _rescore_all_sequential(criteria_version, instructions, ids_to_score, {})
    except Exception:
        logger.exception("Sequential standalone rescore failed")
    finally:
        db.rescore_state["in_progress"] = False


# --- Management Endpoints ---


@app.post("/manage/update-criteria")
async def manage_update_criteria(request: Request):
    """Save new evaluation criteria and trigger background re-score.

    Protected by MANAGE_KEY env var (not user auth).
    Useful for programmatic criteria updates without a browser session.
    Body: {"instructions": "<criteria text>", "created_by": "<email>"}
    Optional query param: ?sequential=true to bypass batch API.
    """
    key = request.headers.get("x-manage-key", "")
    if not settings.manage_key or key != settings.manage_key:
        raise HTTPException(status_code=403, detail="Invalid or missing management key")

    body = await request.json()
    instructions = body.get("instructions", "").strip()
    if not instructions:
        raise HTTPException(status_code=400, detail="instructions cannot be empty")
    created_by = body.get("created_by", "manage-api")

    sequential = request.query_params.get("sequential", "true").lower() != "false"
    new_version = db.save_criteria(instructions, created_by=created_by)
    logger.info(f"Saved criteria v{new_version} via manage API by {created_by}")

    _start_rescore(new_version, instructions, sequential=sequential)

    return {
        "version": new_version,
        "rescore_started": True,
        "mode": "sequential" if sequential else "batch",
        "instructions_preview": instructions[:200] + "...",
    }


@app.post("/manage/sync-criteria")
def sync_criteria(request: Request):
    """Trigger a background re-score with the current active criteria.

    Protected by MANAGE_KEY env var (not user auth).
    Useful for triggering a full rescore after deploying code changes.
    With ?force=true: clears criteria_version on all scores first,
    bypassing the skip-unchanged logic (useful after a failed rescore
    that wrote the version but didn't actually AI-score).
    """
    key = request.headers.get("x-manage-key", "")
    if not settings.manage_key or key != settings.manage_key:
        raise HTTPException(status_code=403, detail="Invalid or missing management key")

    criteria = db.get_active_criteria()
    if not criteria:
        raise HTTPException(status_code=404, detail="No active criteria found — set criteria via AI Criteria in dashboard first")

    force = request.query_params.get("force", "").lower() == "true"
    sequential = request.query_params.get("sequential", "true").lower() != "false"
    if force:
        with db.get_connection() as conn:
            cur = conn.cursor()
            cur.execute("UPDATE scores SET criteria_version = NULL")
        logger.info("Force rescore: cleared criteria_version on all scores")

    _start_rescore(criteria["version"], criteria["instructions"], sequential=sequential)
    logger.info(
        f"Triggered rescore with active criteria v{criteria['version']} "
        f"({'sequential' if sequential else 'batch'})"
    )

    return {
        "synced": True,
        "version": criteria["version"],
        "rescore_started": True,
        "force": force,
        "mode": "sequential" if sequential else "batch",
        "instructions_preview": criteria["instructions"][:200] + "...",
    }


@app.post("/manage/update-listing")
def manage_update_listing(request: Request, body: dict = {}):
    """Update specific fields on a listing by ID.

    Body: {"listing_id": 62, "year_built": 1994}
    Allowed fields: year_built, price, sqft, bedrooms, bathrooms, address, town, state, zip_code
    Protected by MANAGE_KEY env var.
    """
    key = request.headers.get("x-manage-key", "")
    if not settings.manage_key or key != settings.manage_key:
        raise HTTPException(status_code=403, detail="Invalid or missing management key")

    listing_id = body.get("listing_id")
    if not listing_id:
        raise HTTPException(status_code=400, detail="Provide listing_id in JSON body")

    ALLOWED = {"year_built", "price", "sqft", "bedrooms", "bathrooms", "address", "town", "state", "zip_code"}
    fields = {k: v for k, v in body.items() if k in ALLOWED and v is not None}
    if not fields:
        raise HTTPException(status_code=400, detail=f"No valid fields provided. Allowed: {sorted(ALLOWED)}")

    db.update_listing_fields_by_id(listing_id, **fields)
    listing = db.get_listing_by_id(listing_id)
    if not listing:
        raise HTTPException(status_code=404, detail=f"Listing #{listing_id} not found")

    return {"listing_id": listing_id, "updated": fields, "message": "OK"}


@app.post("/manage/poll")
def manage_poll(request: Request):
    """Trigger a Gmail poll cycle. Protected by MANAGE_KEY env var.

    Same as POST /poll but uses management key instead of Google session auth.
    """
    key = request.headers.get("x-manage-key", "")
    if not settings.manage_key or key != settings.manage_key:
        raise HTTPException(status_code=403, detail="Invalid or missing management key")

    results = poll_once()
    return {
        "listings_processed": len(results),
        "results": results,
    }


@app.post("/manage/cleanup")
def manage_cleanup(request: Request, body: dict = {}):
    """Delete listings by ID list. Protected by MANAGE_KEY env var.

    Body: {"listing_ids": [8, 9, 10, ...]}
    """
    key = request.headers.get("x-manage-key", "")
    if not settings.manage_key or key != settings.manage_key:
        raise HTTPException(status_code=403, detail="Invalid or missing management key")

    listing_ids = body.get("listing_ids", [])
    if not listing_ids:
        raise HTTPException(status_code=400, detail="Provide listing_ids in JSON body")

    deleted = 0
    with db.get_connection() as conn:
        cur = conn.cursor()
        ph = db._placeholder()
        for lid in listing_ids:
            cur.execute(f"DELETE FROM scores WHERE listing_id = {ph}", (lid,))
            cur.execute(f"DELETE FROM listings WHERE id = {ph}", (lid,))
            deleted += cur.rowcount
        conn.commit()

    return {"deleted": deleted, "listing_ids": listing_ids}


@app.post("/manage/reset-emails")
def manage_reset_emails(request: Request):
    """Reset processed emails so they get re-ingested on next poll.

    Clears processed_emails records that have no remaining listings,
    and removes the Gmail 'Processed' label so the emails reappear.

    Protected by MANAGE_KEY env var.
    """
    from app.gmail import _build_service, _get_or_create_label, PROCESSED_LABEL

    key = request.headers.get("x-manage-key", "")
    if not settings.manage_key or key != settings.manage_key:
        raise HTTPException(status_code=403, detail="Invalid or missing management key")

    # Find processed_emails with no remaining listings
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT pe.id, pe.gmail_id
            FROM processed_emails pe
            LEFT JOIN listings l ON l.source_email_id = pe.id
            WHERE l.id IS NULL
        """)
        if settings.is_postgres:
            orphans = [(row[0], row[1]) for row in cur.fetchall()]
        else:
            orphans = [(row["id"], row["gmail_id"]) for row in cur.fetchall()]

    if not orphans:
        return {"reset": 0, "message": "No orphaned processed emails found"}

    # Remove Gmail label from orphaned emails
    gmail_reset = 0
    try:
        service = _build_service()
        label_id = _get_or_create_label(service)
        for _, gmail_id in orphans:
            try:
                service.users().messages().modify(
                    userId="me",
                    id=gmail_id,
                    body={"removeLabelIds": [label_id]},
                ).execute()
                gmail_reset += 1
            except Exception as e:
                logger.warning(f"Failed to remove label from {gmail_id}: {e}")
    except Exception as e:
        logger.error(f"Failed to connect to Gmail: {e}")

    # Delete orphaned processed_emails records from DB
    ph = db._placeholder()
    with db.get_connection() as conn:
        cur = conn.cursor()
        for pe_id, _ in orphans:
            cur.execute(f"DELETE FROM processed_emails WHERE id = {ph}", (pe_id,))
        conn.commit()

    return {
        "reset": len(orphans),
        "gmail_labels_removed": gmail_reset,
        "email_ids": [gid for _, gid in orphans],
    }


@app.post("/manage/reprocess")
def manage_reprocess(request: Request):
    """Re-fetch emails, extract URLs, scrape descriptions, and rescore.

    Protected by MANAGE_KEY env var.
    """
    from app.gmail import fetch_email_by_id
    from app.parsers import parser_chain
    from app.parsers.onehome import scrape_listing_description

    key = request.headers.get("x-manage-key", "")
    if not settings.manage_key or key != settings.manage_key:
        raise HTTPException(status_code=403, detail="Invalid or missing management key")

    gmail_ids = db.get_all_processed_gmail_ids()
    updated = 0
    scraped = 0
    images_found = 0

    for gid in gmail_ids:
        raw = fetch_email_by_id(gid)
        if not raw:
            continue
        listings = parser_chain.parse(raw.get("html", ""), raw.get("text", ""))
        for listing in listings:
            if not listing.mls_id or not listing.listing_url:
                continue
            description = None
            image_urls = []
            if listing.listing_url:
                description, image_urls = scrape_listing_description(
                    listing.listing_url,
                    address=listing.address,
                    town=listing.town,
                    state=listing.state,
                    zip_code=listing.zip_code,
                    mls_id=listing.mls_id,
                )
                if description:
                    scraped += 1
            db.update_listing_url_by_mls(listing.mls_id, listing.listing_url, description)

            # Backfill address fields from re-parsed data
            db.backfill_listing_address(
                listing.mls_id,
                listing.address,
                listing.town,
                listing.state,
                listing.zip_code,
            )

            # Attach scraped images
            if image_urls:
                listing_row = db.get_listing_by_mls(listing.mls_id)
                if listing_row:
                    db.add_listing_images(listing_row["id"], image_urls)
                    images_found += len(image_urls)

            updated += 1

    rescore_started = False
    if updated > 0:
        criteria = db.get_active_criteria()
        if criteria:
            _start_rescore(criteria["version"], criteria["instructions"])
            rescore_started = True

    return {
        "emails_checked": len(gmail_ids),
        "urls_updated": updated,
        "descriptions_scraped": scraped,
        "images_found": images_found,
        "rescore_started": rescore_started,
    }


@app.post("/manage/scrape-descriptions")
def manage_scrape_descriptions(request: Request):
    """Find URLs and scrape descriptions for listings.

    Phase 1: For listings WITHOUT a URL, search DuckDuckGo for a Redfin URL.
    Phase 2: For listings WITH a URL but no description, scrape the listing page.
    Protected by MANAGE_KEY env var.
    """
    from app.parsers.onehome import _search_redfin_url, scrape_listing_description, scrape_listing_structured_data

    key = request.headers.get("x-manage-key", "")
    if not settings.manage_key or key != settings.manage_key:
        raise HTTPException(status_code=403, detail="Invalid or missing management key")

    listing_ids = db.get_all_listing_ids()
    urls_found = 0
    scraped = 0
    images_found = 0
    skipped = 0
    errors = []

    for lid in listing_ids:
        listing = db.get_listing_by_id(lid)
        if not listing:
            continue

        url = listing.get("listing_url")

        # Phase 1: find URL via DDG search if missing
        if not url and listing.get("address") and listing.get("town"):
            try:
                found_url = _search_redfin_url(
                    address=listing["address"],
                    town=listing["town"],
                    state=listing.get("state"),
                    zip_code=listing.get("zip_code"),
                    mls_id=listing.get("mls_id"),
                )
                if found_url:
                    # Update URL only, preserve existing description
                    ph = db._placeholder()
                    with db.get_connection() as conn:
                        cur = conn.cursor()
                        cur.execute(
                            f"UPDATE listings SET listing_url = {ph} WHERE id = {ph}",
                            (found_url, lid),
                        )
                    url = found_url
                    urls_found += 1
                    logger.info(f"Found URL for listing #{lid}: {found_url}")
            except Exception as e:
                logger.error(f"URL search failed for listing #{lid}: {e}")
                errors.append(f"#{lid} URL search: {e}")

        # Phase 2: scrape description if we have URL but no description
        existing_desc = listing.get("description")
        if not url or existing_desc:
            skipped += 1
            continue

        logger.info(f"Scraping description for listing #{lid}: {url}")
        try:
            description, image_urls = scrape_listing_description(
                url,
                address=listing.get("address"),
                town=listing.get("town"),
                state=listing.get("state"),
                zip_code=listing.get("zip_code"),
                mls_id=listing.get("mls_id"),
            )

            if description:
                db.update_listing_description(lid, url, description)
                scraped += 1
                logger.info(f"Scraped {len(description)} chars for listing #{lid}")

            if image_urls:
                db.add_listing_images(lid, image_urls)
                images_found += len(image_urls)
        except Exception as e:
            logger.error(f"Failed to scrape listing #{lid} ({url}): {e}")
            errors.append(f"#{lid}: {e}")

    # Phase 2.5: re-enumerate Redfin CDN images for listings with few images
    from app.parsers.onehome import enumerate_redfin_images

    images_expanded = 0
    for lid in listing_ids:
        listing = db.get_listing_by_id(lid)
        if not listing:
            continue
        raw_images = listing.get("image_urls_json")
        if not raw_images:
            continue
        try:
            current_images = json.loads(raw_images)
        except (json.JSONDecodeError, TypeError):
            continue
        if not isinstance(current_images, list) or len(current_images) >= 10:
            continue
        if not any("ssl.cdn-redfin.com" in u for u in current_images):
            continue
        try:
            enumerated = enumerate_redfin_images(current_images)
            if len(enumerated) > len(current_images):
                db.add_listing_images(lid, enumerated)
                images_expanded += 1
                logger.info(f"Re-enumerated images for listing #{lid}: {len(current_images)} → {len(enumerated)}")
        except Exception as e:
            logger.error(f"Image re-enumeration failed for listing #{lid}: {e}")
            errors.append(f"#{lid} image enum: {e}")

    # Phase 3: backfill structured data for listings missing price/beds/baths/sqft
    data_backfilled = 0
    for lid in listing_ids:
        listing = db.get_listing_by_id(lid)
        if not listing:
            continue
        # Check if all structured fields are missing
        has_data = any([
            listing.get("price"),
            listing.get("bedrooms"),
            listing.get("bathrooms"),
            listing.get("sqft"),
        ])
        if has_data:
            continue
        if not listing.get("address") or not listing.get("town"):
            continue

        try:
            structured = scrape_listing_structured_data(
                address=listing["address"],
                town=listing["town"],
                state=listing.get("state"),
                zip_code=listing.get("zip_code"),
            )
            if structured:
                db.update_listing_fields_by_id(lid, **structured)
                data_backfilled += 1
                logger.info(f"Backfilled structured data for listing #{lid}: {structured}")
        except Exception as e:
            logger.error(f"Structured data backfill failed for listing #{lid}: {e}")
            errors.append(f"#{lid} structured data: {e}")

    # Phase 4: backfill year_built/list_date from descriptions, listing page, or OneKeyMLS
    import httpx
    from app.parsers.onehome import _YEAR_BUILT_RE, _LIST_DATE_RE, _LOT_ACRES_TEXT_RE, _LOT_SQFT_TEXT_RE
    from app.parsers.onehome import _extract_property_stats as extract_stats
    from app.parsers.onehome import _search_onekeymls_url

    year_built_backfilled = 0
    for lid in listing_ids:
        listing = db.get_listing_by_id(lid)
        if not listing:
            continue
        # Skip if we already have all backfillable fields
        needs_year_built = not listing.get("year_built")
        needs_lot_acres = listing.get("lot_acres") is None
        if not needs_year_built and not needs_lot_acres:
            continue

        updates = {}

        # 1. Try extracting from stored description first (no network needed)
        desc = listing.get("description") or ""
        if needs_year_built:
            yb_match = _YEAR_BUILT_RE.search(desc)
            if yb_match:
                year = int(yb_match.group(1))
                if 1700 <= year <= 2030:
                    updates["year_built"] = year
            ld_match = _LIST_DATE_RE.search(desc)
            if ld_match:
                updates["list_date"] = ld_match.group(1).strip()
        if needs_lot_acres and desc:
            acres_m = _LOT_ACRES_TEXT_RE.search(desc)
            if acres_m:
                try:
                    val = float(acres_m.group(1).replace(",", ""))
                    if 0.01 <= val <= 1000:
                        updates["lot_acres"] = round(val, 4)
                except ValueError:
                    pass
            if "lot_acres" not in updates:
                sqft_m = _LOT_SQFT_TEXT_RE.search(desc)
                if sqft_m:
                    try:
                        acres = float(sqft_m.group(1).replace(",", "")) / 43560
                        if 0.01 <= acres <= 1000:
                            updates["lot_acres"] = round(acres, 4)
                    except ValueError:
                        pass

        if updates and not needs_lot_acres:
            db.update_listing_fields_by_id(lid, **updates)
            year_built_backfilled += 1
            logger.info(f"Backfilled year_built/list_date for listing #{lid} from description: {updates}")
            continue

        # 2. Try OneKeyMLS (server-rendered, works from cloud IPs)
        address = listing.get("address")
        town = listing.get("town")
        if address and town:
            try:
                mls_url = _search_onekeymls_url(address, town, listing.get("state"), listing.get("zip_code"))
                if mls_url:
                    with httpx.Client(timeout=10, follow_redirects=True, headers={
                        "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
                    }) as client:
                        resp = client.get(mls_url)
                        if resp.status_code == 200:
                            stats = extract_stats(resp.text)
                            if stats:
                                if needs_year_built and stats.get("year_built"):
                                    updates["year_built"] = stats["year_built"]
                                if stats.get("list_date"):
                                    updates["list_date"] = stats["list_date"]
                                if needs_lot_acres and stats.get("lot_acres") is not None:
                                    updates["lot_acres"] = stats["lot_acres"]
            except Exception as e:
                logger.error(f"Year built / lot acres backfill failed for listing #{lid}: {e}")
                errors.append(f"#{lid} year_built/lot_acres: {e}")

        if updates:
            db.update_listing_fields_by_id(lid, **updates)
            year_built_backfilled += 1
            logger.info(f"Backfilled data for listing #{lid}: {updates}")

    # Trigger rescore if we scraped descriptions or backfilled data
    rescore_started = False
    if scraped > 0 or data_backfilled > 0:
        criteria = db.get_active_criteria()
        if criteria:
            _start_rescore(criteria["version"], criteria["instructions"])
            rescore_started = True

    return {
        "listings_checked": len(listing_ids),
        "urls_found": urls_found,
        "descriptions_scraped": scraped,
        "images_found": images_found,
        "images_expanded": images_expanded,
        "data_backfilled": data_backfilled,
        "year_built_backfilled": year_built_backfilled,
        "skipped": skipped,
        "errors": errors,
        "rescore_started": rescore_started,
    }


_enrich_state: dict = {"in_progress": False, "result": None}


def _enrich_all(clear_bogus: bool = False, clear_bogus_commute: bool = False):
    """Background thread: backfill enrichment data for all listings.

    Phase 1 (serial): address keys + school data (SchoolDigger rate-limited).
    Phase 2 (parallel): commute times via Google Routes API (no rate limit).
    clear_bogus_commute: clear commute data for listings with stale transit-only routing
    (commute_mode = 'transit', not 'drive+transit') so they get re-enriched with the
    correct drive+transit hybrid approach.
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from app.enrichment import fetch_commute_time, fetch_school_data, normalize_address, fetch_property_tax, fetch_property_tax_orpts, fetch_power_line_proximity, fetch_flood_zone, fetch_station_proximity, parse_garage_count, parse_hoa_amount, parse_pool_flag, parse_basement, parse_year_built, parse_list_date, _geocode_address

    try:
        listing_ids = db.get_all_listing_ids()
        enriched = 0
        school_calls = 0
        commute_calls = 0
        errors = []

        # Phase 0: Clear ALL bogus school data before enrichment
        if clear_bogus:
            cleared = 0
            for lid in listing_ids:
                listing = db.get_listing_by_id(lid)
                if not listing or not listing.get("school_data_json"):
                    continue
                try:
                    sd = json.loads(listing["school_data_json"])
                    all_schools = sd.get("elementary", []) + sd.get("middle", []) + sd.get("high", [])
                    if any(s.get("name", "").startswith("School #") for s in all_schools):
                        db.update_listing_enrichment(lid, {"school_data_json": None})
                        cleared += 1
                except (json.JSONDecodeError, TypeError):
                    pass
            logger.info(f"Cleared bogus school data from {cleared} listings")

        # Phase 0.5: Clear stale transit-only commute data so listings get re-enriched
        # with the correct drive+transit hybrid. commute_mode = "transit" means the old
        # pure-transit routing was used (often gives 200-240 min for Westchester towns).
        if clear_bogus_commute:
            bogus_cleared = 0
            for lid in listing_ids:
                listing = db.get_listing_by_id(lid)
                if not listing:
                    continue
                cd = listing.get("commute_data_json")
                if not cd:
                    continue
                try:
                    cdata = json.loads(cd)
                    mode = cdata.get("commute_mode", "")
                    if mode == "transit":  # stale pure-transit routing
                        with db.get_connection() as conn:
                            cur = conn.cursor()
                            ph = db._placeholder()
                            cur.execute(
                                f"UPDATE listings SET commute_minutes = NULL, commute_data_json = NULL WHERE id = {ph}",
                                (lid,),
                            )
                        bogus_cleared += 1
                        logger.info(f"Cleared stale transit-only commute for listing #{lid}")
                except (json.JSONDecodeError, TypeError):
                    pass
            logger.info(f"Cleared stale commute data from {bogus_cleared} listings")

        # Phase 1 (serial): address keys + school data
        commute_needed: list[tuple[int, dict]] = []  # (lid, listing) pairs needing commute

        for lid in listing_ids:
            listing = db.get_listing_by_id(lid)
            if not listing:
                continue

            enrichment: dict = {}
            changed = False

            # Address key (for dedup)
            if not listing.get("address_key"):
                address_key = normalize_address(
                    listing.get("address"), listing.get("town"), listing.get("state")
                )
                if address_key:
                    enrichment["address_key"] = address_key
                    changed = True

            # School data — check DB cache first
            if not listing.get("school_data_json"):
                zip_code = listing.get("zip_code")
                cached_json = db.get_school_data_by_zip(zip_code) if zip_code else None
                if cached_json:
                    enrichment["school_data_json"] = cached_json
                    changed = True
                else:
                    school_data = fetch_school_data(zip_code, listing.get("state"))
                    if school_data:
                        enrichment["school_data_json"] = json.dumps(school_data)
                        school_calls += 1
                        changed = True

            # Save address key + school data immediately
            if changed:
                try:
                    db.update_listing_enrichment(lid, enrichment)
                    enriched += 1
                except Exception as e:
                    logger.error(f"Failed to enrich listing #{lid}: {e}")
                    errors.append(f"#{lid}: {e}")

            # Property tax: try NYC SODA first, then NY State ORPTS for non-NYC
            if not listing.get("property_tax_json"):
                tax_data = fetch_property_tax(
                    listing.get("address"),
                    borough=listing.get("town"),
                )
                if not tax_data:
                    # Fall back to NY State ORPTS API (covers Westchester + all non-NYC)
                    tax_data = fetch_property_tax_orpts(
                        listing.get("address"),
                        town=listing.get("town"),
                    )
                if tax_data:
                    enrichment["property_tax_json"] = json.dumps(tax_data)
                    changed = True

            # Power line proximity check (OSM Overpass — free, no key needed)
            if not listing.get("power_line_json"):
                power_data = fetch_power_line_proximity(
                    listing.get("address"),
                    town=listing.get("town"),
                    state=listing.get("state"),
                )
                if power_data:
                    enrichment["power_line_json"] = json.dumps(power_data)
                    changed = True
                elif power_data is None and listing.get("address") and listing.get("town"):
                    # Mark as checked (no power lines found) to avoid re-querying
                    enrichment["power_line_json"] = json.dumps({"nearest_distance_m": None, "source": "osm_overpass"})
                    changed = True

            # FEMA flood zone check (free, no key needed)
            if not listing.get("flood_zone_json"):
                flood_data = fetch_flood_zone(
                    listing.get("address"),
                    town=listing.get("town"),
                    state=listing.get("state"),
                )
                if flood_data:
                    enrichment["flood_zone_json"] = json.dumps(flood_data)
                    changed = True
                elif listing.get("address") and listing.get("town"):
                    # Mark as checked to avoid re-querying
                    enrichment["flood_zone_json"] = json.dumps({"fld_zone": None, "source": "fema_nfhl"})
                    changed = True

            # Metro-North station proximity (static dataset, no API call)
            if not listing.get("station_json"):
                station_data = fetch_station_proximity(
                    listing.get("address"),
                    town=listing.get("town"),
                    state=listing.get("state"),
                )
                if station_data:
                    enrichment["station_json"] = json.dumps(station_data)
                    changed = True
                elif listing.get("address") and listing.get("town"):
                    enrichment["station_json"] = json.dumps({"station": None, "source": "osm_static"})
                    changed = True

            # Persist lat/lng via geocoder (uses in-memory cache when already geocoded above)
            if listing.get("lat") is None:
                coords = _geocode_address(listing.get("address"), listing.get("town"), listing.get("state"))
                if coords:
                    enrichment["lat"] = coords["lat"]
                    enrichment["lng"] = coords["lon"]
                    changed = True

            # Description parsing: garage, HOA, pool, basement (pure regex, no API)
            desc = listing.get("description")
            if desc:
                if listing.get("garage_count") is None:
                    g = parse_garage_count(desc)
                    if g.get("garage_count") is not None:
                        enrichment["garage_count"] = g["garage_count"]
                        enrichment["garage_type"] = g.get("garage_type")
                        changed = True

                # Always re-parse HOA to correct stale/wrong values
                h = parse_hoa_amount(desc)
                if h.get("hoa_monthly") is not None:
                    if listing.get("hoa_monthly") != h["hoa_monthly"]:
                        enrichment["hoa_monthly"] = h["hoa_monthly"]
                        changed = True

                if listing.get("has_pool") is None:
                    p = parse_pool_flag(desc)
                    if p.get("has_pool") is not None:
                        enrichment["has_pool"] = p["has_pool"]
                        enrichment["pool_type"] = p.get("pool_type")
                        changed = True

                if listing.get("has_basement") is None:
                    b = parse_basement(desc)
                    if b.get("has_basement") is not None:
                        enrichment["has_basement"] = b["has_basement"]
                        enrichment["basement_type"] = b.get("basement_type")
                        changed = True

                if listing.get("year_built") is None:
                    yb = parse_year_built(desc)
                    if yb is not None:
                        enrichment["year_built"] = yb
                        changed = True

                if listing.get("list_date") is None:
                    ld = parse_list_date(desc)
                    if ld is not None:
                        enrichment["list_date"] = ld
                        changed = True

            # Save any enrichment changes from this phase
            if changed:
                try:
                    db.update_listing_enrichment(lid, enrichment)
                    enriched += 1
                except Exception as e:
                    logger.error(f"Failed to enrich listing #{lid}: {e}")
                    errors.append(f"#{lid}: {e}")

            # Collect listings needing commute data for parallel fetch
            if listing.get("commute_minutes") is None:
                commute_needed.append((lid, listing))

        # Phase 2 (parallel): commute times — no rate limit on Google Routes API
        if commute_needed:
            logger.info(f"Fetching commute times for {len(commute_needed)} listings in parallel")

            def _fetch_commute(item: tuple[int, dict]) -> tuple[int, dict | None]:
                lid, listing = item
                result = fetch_commute_time(
                    listing.get("address"),
                    listing.get("town"),
                    listing.get("state"),
                    listing.get("zip_code"),
                )
                return lid, result

            with ThreadPoolExecutor(max_workers=5) as pool:
                futures = {pool.submit(_fetch_commute, item): item for item in commute_needed}
                for future in as_completed(futures):
                    try:
                        lid, commute_result = future.result()
                        if commute_result:
                            enrichment = {
                                "commute_minutes": commute_result["commute_minutes"],
                                "commute_data_json": json.dumps(commute_result),
                            }
                            db.update_listing_enrichment(lid, enrichment)
                            commute_calls += 1
                            enriched += 1
                    except Exception as e:
                        lid = futures[future][0]
                        logger.error(f"Commute fetch failed for listing #{lid}: {e}")
                        errors.append(f"#{lid}: {e}")

        # Trigger rescore if we enriched anything
        rescore_started = False
        if enriched > 0:
            criteria = db.get_active_criteria()
            if criteria:
                _start_rescore(criteria["version"], criteria["instructions"])
                rescore_started = True

        _enrich_state["result"] = {
            "listings_checked": len(listing_ids),
            "enriched": enriched,
            "school_api_calls": school_calls,
            "commute_api_calls": commute_calls,
            "errors": errors,
            "rescore_started": rescore_started,
        }
        logger.info(
            f"Enrichment complete: {enriched} enriched, "
            f"{school_calls} school API calls, {commute_calls} commute API calls"
        )
    except Exception as e:
        logger.error(f"Enrichment failed: {e}")
        _enrich_state["result"] = {"error": str(e)}
    finally:
        _enrich_state["in_progress"] = False


@app.post("/manage/enrich")
def manage_enrich(request: Request):
    """Backfill enrichment data (school scores + commute times) for existing listings.

    Runs in a background thread to accommodate SchoolDigger's 1-call/minute rate limit.
    Query params: clear_bogus=true to clear and re-fetch obfuscated school data.

    Protected by MANAGE_KEY env var.
    """
    key = request.headers.get("x-manage-key", "")
    if not settings.manage_key or key != settings.manage_key:
        raise HTTPException(status_code=403, detail="Invalid or missing management key")

    if _enrich_state["in_progress"]:
        return {"status": "already_running", "message": "Enrichment is already in progress"}

    clear_bogus = request.query_params.get("clear_bogus", "").lower() == "true"

    clear_bogus_commute = request.query_params.get("clear_bogus_commute", "").lower() == "true"

    _enrich_state["in_progress"] = True
    _enrich_state["result"] = None
    t = threading.Thread(target=_enrich_all, args=(clear_bogus, clear_bogus_commute), daemon=True)
    t.start()

    return {"status": "started", "clear_bogus": clear_bogus, "clear_bogus_commute": clear_bogus_commute}


@app.get("/manage/enrich/status")
def manage_enrich_status():
    """Check enrichment status."""
    return {
        "in_progress": _enrich_state["in_progress"],
        "result": _enrich_state["result"],
    }


# --- Data Quality ---


_SOLD_INDICATORS = [
    "sold on", "off market", "this home is no longer",
    "no longer for sale", "status: sold", "sale-status-sold",
    '"listingstatus":"sold"',
]

_PENDING_INDICATORS = [
    '"listingstatus":"pending"',
    "pending sale", "sale pending", "under contract",
]


def _prune_sold_listings(fix: bool = False) -> dict:
    """Check listings for sold/off-market/pending status.

    Two passes:
    1. Redfin pass — check Redfin URLs via Jina Reader (existing)
    2. OneKey MLS pass — for remaining listings, search DDG for OneKeyMLS page
       and extract SaleStatus/MlsStatus from the page JSON

    Sold/Closed → delete listing.  Pending/Under Contract → update status only.

    Returns report dict with checked, sold_count, pending_count, etc.
    If fix=True, deletes sold listings and updates pending status in DB.
    """
    import httpx

    from app.parsers.onehome import check_listing_status

    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, address, town, state, zip_code, listing_url "
            "FROM listings WHERE listing_url IS NOT NULL OR address IS NOT NULL"
        )
        if settings.is_postgres:
            columns = [desc[0] for desc in cur.description]
            rows = [dict(zip(columns, r)) for r in cur.fetchall()]
        else:
            rows = [dict(r) for r in cur.fetchall()]

    sold = []
    pending = []
    checked = 0
    errors = 0
    detected_ids: set[int] = set()  # IDs already classified by Redfin pass

    # --- Pass 1: Redfin URLs via Jina Reader ---
    redfin_listings = [r for r in rows if r.get("listing_url") and "redfin.com" in r["listing_url"]]

    for listing in redfin_listings:
        url = listing["listing_url"]
        try:
            with httpx.Client(timeout=15.0, follow_redirects=True) as client:
                resp = client.get(
                    f"https://r.jina.ai/{url}",
                    headers={"Accept": "text/plain", "X-Return-Format": "text"},
                )
                text = resp.text.lower()[:5000]

            entry = {
                "id": listing["id"],
                "address": listing.get("address"),
                "town": listing.get("town"),
                "url": url,
            }

            if any(indicator in text for indicator in _SOLD_INDICATORS):
                sold.append(entry)
                detected_ids.add(listing["id"])
            elif any(indicator in text for indicator in _PENDING_INDICATORS):
                pending.append(entry)
                detected_ids.add(listing["id"])

            checked += 1
        except Exception as e:
            logger.warning(f"Failed to check listing #{listing['id']} ({url}): {e}")
            errors += 1

    # --- Pass 2: OneKey MLS status check (for listings not already detected) ---
    remaining = [
        r for r in rows
        if r["id"] not in detected_ids
        and r.get("address")
        and r.get("town")
    ]

    for listing in remaining:
        try:
            status = check_listing_status(
                listing["address"],
                listing["town"],
                listing.get("state"),
                listing.get("zip_code"),
            )
            if not status:
                checked += 1
                continue

            entry = {
                "id": listing["id"],
                "address": listing.get("address"),
                "town": listing.get("town"),
                "url": listing.get("listing_url", ""),
                "mls_status": status,
            }
            status_lower = status.lower()

            if status_lower in ("sold", "closed"):
                sold.append(entry)
            elif status_lower in ("pending", "under contract"):
                pending.append(entry)

            checked += 1
        except Exception as e:
            logger.warning(f"OneKeyMLS status check failed for listing #{listing['id']}: {e}")
            errors += 1

    report: dict = {
        "checked": checked,
        "sold_count": len(sold),
        "sold": sold,
        "pending_count": len(pending),
        "pending": pending,
        "errors": errors,
        "fix": fix,
    }

    if fix:
        ph = db._placeholder()

        # Delete sold listings
        if sold:
            sold_ids = [s["id"] for s in sold]
            with db.get_connection() as conn:
                cur = conn.cursor()
                for lid in sold_ids:
                    cur.execute(f"DELETE FROM scores WHERE listing_id = {ph}", (lid,))
                    cur.execute(f"DELETE FROM listings WHERE id = {ph}", (lid,))
            report["deleted"] = len(sold_ids)
            logger.info(f"Pruned {len(sold_ids)} sold/off-market listings")

        # Update pending listings status (don't delete)
        if pending:
            for p in pending:
                db.update_listing_status(p["id"], "Pending")
            report["pending_updated"] = len(pending)
            logger.info(f"Updated {len(pending)} listing(s) to Pending status")

    return report


@app.post("/manage/prune-sold")
def manage_prune_sold(request: Request):
    """Check listing URLs for sold/off-market status and remove them.

    Default (dry-run): returns listings detected as sold/off-market.
    With ?fix=true: deletes those listings.

    Uses Jina Reader to check Redfin pages for status indicators.
    Protected by MANAGE_KEY env var.
    """
    key = request.headers.get("x-manage-key", "")
    if not settings.manage_key or key != settings.manage_key:
        raise HTTPException(status_code=403, detail="Invalid or missing management key")

    fix = request.query_params.get("fix", "").lower() == "true"
    return _prune_sold_listings(fix=fix)


@app.post("/manage/data-quality")
def manage_data_quality(request: Request):
    """Audit and optionally fix listings with missing address or URL.

    Default (dry-run): returns counts and listing IDs of bad data.
    With ?fix=true: deletes bad listings, resets orphaned emails
    (removes Gmail label + processed_emails records), then triggers
    a re-poll and rescore.

    Protected by MANAGE_KEY env var.
    """
    key = request.headers.get("x-manage-key", "")
    if not settings.manage_key or key != settings.manage_key:
        raise HTTPException(status_code=403, detail="Invalid or missing management key")

    fix = request.query_params.get("fix", "").lower() == "true"

    # Find bad listings
    from app.parsers.plaintext import REDFIN_URL_ADDR_RE

    no_address = []
    no_url = []
    no_town = []
    with db.get_connection() as conn:
        cur = conn.cursor()
        ph = db._placeholder()

        cur.execute("SELECT id, mls_id, address, town, listing_url FROM listings")
        rows = cur.fetchall()
        if settings.is_postgres:
            columns = [desc[0] for desc in cur.description]
            rows = [dict(zip(columns, r)) for r in rows]
        else:
            rows = [dict(r) for r in rows]

    for row in rows:
        addr = (row.get("address") or "").strip()
        url = (row.get("listing_url") or "").strip()
        town = (row.get("town") or "").strip()
        if not addr:
            no_address.append({"id": row["id"], "mls_id": row.get("mls_id")})
        if not url:
            no_url.append({"id": row["id"], "mls_id": row.get("mls_id")})
        if not town:
            no_town.append({"id": row["id"], "address": row.get("address"), "listing_url": url})

    report = {
        "no_address_count": len(no_address),
        "no_url_count": len(no_url),
        "no_town_count": len(no_town),
        "no_address": no_address,
        "no_url": no_url,
        "no_town": no_town,
        "fix": fix,
    }

    if not fix:
        return report

    # --- Fix mode ---
    # Only delete listings with no address (truly garbage data).
    # No-URL listings are real listings that just can't be clicked — don't delete.
    bad_ids = {item["id"] for item in no_address}

    # Delete bad listings and their scores
    deleted = 0
    with db.get_connection() as conn:
        cur = conn.cursor()
        ph = db._placeholder()
        for lid in bad_ids:
            cur.execute(f"DELETE FROM scores WHERE listing_id = {ph}", (lid,))
            cur.execute(f"DELETE FROM listings WHERE id = {ph}", (lid,))
            deleted += 1

    # Reset orphaned emails (processed_emails with no remaining listings)
    from app.gmail import _build_service, _get_or_create_label

    orphans = []
    with db.get_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT pe.id, pe.gmail_id
            FROM processed_emails pe
            LEFT JOIN listings l ON l.source_email_id = pe.id
            WHERE l.id IS NULL
        """)
        if settings.is_postgres:
            orphans = [(row[0], row[1]) for row in cur.fetchall()]
        else:
            orphans = [(row["id"], row["gmail_id"]) for row in cur.fetchall()]

    gmail_reset = 0
    if orphans:
        try:
            service = _build_service()
            label_id = _get_or_create_label(service)
            for _, gmail_id in orphans:
                try:
                    service.users().messages().modify(
                        userId="me",
                        id=gmail_id,
                        body={"removeLabelIds": [label_id]},
                    ).execute()
                    gmail_reset += 1
                except Exception as e:
                    logger.warning(f"Failed to remove label from {gmail_id}: {e}")
        except Exception as e:
            logger.error(f"Failed to connect to Gmail: {e}")

        # Delete orphaned processed_emails records
        ph = db._placeholder()
        with db.get_connection() as conn:
            cur = conn.cursor()
            for pe_id, _ in orphans:
                cur.execute(f"DELETE FROM processed_emails WHERE id = {ph}", (pe_id,))

    # Backfill missing towns from Redfin URLs
    towns_fixed = 0
    for item in no_town:
        url = item.get("listing_url", "")
        if url:
            m = REDFIN_URL_ADDR_RE.search(url)
            if m:
                town = m.group(2).replace("-", " ").title()
                state = m.group(1).upper()
                zip_code = m.group(4) if m.group(4) else None
                db.update_listing_fields_by_id(item["id"], town=town, state=state, zip_code=zip_code)
                towns_fixed += 1
                logger.info(f"Backfilled town for listing #{item['id']}: {town}")
    report["towns_fixed"] = towns_fixed

    # Re-poll to re-ingest cleaned emails
    poll_result = None
    try:
        poll_result = poll_once()
        logger.info(f"Data-quality re-poll: {len(poll_result)} listing(s)")
    except Exception as e:
        logger.error(f"Data-quality re-poll failed: {e}")

    # Trigger rescore
    rescore_started = False
    criteria = db.get_active_criteria()
    if criteria:
        _start_rescore(criteria["version"], criteria["instructions"])
        rescore_started = True

    report["deleted"] = deleted
    report["emails_reset"] = len(orphans)
    report["gmail_labels_removed"] = gmail_reset
    report["re_polled"] = len(poll_result) if poll_result else 0
    report["rescore_started"] = rescore_started
    return report


@app.get("/manage/image-audit")
def manage_image_audit(request: Request):
    """Audit image coverage across all listings.

    Returns counts of:
    - Total listings
    - Listings with images
    - Listings without images
    - Listings with unknowns in scoring (high priority for re-scrape)

    Protected by MANAGE_KEY env var.
    """
    key = request.headers.get("x-manage-key", "")
    if not settings.manage_key or key != settings.manage_key:
        raise HTTPException(status_code=403, detail="Invalid or missing management key")

    with db.get_connection() as conn:
        cur = conn.cursor()

        # Total listings
        cur.execute("SELECT COUNT(*) FROM listings")
        total = cur.fetchone()[0]

        # Listings with images
        if settings.is_postgres:
            cur.execute("""
                SELECT COUNT(*) FROM listings
                WHERE image_urls_json IS NOT NULL
                AND image_urls_json != '[]'
                AND image_urls_json != 'null'
            """)
        else:
            cur.execute("""
                SELECT COUNT(*) FROM listings
                WHERE image_urls_json IS NOT NULL
                AND image_urls_json != '[]'
                AND image_urls_json != 'null'
            """)
        with_images = cur.fetchone()[0]

        # Listings with unknowns in hard_results
        cur.execute("""
            SELECT COUNT(DISTINCT s.listing_id)
            FROM scores s
            WHERE s.hard_results_json LIKE '%"passed": null%'
        """)
        with_unknowns = cur.fetchone()[0]

        # Listings with unknowns AND no images (highest priority)
        if settings.is_postgres:
            cur.execute("""
                SELECT COUNT(DISTINCT s.listing_id)
                FROM scores s
                JOIN listings l ON l.id = s.listing_id
                WHERE s.hard_results_json LIKE '%"passed": null%'
                AND (l.image_urls_json IS NULL
                     OR l.image_urls_json = '[]'
                     OR l.image_urls_json = 'null')
            """)
        else:
            cur.execute("""
                SELECT COUNT(DISTINCT s.listing_id)
                FROM scores s
                JOIN listings l ON l.id = s.listing_id
                WHERE s.hard_results_json LIKE '%"passed": null%'
                AND (l.image_urls_json IS NULL
                     OR l.image_urls_json = '[]'
                     OR l.image_urls_json = 'null')
            """)
        unknowns_no_images = cur.fetchone()[0]

        # Sample of listings needing images (listing IDs)
        if settings.is_postgres:
            cur.execute("""
                SELECT l.id, l.address, l.town, s.verdict, s.score
                FROM listings l
                LEFT JOIN scores s ON s.listing_id = l.id
                WHERE (l.image_urls_json IS NULL
                       OR l.image_urls_json = '[]'
                       OR l.image_urls_json = 'null')
                AND s.hard_results_json LIKE '%"passed": null%'
                ORDER BY s.score DESC
                LIMIT 20
            """)
            cols = [desc[0] for desc in cur.description]
            sample = [dict(zip(cols, row)) for row in cur.fetchall()]
        else:
            cur.execute("""
                SELECT l.id, l.address, l.town, s.verdict, s.score
                FROM listings l
                LEFT JOIN scores s ON s.listing_id = l.id
                WHERE (l.image_urls_json IS NULL
                       OR l.image_urls_json = '[]'
                       OR l.image_urls_json = 'null')
                AND s.hard_results_json LIKE '%"passed": null%'
                ORDER BY s.score DESC
                LIMIT 20
            """)
            sample = [dict(row) for row in cur.fetchall()]

    return {
        "total_listings": total,
        "with_images": with_images,
        "without_images": total - with_images,
        "with_unknowns": with_unknowns,
        "unknowns_no_images": unknowns_no_images,
        "sample_needs_images": sample,
        "recommendation": (
            f"{unknowns_no_images} listings have unknowns AND no images. "
            f"Run POST /manage/rescrape-unknowns to fetch images and re-score."
        ),
    }


@app.post("/manage/rescrape-unknowns")
def manage_rescrape_unknowns(request: Request):
    """Force re-scrape images for listings with unknowns in scoring.

    Targets listings that:
    1. Have "Unknown" (passed: null) in their hard_results
    2. Either have no images OR fewer than 3 images

    For each listing:
    - Re-scrape the listing URL (if available) for images
    - If no URL, try DuckDuckGo search for listing page
    - Re-score with new images

    Protected by MANAGE_KEY env var.
    """
    key = request.headers.get("x-manage-key", "")
    if not settings.manage_key or key != settings.manage_key:
        raise HTTPException(status_code=403, detail="Invalid or missing management key")

    from app.parsers.onehome import scrape_listing_description

    # Find listings with unknowns and insufficient images
    targets = []
    with db.get_connection() as conn:
        cur = conn.cursor()
        if settings.is_postgres:
            cur.execute("""
                SELECT l.id, l.address, l.town, l.state, l.zip_code,
                       l.mls_id, l.listing_url, l.image_urls_json
                FROM listings l
                JOIN scores s ON s.listing_id = l.id
                WHERE s.hard_results_json LIKE '%"passed": null%'
            """)
            cols = [desc[0] for desc in cur.description]
            rows = [dict(zip(cols, row)) for row in cur.fetchall()]
        else:
            cur.execute("""
                SELECT l.id, l.address, l.town, l.state, l.zip_code,
                       l.mls_id, l.listing_url, l.image_urls_json
                FROM listings l
                JOIN scores s ON s.listing_id = l.id
                WHERE s.hard_results_json LIKE '%"passed": null%'
            """)
            rows = [dict(row) for row in cur.fetchall()]

        for row in rows:
            images_json = row.get("image_urls_json")
            try:
                existing_images = json.loads(images_json) if images_json else []
            except (json.JSONDecodeError, TypeError):
                existing_images = []

            # Target if no images or fewer than 3 images (likely missing floor plans)
            if len(existing_images) < 3:
                targets.append(row)

    logger.info(f"Re-scraping {len(targets)} listings with unknowns and insufficient images")

    scraped = 0
    rescored = 0
    errors = []

    criteria = db.get_active_criteria()
    if not criteria:
        return {
            "status": "error",
            "message": "No active criteria found - cannot re-score",
        }

    for listing in targets:
        listing_id = listing["id"]
        url = listing.get("listing_url")

        if not url:
            logger.warning(f"Listing {listing_id} has no URL, skipping")
            continue

        try:
            # Re-scrape for images (description already exists, just get new images)
            _, new_images = scrape_listing_description(
                url,
                address=listing.get("address"),
                town=listing.get("town"),
                state=listing.get("state"),
                zip_code=listing.get("zip_code"),
                mls_id=listing.get("mls_id"),
            )

            if new_images:
                # Update images in DB
                db.add_listing_images(listing_id, new_images)
                scraped += 1
                logger.info(f"Scraped {len(new_images)} images for listing {listing_id}")

                # Re-score with new images
                listing_data = db.get_listing_by_id(listing_id)
                if listing_data:
                    result, reasoning = ai_score_listing(
                        listing_data,
                        criteria["instructions"],
                        image_urls=new_images,
                    )
                    db.save_score(
                        listing_id,
                        result,
                        reasoning,
                        criteria_version=criteria["version"],
                    )
                    rescored += 1
                    logger.info(
                        f"Re-scored listing {listing_id}: {result.verdict} ({result.score}/100)"
                    )
            else:
                logger.warning(f"No images found for listing {listing_id} at {url[:80]}")

        except Exception as e:
            logger.error(f"Failed to rescrape listing {listing_id}: {e}")
            errors.append({"listing_id": listing_id, "error": str(e)})

    return {
        "status": "complete",
        "targets": len(targets),
        "scraped": scraped,
        "rescored": rescored,
        "errors": len(errors),
        "error_details": errors[:10],  # First 10 errors
    }
