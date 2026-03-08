"""Email alerts via Resend API.

Sends a notification when a listing scores >= 80.
One alert per listing ever (dedup via alerted_at column).
"""

import json
import logging

import httpx

from app import db
from app.config import settings

logger = logging.getLogger(__name__)

ALERT_THRESHOLD = 80

_TO = ["akibalogh@gmail.com"]
_CC = ["bronwyneharris@gmail.com", "ken.wile@redfin.com"]
_FROM = "Listings Analyzer <alerts@bitsafe.finance>"

RESEND_SEND_URL = "https://api.resend.com/emails"


def _format_price(price: int | None) -> str:
    """Format price as $X,XXX,XXX."""
    if price is None:
        return "N/A"
    return f"${price:,}"


def _parse_image_urls(image_urls_json: str | None) -> list[str]:
    """Parse image_urls_json string into a list."""
    if not image_urls_json:
        return []
    try:
        urls = json.loads(image_urls_json)
        return urls if isinstance(urls, list) else []
    except (json.JSONDecodeError, TypeError):
        return []


def _extract_bullets(property_summary: str | None) -> list[str]:
    """Extract top 2-3 key signal bullets from property_summary text."""
    if not property_summary:
        return []
    lines = [
        line.strip().lstrip("-").lstrip("*").lstrip("•").strip()
        for line in property_summary.strip().splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    # Take the first 3 non-empty lines as key signals
    return [l for l in lines if l][:3]


def _build_html(listing: dict, score: int, property_summary: str | None) -> str:
    """Build a clean HTML email body for the listing alert."""
    address = listing.get("address") or "Unknown Address"
    town = listing.get("town") or ""
    state = listing.get("state") or ""
    price = _format_price(listing.get("price"))
    beds = listing.get("bedrooms")
    baths = listing.get("bathrooms")
    sqft = listing.get("sqft")
    year_built = listing.get("year_built")
    mls_id = listing.get("mls_id") or ""

    image_urls = _parse_image_urls(listing.get("image_urls_json"))
    first_image = image_urls[0] if image_urls else None

    # Score badge color: green 80-89, gold 90+
    if score >= 90:
        badge_bg = "#D4AF37"
        badge_label = "Top Match"
    else:
        badge_bg = "#2E8B57"
        badge_label = "Strong Match"

    listing_link = f"https://listings-analyzer.fly.dev/?listing={mls_id}" if mls_id else ""

    # Property details line
    details_parts = []
    if beds is not None:
        details_parts.append(f"{beds} bed")
    if baths is not None:
        details_parts.append(f"{baths} bath")
    if sqft is not None:
        details_parts.append(f"{sqft:,} sqft")
    details_line = " &middot; ".join(details_parts) if details_parts else ""

    # Key signals
    bullets = _extract_bullets(property_summary)
    bullets_html = ""
    if bullets:
        items = "".join(f"<li style='margin-bottom:4px;'>{b}</li>" for b in bullets)
        bullets_html = f"""
        <div style="margin-top:16px;">
            <strong style="font-size:14px;">Key Signals</strong>
            <ul style="margin:8px 0 0 0; padding-left:20px; color:#333; font-size:14px; line-height:1.5;">
                {items}
            </ul>
        </div>"""

    # Image block
    image_html = ""
    if first_image:
        image_html = f"""
        <div style="margin-bottom:16px;">
            <img src="{first_image}" alt="Property photo"
                 style="width:100%; max-width:560px; border-radius:8px; display:block;" />
        </div>"""

    # Year built line
    year_html = ""
    if year_built:
        year_html = f"<p style='margin:4px 0; color:#666; font-size:14px;'>Built {year_built}</p>"

    # Link button
    link_html = ""
    if listing_link:
        link_html = f"""
        <div style="margin-top:20px;">
            <a href="{listing_link}"
               style="display:inline-block; padding:10px 24px; background:#2563EB;
                      color:#fff; text-decoration:none; border-radius:6px;
                      font-size:14px; font-weight:600;">
                View Listing Details
            </a>
        </div>"""

    location = f"{town}, {state}".strip(", ") if town or state else ""

    html = f"""
    <div style="font-family:-apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
                max-width:600px; margin:0 auto; padding:20px; color:#1a1a1a;">

        {image_html}

        <div style="display:inline-block; padding:4px 12px; border-radius:20px;
                    background:{badge_bg}; color:#fff; font-size:13px;
                    font-weight:600; margin-bottom:12px;">
            Score {score} &mdash; {badge_label}
        </div>

        <h2 style="margin:12px 0 4px; font-size:20px;">{address}</h2>
        <p style="margin:0 0 4px; color:#666; font-size:15px;">{location}</p>
        <p style="margin:4px 0; font-size:18px; font-weight:600;">{price}</p>
        <p style="margin:4px 0; color:#555; font-size:14px;">{details_line}</p>
        {year_html}

        {bullets_html}
        {link_html}

        <hr style="margin-top:24px; border:none; border-top:1px solid #eee;" />
        <p style="font-size:12px; color:#999; margin-top:12px;">
            Sent by Listings Analyzer
        </p>
    </div>
    """
    return html


def send_score_alert(listing: dict, score: int, property_summary: str | None = None):
    """Send an email alert for a high-scoring listing.

    Only sends if:
    - score >= ALERT_THRESHOLD (80)
    - listing has not been alerted before (alerted_at is NULL)
    - RESEND_API_KEY is configured

    Args:
        listing: Full listing dict from DB (must include 'id', 'address', etc.)
        score: The numeric score from scoring.
        property_summary: AI-generated summary text (optional).
    """
    if score < ALERT_THRESHOLD:
        return

    if not settings.resend_api_key:
        logger.debug("RESEND_API_KEY not configured, skipping email alert")
        return

    listing_id = listing.get("id")
    if not listing_id:
        logger.warning("Cannot send alert: listing has no id")
        return

    # Dedup check
    if db.is_listing_alerted(listing_id):
        logger.debug("Listing #%s already alerted, skipping", listing_id)
        return

    address = listing.get("address") or "Unknown"
    town = listing.get("town") or ""
    subject = f"\U0001f3e0 New Match: {address}, {town} \u2014 Score {score}"

    html_body = _build_html(listing, score, property_summary)

    payload = {
        "from": _FROM,
        "to": _TO,
        "cc": _CC,
        "subject": subject,
        "html": html_body,
    }

    try:
        with httpx.Client(timeout=10) as client:
            resp = client.post(
                RESEND_SEND_URL,
                headers={
                    "Authorization": f"Bearer {settings.resend_api_key}",
                    "Content-Type": "application/json",
                },
                json=payload,
            )
        if resp.status_code in (200, 201):
            db.mark_alerted(listing_id)
            logger.info(
                "Alert sent for listing #%s (%s) — score %s",
                listing_id, address, score,
            )
        else:
            logger.error(
                "Resend API error %s for listing #%s: %s",
                resp.status_code, listing_id, resp.text[:500],
            )
    except Exception:
        logger.exception("Failed to send alert for listing #%s", listing_id)
