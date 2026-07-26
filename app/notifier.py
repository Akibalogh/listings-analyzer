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


def notify_weekly_digest(new_listings: list[dict], quality_pct: float, ingest: dict) -> None:
    """Post the weekly heartbeat to Slack. Fails silently.

    Email alerts are the live channel (polled hourly); this is a once-a-week
    summary of what came in, plus a loud line if the pipeline is unhealthy.
    """
    if not settings.slack_webhook_url:
        return
    n = len(new_listings)
    good = [l for l in new_listings if (l.get("verdict") or "") in ("Worth Touring", "Strong Match")]
    lines = [
        "📋 *Weekly digest*",
        f"• {n} new listing{'s' if n != 1 else ''} in the last 7 days"
        + (f" ({len(good)} Worth Touring+)" if good else ""),
        f"• Data quality: {quality_pct:.0f}%",
    ]
    for l in good[:5]:
        addr = f"{l.get('address', '?')}, {l.get('town', '')}".strip(", ")
        lines.append(f"   • {l.get('score')} {l.get('verdict')} — {addr}")
    if not ingest.get("healthy"):
        if ingest.get("auth_expired"):
            lines.append("• ⚠️ *Gmail connection expired — listings are NOT updating. Reconnect in the app.*")
        else:
            hrs = ingest.get("hours_since_success")
            lines.append(f"• ⚠️ *No successful sync in {hrs}h — the pipeline may be stuck.*")
    _post_slack("\n".join(lines), context="weekly digest")


def _listing_summary(listing: dict) -> tuple[str, str, str]:
    """Return (headline, stats_line, url) for a listing alert."""
    address = listing.get("address", "Unknown")
    town = listing.get("town", "")
    state = listing.get("state", "NY")
    price = listing.get("price")
    sqft = listing.get("sqft")
    beds = listing.get("bedrooms")
    commute = listing.get("commute_minutes")
    price_str = f"${price:,}" if price else "Price unknown"
    parts = [f"{sqft:,} sqft" if sqft else "", f"{beds} bd" if beds else "",
             f"{commute} min commute" if commute else ""]
    stats = " · ".join([p for p in parts if p])
    return f"{address}, {town} {state}", f"{price_str}  {stats}".strip(), listing.get("listing_url", "")


def notify_pushover(listing: dict, score: int, verdict: str) -> bool:
    """Send a phone push via Pushover. Returns True if sent. Fails silently."""
    if not (settings.pushover_token and settings.pushover_user):
        return False
    headline, stats, url = _listing_summary(listing)
    emoji = "🏡" if verdict == "Strong Match" else "🏠"
    payload = {
        "token": settings.pushover_token,
        "user": settings.pushover_user,
        "title": f"{emoji} New {verdict} ({score}) — {listing.get('town', '')}",
        "message": f"{headline}\n{stats}",
        # High priority: prominent alert, plays a sound, bypasses quiet hours —
        # so it surfaces on the lock screen rather than sitting silently.
        "priority": 1,
        "sound": "magic",
    }
    if url:
        payload["url"] = url
        payload["url_title"] = "View on Redfin"
    try:
        resp = httpx.post("https://api.pushover.net/1/messages.json", data=payload, timeout=10.0)
        resp.raise_for_status()
        logger.info(f"Pushover sent for {headline} ({verdict} {score})")
        return True
    except Exception as e:
        logger.warning(f"Pushover notification failed for {headline}: {e}")
        return False


def send_high_score_alert(listing: dict, score: int, verdict: str) -> None:
    """Fan out a new-high-score alert to all configured channels (Pushover + Slack)."""
    notify_pushover(listing, score, verdict)
    notify_new_listing(listing, score, verdict, listing.get("evaluation_method", "ai"))


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
