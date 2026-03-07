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

# Suffix map — normalize long forms to USPS standard abbreviations
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
    "parkway": "pkwy",
    "highway": "hwy",
    "trail": "trl",
    "crossing": "xing",
    "turnpike": "tpke",
    "expressway": "expy",
    "way": "way",
}

# Directional words → abbreviations (USPS standard)
_DIRECTION_MAP = {
    "north": "n",
    "south": "s",
    "east": "e",
    "west": "w",
    "northeast": "ne",
    "northwest": "nw",
    "southeast": "se",
    "southwest": "sw",
}


def normalize_address(
    address: str | None, town: str | None, state: str | None
) -> str | None:
    """Generate a normalized address key for duplicate detection.

    Returns "{normalized_addr}|{normalized_town}|{normalized_state}" or None.
    """
    if not address or not town:
        return None

    # Lowercase, strip periods, hyphens (e.g. Croton-On-Hudson → Croton On Hudson), extra whitespace
    addr = address.lower().strip().replace(".", "").replace("-", " ")
    town_norm = town.lower().strip().replace(".", "").replace("-", " ")
    state_raw = (state or "").lower().strip().replace(".", "")

    # Normalize state to 2-letter code ("new york" → "ny")
    state_norm = _STATE_MAP.get(state_raw, state_raw).lower() if state_raw else ""

    # Compress whitespace
    addr = re.sub(r"\s+", " ", addr)
    town_norm = re.sub(r"\s+", " ", town_norm)

    # Normalize suffixes: "avenue" -> "ave", "street" -> "st", etc.
    for long_form, short_form in _SUFFIX_MAP.items():
        addr = re.sub(rf"\b{long_form}\b", short_form, addr)

    # Normalize directions: "north" -> "n", "southwest" -> "sw", etc.
    # Process longer forms first so "northeast" matches before "north"
    for long_form, short_form in sorted(_DIRECTION_MAP.items(), key=lambda x: -len(x[0])):
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


def _parse_duration(route: dict) -> int:
    """Extract duration in seconds from a Routes API route dict."""
    return int(route.get("duration", "0s").rstrip("s"))


# Towns whose nearest Metro-North station has a different name
_STATION_OVERRIDES: dict[str, str] = {
    "bedford": "Bedford Hills",
    "pound ridge": "Katonah",
    "south salem": "Katonah",
    "lewisboro": "Katonah",
    "north salem": "Purdy's",
    "waccabuc": "Katonah",
    "cross river": "Katonah",
    "yorktown": "Croton-Harmon",
    "yorktown heights": "Croton-Harmon",
    "cortlandt": "Croton-Harmon",
    "cortlandt manor": "Croton-Harmon",
    "mohegan lake": "Cortlandt",
    "somers": "Katonah",
    "briarcliff manor": "Scarborough",
    "ossining": "Ossining",
    "pleasantville": "Pleasantville",
    "chappaqua": "Chappaqua",
    "new castle": "Chappaqua",
    "millwood": "Chappaqua",
    "armonk": "North White Plains",
    "north castle": "North White Plains",
}


