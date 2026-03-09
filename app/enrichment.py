"""Data enrichment: address normalization, school data, commute times.

External API integrations:
- SchoolDigger (free dev tier, 20 calls/day) for school rankings
- Google Routes API (10K free/month) for transit commute times
"""

import json
import logging
import math
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
# Metro-North station proximity (static dataset from OSM, no API call at runtime)
# ---------------------------------------------------------------------------

# Stations sourced from OSM (railway=station, network=Metro-North Railroad).
# Covers Harlem, Hudson, and New Haven lines in Westchester + CT search area.
_METRO_NORTH_STATIONS: list[dict] = [
    {"name": "Ardsley-on-Hudson", "lat": 41.0270084, "lon": -73.876681},
    {"name": "Bedford Hills", "lat": 41.2371203, "lon": -73.6969658},
    {"name": "Branchville", "lat": 41.1009, "lon": -73.4886},
    {"name": "Bronxville", "lat": 40.9388, "lon": -73.8333},
    {"name": "Cannondale", "lat": 41.1919, "lon": -73.4197},
    {"name": "Chappaqua", "lat": 41.1594, "lon": -73.7659},
    {"name": "Cortlandt", "lat": 41.2549, "lon": -73.9217},
    {"name": "Cos Cob", "lat": 41.0582, "lon": -73.5997},
    {"name": "Crestwood", "lat": 40.9636, "lon": -73.8372},
    {"name": "Croton-Harmon", "lat": 41.1897, "lon": -73.8836},
    {"name": "Darien", "lat": 41.0779, "lon": -73.4683},
    {"name": "Dobbs Ferry", "lat": 41.0090, "lon": -73.8740},
    {"name": "East Norwalk", "lat": 41.1008, "lon": -73.4082},
    {"name": "Fleetwood", "lat": 40.9494, "lon": -73.8358},
    {"name": "Glenbrook", "lat": 41.0669, "lon": -73.5405},
    {"name": "Goldens Bridge", "lat": 41.3060, "lon": -73.6798},
    {"name": "Greenwich", "lat": 41.0264, "lon": -73.6264},
    {"name": "Greystone", "lat": 41.0028, "lon": -73.8999},
    {"name": "Harrison", "lat": 40.9761, "lon": -73.7130},
    {"name": "Hartsdale", "lat": 41.0192, "lon": -73.7986},
    {"name": "Hastings-on-Hudson", "lat": 40.9926, "lon": -73.8799},
    {"name": "Hawthorne", "lat": 41.1053, "lon": -73.7980},
    {"name": "Irvington", "lat": 41.0394, "lon": -73.8682},
    {"name": "Katonah", "lat": 41.2578, "lon": -73.6884},
    {"name": "Larchmont", "lat": 40.9280, "lon": -73.7530},
    {"name": "Ludlow", "lat": 40.9826, "lon": -73.8535},
    {"name": "Mamaroneck", "lat": 40.9491, "lon": -73.7362},
    {"name": "Merritt 7", "lat": 41.1172, "lon": -73.3894},
    {"name": "Mount Kisco", "lat": 41.2044, "lon": -73.7271},
    {"name": "Mount Pleasant", "lat": 41.1139, "lon": -73.8065},
    {"name": "Mount Vernon East", "lat": 40.9107, "lon": -73.8233},
    {"name": "Mount Vernon West", "lat": 40.9120, "lon": -73.8378},
    {"name": "New Canaan", "lat": 41.1464, "lon": -73.4959},
    {"name": "New Rochelle", "lat": 40.9113, "lon": -73.7824},
    {"name": "Noroton Heights", "lat": 41.0683, "lon": -73.4829},
    {"name": "North White Plains", "lat": 41.0615, "lon": -73.7769},
    {"name": "Old Greenwich", "lat": 41.0322, "lon": -73.5698},
    {"name": "Ossining", "lat": 41.1621, "lon": -73.8657},
    {"name": "Peekskill", "lat": 41.2944, "lon": -73.9221},
    {"name": "Pelham", "lat": 40.9102, "lon": -73.8044},
    {"name": "Philipse Manor", "lat": 41.0880, "lon": -73.8651},
    {"name": "Pleasantville", "lat": 41.1330, "lon": -73.7929},
    {"name": "Port Chester", "lat": 40.9911, "lon": -73.6686},
    {"name": "Riverdale", "lat": 40.9012, "lon": -73.9125},
    {"name": "Riverside", "lat": 41.0437, "lon": -73.5856},
    {"name": "Rowayton", "lat": 41.0758, "lon": -73.4456},
    {"name": "Rye", "lat": 40.9808, "lon": -73.6880},
    {"name": "Scarborough", "lat": 41.0992, "lon": -73.8634},
    {"name": "Scarsdale", "lat": 41.0050, "lon": -73.7855},
    {"name": "South Norwalk", "lat": 41.1029, "lon": -73.4219},
    {"name": "Springdale", "lat": 41.0847, "lon": -73.5282},
    {"name": "Stamford", "lat": 41.0468, "lon": -73.5427},
    {"name": "Talmadge Hill", "lat": 41.1282, "lon": -73.4832},
    {"name": "Tarrytown", "lat": 41.0627, "lon": -73.8659},
    {"name": "Tuckahoe", "lat": 40.9541, "lon": -73.8264},
    {"name": "Valhalla", "lat": 41.0769, "lon": -73.7778},
    {"name": "Wakefield", "lat": 40.8978, "lon": -73.8591},
    {"name": "White Plains", "lat": 41.0341, "lon": -73.7629},
    {"name": "Wilton", "lat": 41.1956, "lon": -73.4399},
    {"name": "Yonkers", "lat": 40.9311, "lon": -73.8988},
]

# Average walking speed ~83m/min (5km/h)
_WALK_SPEED_M_PER_MIN = 83.0

# Cache: "lat|lon" → station proximity result
_station_cache: dict[str, dict | None] = {}


