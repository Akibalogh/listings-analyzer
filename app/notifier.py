"""Notifications for high-scoring listings: phone push (ntfy, or Pushover
before the migration completes) plus a Slack webhook."""
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
    listing_url = _alert_link(listing)

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
    """Return (headline, stats_line, url) for a listing alert.

    An unknown listing status is called out explicitly. OneHome alert emails
    carry no status at all — only 11 of 20 such listings have one — so a house
    that already sold arrives looking every bit as available as a live one, and
    516 Bellwood Avenue was alerted as a new Worth Touring after the sale. The
    criteria are right that a null status is unknown rather than sold, so the
    alert says so instead of implying the house is on the market.
    """
    address = listing.get("address", "Unknown")
    town = listing.get("town", "")
    state = listing.get("state", "NY")
    price = listing.get("price")
    sqft = listing.get("sqft")
    beds = listing.get("bedrooms")
    commute = listing.get("commute_minutes")
    price_str = f"${price:,}" if price else "Price unknown"
    parts = [f"{sqft:,} sqft" if sqft else "", f"{beds} bd" if beds else "",
             f"{commute} min commute" if commute else "",
             "" if (listing.get("listing_status") or "").strip() else "status unknown — may be sold",
             f"MLS #{listing['mls_id']}" if listing.get("mls_id") else ""]
    stats = " · ".join([p for p in parts if p])
    return f"{address}, {town} {state}", f"{price_str}  {stats}".strip(), _alert_link(listing)


def notify_pushover(listing: dict, score: int, verdict: str) -> bool:
    """Send a phone push via Pushover to every configured recipient.

    Returns True if at least one recipient was notified. Fails silently.
    Pushover's `user` field takes a single user/group key per request, so we
    send once per key (supports both spouses without a delivery group).
    """
    keys = settings.pushover_user_keys
    if not (settings.pushover_token and keys):
        return False
    headline, stats, url = _listing_summary(listing)
    emoji = "🏡" if verdict == "Strong Match" else "🏠"
    base = {
        "token": settings.pushover_token,
        "title": f"{emoji} New {verdict} ({score}) — {listing.get('town', '')}",
        "message": f"{headline}\n{stats}",
        # High priority: prominent alert, plays a sound, bypasses quiet hours —
        # so it surfaces on the lock screen rather than sitting silently.
        "priority": 1,
        "sound": "magic",
    }
    if url:
        base["url"] = url
        base["url_title"] = "View on Redfin"
    sent_any = False
    for key in keys:
        try:
            resp = httpx.post(
                "https://api.pushover.net/1/messages.json",
                data={**base, "user": key}, timeout=10.0,
            )
            resp.raise_for_status()
            sent_any = True
        except Exception as e:
            logger.warning(f"Pushover failed for recipient …{key[-4:]}: {e}")
    if sent_any:
        logger.info(f"Pushover sent to {len(keys)} recipient(s) for {headline} ({verdict} {score})")
    return sent_any


def _header_safe(text: str) -> str:
    """Strip characters HTTP headers can't carry (emoji, smart quotes)."""
    return text.encode("ascii", "ignore").decode("ascii").strip()


def _alert_link(listing: dict) -> str:
    """Where an alert should send you: the listing itself, when there is one.

    OneHome portal links were substituted for the dashboard for a while, because
    their `token` query parameter is base64 JSON carrying the buyer's email,
    contact id, agent id, and the saved search's key — and there is no login
    behind it, so the URL is effectively a bearer credential.

    Restored at Aki's request (2026-08). The judgement is his to make and it is
    a defensible one: the topic is 48 random hex characters, the exposure is a
    house shortlist rather than anything of value, and an alert you cannot tap
    is a real cost every day against a theoretical one. 56 of 159 listings are
    OneHome links; the other 102 are Redfin and never carried a credential.

    There is no credential-free alternative to weigh against it. OneKeyMLS moved
    from /address/{slug}/{mls_id} — derivable from the MLS number — to
    /home-details/{slug}/{opaqueId}, and the opaque id cannot be constructed
    (see the note in app/parsers/onehome.py). So it is the portal link or
    nothing clickable at all.

    The dashboard remains the fallback for listings with no URL of any kind.
    """
    return (listing.get("listing_url") or "").strip() or settings.public_base_url.rstrip("/")


def _redact_topic(text: str) -> str:
    """Blank the ntfy topic out of text destined for a response or a log."""
    topic = (settings.ntfy_topic or "").strip()
    return text.replace(topic, "<redacted>") if topic else text


