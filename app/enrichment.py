"""Data enrichment: address normalization, school data, commute times.

External API integrations:
- SchoolDigger (free dev tier, 20 calls/day) for school rankings
- Google Routes API (10K free/month) for transit commute times
"""

import json
import logging
import re
import time
from datetime import datetime, timedelta, timezone

import httpx

from app.config import settings

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Address normalization for dedup
# ---------------------------------------------------------------------------

# Suffix map — derived from _SUFFIXES in plaintext.py
_SUFFIX_MAP = {
    "street": "st",
    "avenue": "ave",
    "lane": "ln",
    "drive": "dr",
    "road": "rd",
    "court": "ct",
    "place": "pl",
    "circle": "cir",
    "boulevard": "blvd",
    "terrace": "ter",
}


def normalize_address(
    address: str | None, town: str | None, state: str | None
) -> str | None:
    """Generate a normalized address key for duplicate detection.

    Returns "{normalized_addr}|{normalized_town}|{normalized_state}" or None.
    """
    if not address or not town:
        return None

    # Lowercase, strip periods and extra whitespace
    addr = address.lower().strip().replace(".", "")
    town_norm = town.lower().strip().replace(".", "")
    state_norm = (state or "").lower().strip().replace(".", "")

    # Compress whitespace
    addr = re.sub(r"\s+", " ", addr)
    town_norm = re.sub(r"\s+", " ", town_norm)

    # Normalize suffixes: "avenue" -> "ave", "street" -> "st", etc.
    for long_form, short_form in _SUFFIX_MAP.items():
        addr = re.sub(rf"\b{long_form}\b", short_form, addr)

    return f"{addr}|{town_norm}|{state_norm}"


# ---------------------------------------------------------------------------
# School data (SchoolDigger API)
# ---------------------------------------------------------------------------

_SCHOOLDIGGER_BASE = "https://api.schooldigger.com/v2.0/schools"

# Rate limiter: SchoolDigger free tier allows 1 call/minute, 20 calls/day
_schooldigger_last_call: float = 0.0
_SCHOOLDIGGER_MIN_INTERVAL = 61  # seconds between calls

# State name → 2-letter code (NY metro area)
_STATE_MAP = {
    "new york": "NY",
    "new jersey": "NJ",
    "connecticut": "CT",
    "pennsylvania": "PA",
    "massachusetts": "MA",
}


def _normalize_state_code(state: str) -> str | None:
    """Convert state name to 2-letter code if needed."""
    state = state.strip()
    if len(state) == 2:
        return state.upper()
    return _STATE_MAP.get(state.lower())


def fetch_school_data(zip_code: str | None, state: str | None) -> dict | None:
    """Fetch top schools near a zip code from SchoolDigger API.

    Returns dict with keys: elementary, middle, high (each a list of school dicts)
    or None on failure/missing config.

    Caller should check DB cache first via db.get_school_data_by_zip() before
    calling this function to avoid exceeding the 20 calls/day free tier limit.
    """
    if not zip_code or not state:
        return None
    if not settings.schooldigger_app_id or not settings.schooldigger_app_key:
        logger.debug("SchoolDigger API not configured, skipping")
        return None

    state_code = _normalize_state_code(state)
    if not state_code:
        logger.warning(f"Cannot normalize state: {state}")
        return None

    try:
        # Enforce 1-call-per-minute rate limit
        global _schooldigger_last_call
        elapsed = time.monotonic() - _schooldigger_last_call
        if _schooldigger_last_call > 0 and elapsed < _SCHOOLDIGGER_MIN_INTERVAL:
            wait = _SCHOOLDIGGER_MIN_INTERVAL - elapsed
            logger.info(f"SchoolDigger rate limit: waiting {wait:.0f}s")
            time.sleep(wait)

        params = {
            "st": state_code,
            "zip": zip_code,
            "distanceMiles": 3,
            "perPage": 10,
            "appID": settings.schooldigger_app_id,
            "appKey": settings.schooldigger_app_key,
        }
        with httpx.Client(timeout=10.0) as client:
            response = client.get(_SCHOOLDIGGER_BASE, params=params)
            _schooldigger_last_call = time.monotonic()
            response.raise_for_status()
            data = response.json()

        # Detect rate-limited bogus responses
        comment = data.get("_comment", "")
        if "bogus" in comment.lower() or "limit has been reached" in comment.lower():
            logger.warning(f"SchoolDigger rate limit exceeded for {zip_code}, skipping")
            return None

        schools = data.get("schoolList", [])
        result: dict[str, list] = {"elementary": [], "middle": [], "high": []}

        for school in schools:
            level = school.get("schoolLevel", "").lower()

            # v2.0 nests ranking in rankHistory; percentile is rankStatewidePercentage
            rank_pct = None
            rank_history = school.get("rankHistory") or []
            if rank_history:
                rank_pct = rank_history[0].get("rankStatewidePercentage")

            # v2.0 nests city/zip inside address object
            address_obj = school.get("address") or {}

            entry = {
                "name": school.get("schoolName"),
                "rank_percentile": rank_pct,
                "distance_miles": school.get("distanceMiles"),
                "city": address_obj.get("city") or school.get("city"),
                "zip": address_obj.get("zip") or school.get("zip"),
            }
            if "elem" in level:
                result["elementary"].append(entry)
            elif "mid" in level:
                result["middle"].append(entry)
            elif "high" in level:
                result["high"].append(entry)

        # Keep top 3 per level (sorted by rank, highest first)
        for level in result:
            result[level] = sorted(
                result[level],
                key=lambda s: s.get("rank_percentile") or 0,
                reverse=True,
            )[:3]

        logger.info(
            f"SchoolDigger: {zip_code} → "
            f"elem={len(result['elementary'])}, "
            f"mid={len(result['middle'])}, "
            f"high={len(result['high'])}"
        )
        return result

    except Exception as e:
        logger.warning(f"SchoolDigger API error for {zip_code}: {e}")
        return None