def fetch_station_proximity(
    address: str | None,
    town: str | None,
    state: str | None,
) -> dict | None:
    """Find nearest Metro-North station to the given address.

    Uses static station dataset (no API call at runtime).
    Walking time estimated at 5 km/h (83 m/min).

    Returns:
        {
            "station": str,
            "distance_m": float,
            "walk_minutes": int,
            "source": "osm_static"
        }
        or None if geocoding fails.
    """
    coords = _geocode_address(address, town, state)
    if not coords:
        return None

    lat, lon = coords["lat"], coords["lon"]
    cache_key = f"{lat:.5f}|{lon:.5f}|station"
    if cache_key in _station_cache:
        return _station_cache[cache_key]

    nearest_dist = float("inf")
    nearest_name = None
    for station in _METRO_NORTH_STATIONS:
        d = _haversine_m(lat, lon, station["lat"], station["lon"])
        if d < nearest_dist:
            nearest_dist = d
            nearest_name = station["name"]

    if nearest_name is None:
        _station_cache[cache_key] = None
        return None

    walk_minutes = round(nearest_dist / _WALK_SPEED_M_PER_MIN)
    result = {
        "station": nearest_name,
        "distance_m": round(nearest_dist),
        "walk_minutes": walk_minutes,
        "source": "osm_static",
    }
    _station_cache[cache_key] = result
    logger.info(
        f"Station proximity {address}, {town}: nearest={nearest_name} "
        f"{nearest_dist:.0f}m ({walk_minutes} min walk)"
    )
    return result


# ---------------------------------------------------------------------------
# Power line proximity (OpenStreetMap Overpass API + Nominatim geocoder)
# ---------------------------------------------------------------------------

_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_OVERPASS_URL = "https://overpass-api.de/api/interpreter"
_NOMINATIM_RATE_LIMIT = 1.1  # seconds between Nominatim calls (ToS: max 1 req/s)
_nominatim_last_call: float = 0.0

# Cache: "address|town|state" → {"lat": float, "lon": float} or None
_geocode_cache: dict[str, dict | None] = {}

# Cache: "lat|lon" → power line proximity result dict
_power_line_cache: dict[str, dict | None] = {}


