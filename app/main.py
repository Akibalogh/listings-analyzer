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


def _scheduled_poll_loop(interval_hours: int):
    """Background thread: poll Gmail on a fixed interval."""
    interval_secs = interval_hours * 3600
    logger.info(f"Scheduled poller started (every {interval_hours}h)")
    while True:
        time.sleep(interval_secs)
        try:
            results = poll_once()
            logger.info(f"Scheduled poll complete: {len(results)} listing(s)")
        except Exception:
            logger.exception("Scheduled poll failed")


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


# --- Re-scoring ---


@app.post("/listings/{listing_id}/rescore")
def rescore_single(request: Request, listing_id: int):
    """Re-score a single listing with current criteria."""
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
    return db.rescore_state


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

    Falls back to sequential scoring if batch submission fails.
    """
    import anthropic

    try:
        listing_ids = db.get_all_listing_ids()
        score_metadata = db.get_all_score_metadata()
        criteria = {"instructions": instructions, "version": criteria_version}

        # Build batch requests, skipping unchanged listings
        batch_requests = []
        skip_count = 0
        for lid in listing_ids:
            listing = db.get_listing_by_id(lid)
            if not listing:
                continue

            meta = score_metadata.get(lid)
            if _should_skip(listing, meta, criteria_version):
                skip_count += 1
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

        total_to_score = len(batch_requests)
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

        # Try batch API first
        if not settings.anthropic_api_key:
            logger.error("ANTHROPIC_API_KEY not set, cannot rescore")
            return

        try:
            client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
            batch = client.messages.batches.create(requests=batch_requests)
            batch_id = batch.id
            db.rescore_state["batch_id"] = batch_id
            logger.info(f"Batch submitted: {batch_id} ({total_to_score} requests)")

            # Poll until batch completes
            while True:
                time.sleep(30)
                batch = client.messages.batches.retrieve(batch_id)
                status = batch.processing_status
                logger.info(f"Batch {batch_id}: {status}")
                if status == "ended":
                    break

            # Process results
            processed = 0
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

                processed += 1
                db.rescore_state["completed"] = skip_count + processed

            logger.info(
                f"Batch rescore complete: {processed}/{total_to_score} scored, "
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


def _start_rescore(criteria_version: int, instructions: str):
    """Launch background re-score thread if not already running."""
    if db.rescore_state["in_progress"]:
        logger.warning("Re-score already in progress, skipping")
        return

    db.rescore_state["in_progress"] = True
    db.rescore_state["criteria_version"] = criteria_version
    db.rescore_state["skipped"] = 0
    db.rescore_state["batch_id"] = None
    t = threading.Thread(
        target=_rescore_all,
        args=(criteria_version, instructions),
        daemon=True,
    )
    t.start()


# --- Management Endpoints ---


@app.post("/manage/sync-criteria")
def sync_criteria(request: Request):
    """Trigger a background re-score with the current active criteria.

    Protected by MANAGE_KEY env var (not user auth).
    Useful for triggering a full rescore after deploying code changes.
    Criteria are set via the dashboard AI Criteria panel — not hardcoded.
    """
    key = request.headers.get("x-manage-key", "")
    if not settings.manage_key or key != settings.manage_key:
        raise HTTPException(status_code=403, detail="Invalid or missing management key")

    criteria = db.get_active_criteria()
    if not criteria:
        raise HTTPException(status_code=404, detail="No active criteria found — set criteria via AI Criteria in dashboard first")

    _start_rescore(criteria["version"], criteria["instructions"])
    logger.info(f"Triggered rescore with active criteria v{criteria['version']}")

    return {
        "synced": True,
        "version": criteria["version"],
        "rescore_started": True,
        "instructions_preview": criteria["instructions"][:200] + "...",
    }


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
    """Scrape descriptions + images for listings that have URLs but no description.

    Iterates existing listings in the DB directly — does NOT re-parse emails.
    Protected by MANAGE_KEY env var.
    """
    from app.parsers.onehome import scrape_listing_description

    key = request.headers.get("x-manage-key", "")
    if not settings.manage_key or key != settings.manage_key:
        raise HTTPException(status_code=403, detail="Invalid or missing management key")

    listing_ids = db.get_all_listing_ids()
    scraped = 0
    images_found = 0
    skipped = 0
    errors = []

    for lid in listing_ids:
        listing = db.get_listing_by_id(lid)
        if not listing:
            continue

        url = listing.get("listing_url")
        existing_desc = listing.get("description")

        # Skip if no URL or already has description
        if not url:
            skipped += 1
            continue
        if existing_desc:
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

    # Trigger rescore if we scraped anything
    rescore_started = False
    if scraped > 0:
        criteria = db.get_active_criteria()
        if criteria:
            _start_rescore(criteria["version"], criteria["instructions"])
            rescore_started = True

    return {
        "listings_checked": len(listing_ids),
        "descriptions_scraped": scraped,
        "images_found": images_found,
        "skipped": skipped,
        "errors": errors,
        "rescore_started": rescore_started,
    }


_enrich_state: dict = {"in_progress": False, "result": None}


def _enrich_all(clear_bogus: bool = False):
    """Background thread: backfill enrichment data for all listings.

    Phase 1 (serial): address keys + school data (SchoolDigger rate-limited).
    Phase 2 (parallel): commute times via Google Routes API (no rate limit).
    """
    from concurrent.futures import ThreadPoolExecutor, as_completed

    from app.enrichment import fetch_commute_time, fetch_school_data, normalize_address

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

    _enrich_state["in_progress"] = True
    _enrich_state["result"] = None
    t = threading.Thread(target=_enrich_all, args=(clear_bogus,), daemon=True)
    t.start()

    return {"status": "started", "clear_bogus": clear_bogus}


@app.get("/manage/enrich/status")
def manage_enrich_status():
    """Check enrichment status."""
    return {
        "in_progress": _enrich_state["in_progress"],
        "result": _enrich_state["result"],
    }
