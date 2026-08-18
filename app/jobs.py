"""Persistent job queue for per-listing background work.

Jobs live in the `jobs` table (see app/db.py) so they survive deploys and
crashes — the failure mode of the old daemon-thread approach, where a
restart mid-run silently dropped whatever work remained.

Flow:
  * /listings/add and /manage/import-csv call enqueue_listing() after saving
    a listing, then kick() to process immediately without blocking the
    request. (The poller still enriches inline during its poll; the gap scan
    below repairs anything it misses.)
  * The hourly scheduler tick calls enqueue_missing() + kick(): listings are
    scanned for data gaps and repair jobs are enqueued — terminal job rows
    are resurrected when their gap demonstrably still exists, so the system
    converges without manual backfill calls. init_db resets jobs orphaned as
    'running' by a restart.
  * Each task handler is idempotent: it no-ops (job -> done) when the data
    it would fetch is already present, so re-enqueueing is always safe.
  * A job that fails is not retried within the same drain — its remaining
    attempts (3 total) spread across later drains, giving transient failures
    (rate limits, API blips, daily quotas) time to clear.

Task types and per-listing order (score runs last, and is deferred by
claim_pending_jobs until the listing's enrichment jobs have settled):
  scrape_desc  -> listing URL (search if missing), description, images
  stats        -> price/beds/baths/sqft/year_built via page, description,
                  or OneKeyMLS
  commute      -> door-to-door commute via Google Routes
  schools      -> SchoolDigger district data (zip-cached)
  score        -> AI evaluation against active criteria
"""

import json
import logging
import random
import threading
import time

from app import db
from app.config import settings
from app.listing_status import is_unknown
from app.scorer import score_input_fingerprint

logger = logging.getLogger(__name__)

# Per-listing execution order; 'score' is also the claim-deferred final task.
TASK_ORDER = ["scrape_desc", "stats", "commute", "schools", "status", "score"]

_drain_lock = threading.Lock()

# Monotonic timestamp of the last listing-page scrape, so consecutive scrapes
# stay spaced out without penalizing a single one.
_last_scrape_at: float = 0.0


def enqueue_listing(listing_id: int, tasks: list[str] | None = None, force: bool = False) -> int:
    """Queue the standard pipeline (or a subset) for a listing."""
    return db.enqueue_jobs(listing_id, tasks or TASK_ORDER, force=force)


def kick() -> None:
    """Run a drain in a daemon thread (no-op if one is already running)."""
    threading.Thread(target=drain, daemon=True).start()


def drain(max_jobs: int = 500) -> dict:
    """Process pending jobs until the queue is empty or max_jobs is hit.

    Single-flight: concurrent calls return immediately. A job that fails is
    excluded from re-claiming for the rest of this drain; it retries on a
    later drain until its attempts (counted at claim time) are exhausted.
    """
    if not _drain_lock.acquire(blocking=False):
        return {"status": "already_running"}
    try:
        processed = 0
        failed_ids: set[int] = set()
        while processed + len(failed_ids) < max_jobs:
            batch = db.claim_pending_jobs(
                limit=20, task_order=TASK_ORDER, exclude_ids=failed_ids,
            )
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
                    failed_ids.add(job["id"])
        result = {"processed": processed, "failed": len(failed_ids)}
        if processed or failed_ids:
            logger.info(f"Job drain finished: {result}")
        _notify_high_scores()
        return result
    finally:
        _drain_lock.release()


def _notify_high_scores() -> None:
    """Push an alert for any listing that newly reached the score threshold.

    Source-agnostic and once-per-listing (claim_unnotified_high_scores marks
    them notified atomically), so it catches email-poller, manual-add, sync,
    and import listings alike without ever double-alerting.
    """
    try:
        rows = db.claim_unnotified_high_scores(settings.notify_score_threshold)
        if not rows:
            return
        from app.notifier import send_high_score_alert
        for r in rows:
            delivery = send_high_score_alert(r, r.get("score") or 0, r.get("verdict") or "")
            db.log_alert(r, r.get("score") or 0, r.get("verdict") or "", delivery)
        repeats = sum(1 for r in rows if r.get("alert_reason") == "re_armed")
        logger.info(
            f"Sent {len(rows)} high-score alert(s)"
            + (f" ({repeats} repeat)" if repeats else "")
        )
    except Exception:
        logger.exception("High-score notification sweep failed")