def _geocode_address(address: str | None, town: str | None, state: str | None) -> dict | None:
    """Geocode an address using Nominatim (free OSM geocoder).

    Returns {"lat": float, "lon": float} or None on failure.
    Rate-limited to 1 req/s per Nominatim ToS.
    """
    global _nominatim_last_call
    if not address or not town:
        return None
    cache_key = f"{(address or '').lower()}|{(town or '').lower()}|{(state or '').lower()}"
    if cache_key in _geocode_cache:
        return _geocode_cache[cache_key]

    # Rate limit
    elapsed = time.time() - _nominatim_last_call
    if elapsed < _NOMINATIM_RATE_LIMIT:
        time.sleep(_NOMINATIM_RATE_LIMIT - elapsed)

    query = f"{address}, {town}, {state or 'NY'}"
    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(
                _NOMINATIM_URL,
                params={"q": query, "format": "json", "limit": 1},
                headers={"User-Agent": "listings-analyzer/1.0 (contact: aki@bitsafe.finance)"},
            )
            _nominatim_last_call = time.time()
            resp.raise_for_status()
            results = resp.json()
            if results:
                result = {"lat": float(results[0]["lat"]), "lon": float(results[0]["lon"])}
                _geocode_cache[cache_key] = result
                return result
    except Exception as e:
        logger.debug(f"Nominatim geocoding failed for {query}: {e}")

    _geocode_cache[cache_key] = None
    return None


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in meters between two lat/lon points."""
    R = 6_371_000  # Earth radius in meters
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlam / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


def fetch_power_line_proximity(
    address: str | None,
    town: str | None,
    state: str | None,
    radius_m: int = 300,
) -> dict | None:
    """Check for high-voltage power line/tower proximity via OSM Overpass API.

    Only flags transmission infrastructure (power=line, power=tower).
    Distribution poles (power=pole) are ubiquitous and not flagged.

    Returns:
        {
            "nearest_distance_m": float,
            "nearest_type": "line" | "tower",
            "voltage": str | None,
            "count_within_300m": int,
            "source": "osm_overpass"
        }
        or None if geocoding fails or no infrastructure found.
    """
    coords = _geocode_address(address, town, state)
    if not coords:
        return None

    lat, lon = coords["lat"], coords["lon"]
    cache_key = f"{lat:.5f}|{lon:.5f}"
    if cache_key in _power_line_cache:
        return _power_line_cache[cache_key]

    overpass_query = (
        f"[out:json];"
        f"(way[power=line](around:{radius_m},{lat},{lon});"
        f"way[power=cable](around:{radius_m},{lat},{lon});"
        f"node[power=tower](around:{radius_m},{lat},{lon});"
        f");out geom;"
    )

    try:
        with httpx.Client(timeout=15) as client:
            resp = client.post(
                _OVERPASS_URL,
                content=overpass_query,
                headers={"User-Agent": "listings-analyzer/1.0", "Content-Type": "text/plain"},
            )
            resp.raise_for_status()
            data = resp.json()

        elements = data.get("elements", [])
        if not elements:
            _power_line_cache[cache_key] = None
            return None

        # Calculate distance to nearest element
        nearest_dist = float("inf")
        nearest_type = None
        nearest_voltage = None

        for elem in elements:
            voltage = elem.get("tags", {}).get("voltage")
            etype = elem.get("tags", {}).get("power", "line")

            # For ways (line segments), check all geometry nodes
            if elem.get("type") == "way" and "geometry" in elem:
                for node in elem["geometry"]:
                    d = _haversine_m(lat, lon, node["lat"], node["lon"])
                    if d < nearest_dist:
                        nearest_dist = d
                        nearest_type = etype
                        nearest_voltage = voltage
            # For nodes (towers)
            elif elem.get("type") == "node":
                nlat = elem.get("lat", lat)
                nlon = elem.get("lon", lon)
                d = _haversine_m(lat, lon, nlat, nlon)
                if d < nearest_dist:
                    nearest_dist = d
                    nearest_type = etype
                    nearest_voltage = voltage

        if nearest_dist == float("inf"):
            _power_line_cache[cache_key] = None
            return None

        result = {
            "nearest_distance_m": round(nearest_dist, 1),
            "nearest_type": nearest_type or "line",
            "voltage": nearest_voltage,
            "count_within_300m": len(elements),
            "source": "osm_overpass",
        }
        _power_line_cache[cache_key] = result
        logger.info(
            f"Power line check {address}, {town}: nearest={nearest_dist:.0f}m "
            f"({nearest_type}, {nearest_voltage or 'voltage unknown'})"
        )
        return result

    except Exception as e:
        logger.debug(f"Power line proximity check failed for {address}, {town}: {e}")
        _power_line_cache[cache_key] = None
        return None


# ---------------------------------------------------------------------------
# FEMA flood zone lookup (NFHL ArcGIS REST API — free, no key required)
# ---------------------------------------------------------------------------

_FEMA_NFHL_URL = (
    "https://hazards.fema.gov/arcgis/rest/services/public/NFHL/MapServer/28/query"
)

# Cache: "lat|lon" → flood zone result dict or sentinel
_flood_zone_cache: dict[str, dict | None] = {}

# Flood zones that require flood insurance (Special Flood Hazard Areas)
_SFHA_ZONES = {"A", "AE", "AH", "AO", "AR", "A99", "V", "VE"}


def fetch_flood_zone(
    address: str | None,
    town: str | None,
    state: str | None,
) -> dict | None:
    """Look up FEMA flood zone designation via NFHL ArcGIS REST API.

    Free, no API key required. Uses Nominatim geocoding (already rate-limited).

    Returns:
        {
            "fld_zone": str,       # e.g. "X", "AE", "A"
            "zone_subty": str,     # e.g. "AREA OF MINIMAL FLOOD HAZARD"
            "sfha": bool,          # True = Special Flood Hazard Area (insurance required)
            "source": "fema_nfhl"
        }
        or None if geocoding fails or API error.
    """
    coords = _geocode_address(address, town, state)
    if not coords:
        return None

    lat, lon = coords["lat"], coords["lon"]
    cache_key = f"{lat:.5f}|{lon:.5f}|flood"
    if cache_key in _flood_zone_cache:
        return _flood_zone_cache[cache_key]

    try:
        with httpx.Client(timeout=10) as client:
            resp = client.get(
                _FEMA_NFHL_URL,
                params={
                    "geometry": f"{lon},{lat}",
                    "geometryType": "esriGeometryPoint",
                    "inSR": "4326",
                    "spatialRel": "esriSpatialRelIntersects",
                    "outFields": "FLD_ZONE,ZONE_SUBTY",
                    "f": "json",
                },
                headers={"User-Agent": "listings-analyzer/1.0 (contact: aki@bitsafe.finance)"},
            )
            resp.raise_for_status()
            data = resp.json()

        features = data.get("features", [])
        if not features:
            _flood_zone_cache[cache_key] = None
            return None

        attrs = features[0].get("attributes", {})
        fld_zone = (attrs.get("FLD_ZONE") or "").strip()
        zone_subty = (attrs.get("ZONE_SUBTY") or "").strip()

        if not fld_zone:
            _flood_zone_cache[cache_key] = None
            return None

        result = {
            "fld_zone": fld_zone,
            "zone_subty": zone_subty,
            "sfha": any(fld_zone.startswith(z) for z in _SFHA_ZONES),
            "source": "fema_nfhl",
        }
        _flood_zone_cache[cache_key] = result
        logger.info(
            f"Flood zone {address}, {town}: {fld_zone} ({zone_subty}) sfha={result['sfha']}"
        )
        return result

    except Exception as e:
        logger.debug(f"Flood zone lookup failed for {address}, {town}: {e}")
        _flood_zone_cache[cache_key] = None
        return None


# ---------------------------------------------------------------------------
# Parcel lot size lookup (NYS GIS Tax Parcels — free, no key required)
# ---------------------------------------------------------------------------

_NYS_PARCELS_URL = (
    "https://gisservices.its.ny.gov/arcgis/rest/services"
    "/NYS_Tax_Parcels_Public/FeatureServer/1/query"
)

# Cache: "address|town|state" → acres (float) or None
_parcel_cache: dict[str, float | None] = {}

# Rate limit: NYS GIS service handles ~1 req/s comfortably
_parcel_last_call: float = 0.0
_PARCEL_MIN_INTERVAL = 1.2  # seconds between calls

# Suffix expansions for compound street names (e.g. "LAKESHORE" → "LAKE SHORE")
_STREET_COMPOUNDS = [
    ("LAKESHORE", "LAKE SHORE"),
    ("LAKESIDE", "LAKE SIDE"),
    ("LAKEVIEW", "LAKE VIEW"),
    ("BROOKSIDE", "BROOK SIDE"),
    ("HILLSIDE", "HILL SIDE"),
    ("HILLCREST", "HILL CREST"),
    ("RIDGEWOOD", "RIDGE WOOD"),
    ("RIDGEWAY", "RIDGE WAY"),
    ("WOODSIDE", "WOOD SIDE"),
    ("CLIFFSIDE", "CLIFF SIDE"),
    ("PARKWAY", "PARK WAY"),
]

# NY county name → COUNTY_NAME value in NYS parcel service
_NY_COUNTY_MAP = {
    "ny": "Westchester",  # default for NY addresses
    "nj": None,            # NJ not in NYS service
    "ct": None,            # CT not in NYS service
}

# Override county for known out-of-Westchester NYS listings
_TOWN_TO_COUNTY: dict[str, str] = {
    "new rochelle": "Westchester",
    "mamaroneck": "Westchester",
    "larchmont": "Westchester",
    "scarsdale": "Westchester",
    "white plains": "Westchester",
    "harrison": "Westchester",
    "rye": "Westchester",
    "greenwich": "Fairfield",  # CT — not in NYS service
    "basking ridge": None,     # NJ
    "bernardsville": None,     # NJ
    "harding township": None,  # NJ
    "morristown": None,        # NJ
    "bernards twp.": None,     # NJ
    "bernardsville boro": None,  # NJ
    "beacon": "Dutchess",
    "highland": "Ulster",
    "ossining": "Westchester",
    "briarcliff manor": "Westchester",
    "sleepy hollow": "Westchester",
    "tarrytown": "Westchester",
    "irvington": "Westchester",
    "dobbs ferry": "Westchester",
    "ardsley": "Westchester",
    "elmsford": "Westchester",
    "hartsdale": "Westchester",
    "hastings-on-hudson": "Westchester",
    "pleasantville": "Westchester",
    "mount pleasant": "Westchester",
    "chappaqua": "Westchester",
    "armonk": "Westchester",
    "bedford": "Westchester",
    "mount kisco": "Westchester",
    "katonah": "Westchester",
    "bedford hills": "Westchester",
    "pound ridge": "Westchester",
    "south salem": "Westchester",
    "north salem": "Westchester",
    "lewisboro": "Westchester",
    "cross river": "Westchester",
    "somers": "Westchester",
    "yorktown": "Westchester",
    "yorktown heights": "Westchester",
    "cortlandt": "Westchester",
    "cortlandt manor": "Westchester",
    "peekskill": "Westchester",
    "eastchester": "Westchester",
    "tuckahoe": "Westchester",
    "pelham": "Westchester",
    "pelham manor": "Westchester",
    "valhalla": "Westchester",
    "thornwood": "Westchester",
    "hawthorne": "Westchester",
    "croton-on-hudson": "Westchester",
    "croton on hudson": "Westchester",
    "yonkers": "Westchester",
    "new rochelle": "Westchester",
    "bronxville": "Westchester",
    "larchmont": "Westchester",
    "mamaroneck": "Westchester",
}


def _nys_parcel_query(county: str, st_nbr: str, street_prefix: str) -> list[dict]:
    """Run one NYS parcels query and return features list."""
    global _parcel_last_call
    import urllib.parse
    import urllib.request

    # Rate limit
    elapsed = time.time() - _parcel_last_call
    if _parcel_last_call > 0 and elapsed < _PARCEL_MIN_INTERVAL:
        time.sleep(_PARCEL_MIN_INTERVAL - elapsed)

    params = {
        "where": (
            f"COUNTY_NAME='{county}'"
            f" AND LOC_ST_NBR='{st_nbr}'"
            f" AND UPPER(LOC_STREET) LIKE '{street_prefix.upper()}%'"
        ),
        "outFields": "PARCEL_ADDR,LOC_ST_NBR,LOC_STREET,ACRES,CALC_ACRES,CITYTOWN_NAME,MUNI_NAME",
        "f": "json",
        "returnGeometry": "false",
    }
    data = urllib.parse.urlencode(params).encode()
    req = urllib.request.Request(
        _NYS_PARCELS_URL,
        data=data,
        headers={"User-Agent": "listings-analyzer/1.0 (contact: aki@bitsafe.finance)"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        result = json.loads(resp.read())
    _parcel_last_call = time.time()
    return result.get("features", [])


def fetch_lot_acres_parcel(
    address: str | None,
    town: str | None,
    state: str | None,
) -> float | None:
    """Look up parcel lot size (acres) from NYS GIS Tax Parcels public service.

    Free, no API key required. Covers all NY counties that have authorized
    NYS GIS to share their data, including Westchester.

    NYS service is NOT available for NJ or CT addresses — returns None for those.

    Args:
        address: Street address (e.g. "9 Irving Pl")
        town: Town/village name (e.g. "Irvington")
        state: State abbreviation or name (e.g. "NY")

    Returns:
        float: lot size in acres (from ACRES field; CALC_ACRES used as fallback)
        None: if not found, unsupported state, or API error
    """
    if not address or not town:
        return None

    cache_key = f"parcel:{(address or '').lower()}|{(town or '').lower()}|{(state or '').lower()}"
    if cache_key in _parcel_cache:
        return _parcel_cache[cache_key]

    # Resolve county — skip non-NY states
    state_norm = (state or "").strip().upper()[:2]
    if state_norm in ("NJ", "CT", "PA", "MA"):
        _parcel_cache[cache_key] = None
        return None

    town_lower = (town or "").lower().strip()
    county = _TOWN_TO_COUNTY.get(town_lower, "Westchester")
    if county is None:
        # Explicitly excluded (NJ/CT town)
        _parcel_cache[cache_key] = None
        return None

    # Parse street number and first word of street name
    parts = address.strip().split(None, 1)
    if len(parts) < 2:
        _parcel_cache[cache_key] = None
        return None
    st_nbr = parts[0]
    street_raw = parts[1].upper().strip()
    # Use first word of street as prefix (e.g. "OLD ARMY RD" → "OLD")
    street_first_word = street_raw.split()[0] if street_raw.split() else street_raw

    def _pick_best(features: list[dict]) -> float | None:
        """From a list of candidate features, pick the best match and return acres.

        Scoring priority:
          3 — PARCEL_ADDR matches input address (first 3 words: number + 2 street words)
          2 — MUNI_NAME matches town name
          1 — CITYTOWN_NAME matches town name
          0 — no geographic match (only used when there's exactly 1 result)
        """
        town_upper = town.upper().strip() if town else ""
        # Normalize input address for comparison
        addr_upper = address.upper().strip()

        candidates = []
        for f in features:
            a = f.get("attributes", {})
            muni = (a.get("MUNI_NAME") or "").upper()
            citytown = (a.get("CITYTOWN_NAME") or "").upper()
            parcel_addr = (a.get("PARCEL_ADDR") or "").upper()

            score = 0
            # Highest priority: parcel address matches input address.
            # Compare the first N significant words: street number + first 2 street words
            # (3 total). This disambiguates "8 OLD ROARING BROOK RD" from "8 OLD FARM LN".
            # For short street names (< 3 words total), use MUNI/CITYTOWN match instead.
            addr_words = addr_upper.split()
            parcel_words = parcel_addr.split()
            if len(addr_words) >= 3 and len(parcel_words) >= 3:
                if addr_words[:3] == parcel_words[:3]:
                    score = 3

            if score < 3:
                if town_upper in muni or muni in town_upper:
                    score = max(score, 2)
                elif town_upper in citytown or citytown in town_upper:
                    score = max(score, 1)

            candidates.append((score, a))

        # Sort by score descending; take highest-scoring match
        candidates.sort(key=lambda x: -x[0])

        # Only accept score=0 candidates when there's exactly 1 result
        min_score = 0 if len(features) == 1 else 1

        for score, a in candidates:
            if score < min_score:
                break
            acres = a.get("ACRES")
            calc = a.get("CALC_ACRES")
            # Use ACRES if non-zero; fall back to CALC_ACRES
            if acres and acres > 0:
                return round(float(acres), 4)
            if calc and calc > 0:
                return round(float(calc), 4)
        return None

    try:
        # Attempt 1: query with first word of street name
        features = _nys_parcel_query(county, st_nbr, street_first_word)

        # Attempt 2: if no results and street name is a compound word, try split form
        if not features:
            for compound, expanded in _STREET_COMPOUNDS:
                if street_raw.startswith(compound):
                    expanded_first = expanded.split()[0]
                    features = _nys_parcel_query(county, st_nbr, expanded_first)
                    if features:
                        break

        if not features:
            logger.debug(f"NYS parcel: no results for {address}, {town}, {county}")
            _parcel_cache[cache_key] = None
            return None

        result = _pick_best(features)
        _parcel_cache[cache_key] = result
        if result:
            logger.info(f"NYS parcel: {address}, {town} → {result} acres")
        return result

    except Exception as e:
        logger.warning(f"NYS parcel lookup failed for {address}, {town}: {e}")
        _parcel_cache[cache_key] = None
        return None


# ---------------------------------------------------------------------------
# Property tax (NY Open Data SODA API — free, no key required)
# ---------------------------------------------------------------------------

_SODA_API_URL = "https://data.cityofnewyork.us/resource/yjxr-fw8i.json"
_ORPTS_API_URL = "https://data.ny.gov/resource/7vem-aaz7.json"

# Cache: address_key → tax data dict
_tax_cache: dict[str, dict] = {}

# Map listing town names → NY ORPTS municipality_name values
# ORPTS uses town names (not hamlet/village names) for Westchester
_ORPTS_MUNICIPALITY_MAP: dict[str, str] = {
    # Westchester hamlets/villages → ORPTS town
    "armonk": "North Castle",
    "chappaqua": "New Castle",
    "yorktown heights": "Yorktown",
    "sleepy hollow": "Mount Pleasant",
    "tarrytown": "Greenburgh",
    "briarcliff manor": "Ossining",
    "ossining": "Ossining",
    "pleasantville": "Mount Pleasant",
    "mount pleasant": "Mount Pleasant",
    "hawthorne": "Mount Pleasant",
    "valhalla": "Mount Pleasant",
    "scarsdale": "Scarsdale",
    "white plains": "White Plains",
    "harrison": "Harrison",
    "rye": "Rye",
    "mamaroneck": "Mamaroneck",
    "larchmont": "Mamaroneck",
    "new rochelle": "New Rochelle",
    "yonkers": "Yonkers",
    "mount kisco": "Mount Kisco",
    "bedford": "Bedford",
    "bedford hills": "Bedford",
    "katonah": "Bedford",
    "somers": "Somers",
    "north salem": "North Salem",
    "lewisboro": "Lewisboro",
    "south salem": "Lewisboro",
    "pound ridge": "Pound Ridge",
    "cortlandt": "Cortlandt",
    "croton-on-hudson": "Cortlandt",
    "croton on hudson": "Cortlandt",
    "peekskill": "Peekskill",
    "eastchester": "Eastchester",
    "tuckahoe": "Eastchester",
    "pelham": "Pelham",
    "pelham manor": "Pelham",
    # Greenburgh villages/hamlets
    "irvington": "Greenburgh",
    "dobbs ferry": "Greenburgh",
    "hastings-on-hudson": "Greenburgh",
    "hastings on hudson": "Greenburgh",
    "hartsdale": "Greenburgh",
    "ardsley": "Greenburgh",
    "elmsford": "Greenburgh",
    # Mount Pleasant hamlets
    "thornwood": "Mount Pleasant",
    # Rye area
    "rye brook": "Rye",
    "port chester": "Rye",
    # Cortlandt
    "cortlandt manor": "Cortlandt",
    # Putnam County
    "carmel": "Carmel",
    "brewster": "Southeast",
    # Dutchess County
    "beacon": "Beacon",
    # Ulster County
    "highland": "Lloyd",
    # Rockland County
    "palisades": "Orangetown",
    # Direct matches (town = municipality)
    "north castle": "North Castle",
    "new castle": "New Castle",
    "yorktown": "Yorktown",
    "greenburgh": "Greenburgh",
}


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


def fetch_property_tax_orpts(
    address: str | None,
    town: str | None,
) -> dict | None:
    """Fetch property tax assessment data from NY State ORPTS open data API.

    Covers all NY municipalities EXCEPT NYC. Free, no API key required.
    Dataset: data.ny.gov/resource/7vem-aaz7 (Property Assessment Data from
    Local Assessment Rolls).

    Args:
        address: Street address (e.g. "21 Pheasant Dr")
        town: Town/city name (e.g. "Armonk", "Chappaqua")

    Returns dict with:
      - assessed_value (int): assessed total value
      - market_value (int | None): full market value (often 0 for fractional-assessment towns)
      - school_taxable (int | None): taxable value for school district
      - county_taxable (int | None): taxable value for county
      - municipality (str): matched municipality name
      - address (str): matched address string
      - source (str): "orpts"
    Or None if not found.
    """
    if not address or not town:
        return None

    cache_key = f"orpts:{address}|{town}"
    if cache_key in _tax_cache:
        return _tax_cache[cache_key]

    # Map listing town → ORPTS municipality name
    town_lower = town.lower().strip()
    municipality = _ORPTS_MUNICIPALITY_MAP.get(town_lower)
    if not municipality:
        municipality = town.strip()

    # Parse street number and street name from address
    # e.g. "21 Pheasant Dr" → number="21", street first word="PHEASANT"
    addr_parts = address.strip().split(None, 1)
    if len(addr_parts) < 2:
        return None
    number = addr_parts[0]
    street_raw = addr_parts[1].upper()
    street_word = street_raw.split()[0] if street_raw.split() else street_raw

    _select = (
        "parcel_address_number,parcel_address_street,parcel_address_suff,"
        "municipality_name,county_name,assessment_total,full_market_value,"
        "school_taxable,county_taxable_value,town_taxable_value,roll_year"
    )

    def _build_result(row: dict) -> dict:
        matched_addr = (
            f"{row.get('parcel_address_number', '')} "
            f"{row.get('parcel_address_street', '')} "
            f"{row.get('parcel_address_suff', '')}".strip()
        )
        return {
            "assessed_value": int(row.get("assessment_total") or 0) or None,
            "market_value": int(row.get("full_market_value") or 0) or None,
            "school_taxable": int(row.get("school_taxable") or 0) or None,
            "county_taxable": int(row.get("county_taxable_value") or 0) or None,
            "municipality": row.get("municipality_name"),
            "address": matched_addr,
            "source": "orpts",
        }

    try:
        with httpx.Client(timeout=10.0) as client:
            # Pass 1: municipality-scoped lookup (fast, exact)
            where = (
                f"municipality_name='{municipality}'"
                f" AND parcel_address_number='{number}'"
                f" AND upper(parcel_address_street) like '{street_word}%'"
            )
            resp = client.get(_ORPTS_API_URL, params={"$where": where, "$select": _select, "$limit": 1})
            resp.raise_for_status()
            data = resp.json()

            if not data:
                # Pass 2: county-level fallback — village/hamlet parcels often filed
                # under a different municipality than the mailing address
                # (e.g. Scarsdale addresses → Greenburgh; Bedford addresses → North Castle)
                county = "Westchester"  # covers most listings; safe default
                where2 = (
                    f"county_name='{county}'"
                    f" AND parcel_address_number='{number}'"
                    f" AND upper(parcel_address_street) like '{street_word}%'"
                )
                resp2 = client.get(_ORPTS_API_URL, params={"$where": where2, "$select": _select, "$limit": 1})
                resp2.raise_for_status()
                data = resp2.json()

        if not data:
            _tax_cache[cache_key] = None
            return None

        result = _build_result(data[0])
        _tax_cache[cache_key] = result
        logger.info(f"ORPTS tax data found for {address}, {town}: assessed={result['assessed_value']}")
        return result

    except Exception as e:
        logger.debug(f"ORPTS property tax lookup failed for {address}, {town}: {e}")
        return None


# ---------------------------------------------------------------------------
# Structured description parsing (no API — regex on listing text)
# ---------------------------------------------------------------------------


def parse_garage_count(description: str | None) -> dict:
    """Parse garage stall count from listing description text.

    Looks for patterns like "2-car garage", "attached 2 car", "3 car garage",
    "1-car attached", "no garage", "carport", etc.

    Returns:
        dict with keys:
          - garage_count (int | None): number of stalls, 0 if explicitly no garage
          - garage_type (str | None): "attached", "detached", "carport", or None
          - source (str): "description_parse"
    """
    if not description:
        return {"garage_count": None, "garage_type": None, "source": "description_parse"}

    text = description.lower()

    # Explicit no-garage signals
    if re.search(r"\bno garage\b|\bwithout garage\b|\bno attached garage\b", text):
        return {"garage_count": 0, "garage_type": None, "source": "description_parse"}

    # Carport (not a full garage)
    if re.search(r"\bcarport\b", text) and not re.search(r"\bgarage\b", text):
        return {"garage_count": 1, "garage_type": "carport", "source": "description_parse"}

    # Numeric patterns: "2-car garage", "2 car garage", "3-car attached", etc.
    m = re.search(
        r"\b([1-9])\s*[-\u2013]?\s*car\b(?:\s+(?:attached|detached))?\s*garage\b"
        r"|\bgarage\s+(?:with\s+)?([1-9])\s*[-\u2013]?\s*car\b"
        r"|\b([1-9])\s*[-\u2013]?\s*car\s+(?:attached|detached)\b",
        text,
    )
    if m:
        count = int(next(g for g in m.groups() if g is not None))
        garage_type = None
        if "attached" in text[max(0, m.start() - 20) : m.end() + 20]:
            garage_type = "attached"
        elif "detached" in text[max(0, m.start() - 20) : m.end() + 20]:
            garage_type = "detached"
        return {"garage_count": count, "garage_type": garage_type, "source": "description_parse"}

    # Generic "garage" without count — assume 1
    if re.search(r"\bgarage\b", text):
        garage_type = None
        if re.search(r"\battached\b", text):
            garage_type = "attached"
        elif re.search(r"\bdetached\b", text):
            garage_type = "detached"
        return {"garage_count": 1, "garage_type": garage_type, "source": "description_parse"}

    return {"garage_count": None, "garage_type": None, "source": "description_parse"}


def parse_hoa_amount(description: str | None) -> dict:
    """Parse monthly HOA fee amount from listing description.

    Returns:
        dict with keys:
          - hoa_monthly (int | None): monthly fee in dollars, 0 if explicitly no HOA
          - hoa_annual (int | None): annual fee if stated as annual
          - source (str): "description_parse"
    """
    if not description:
        return {"hoa_monthly": None, "hoa_annual": None, "source": "description_parse"}

    text = description.lower()

    # Explicit no-HOA signals
    if re.search(r"\bno hoa\b|\bno association fee\b|\bhoa\s*(?:fee|dues|is|:)?\s*\$\s*0\b", text):
        return {"hoa_monthly": 0, "hoa_annual": None, "source": "description_parse"}

    # Monthly patterns: "$350/mo", "HOA $350"
    # IMPORTANT: "$X,XXX hoa" removed — it false-positives on "property taxes $1,992 hoa dues $0"
    monthly_patterns = [
        r"\$\s*(\d[\d,]*)\s*/\s*(?:mo|month)\b",
        r"\$\s*(\d[\d,]*)\s*per\s+month",
        r"\bhoa\s*(?:fee|dues|is|:)?\s*\$\s*(\d[\d,]*)\b",
        r"\bmonthly\s+(?:hoa|dues|association\s+fee)\s*(?:is|of|:)?\s*\$\s*(\d[\d,]*)\b",
        r"\bassociation\s+fees?\s*(?:of|is|:)?\s*\$\s*(\d[\d,]*)\s*(?:/mo|/month|per\s+month)\b",
    ]
    for pat in monthly_patterns:
        m = re.search(pat, text)
        if m:
            # Skip if followed by annual suffix (let annual patterns handle it)
            end_pos = m.end()
            after = text[end_pos:end_pos + 20]
            if re.match(r"\s*(?:/\s*(?:yr|year)|per\s+year|annually)", after):
                continue
            amount = int(next(g for g in m.groups() if g is not None).replace(",", ""))
            if 10 <= amount <= 5000:
                return {"hoa_monthly": amount, "hoa_annual": None, "source": "description_parse"}

    # Annual patterns: "$3,600/year"
    annual_patterns = [
        r"\$\s*(\d[\d,]*)\s*/\s*(?:yr|year)\b",
        r"\$\s*(\d[\d,]*)\s*(?:per\s+year|annually)\b",
        r"\bhoa\s*(?:fee|dues|is|:)?\s*\$\s*(\d[\d,]*)\s*(?:per\s+year|annually|/yr|/year)\b",
    ]
    for pat in annual_patterns:
        m = re.search(pat, text)
        if m:
            amount = int(next(g for g in m.groups() if g is not None).replace(",", ""))
            if 100 <= amount <= 60000:
                return {"hoa_monthly": None, "hoa_annual": amount, "source": "description_parse"}

    return {"hoa_monthly": None, "hoa_annual": None, "source": "description_parse"}


def parse_pool_flag(description: str | None) -> dict:
    """Detect pool presence from listing description.

    Returns:
        dict with keys:
          - has_pool (bool | None): True/False/None if unknown
          - pool_type (str | None): "inground", "above_ground", "community", or None
          - source (str): "description_parse"
    """
    if not description:
        return {"has_pool": None, "pool_type": None, "source": "description_parse"}

    text = description.lower()

    # Negative: community/HOA pool (not on property)
    if re.search(r"\bcommunity\s+pool\b|\bhoa\s+pool\b|\bclub(?:house)?\s+pool\b", text):
        return {"has_pool": False, "pool_type": "community", "source": "description_parse"}

    # Above-ground
    if re.search(r"\babove[\s-]ground\s+pool\b|\babove\s+grade\s+pool\b", text):
        return {"has_pool": True, "pool_type": "above_ground", "source": "description_parse"}

    # In-ground (explicit)
    if re.search(r"\bin[\s-]ground\s+pool\b|\binground\s+pool\b", text):
        return {"has_pool": True, "pool_type": "inground", "source": "description_parse"}

    # Generic pool mention — default to inground
    if re.search(r"\bswimming\s+pool\b|\bpool\b", text):
        # Exclude false positives: "pool table", "car pool", "pool of"
        if not re.search(r"\bpool\s+table\b|\bcar\s*pool\b|\bpool\s+of\b", text):
            return {"has_pool": True, "pool_type": "inground", "source": "description_parse"}

    return {"has_pool": False, "pool_type": None, "source": "description_parse"}


def parse_basement(description: str | None) -> dict:
    """Detect basement presence and finish level from listing description.

    Returns:
        dict with keys:
          - has_basement (bool | None): True/False/None
          - basement_type (str | None): "finished", "partially_finished", "unfinished", "walk_out", or None
          - source (str): "description_parse"
    """
    if not description:
        return {"has_basement": None, "basement_type": None, "source": "description_parse"}

    text = description.lower()

    # Explicit no-basement signals
    if re.search(r"\bno basement\b|\bslab foundation\b|\bslab\s+on\s+grade\b|\bcrawl\s*space\b", text):
        return {"has_basement": False, "basement_type": None, "source": "description_parse"}

    # Walk-out basement (most desirable)
    if re.search(r"\bwalk[\s-]?out\s+basement\b|\bwalkout\s+basement\b|\bwalk[\s-]?out\s+lower\b", text):
        return {"has_basement": True, "basement_type": "walk_out", "source": "description_parse"}

    # Partially finished (check BEFORE finished to avoid false match on "partially finished")
    if re.search(r"\bpartially?\s+finished\s+basement\b|\bhalf\s+finished\s+basement\b|\bpartial(?:ly)?\s+finished\s+(?:lower|basement)\b", text):
        return {"has_basement": True, "basement_type": "partially_finished", "source": "description_parse"}

    # Finished basement
    if re.search(r"\bfinished\s+basement\b|\bfully\s+finished\s+(?:lower|basement)\b|\bbasement.*finished\b", text):
        return {"has_basement": True, "basement_type": "finished", "source": "description_parse"}

    # Unfinished basement
    if re.search(r"\bunfinished\s+basement\b|\bfull\s+basement\b|\bbasement\s+(?:with\s+)?(?:utility|storage|laundry|mechanicals)\b", text):
        return {"has_basement": True, "basement_type": "unfinished", "source": "description_parse"}

    # Generic basement mention
    if re.search(r"\bbasement\b|\blower\s+level\b|\bfinished\s+lower\b", text):
        return {"has_basement": True, "basement_type": None, "source": "description_parse"}

    return {"has_basement": None, "basement_type": None, "source": "description_parse"}


def parse_list_date(description: str | None) -> str | None:
    """Extract on-market list date from listing description text.

    Looks for patterns like:
    - "listed: 01/15/2026", "listed on 01/15/2026"
    - "on market since January 15, 2026"
    - "date listed: 2026-01-15"
    - Redfin structured metadata: "Jan 15, 2026 listed"

    Returns ISO date string "YYYY-MM-DD" or None.
    """
    if not description:
        return None

    import re as _re
    from datetime import datetime

    text = description.lower()

    # Pattern 1: ISO date after "listed" / "on market since" / "date listed"
    m = _re.search(
        r"(?:list(?:ed|ing)\s*(?:date|on|since)?|on\s*(?:the\s*)?market(?:\s*since)?|date\s*listed)\s*:?\s*"
        r"(\d{4}-\d{2}-\d{2})",
        text,
    )
    if m:
        return m.group(1)

    # Pattern 2: M/D/YYYY or MM/DD/YYYY after "listed"
    m = _re.search(
        r"(?:list(?:ed|ing)\s*(?:date|on|since)?|on\s*(?:the\s*)?market(?:\s*since)?|date\s*listed)\s*:?\s*"
        r"(\d{1,2}[/-]\d{1,2}[/-]\d{4})",
        text,
    )
    if m:
        raw = m.group(1).replace("-", "/")
        for fmt in ("%m/%d/%Y", "%d/%m/%Y"):
            try:
                return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
            except ValueError:
                pass

    # Pattern 3: "Month DD, YYYY listed" (Redfin metadata style)
    m = _re.search(
        r"(?:list(?:ed|ing)\s*(?:date|on|since)?|on\s*(?:the\s*)?market(?:\s*since)?|date\s*listed)\s*:?\s*"
        r"([A-Za-z]+\s+\d{1,2},?\s+\d{4})",
        text,
    )
    if m:
        for fmt in ("%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y"):
            try:
                return datetime.strptime(m.group(1).strip(), fmt).strftime("%Y-%m-%d")
            except ValueError:
                pass

    return None


def parse_year_built(description: str | None) -> int | None:
    """Extract year built from listing description.

    Looks for Redfin-style "YYYY year built" or "year built YYYY" patterns.
    Filters out listing-update dates (2026+) and renovation years.

    Returns:
        int year or None
    """
    if not description:
        return None

    text = description.lower()

    # Priority 1: Redfin structured metadata — "YYYY year built" (most reliable)
    m = re.search(r"\b(1[89]\d{2}|20[012]\d)\s+year\s+built\b", text)
    if m:
        return int(m.group(1))

    # Priority 2: "year built: YYYY" or "year built YYYY"
    m = re.search(r"\byear\s+built\s*[:\s]?\s*(1[89]\d{2}|20[012]\d)\b", text)
    if m:
        return int(m.group(1))

    # Priority 3: "built in YYYY" or "constructed in YYYY"
    m = re.search(r"\b(?:built|constructed|erected)\s+(?:in\s+)?(1[89]\d{2}|20[012]\d)\b", text)
    if m:
        year = int(m.group(1))
        if year <= 2025:  # filter out current/future years from listing dates
            return year

    return None


# ---------------------------------------------------------------------------
# Redfin autocomplete URL resolution
# ---------------------------------------------------------------------------

_REDFIN_AUTOCOMPLETE_URL = (
    "https://www.redfin.com/stingray/do/location-autocomplete"
)
_REDFIN_AUTOCOMPLETE_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.redfin.com/",
    "X-Requested-With": "XMLHttpRequest",
}


def fetch_redfin_url(address: str | None, town: str | None, state: str | None) -> str | None:
    """Look up the canonical Redfin listing URL for an address via autocomplete API.

    Calls Redfin's public location-autocomplete endpoint with the address string.
    Response is prefixed with '{}&&' before the JSON payload; strips that before
    parsing. Returns the full https://www.redfin.com URL if an exactMatch or first
    row result is found, else None.

    Includes a 1-second delay per call to be polite to Redfin's servers.
    """
    if not address or not town:
        return None

    query = f"{address} {town}"
    if state:
        query += f" {state}"

    try:
        time.sleep(1)
        with httpx.Client(timeout=10.0, follow_redirects=True) as client:
            resp = client.get(
                _REDFIN_AUTOCOMPLETE_URL,
                params={"location": query, "count": "1", "v": "2"},
                headers=_REDFIN_AUTOCOMPLETE_HEADERS,
            )

        if resp.status_code != 200:
            logger.debug(f"Redfin autocomplete returned {resp.status_code} for: {query}")
            return None

        raw = resp.text.strip()
        # Redfin responses are prefixed with '{}&&' before the JSON payload
        if raw.startswith("{}&&"):
            raw = raw[4:]

        data = json.loads(raw)
        payload = data.get("payload", {})

        # Prefer exactMatch, fall back to first row in sections
        url_path: str | None = None
        exact = payload.get("exactMatch")
        if exact and exact.get("url"):
            url_path = exact["url"]
        else:
            sections = payload.get("sections", [])
            for section in sections:
                rows = section.get("rows", [])
                if rows and rows[0].get("url"):
                    url_path = rows[0]["url"]
                    break

        if not url_path:
            logger.debug(f"Redfin autocomplete: no URL in response for: {query}")
            return None

        # Prepend base URL if path is relative
        if url_path.startswith("/"):
            return f"https://www.redfin.com{url_path}"
        return url_path

    except Exception as e:
        logger.debug(f"Redfin autocomplete error for {query}: {e}")
        return None


def enrich_missing_urls(listings_without_url: list[dict]) -> dict:
    """Resolve listing URLs for listings that have no listing_url.

    For each listing in the provided list, calls fetch_redfin_url() to find
    the canonical Redfin page, then updates the DB record. Silently skips
    listings where Redfin returns nothing or where address/town are missing.

    Args:
        listings_without_url: list of listing dicts (each must have 'id',
            'address', 'town', 'state').

    Returns:
        dict with keys 'checked', 'found', 'errors'.
    """
    from app import db as _db
    from app.db import get_connection

    checked = 0
    found = 0
    errors: list[str] = []

    for listing in listings_without_url:
        lid = listing.get("id")
        address = listing.get("address")
        town = listing.get("town")
        state = listing.get("state")

        if not lid or not address or not town:
            continue

        checked += 1
        try:
            url = fetch_redfin_url(address, town, state)
            if url:
                ph = _db._placeholder()
                with get_connection() as conn:
                    cur = conn.cursor()
                    cur.execute(
                        f"UPDATE listings SET listing_url = {ph} WHERE id = {ph} AND (listing_url IS NULL OR listing_url = '')",
                        (url, lid),
                    )
                found += 1
                logger.info(
                    f"Resolved Redfin URL for listing #{lid} ({address}, {town}): {url}"
                )
            else:
                logger.debug(f"No Redfin URL found for listing #{lid} ({address}, {town})")
        except Exception as e:
            logger.warning(f"enrich_missing_urls: error for listing #{lid}: {e}")
            errors.append(f"#{lid}: {e}")

    logger.info(
        f"enrich_missing_urls: checked={checked}, found={found}, errors={len(errors)}"
    )
    return {"checked": checked, "found": found, "errors": errors}
