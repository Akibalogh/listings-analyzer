"""Poller orchestrator: fetch emails → parse → score → store.

Can be run as a CLI command or triggered via the API.
"""

import json
import logging
import re
import sys

from app import db
from app.config import settings
from app.enrichment import fetch_commute_time, fetch_school_data, normalize_address
from app.gmail import fetch_new_emails, mark_processed
from app.models import ParsedListing, ScoringResult
from app.parsers import parser_chain
from app.parsers.onehome import scrape_listing_description, scrape_listing_structured_data
from app.scorer import ai_score_listing

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


def poll_once() -> list[dict]:
    """Run one poll cycle: fetch, parse, score, store.

    Returns a list of result dicts for each processed listing.
    """
    db.init_db()
    results = []

    try:
        emails = fetch_new_emails()
    except Exception as e:
        logger.error(f"Failed to fetch emails: {e}")
        raise

    for email_data in emails:
        gmail_id = email_data["gmail_id"]
        subject = email_data["subject"]
        sender = email_data["sender"]

        logger.info(f"Processing email: {subject} from {sender}")

        if db.is_email_processed(gmail_id):
            logger.info(f"Email {gmail_id} already processed, skipping")
            continue

        # Parse listings from email
        listings = parser_chain.parse(
            html=email_data["html"],
            text=email_data["text"],
            subject=subject,
        )

        if not listings:
            logger.warning(f"No listings extracted from email: {subject}")

        # Determine which parser was used
        parser_used = listings[0].source_format if listings else "none"

        # Save email record
        email_id = db.save_processed_email(
            gmail_id=gmail_id,
            message_id=email_data.get("message_id", ""),
            sender=sender,
            subject=subject,
            parser_used=parser_used,
            listings_found=len(listings),
        )

        # Score and save each listing
        for listing in listings:
            # Validation: reject listings with no identifying info
            if not listing.mls_id and not listing.address:
                logger.warning(
                    f"Skipping listing with no address and no MLS ID "
                    f"(price={listing.price}, source={listing.source_format})"
                )
                continue

            # Dedup: check MLS ID first
            if db.is_listing_duplicate(listing.mls_id):
                existing = db.get_listing_id_and_status_by_mls(listing.mls_id)
                if existing:
                    _update_duplicate(existing, listing)
                logger.info(f"Duplicate listing MLS #{listing.mls_id}, skipping")
                continue

            # Dedup: check normalized address
            address_key = normalize_address(listing.address, listing.town, listing.state)
            if address_key and db.is_listing_duplicate_by_address(address_key):
                existing = db.get_listing_id_and_status_by_address_key(address_key)
                if existing:
                    _update_duplicate(existing, listing)
                logger.info(f"Duplicate listing by address: {listing.address}, {listing.town}")
                continue

            # Scrape full listing page for description + images
            scraped_images = []
            if listing.listing_url and not listing.description:
                logger.info(f"Scraping listing page: {listing.listing_url}")
                listing.description, scraped_images = scrape_listing_description(
                    listing.listing_url,
                    address=listing.address,
                    town=listing.town,
                    state=listing.state,
                    zip_code=listing.zip_code,
                    mls_id=listing.mls_id,
                )

            # --- Backfill structured data from OneKey MLS if missing ---
            if listing.listing_url and _is_missing_structured_data(listing):
                structured = scrape_listing_structured_data(
                    listing.address, listing.town, listing.state, listing.zip_code
                )
                if structured:
                    logger.info(f"Backfilled structured data for {listing.address}: {structured}")
                    listing.price = listing.price or structured.get("price")
                    listing.bedrooms = listing.bedrooms or structured.get("bedrooms")
                    listing.bathrooms = listing.bathrooms or structured.get("bathrooms")
                    listing.sqft = listing.sqft or structured.get("sqft")
                    listing.year_built = listing.year_built or structured.get("year_built")
                    listing.list_date = listing.list_date or structured.get("list_date")

            # --- Enrichment ---
            enrichment = _enrich_listing(listing, address_key)

            score = _evaluate_listing(
                listing,
                image_urls=scraped_images or None,
                enrichment=enrichment,
            )
            listing_id = db.save_listing(listing, score, email_id, enrichment=enrichment)

            # Tag listing with agent name based on sender email
            agent_name = settings.resolve_agent_name(sender)
            if agent_name and listing_id:
                db.update_listing_fields_by_id(listing_id, force=True, agent_name=agent_name)

            # Attach scraped images if found
            if scraped_images and listing_id:
                db.add_listing_images(listing_id, scraped_images)

            result = {
                "address": listing.address,
                "town": listing.town,
                "price": listing.price,
                "sqft": listing.sqft,
                "bedrooms": listing.bedrooms,
                "mls_id": listing.mls_id,
                "verdict": score.verdict,
                "score": score.score,
                "confidence": score.confidence,
                "concerns": score.concerns,
                "commute_minutes": enrichment.get("commute_minutes"),
            }
            results.append(result)
            _print_result(listing, score)

        # Mark email as processed in Gmail
        try:
            mark_processed(gmail_id, email_data["label_id"])
        except Exception as e:
            logger.error(f"Failed to mark email as processed: {e}")

    return results