def ntfy_probe() -> dict:
    """Publish a bare message to the configured topic and report the raw result.

    A diagnostic, not a notification path. notify_ntfy() sends Title, Tags,
    Priority and Click headers; this sends none of them, so a probe that
    succeeds while the real alert fails isolates the fault to a header, and one
    that fails identically points at the network, the topic, or a rate limit.

    ntfy names the reason in the response body (its own numeric code plus a
    message), which is the piece that was being swallowed. The topic is never
    echoed — it is the only access control on public ntfy.sh.

    Authorization is the one header the probe DOES send. It is not cosmetic:
    ntfy.sh meters anonymous publishes per visitor IP, and a Fly app shares its
    egress IP with other tenants, so an unauthenticated probe inherits a quota
    strangers can exhaust. Omitting it made the probe report a limit the real
    alert path would never have hit, which is worse than not probing at all.
    """
    if not settings.ntfy_url:
        return {"probe": "skipped", "reason": "NTFY_TOPIC is not set"}
    headers = {}
    if settings.ntfy_token:
        headers["Authorization"] = f"Bearer {settings.ntfy_token}"
    out: dict = {
        "probe": "ran",
        "server": settings.ntfy_server,
        "authenticated": bool(settings.ntfy_token),
    }
    try:
        resp = httpx.post(
            settings.ntfy_url,
            content=b"probe from listings-analyzer",
            headers=headers,
            timeout=10.0,
        )
        out["status_code"] = resp.status_code
        # ntfy echoes the topic in its success body. The topic is the only
        # access control on public ntfy.sh, so it is redacted here for the same
        # reason /health reports a fingerprint instead of the value.
        out["response_body"] = _redact_topic((resp.text or "")[:400])
        out["ok"] = resp.is_success
    except Exception as e:
        out["ok"] = False
        out["exception_type"] = type(e).__name__
        out["exception"] = str(e)[:400]
    return out


def notify_ntfy(listing: dict, score: int, verdict: str) -> bool:
    """Send a phone push via ntfy to the configured topic.

    One topic serves everyone subscribed (Aki, Bronwyn, Ken) — a single publish
    reaches all of them, so there is no per-recipient fan-out.

    The title and body carry the address, price and score so the alert is
    readable straight from the lock screen; ntfy renders both without opening
    the app. Returns True when published. Fails silently.
    """
    url = settings.ntfy_url
    if not url:
        return False
    headline, stats, listing_url = _listing_summary(listing)
    headers = {
        # Emoji go in Tags, not the Title — header values must be ASCII
        "Title": _header_safe(f"New {verdict} ({score}) - {listing.get('town', '')}"),
        "Tags": "house_with_garden" if verdict == "Strong Match" else "house",
        # Strong Match gets max, everything else high. iOS was filing these in
        # Notification Center without a banner, and max asks it to surface more
        # insistently. Reserved for Strong Match on purpose: a database where
        # every alert is urgent has no urgent alerts, and Worth Touring is the
        # common verdict — 12 of the 36 live listings carry it.
        "Priority": "max" if verdict == "Strong Match" else "high",
    }
    if listing_url:
        headers["Click"] = listing_url
    if settings.ntfy_token:
        headers["Authorization"] = f"Bearer {settings.ntfy_token}"
    try:
        resp = httpx.post(
            url,
            content=f"{headline}\n{stats}".encode("utf-8"),
            headers=headers,
            timeout=10.0,
        )
        resp.raise_for_status()
        logger.info(f"ntfy sent for {headline} ({verdict} {score})")
        return True
    except httpx.HTTPStatusError as e:
        # ntfy explains rejections in the body (its own error code + message).
        # Without this the failure was indistinguishable from success: the
        # caller discarded the return value and /manage/notify-test reported
        # "sent" regardless, so a rejected publish looked like a delivered one.
        logger.warning(
            "ntfy rejected the publish for %s: HTTP %s — %s",
            headline, e.response.status_code, (e.response.text or "")[:300],
        )
        return False
    except Exception as e:
        logger.warning(f"ntfy notification failed for {headline}: {e}")
        return False


def send_high_score_alert(listing: dict, score: int, verdict: str) -> dict:
    """Fan out a new-high-score alert to every configured channel.

    ntfy supersedes Pushover (open source, no per-seat cost) and takes over as
    soon as NTFY_TOPIC is set; Pushover stays wired up so nothing goes quiet
    mid-migration while the topic is unset. Exactly one of the two runs, so a
    half-migrated config can't double-buzz every phone.

    Returns {channel: delivered} so a caller can tell a rejected publish from a
    delivered one. Discarding this is what made a failing ntfy publish look
    identical to a working one for an entire debugging session.

    Pushover backs ntfy up rather than merely preceding it. Public ntfy.sh
    rate-limits per visitor IP, and a Fly app shares its egress IP with other
    tenants — so the daily quota can be exhausted by strangers, returning
    HTTP 429 (code 42908) on every publish for the rest of the day. A house
    worth touring should not go unannounced because of that, and the fallback
    only fires when ntfy actually failed, so the normal path still buzzes once.
    """
    if settings.ntfy_url:
        results = {"ntfy": notify_ntfy(listing, score, verdict)}
        if not results["ntfy"]:
            logger.warning("ntfy publish failed — falling back to Pushover")
            results["pushover"] = notify_pushover(listing, score, verdict)
    else:
        results = {"pushover": notify_pushover(listing, score, verdict)}
    notify_new_listing(listing, score, verdict, listing.get("evaluation_method", "ai"))
    return results


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
