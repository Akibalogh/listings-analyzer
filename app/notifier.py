"""Slack webhook notifications for high-scoring listings."""
import logging
import httpx
from app.config import settings

logger = logging.getLogger(__name__)

NOTIFY_VERDICTS = {"Worth Touring", "Strong Match"}


def notify_new_listing(listing: dict, score: int, verdict: str, evaluation_method: str) -> None:
    """Post a Slack notification for a high-scoring listing. Fails silently."""
    if not settings.slack_webhook_url:
        return
    if verdict not in NOTIFY_VERDICTS:
        return
    # Build the message
    address = listing.get("address", "Unknown")
    town = listing.get("town", "")
    state = listing.get("state", "NY")
    price = listing.get("price")
    sqft = listing.get("sqft")
    beds = listing.get("bedrooms")
    baths = listing.get("bathrooms")
    commute = listing.get("commute_minutes")
    listing_url = listing.get("listing_url", "")

    price_str = f"${price:,}" if price else "Price unknown"
    sqft_str = f"{sqft:,} sqft" if sqft else ""
    beds_str = f"{beds} beds" if beds else ""
    baths_str = f"{baths} baths" if baths else ""
    stats = " | ".join(filter(None, [sqft_str, beds_str, baths_str]))
    commute_str = f"Commute: {commute} min" if commute else ""

    emoji = "🏡" if verdict == "Strong Match" else "🏠"
    url_part = f"\n<{listing_url}|View on Redfin>" if listing_url else ""

    text = (
        f"{emoji} *New listing: {verdict} ({score})*\n"
        f"*{address}, {town} {state}* — {price_str}\n"
        f"{stats}"
        + (f"\n{commute_str}" if commute_str else "")
        + url_part
    )

    _post_slack(text, context=f"{address} ({verdict})")


def notify_sync_digest(sync_report: dict, sold_removed: int, quality_pct: float) -> None:
    """Post the weekly sync summary to Slack. Fails silently.

    Verdicts for new finds aren't known yet (scoring runs in the job queue) —
    good matches notify individually via notify_new_listing when scored.
    """
    if not settings.slack_webhook_url:
        return
    added = sync_report.get("added", 0)
    skipped = sync_report.get("skipped_existing", 0)
    errors = sync_report.get("errors") or []
    lines = [
        "📋 *Weekly listing sync*",
        f"• {added} new listing{'s' if added != 1 else ''} added"
        + (" (scoring queued — good matches will ping here)" if added else ""),
        f"• {skipped} already tracked",
        f"• {sold_removed} sold listing{'s' if sold_removed != 1 else ''} removed today",
        f"• Data quality: {quality_pct:.0f}%",
    ]
    if errors:
        lines.append(f"• ⚠️ {len(errors)} fetch error{'s' if len(errors) != 1 else ''}")
    _post_slack("\n".join(lines), context="weekly sync digest")


def _post_slack(text: str, context: str) -> None:
    try:
        resp = httpx.post(
            settings.slack_webhook_url,
            json={"text": text},
            timeout=10.0,
        )
        resp.raise_for_status()
        logger.info(f"Slack notification sent for {context}")
    except Exception as e:
        logger.warning(f"Slack notification failed for {context}: {e}")
