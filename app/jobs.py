"""Persistent job queue for per-listing background work.

Jobs live in the `jobs` table (see app/db.py) so they survive deploys and
crashes — the failure mode of the old daemon-thread approach, where a
restart mid-run silently dropped whatever work remained.

Flow:
  * Entry points (poller, /listings/add, /manage/import-csv) call
    enqueue_listing() after saving a listing, then kick() to process
    immediately without blocking the request.
  * The hourly scheduler calls drain() every tick, picking up anything a
    restart orphaned (init_db resets running -> pending on startup).
  * Each task handler is idempotent: it no-ops (job -> done) when the data
    it would fetch is already present, so re-enqueueing is always safe.

Task types and per-listing order (score runs last, and is deferred by
claim_pending_jobs until the listing's enrichment jobs have settled):
  scrape_desc  -> listing URL (search if missing), description, images
  commute      -> door-to-door commute via Google Routes
  schools      -> SchoolDigger district data (zip-cached)
  score        -> AI evaluation against active criteria
"""

import json
import logging
import threading

from app import db

logger = logging.getLogger(__name__)

# Per-listing execution order; 'score' is also the claim-deferred final task.
TASK_ORDER = ["scrape_desc", "stats", "commute", "schools", "score"]

_drain_lock = threading.Lock()


def enqueue_listing(listing_id: int, tasks: list[str] | None = None, force: bool = False) -> int:
    """Queue the standard pipeline (or a subset) for a listing."""
    return db.enqueue_jobs(listing_id, tasks or TASK_ORDER, force=force)


def kick() -> None:
    """Run a drain in a daemon thread (no-op if one is already running)."""
    threading.Thread(target=drain, daemon=True).start()


def drain(max_jobs: int = 500) -> dict:
    """Process pending jobs until the queue is empty or max_jobs is hit.

    Single-flight: concurrent calls return immediately. Failed jobs return
    to pending and retry (up to db.JOB_MAX_ATTEMPTS claims total, counted
    at claim time), then land in 'failed' with last_error recorded.
    """
    if not _drain_lock.acquire(blocking=False):
        return {"status": "already_running"}
    try:
        processed = 0
        failed = 0
        while processed + failed < max_jobs:
            batch = db.claim_pending_jobs(limit=20, task_order=TASK_ORDER)
            if not batch:
                break
            for job in batch:
                try:
                    _run_job(job)
                    db.complete_job(job["id"])
                    processed += 1
                except Exception as e:
                    logger.error(
                        f"Job #{job['id']} {job['task_type']} "
                        f"(listing {job['listing_id']}) failed: {e}"
                    )
                    db.fail_job(job["id"], str(e))
                    failed += 1
        result = {"processed": processed, "failed": failed}
        if processed or failed:
            logger.info(f"Job drain finished: {result}")
        return result
    finally:
        _drain_lock.release()


def enqueue_missing(force: bool = False) -> dict:
    """Scan all listings for data gaps and enqueue repair jobs.

    Runs on every scheduler tick, so the system converges on complete data
    without manual backfill calls. force=False (the default) leaves
    attempts-exhausted failed jobs alone; force=True re-queues them.
    """
    criteria = db.get_active_criteria()
    score_meta = db.get_all_score_metadata()
    counts: dict[str, int] = {t: 0 for t in TASK_ORDER}
    for lid in db.get_all_listing_ids():
        listing = db.get_listing_by_id(lid)
        if not listing:
            continue
        tasks = []
        if not listing.get("description") or not _has_images(listing):
            tasks.append("scrape_desc")
        core_stats = ("price", "sqft", "bedrooms", "bathrooms", "year_built")
        if (any(listing.get(f) is None for f in core_stats)
                and (listing.get("listing_url") or listing.get("description"))):
            tasks.append("stats")
        if listing.get("commute_minutes") is None and listing.get("address") and listing.get("town"):
            tasks.append("commute")
        if not listing.get("school_data_json") and listing.get("zip_code"):
            tasks.append("schools")

        meta = score_meta.get(lid)
        needs_score = (
            bool(tasks)  # enrichment will change the data → rescore after
            or not meta
            or meta.get("evaluation_method") not in ("ai", "deterministic-gate")
            or (criteria and meta.get("criteria_version") != criteria["version"])
        )
        if needs_score:
            tasks.append("score")
        if tasks:
            db.enqueue_jobs(lid, tasks, force=force)
            for t in tasks:
                counts[t] += 1
    return counts


def _run_job(job: dict) -> None:
    listing = db.get_listing_by_id(job["listing_id"])
    if not listing:
        return  # listing deleted since enqueue — nothing to do
    _HANDLERS[job["task_type"]](listing)


# --- Task handlers (idempotent: no-op when data already present) ---