def sync_search(max_pages: int = 5) -> dict:
    """Weekly search sync: scrape the configured Redfin filter for listings.

    Fetches each result page through Jina Reader (Redfin bot-blocks cloud
    IPs), extracts listing URLs, saves ones not already in the DB (address
    parsed from the URL slug), and queues the standard enrichment + scoring
    pipeline for each. Sold cleanup is unchanged — the hourly prune already
    handles it per-listing.

    Returns {"pages_fetched", "urls_found", "added", "skipped_existing", "errors"}.
    """
    import time as _time

    import httpx

    from app import jobs
    from app.parsers.plaintext import REDFIN_URL_ADDR_RE

    search_url = (settings.redfin_search_url or "").rstrip("/")
    if not search_url:
        return {"error": "REDFIN_SEARCH_URL not configured"}

    _browser_ua = (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    seen_urls: set[str] = set()
    pages_fetched = 0
    errors: list[str] = []

    for page in range(1, max_pages + 1):
        page_url = search_url if page == 1 else f"{search_url}/page-{page}"
        try:
            with httpx.Client(timeout=60, follow_redirects=True,
                              headers={"User-Agent": _browser_ua}) as client:
                resp = client.get(f"https://r.jina.ai/{page_url}")
                resp.raise_for_status()
                body = resp.text
        except Exception as e:
            logger.error(f"Search sync: page {page} fetch failed: {e}")
            errors.append(f"page {page}: {e}")
            break
        pages_fetched += 1

        page_urls = set()
        for m in re.finditer(
            r"https://www\.redfin\.com/[A-Z]{2}/[^/\s)\"']+/[^/\s)\"']+/home/\d+", body
        ):
            page_urls.add(m.group(0))
        new_on_page = page_urls - seen_urls
        seen_urls |= page_urls
        logger.info(f"Search sync: page {page} → {len(page_urls)} URLs ({len(new_on_page)} new)")
        if not new_on_page:
            break  # past the last page — Redfin repeats or empties results
        _time.sleep(2)

    added = 0
    skipped = 0
    added_ids: list[int] = []
    for url in sorted(seen_urls):
        try:
            m = REDFIN_URL_ADDR_RE.search(url)
            if not m:
                continue
            state = m.group(1).upper()
            town = m.group(2).replace("-", " ").title()
            address = m.group(3).replace("-", " ").title()
            zip_code = m.group(4)

            address_key = normalize_address(address, town, state)
            if address_key and db.is_listing_duplicate_by_address(address_key):
                skipped += 1
                continue

            email_id = db.save_processed_email(
                gmail_id=f"search-sync-{url}",
                message_id="",
                sender="redfin-search-sync",
                subject=f"Search sync: {address}, {town}",
                parser_used="redfin-sync",
                listings_found=1,
            )
            listing = ParsedListing(
                source_format="redfin-sync",
                address=address,
                town=town,
                state=state,
                zip_code=zip_code,
                listing_url=url,
            )
            placeholder = ScoringResult(
                score=0, verdict="Reject", hard_results=[], soft_points={},
                concerns=["Pending enrichment"], confidence="low",
            )
            enrichment = {"address_key": address_key} if address_key else {}
            listing_id = db.save_listing(listing, placeholder, email_id, enrichment)
            jobs.enqueue_listing(listing_id)
            added_ids.append(listing_id)
            added += 1
            logger.info(f"Search sync: added listing #{listing_id}: {address}, {town}")
        except Exception as e:
            logger.error(f"Search sync: failed to add {url}: {e}")
            errors.append(f"{url}: {e}")

    if added_ids:
        jobs.kick()

    return {
        "pages_fetched": pages_fetched,
        "urls_found": len(seen_urls),
        "added": added,
        "skipped_existing": skipped,
        "errors": errors,
    }


def _update_duplicate(existing: tuple[int, str | None], listing: ParsedListing):
    """Update an existing duplicate listing's status and backfill URL if missing."""
    existing_id, existing_status = existing

    # Update status if changed
    if listing.listing_status and listing.listing_status != existing_status:
        db.update_listing_status(existing_id, listing.listing_status)
        logger.info(
            f"Updated listing #{existing_id} status: {existing_status!r} → {listing.listing_status!r}"
        )

    # Backfill URL if the existing listing has none
    if listing.listing_url:
        db.update_listing_fields_by_id(existing_id, listing_url=listing.listing_url)


def _is_missing_structured_data(listing: ParsedListing) -> bool:
    """Return True if a listing has no price, beds, baths, or sqft."""
    return not any([listing.price, listing.bedrooms, listing.bathrooms, listing.sqft])


def _enrich_listing(listing: ParsedListing, address_key: str | None) -> dict:
    """Fetch school data and commute time for a listing.

    Returns an enrichment dict with address_key, school_data_json,
    commute_minutes, commute_data_json. All values may be None.
    """
    # School data — check DB cache first, then call API
    school_data = None
    cached_json = db.get_school_data_by_zip(listing.zip_code) if listing.zip_code else None
    if cached_json:
        logger.info(f"Using cached school data for zip {listing.zip_code}")
        school_data_json = cached_json
    else:
        school_data = fetch_school_data(listing.zip_code, listing.state)
        school_data_json = json.dumps(school_data) if school_data else None

    # Commute time
    commute_result = fetch_commute_time(
        listing.address, listing.town, listing.state, listing.zip_code
    )
    commute_minutes = commute_result["commute_minutes"] if commute_result else None
    commute_data_json = json.dumps(commute_result) if commute_result else None

    return {
        "address_key": address_key,
        "school_data_json": school_data_json,
        "commute_minutes": commute_minutes,
        "commute_data_json": commute_data_json,
    }


def _evaluate_listing(
    listing: ParsedListing,
    image_urls: list[str] | None = None,
    enrichment: dict | None = None,
) -> ScoringResult:
    """Evaluate a listing using AI criteria.

    Returns a placeholder result if no API key or criteria are configured.
    AI failures are recorded as low-confidence Weak Match results (no deterministic fallback).
    image_urls are passed directly to the AI scorer for vision analysis.
    """
    if not settings.anthropic_api_key:
        return ScoringResult(
            verdict="Weak Match",
            score=0,
            confidence="low",
            concerns=["No Anthropic API key configured — listing not evaluated"],
        )

    try:
        criteria = db.get_active_criteria()
    except Exception:
        criteria = None

    if not criteria:
        return ScoringResult(
            verdict="Weak Match",
            score=0,
            confidence="low",
            concerns=["No evaluation criteria set — configure via AI Criteria in dashboard"],
        )

    listing_data = {
        "address": listing.address,
        "town": listing.town,
        "state": listing.state,
        "zip_code": listing.zip_code,
        "mls_id": listing.mls_id,
        "price": listing.price,
        "sqft": listing.sqft,
        "bedrooms": listing.bedrooms,
        "bathrooms": listing.bathrooms,
        "property_type": listing.property_type,
        "listing_status": listing.listing_status,
        "description": listing.description,
        "year_built": listing.year_built,
        "list_date": listing.list_date,
    }

    # Add enrichment data so AI can factor in school quality + commute
    if enrichment:
        if enrichment.get("school_data_json"):
            try:
                listing_data["school_data"] = json.loads(enrichment["school_data_json"])
            except (json.JSONDecodeError, TypeError):
                pass
        if enrichment.get("commute_minutes") is not None:
            listing_data["commute_minutes"] = enrichment["commute_minutes"]

    score, _ = ai_score_listing(
        listing_data=listing_data,
        instructions=criteria["instructions"],
        image_urls=image_urls,
    )
    score.criteria_version = criteria["version"]
    return score


def _print_result(listing: ParsedListing, score: ScoringResult):
    """Print a formatted result to console."""
    addr = f"{listing.address}, {listing.town}" if listing.address else "Unknown"
    price_str = f"${listing.price:,}" if listing.price else "?"
    sqft_str = f"{listing.sqft:,}" if listing.sqft else "?"

    print()
    print(f"{'=' * 50}")
    print(f"Listing: {addr}")
    print(f"{'=' * 50}")
    print()
    print("Hard Criteria")
    for hr in score.hard_results:
        if hr.passed is True:
            icon = "✓"
        elif hr.passed is False:
            icon = "✗"
        else:
            icon = "?"
        reason = f" ({hr.reason})" if hr.reason else ""
        print(f"  {hr.criterion}: {hr.value} {icon}{reason}")
    print()
    print(f"  Price: {price_str}")
    print(f"  Sqft: {sqft_str}")
    print(f"  Beds: {listing.bedrooms or '?'} | Baths: {listing.bathrooms or '?'}")
    if listing.mls_id:
        print(f"  MLS: #{listing.mls_id}")
    print()
    print(f"Score: {score.score} / 100")
    print(f"Verdict: {score.verdict}")
    print(f"Confidence: {score.confidence}")

    if score.concerns:
        print()
        print("Concerns:")
        for concern in score.concerns:
            print(f"  - {concern}")
    print()


def main():
    """CLI entry point."""
    logger.info("Starting listing analyzer poll...")
    results = poll_once()
    logger.info(f"Poll complete. Processed {len(results)} listing(s).")

    if not results:
        print("\nNo new listings found.")
        return

    # Summary
    print(f"\n{'=' * 50}")
    print(f"SUMMARY: {len(results)} listing(s) processed")
    print(f"{'=' * 50}")
    for r in results:
        addr = f"{r['address']}, {r['town']}" if r.get("address") else "Unknown"
        print(f"  [{r['verdict']}] {addr} — ${r.get('price', 0):,}")


if __name__ == "__main__":
    main()