def enqueue_missing(force: bool = False) -> dict:
    """Scan all listings for data gaps and enqueue repair jobs.

    Runs on every scheduler tick. Because each gap was just verified to still
    exist, terminal job rows are resurrected (requeue semantics): done rows
    retry with a full budget, failed rows get one attempt per scan. force=True
    additionally restores the full budget on failed rows.

    Score jobs are not enqueued while a criteria rescore is in flight —
    otherwise every not-yet-rescored listing would be scored twice (once by
    the rescore, once by the drain) at double the API cost.
    """
    db.delete_orphan_jobs()
    criteria = db.get_active_criteria()
    score_meta = db.get_all_score_metadata()
    rescore_running = db.rescore_state.get("in_progress", False)
    counts: dict[str, int] = {t: 0 for t in TASK_ORDER}
    for lid in db.get_all_listing_ids():
        listing = db.get_listing_by_id(lid)
        if not listing:
            continue
        tasks = []
        if not listing.get("description") or not _has_images(listing):
            tasks.append("scrape_desc")
        # Core fields only — year_built/list_date are often unpublished, and
        # re-scanning for them every tick would refetch pages for nothing
        core_stats = ("price", "sqft", "bedrooms", "bathrooms")
        if (any(listing.get(f) is None for f in core_stats)
                and (listing.get("listing_url") or listing.get("description"))):
            tasks.append("stats")
        if listing.get("commute_minutes") is None and listing.get("address") and listing.get("town"):
            tasks.append("commute")
        if not listing.get("school_data_json") and listing.get("zip_code"):
            tasks.append("schools")
        # An unknown status blocks the alert, so it is a data gap like any other.
        if is_unknown(listing.get("listing_status")):
            tasks.append("status")

        # Rescore when the model's inputs have changed since it last looked, not
        # when the listing merely has a gap. `bool(tasks)` was the old trigger —
        # "enrichment is about to change the data, so rescore after" — but 85 of
        # 163 listings have a description or image set Redfin will never let us
        # scrape. The gap never closes, so the scrape failed and the score ran
        # every hour anyway: ~2,000 Haiku calls a day re-deriving the same answer,
        # with enough jitter to flap scores across the alert threshold and re-push
        # houses nothing had changed about.
        #
        # A fingerprint of the scored fields states the real condition. Enrichment
        # that lands changes the fingerprint and earns its rescore; a gap that
        # cannot be filled changes nothing and gets nothing.
        meta = score_meta.get(lid)
        needs_score = not rescore_running and (
            not meta
            or meta.get("evaluation_method") not in ("ai", "deterministic-gate")
            or (criteria and meta.get("criteria_version") != criteria["version"])
            or meta.get("input_fingerprint") != score_input_fingerprint(listing)
        )
        if needs_score:
            tasks.append("score")
        if tasks:
            queued = db.enqueue_jobs(lid, tasks, force=force, requeue=True)
            if queued:
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
        logger.warning(
            f"Listing #{listing.get('id')}: unparseable image_urls_json "
            f"({str(raw)[:80]!r}) — treating as no images"
        )
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

    # Space out CONSECUTIVE scrapes (Redfin rate-limits bursts), but don't make
    # a lone scrape — e.g. a listing you just added — wait for nothing.
    global _last_scrape_at
    gap = 2.0 + random.random()
    elapsed = time.monotonic() - _last_scrape_at
    if _last_scrape_at and elapsed < gap:
        time.sleep(gap - elapsed)
    _last_scrape_at = time.monotonic()

    description, image_urls = scrape_listing_description(
        url,
        address=listing.get("address"),
        town=listing.get("town"),
        state=listing.get("state"),
        zip_code=listing.get("zip_code"),
        mls_id=listing.get("mls_id"),
    )
    # Persist the resolved URL and the best description we have; an existing
    # description is never overwritten by a (possibly worse) rescrape
    db.update_listing_description(
        listing["id"], url, listing.get("description") or description,
    )
    if image_urls:
        db.add_listing_images(listing["id"], image_urls)
    if not description and not image_urls and not listing.get("description"):
        raise RuntimeError(f"scrape returned no description or images for {url}")


_STATS_FIELDS = ("price", "bedrooms", "bathrooms", "sqft", "year_built", "list_date", "lot_acres")