def _has_images(listing: dict) -> bool:
    raw = listing.get("image_urls_json")
    if not raw:
        return False
    try:
        return bool(json.loads(raw))
    except (json.JSONDecodeError, TypeError):
        return False


def _handle_scrape_desc(listing: dict) -> None:
    from app.parsers.onehome import _search_redfin_url, scrape_listing_description

    if listing.get("description") and _has_images(listing):
        return

    url = listing.get("listing_url")
    if not url:
        url = _search_redfin_url(
            address=listing.get("address"),
            town=listing.get("town"),
            state=listing.get("state"),
            zip_code=listing.get("zip_code"),
            mls_id=listing.get("mls_id"),
        )
        if not url:
            raise RuntimeError("no listing URL and search found none")

    description, image_urls = scrape_listing_description(
        url,
        address=listing.get("address"),
        town=listing.get("town"),
        state=listing.get("state"),
        zip_code=listing.get("zip_code"),
        mls_id=listing.get("mls_id"),
    )
    if description and not listing.get("description"):
        db.update_listing_description(listing["id"], url, description)
    elif not listing.get("listing_url"):
        db.update_listing_description(listing["id"], url, listing.get("description"))
    if image_urls:
        db.add_listing_images(listing["id"], image_urls)
    if not description and not image_urls and not listing.get("description"):
        raise RuntimeError(f"scrape returned no description or images for {url}")


_STATS_FIELDS = ("price", "bedrooms", "bathrooms", "sqft", "year_built", "list_date", "lot_acres")


def _handle_stats(listing: dict) -> None:
    """Backfill structured fields from the listing page, falling back to the description."""
    import httpx
    from app.parsers.onehome import _extract_property_stats

    needed = [f for f in _STATS_FIELDS if listing.get(f) is None]
    if not needed:
        return

    stats = None
    url = listing.get("listing_url")
    if url:
        try:
            with httpx.Client(timeout=10, follow_redirects=True, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            }) as client:
                resp = client.get(url)
                if resp.status_code == 200:
                    stats = _extract_property_stats(resp.text)
        except Exception:
            pass  # bot-blocked or unreachable — description fallback below
    if not stats and listing.get("description"):
        stats = _extract_property_stats(listing["description"])
    if not stats:
        raise RuntimeError("no structured stats extractable from page or description")

    fields = {
        k: v for k, v in stats.items()
        if k in _STATS_FIELDS and v is not None and listing.get(k) is None
    }
    if fields:
        db.update_listing_fields_by_id(listing["id"], **fields)


def _handle_commute(listing: dict) -> None:
    from app.enrichment import fetch_commute_time

    if listing.get("commute_minutes") is not None:
        return
    result = fetch_commute_time(
        listing.get("address"),
        listing.get("town"),
        listing.get("state"),
        listing.get("zip_code"),
    )
    if not result:
        raise RuntimeError("commute lookup returned nothing (missing address or API failure)")
    db.update_listing_enrichment(listing["id"], {
        "commute_minutes": result.get("commute_minutes"),
        "commute_data_json": json.dumps(result),
    })


def _handle_schools(listing: dict) -> None:
    from app.enrichment import fetch_school_data

    if listing.get("school_data_json"):
        return
    zip_code = listing.get("zip_code")
    if not zip_code:
        raise RuntimeError("no zip code — cannot fetch school data")

    # Zip-level cache: SchoolDigger free tier is 20 calls/day
    cached = db.get_school_data_by_zip(zip_code)
    if cached:
        db.update_listing_enrichment(listing["id"], {"school_data_json": cached})
        return
    data = fetch_school_data(zip_code, listing.get("state"))
    if not data:
        raise RuntimeError("school data fetch failed")
    db.update_listing_enrichment(listing["id"], {"school_data_json": json.dumps(data)})


def _handle_score(listing: dict) -> None:
    # Lazy import to avoid a circular import at module load (main imports jobs)
    from app.main import _rescore_one_listing

    criteria = db.get_active_criteria()
    if not criteria:
        raise RuntimeError("no active criteria — cannot score")

    # Notify only on the first real scoring (new listing), not on rescores
    prior = db.get_all_score_metadata().get(listing["id"])
    first_real_score = not prior or prior.get("evaluation_method") not in ("ai", "deterministic-gate")

    # Re-read: enrichment handlers in this drain updated the row
    fresh = db.get_listing_by_id(listing["id"])
    if not fresh:
        return
    score = _rescore_one_listing(fresh, criteria)
    if first_real_score:
        from app.notifier import notify_new_listing
        notify_new_listing(fresh, score.score, score.verdict, score.evaluation_method)


_HANDLERS = {
    "scrape_desc": _handle_scrape_desc,
    "stats": _handle_stats,
    "commute": _handle_commute,
    "schools": _handle_schools,
    "score": _handle_score,
}
