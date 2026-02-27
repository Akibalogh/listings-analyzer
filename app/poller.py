"""Poller orchestrator: fetch emails → parse → score → store.

Can be run as a CLI command or triggered via the API.
"""

import logging
import sys

from app import db
from app.gmail import fetch_new_emails, mark_processed
from app.models import ParsedListing, ScoringResult
from app.parsers import parser_chain
from app.scorer import score_listing

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
        return results

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
            if db.is_listing_duplicate(listing.mls_id):
                logger.info(f"Duplicate listing MLS #{listing.mls_id}, skipping")
                continue

            score = score_listing(listing)
            db.save_listing(listing, score, email_id)

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
            }
            results.append(result)
            _print_result(listing, score)

        # Mark email as processed in Gmail
        try:
            mark_processed(gmail_id, email_data["label_id"])
        except Exception as e:
            logger.error(f"Failed to mark email as processed: {e}")

    return results


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