# ---------------------------------------------------------------------------
# Commute time (Google Routes API)
# ---------------------------------------------------------------------------

_ROUTES_API_URL = "https://routes.googleapis.com/directions/v2:computeRoutes"


def _next_weekday_8am() -> datetime:
    """Return the next weekday at 8:00 AM Eastern time (as UTC)."""
    from zoneinfo import ZoneInfo

    et = ZoneInfo("America/New_York")
    now = datetime.now(et)
    target = now.replace(hour=8, minute=0, second=0, microsecond=0)

    # Advance to next occurrence if today's 8am has passed
    if target <= now:
        target += timedelta(days=1)
    # Skip weekends
    while target.weekday() >= 5:
        target += timedelta(days=1)

    return target.astimezone(timezone.utc)


def _routes_request(
    origin: str,
    destination: str,
    travel_mode: str,
    departure_time: datetime | None = None,
) -> dict | None:
    """Make a single Google Routes API request. Returns parsed JSON or None."""
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": settings.google_maps_api_key,
        "X-Goog-FieldMask": "routes.duration,routes.distanceMeters",
    }
    body: dict = {
        "origin": {"address": origin},
        "destination": {"address": destination},
        "travelMode": travel_mode,
        "computeAlternativeRoutes": False,
    }
    if departure_time:
        body["departureTime"] = departure_time.strftime("%Y-%m-%dT%H:%M:%SZ")

    try:
        with httpx.Client(timeout=15.0) as client:
            response = client.post(_ROUTES_API_URL, headers=headers, json=body)
            response.raise_for_status()
            data = response.json()
        routes = data.get("routes", [])
        if not routes:
            return None
        return routes[0]
    except Exception as e:
        logger.warning(f"Google Routes API error ({travel_mode}) for {origin}: {e}")
        return None


def fetch_commute_time(
    address: str | None,
    town: str | None,
    state: str | None,
    zip_code: str | None,
) -> dict | None:
    """Fetch commute time to configured destination via Google Routes API.

    Tries TRANSIT first; falls back to DRIVE if no transit route is found
    (common for suburban addresses too far from a station for walking access).

    Returns dict with keys: commute_minutes, commute_mode ("transit"|"drive"),
    departure_time, route_duration_seconds — or None on failure/missing config.
    """
    if not settings.google_maps_api_key:
        logger.debug("Google Maps API key not configured, skipping commute")
        return None

    if not address or not town:
        return None

    destination = settings.commute_destination
    if not destination:
        logger.debug("COMMUTE_DESTINATION not configured, skipping")
        return None

    origin = f"{address}, {town}, {state or ''} {zip_code or ''}".strip()
    departure_time = _next_weekday_8am()

    # Try TRANSIT first
    route = _routes_request(origin, destination, "TRANSIT", departure_time)
    mode = "transit"

    # Fall back to DRIVE if no transit route
    if not route:
        logger.info(f"No transit routes from {origin}, falling back to DRIVE")
        route = _routes_request(origin, destination, "DRIVE")
        mode = "drive"

    if not route:
        logger.warning(f"No routes found (transit or drive) from {origin}")
        return None

    duration_str = route.get("duration", "0s")  # e.g., "4320s"
    duration_seconds = int(duration_str.rstrip("s"))
    commute_minutes = round(duration_seconds / 60)

    logger.info(
        f"Commute ({mode}): {origin} → {destination} = {commute_minutes} min"
    )

    return {
        "commute_minutes": commute_minutes,
        "commute_mode": mode,
        "departure_time": departure_time.isoformat(),
        "route_duration_seconds": duration_seconds,
    }