def fetch_commute_time(
    address: str | None,
    town: str | None,
    state: str | None,
    zip_code: str | None,
) -> dict | None:
    """Fetch commute time to configured destination via Google Routes API.

    Tries both strategies and returns the shorter commute:
    1. TRANSIT from address (walking access to station)
    2. Drive-to-station hybrid: DRIVE to nearest train station + TRANSIT
       from station to destination (typical for suburban Westchester)

    Returns dict with keys: commute_minutes, commute_mode ("transit"|"drive+transit"),
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

    # Drive to nearest Metro-North station + transit to destination
    station_town = _STATION_OVERRIDES.get(town.lower(), town)
    station = f"{station_town} train station, {state or 'NY'}"
    station_transit = _routes_request(station, destination, "TRANSIT", departure_time)
    if not station_transit:
        logger.warning(f"No transit route from {station} to {destination} for {origin}")
        return None

    drive_to_station = _routes_request(origin, station, "DRIVE")
    if not drive_to_station:
        logger.warning(f"No drive route from {origin} to {station}")
        return None

    drive_secs = _parse_duration(drive_to_station)
    transit_secs = _parse_duration(station_transit)
    total_seconds = drive_secs + transit_secs
    commute_minutes = round(total_seconds / 60)
    logger.info(
        f"Commute (drive+transit): {origin} → {station} ({round(drive_secs/60)} min drive) "
        f"→ {destination} ({round(transit_secs/60)} min transit) = {commute_minutes} min total"
    )
    return {
        "commute_minutes": commute_minutes,
        "commute_mode": "drive+transit",
        "departure_time": departure_time.isoformat(),
        "route_duration_seconds": total_seconds,
        "drive_minutes": round(drive_secs / 60),
        "transit_minutes": round(transit_secs / 60),
        "station": station,
    }


# ---------------------------------------------------------------------------
# Age / condition scoring (pure code, no external API)
# ---------------------------------------------------------------------------

# Keyword → point delta (applied to description text, case-insensitive)
_CONDITION_KEYWORDS: list[tuple[str, int]] = [
    # Positive signals
    ("new roof", +6),
    ("new construction", +8),
    ("newly built", +8),
    ("gut renovated", +7),
    ("fully renovated", +6),
    ("fully updated", +5),
    ("completely renovated", +6),
    ("recently renovated", +5),
    ("recently updated", +4),
    ("updated kitchen", +3),
    ("updated bath", +3),
    ("updated bathroom", +3),
    ("new kitchen", +4),
    ("new bath", +3),
    ("new bathroom", +3),
    ("new hvac", +4),
    ("new windows", +3),
    ("new floors", +3),
    ("move-in ready", +3),
    ("turnkey", +3),
    # Negative signals
    ("as is", -12),
    ("as-is", -12),
    ("sold as is", -15),
    ("needs work", -8),
    ("needs tlc", -8),
    ("tlc needed", -8),
    ("fixer", -10),
    ("fixer-upper", -12),
    ("handyman special", -12),
    ("cesspool", -6),
    ("oil heat", -4),
    ("knob and tube", -8),
    ("original condition", -6),
    ("original kitchen", -4),
    ("original bath", -4),
    ("deferred maintenance", -8),
    ("major repairs", -10),
    ("foundation issue", -12),
    ("flood zone", -5),
    ("flood damage", -10),
]


def score_age_condition(year_built: int | None, description: str | None) -> dict:
    """Compute an age/condition score adjustment from year_built and listing description.

    Returns:
        dict with keys:
          - age_adjustment (int): point delta from age tier (-22 to 0)
          - condition_adjustment (int): point delta from keyword scan (clamped -25 to +15)
          - age_tier (str): human-readable tier label
          - keywords_matched (list[str]): matched condition keywords
    """
    # --- Age tier ---
    if year_built is None:
        age_adj = 0
        tier = "unknown"
    elif year_built < 1940:
        age_adj = -22
        tier = "pre-1940"
    elif year_built < 1960:
        age_adj = -18
        tier = "1940-1959"
    elif year_built < 1975:
        age_adj = -12
        tier = "1960-1974"
    elif year_built < 1990:
        age_adj = -6
        tier = "1975-1989"
    elif year_built < 2005:
        age_adj = -2
        tier = "1990-2004"
    else:
        age_adj = 0
        tier = "2005+"

    # --- Condition keywords ---
    condition_adj = 0
    matched: list[str] = []
    if description:
        desc_lower = description.lower()
        for keyword, delta in _CONDITION_KEYWORDS:
            if keyword in desc_lower:
                condition_adj += delta
                matched.append(keyword)

    # Clamp condition adjustment
    condition_adj = max(-25, min(+15, condition_adj))

    return {
        "age_adjustment": age_adj,
        "condition_adjustment": condition_adj,
        "age_tier": tier,
        "keywords_matched": matched,
    }


# ---------------------------------------------------------------------------
# Price per sqft benchmark (Zillow Research CSV — loaded at startup)
# ---------------------------------------------------------------------------

_ZILLOW_CSV_URL = (
    "https://files.zillowstatic.com/research/public_csvs/zhvi/"
    "Zip_zhvi_uc_sfrcondo_tier_0.33_0.67_sm_sa_month.csv"
)

# In-memory cache: zip_code → median_price (most recent month)
_zillow_median: dict[str, float] = {}
_zillow_loaded = False


def _load_zillow_csv() -> None:
    """Download and parse Zillow ZHVI CSV into _zillow_median map.

    Loads median home value by ZIP — used to derive $/sqft benchmark when
    a listing's sqft is known. Called once at startup (or on first use).
    Silently skips on network failure.
    """
    global _zillow_loaded, _zillow_median
    if _zillow_loaded:
        return
    _zillow_loaded = True  # Set now to avoid retry loops on network failure

    try:
        logger.info("Loading Zillow ZHVI CSV...")
        with httpx.Client(timeout=30.0, follow_redirects=True) as client:
            resp = client.get(_ZILLOW_CSV_URL)
            resp.raise_for_status()
            text = resp.text

        lines = text.splitlines()
        if not lines:
            return

        headers = lines[0].split(",")
        # The last date column holds the most recent month's value
        last_date_col = len(headers) - 1
        zip_col = next((i for i, h in enumerate(headers) if h.strip() == "RegionName"), None)
        region_type_col = next(
            (i for i, h in enumerate(headers) if h.strip() == "RegionType"), None
        )
        if zip_col is None:
            logger.warning("Zillow CSV: RegionName column not found")
            return

        loaded = 0
        for line in lines[1:]:
            parts = line.split(",")
            if len(parts) <= last_date_col:
                continue
            # Skip non-zip rows (e.g., metro, state)
            if region_type_col is not None and parts[region_type_col].strip() != "zip":
                continue
            zip_code = parts[zip_col].strip().zfill(5)
            val_str = parts[last_date_col].strip()
            if val_str:
                try:
                    _zillow_median[zip_code] = float(val_str)
                    loaded += 1
                except ValueError:
                    pass

        logger.info(f"Zillow ZHVI loaded: {loaded} ZIPs")
    except Exception as e:
        logger.warning(f"Failed to load Zillow CSV: {e}")


def get_price_per_sqft_signal(
    price: int | None, sqft: int | None, zip_code: str | None
) -> dict | None:
    """Compare listing $/sqft against Zillow's median home value for the ZIP.

    Returns dict with:
      - listing_price_per_sqft (float)
      - zillow_median_home_value (float)
      - implied_benchmark_per_sqft (float): median / 1500 (rough typical sqft)
      - ratio (float): listing $/sqft ÷ benchmark (>1 = above market)
      - signal (str): "below_market" | "at_market" | "above_market" | "no_data"

    Returns None if price/sqft/zip are missing or no Zillow data.
    """
    if not price or not sqft or sqft <= 0 or not zip_code:
        return None

    _load_zillow_csv()

    zip_norm = str(zip_code).strip().zfill(5)
    median_value = _zillow_median.get(zip_norm)
    if not median_value:
        return None

    listing_ppsf = price / sqft
    # Zillow ZHVI is for a "middle tier" home — we use 1,500 sqft as benchmark
    benchmark_ppsf = median_value / 1500
    ratio = listing_ppsf / benchmark_ppsf if benchmark_ppsf else None

    if ratio is None:
        signal = "no_data"
    elif ratio < 0.85:
        signal = "below_market"
    elif ratio > 1.20:
        signal = "above_market"
    else:
        signal = "at_market"

    return {
        "listing_price_per_sqft": round(listing_ppsf, 2),
        "zillow_median_home_value": round(median_value, 0),
        "implied_benchmark_per_sqft": round(benchmark_ppsf, 2),
        "ratio": round(ratio, 3) if ratio else None,
        "signal": signal,
    }


# ---------------------------------------------------------------------------
# Property tax (NY Open Data SODA API — free, no key required)
# ---------------------------------------------------------------------------

_SODA_API_URL = "https://data.cityofnewyork.us/resource/yjxr-fw8i.json"

# Cache: address_key → tax data dict
_tax_cache: dict[str, dict] = {}


def fetch_property_tax(
    address: str | None,
    borough: str | None = None,
    bbl: str | None = None,
) -> dict | None:
    """Fetch NYC property tax assessment data from NY Open Data SODA API.

    Free, no API key required. Covers NYC 5 boroughs only.

    Args:
        address: Street address (e.g. "123 Main St")
        borough: NYC borough name or number (1=Manhattan, 2=Bronx, 3=Brooklyn, 4=Queens, 5=Staten Island)
        bbl: Borough-Block-Lot identifier (10-digit string)

    Returns dict with:
      - assessed_value (int): NYC assessed value
      - market_value (int): estimated market value
      - tax_class (str): property tax class
      - address (str): matched address
    Or None if not found / not NYC.
    """
    if not address:
        return None

    cache_key = f"{address}|{borough}|{bbl}"
    if cache_key in _tax_cache:
        return _tax_cache[cache_key]

    try:
        params: dict = {"$limit": 1}
        if bbl:
            params["bbl"] = bbl
        else:
            # Normalize address for SODA query
            addr_clean = address.strip().upper()
            params["$where"] = f"upper(address) like '{addr_clean}%'"
            if borough:
                # Accept name or number
                borough_map = {
                    "manhattan": "1", "bronx": "2", "brooklyn": "3",
                    "queens": "4", "staten island": "5",
                }
                b = str(borough).lower()
                borough_num = borough_map.get(b, b)
                params["boro"] = borough_num

        with httpx.Client(timeout=10.0) as client:
            resp = client.get(_SODA_API_URL, params=params)
            resp.raise_for_status()
            data = resp.json()

        if not data:
            return None

        row = data[0]
        result = {
            "assessed_value": int(row.get("assessed_value_total") or 0) or None,
            "market_value": int(row.get("market_value_total") or 0) or None,
            "tax_class": row.get("tax_class_at_present"),
            "address": row.get("address"),
        }
        _tax_cache[cache_key] = result
        return result

    except Exception as e:
        logger.debug(f"Property tax lookup failed for {address}: {e}")
        return None