def _handle_stats(listing: dict) -> None:
    """Backfill structured fields from the listing page, description, or OneKeyMLS."""
    import httpx
    from app.parsers.onehome import _extract_property_stats, scrape_listing_structured_data

    needed = [f for f in _STATS_FIELDS if listing.get(f) is None]
    if not needed:
        return

    merged: dict = {}

    def _absorb(stats: dict | None) -> None:
        for k, v in (stats or {}).items():
            if k in _STATS_FIELDS and v is not None and k not in merged:
                merged[k] = v

    url = listing.get("listing_url")
    if url:
        try:
            with httpx.Client(timeout=10, follow_redirects=True, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
            }) as client:
                resp = client.get(url)
                if resp.status_code == 200:
                    _absorb(_extract_property_stats(resp.text))
        except Exception as e:
            logger.warning(
                f"Listing #{listing['id']}: stats page fetch failed ({e}) — trying fallbacks"
            )

    # Jina Reader fallback: renders the Redfin page from Jina's IPs (works
    # where direct fetch is bot-blocked); _parse_jina_redfin knows its format
    if url and any(f not in merged for f in needed):
        from app.main import _parse_jina_redfin
        try:
            jina_headers = {"User-Agent": "Mozilla/5.0"}
            if settings.jina_api_key:
                jina_headers["Authorization"] = f"Bearer {settings.jina_api_key}"
            with httpx.Client(timeout=60, follow_redirects=True) as client:
                resp = client.get(f"https://r.jina.ai/{url}", headers=jina_headers)
                if resp.status_code == 200:
                    _absorb(_parse_jina_redfin(resp.text))
        except Exception as e:
            logger.warning(f"Listing #{listing['id']}: Jina stats fallback failed: {e}")

    # Per-field fallbacks: description text, then OneKeyMLS (server-rendered,
    # reachable from cloud IPs where Redfin bot-blocks)
    if listing.get("description") and any(f not in merged for f in needed):
        _absorb(_extract_property_stats(listing["description"]))
    if listing.get("address") and listing.get("town") and any(f not in merged for f in needed):
        try:
            _absorb(scrape_listing_structured_data(
                listing["address"], listing["town"],
                listing.get("state"), listing.get("zip_code"),
            ))
        except Exception as e:
            logger.warning(f"Listing #{listing['id']}: OneKeyMLS stats fallback failed: {e}")

    fields = {k: v for k, v in merged.items() if listing.get(k) is None}
    if fields:
        db.update_listing_fields_by_id(listing["id"], **fields)
        return
    # Fail (and retry) only when core scoring fields are still missing —
    # year_built/list_date/lot_acres are often simply unpublished, and
    # failing over them buries real errors in noise
    core_missing = [f for f in ("price", "bedrooms", "bathrooms", "sqft") if listing.get(f) is None]
    if core_missing:
        raise RuntimeError(
            f"no structured stats extractable (missing core: {', '.join(core_missing)})"
        )
    logger.info(
        f"Listing #{listing['id']}: optional stats unpublished ({', '.join(needed)}) — done"
    )


def _handle_commute(listing: dict) -> None:
    from app.enrichment import fetch_commute_time

    if listing.get("commute_minutes") is not None:
        return
    if not settings.google_maps_api_key:
        return  # not configured — leave the gap for when the key is set
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
    if not settings.schooldigger_app_id or not settings.schooldigger_app_key:
        return  # not configured — leave the gap for when the keys are set
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

    # Notifications are handled by the drain's high-score sweep (source-agnostic,
    # once-per-listing via the `notified` flag), so scoring just persists.
    _rescore_one_listing(listing, criteria)


def _handle_status(listing: dict) -> None:
    """Resolve a listing's market status, so "unknown" is a delay not a verdict.

    The alert path requires a status that positively says the home is on the
    market, which means an unknown status is silence. That is the right default —
    516 Bellwood was pushed after it sold — but it must be temporary, or a good
    house with a quiet sender never gets mentioned. Resolution existed only as a
    manual POST /manage/backfill-status behind the management key, so nothing ran
    it.

    Two sources, cheapest first: the alert subject line, which is free and needs
    no network (the Matrix MLS alerts are filtered saved searches — "Only sold",
    "Only pending" — and the subject was sitting in processed_emails all along),
    then the OneKey MLS lookup by search.

    Raises when neither works, so the job queue's retry budget applies and a
    permanently unresolvable listing costs one lookup per scan rather than a tight
    loop. Filling the status also changes the score fingerprint, so the rescore
    that follows sees Sold and rejects it.
    """
    from app.listing_status import is_unknown

    if not is_unknown(listing.get("listing_status")):
        return

    from app.parsers import status_from_subject

    subject = db.get_source_subject(listing["id"])
    status = status_from_subject(subject)
    if status:
        # force=True because the value being replaced may be a non-empty event
        # label ("Updated MLS Listing"), which the default fill-blanks-only update
        # would leave in place. A genuinely known status never reaches here — the
        # is_unknown check above returned already.
        db.update_listing_fields_by_id(listing["id"], force=True, listing_status=status)
        logger.info(f"Listing #{listing['id']} status {status!r} from alert subject")
        return

    address, town = listing.get("address"), listing.get("town")
    if not address or not town:
        raise RuntimeError("no address — cannot look up status")

    from app.parsers.onehome import check_listing_status

    status = check_listing_status(address, town, listing.get("state"), listing.get("zip_code"))
    if not status:
        raise RuntimeError(f"status unresolved for {address}, {town}")
    db.update_listing_fields_by_id(listing["id"], force=True, listing_status=status)
    logger.info(f"Listing #{listing['id']} status {status!r} from OneKeyMLS")


_HANDLERS = {
    "scrape_desc": _handle_scrape_desc,
    "stats": _handle_stats,
    "commute": _handle_commute,
    "schools": _handle_schools,
    "status": _handle_status,
    "score": _handle_score,
}
