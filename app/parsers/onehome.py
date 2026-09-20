"""Parser for OneHome/Matrix MLS email alerts.

Extracts listings from the HTML structure used by OneKey MLS NY
email alerts. The email contains listing cards with
predictable CSS classes.

Also provides scrape_listing_description() to fetch the full listing
page and extract the property description (basement, features, etc.).

Supports both static HTML pages and JavaScript SPAs:
- Static HTTP scrape for server-rendered pages (Redfin, etc.)
- Jina Reader API (r.jina.ai) for SPA pages (OneHome Angular portal)
"""

import logging
import random
import re
import time
from typing import NamedTuple
from urllib.parse import quote, urljoin, urlparse

import threading

import httpx

from app.config import settings
from bs4 import BeautifulSoup

from app.models import ParsedListing
from app.parsers.base import EmailParser

logger = logging.getLogger(__name__)

# Regex patterns for extracting specs from highlight-specs text
BEDS_RE = re.compile(r"(\d+)\s*bd", re.IGNORECASE)
BATHS_RE = re.compile(r"(\d+)\s*ba", re.IGNORECASE)
SQFT_RE = re.compile(r"([\d,]+)\s*sqft", re.IGNORECASE)
MLS_RE = re.compile(r"MLS\s*#?\s*(\d+)", re.IGNORECASE)

# City, State Zip pattern
ADDRESS_RE = re.compile(r"^(.+?),\s*(\w[\w\s]*?)\s+(\d{5}(?:-\d{4})?)$")

# ---------------------------------------------------------------------------
# Property type normalization (shared with plaintext parser)
# ---------------------------------------------------------------------------

_PROPERTY_TYPE_CANONICAL_MAP = {
    "single family residential": "Single Family Residential",
    "single family home": "Single Family Residential",
    "single family": "Single Family Residential",
    "single-family residential": "Single Family Residential",
    "single-family home": "Single Family Residential",
    "single-family": "Single Family Residential",
    "residential": "Single Family Residential",
    "condominium": "Condo/Co-op",
    "condo": "Condo/Co-op",
    "co-op": "Condo/Co-op",
    "co op": "Condo/Co-op",
    "coop": "Condo/Co-op",
    "townhouse": "Townhouse",
    "townhome": "Townhouse",
    "multi-family": "Multi-Family",
    "multi family": "Multi-Family",
    "multifamily": "Multi-Family",
    "two-family": "Multi-Family",
    "two family": "Multi-Family",
    "colonial": "Single Family Residential",
    "ranch": "Single Family Residential",
    "cape cod": "Single Family Residential",
    "split level": "Single Family Residential",
    "split-level": "Single Family Residential",
    "contemporary": "Single Family Residential",
    "victorian": "Single Family Residential",
    "tudor": "Single Family Residential",
    "farmhouse": "Single Family Residential",
    "craftsman": "Single Family Residential",
    "mobile home": "Mobile/Manufactured Home",
    "manufactured home": "Mobile/Manufactured Home",
}


def _canonicalize_property_type(raw: str) -> str | None:
    """Normalize a raw property type string to a canonical value.

    Returns a canonical string (e.g. "Single Family Residential") or the
    title-cased raw value if no mapping is found but the string is not blank.
    """
    if not raw:
        return None
    normalized = raw.strip().lower()
    if normalized in _PROPERTY_TYPE_CANONICAL_MAP:
        return _PROPERTY_TYPE_CANONICAL_MAP[normalized]
    # Prefix match handles "Single Family Residential - Detached", etc.
    for key, canonical in _PROPERTY_TYPE_CANONICAL_MAP.items():
        if normalized.startswith(key):
            return canonical
    return raw.strip().title()


class OneHomeParser(EmailParser):
    def can_parse(self, html: str | None, text: str | None) -> bool:
        if not html:
            return False
        return "highlight-price" in html or "multiLineDisplay" in html

    def parse(self, html: str | None, text: str | None) -> list[ParsedListing]:
        if not html:
            return []

        soup = BeautifulSoup(html, "html.parser")
        listings = []

        for block in soup.find_all("div", class_="multiLineDisplay"):
            listing = self._parse_block(block)
            if listing:
                listings.append(listing)

        return listings

    def _parse_block(self, block) -> ParsedListing | None:
        listing = ParsedListing(source_format="onehome_html")

        # Listing URL (from the first link in the block)
        link_el = block.find("a", href=True)
        if link_el:
            href = link_el["href"]
            if "portal.onehome.com" in href or "listing" in href.lower():
                listing.listing_url = href

        # Price
        price_el = block.find(class_="highlight-price")
        if price_el:
            price_text = price_el.get_text(strip=True)
            price_clean = re.sub(r"[^\d]", "", price_text)
            if price_clean:
                listing.price = int(price_clean)

        # Street address
        desc_el = block.find(class_="highlight-description")
        if desc_el:
            listing.address = desc_el.get_text(strip=True)

        # City, State, Zip
        addr_el = block.find(class_="highlight-address")
        if addr_el:
            addr_text = addr_el.get_text(strip=True)
            match = ADDRESS_RE.match(addr_text)
            if match:
                listing.town = match.group(1).strip()
                listing.state = match.group(2).strip()
                listing.zip_code = match.group(3).strip()
            else:
                # Fallback: store the whole thing as town
                listing.town = addr_text

        # Specs (beds, baths, sqft) and MLS ID — in separate <p> elements
        specs_els = block.find_all(class_="highlight-specs")
        for el in specs_els:
            spec_text = el.get_text(strip=True)

            beds_match = BEDS_RE.search(spec_text)
            if beds_match:
                listing.bedrooms = int(beds_match.group(1))

            baths_match = BATHS_RE.search(spec_text)
            if baths_match:
                listing.bathrooms = int(baths_match.group(1))

            sqft_match = SQFT_RE.search(spec_text)
            if sqft_match:
                listing.sqft = int(sqft_match.group(1).replace(",", ""))

            mls_match = MLS_RE.search(spec_text)
            if mls_match:
                listing.mls_id = mls_match.group(1)

        # Property type
        title_el = block.find(class_="highlight-title")
        if title_el:
            raw_type = title_el.get_text(strip=True)
            listing.property_type = _canonicalize_property_type(raw_type)

        # Listing status (New Listing, Price Increased, etc.)
        status_el = block.find(class_="highlight-status")
        if status_el:
            img = status_el.find("img")
            if img and img.get("alt"):
                listing.listing_status = img["alt"]

        return listing


# ---------------------------------------------------------------------------
# Listing page scraper — fetch full description from OneHome portal
# ---------------------------------------------------------------------------

_SCRAPE_TIMEOUT = 15.0
_MAX_DESCRIPTION_LEN = 5000  # Truncate very long descriptions

# Common CSS selectors / patterns for listing descriptions on MLS portals
_DESCRIPTION_SELECTORS = [
    # OneKey MLS (onekeymls.com) — description is in section#overview
    'section#overview',
    # Redfin — description lives in div#house-info or .remarksContainer
    'div#house-info',
    '.remarksContainer',
    '#marketing-remarks-scroll',
    '.remarks-container',
    '[data-rf-test-id="listingRemarks"]',
    # OneHome / Matrix portal common patterns
    '[data-testid="listing-description"]',
    '[id*="remarks"]',
    '[class*="property-details"]',
    '[class*="listing-detail"]',
    '.keyDetailsList',
    '.propertyDetailsSectionContent',
    '.amenity-group',
    # Generic real estate page patterns
    'div.remarks',
    'div.description',
    'section.description',
    '.public-remarks',
    '.agent-remarks',
]

# Image URL patterns for extracting listing photos
_IMAGE_SELECTORS = [
    # OneHome portal
    'img[src*="photos.onehome.com"]',
    'img[src*="mlsmatrix"]',
    # Redfin
    'img[src*="ssl.cdn-redfin"]',
    'img[src*="redfin-static"]',
    # OneKey MLS / CloudFront CDN
    'img[src*="cloudfront.net"]',
    # Coldwell Banker
    'img[src*="s.cbhomes.com"]',
    # Generic MLS patterns
    'img[src*="listing"]',
    'img[class*="photo"]',
    'img[class*="gallery"]',
    'img[data-testid*="photo"]',
    '[class*="carousel"] img',
    '[class*="gallery"] img',
    '[class*="slider"] img',
]

_MIN_IMAGE_WIDTH = 200  # Skip tiny icons/thumbnails

# Redfin CDN image enumeration
_REDFIN_CDN_RE = re.compile(
    r"(https://ssl\.cdn-redfin\.com/photo/\d+/genMid\.\d+_)(\d+)(_\w+\.jpg)",
    re.IGNORECASE,
)
_REDFIN_ENUM_MAX = 80  # Max photo index to try
_REDFIN_ENUM_STOP_AFTER = 3  # Stop after N consecutive 404s
_REDFIN_HEAD_TIMEOUT = 5.0


def enumerate_redfin_images(seed_urls: list[str]) -> list[str]:
    """Enumerate all Redfin CDN images by probing sequential photo indices.

    Redfin CDN URLs follow the pattern:
        https://ssl.cdn-redfin.com/photo/{id}/genMid.{mls_id}_{n}_{variant}.jpg
    where n is the photo index (0-based). Static HTML scraping only captures ~7
    images, but listings often have 30-50+. Floor plans are typically the last
    images and contain critical layout info.

    Args:
        seed_urls: Image URLs extracted from HTML (may include non-Redfin URLs).

    Returns:
        Full list of enumerated URLs if Redfin pattern found and more images
        discovered, otherwise the original seed_urls unchanged.
    """
    # Find a seed URL matching the Redfin CDN pattern
    prefix = suffix = None
    seen_indices: set[int] = set()
    for url in seed_urls:
        m = _REDFIN_CDN_RE.match(url)
        if m:
            prefix = m.group(1)
            suffix = m.group(3)
            seen_indices.add(int(m.group(2)))

    if not prefix or not suffix:
        return seed_urls

    # Probe indices 0.._REDFIN_ENUM_MAX, collecting valid URLs
    found: list[str] = []
    consecutive_misses = 0
    try:
        with httpx.Client(timeout=_REDFIN_HEAD_TIMEOUT, follow_redirects=True) as client:
            for n in range(_REDFIN_ENUM_MAX + 1):
                url = f"{prefix}{n}{suffix}"
                try:
                    resp = client.head(url)
                    if resp.status_code < 400:
                        found.append(url)
                        consecutive_misses = 0
                    else:
                        consecutive_misses += 1
                except Exception:
                    consecutive_misses += 1

                if consecutive_misses >= _REDFIN_ENUM_STOP_AFTER:
                    break
    except Exception as e:
        logger.warning(f"Redfin image enumeration failed: {e}")
        return seed_urls

    if found:
        logger.info(f"Redfin CDN enumeration: {len(seed_urls)} seed → {len(found)} total images")
        return found
    return seed_urls

# Keywords that indicate useful description text (vs. boilerplate)
_DESCRIPTION_KEYWORDS = [
    "basement", "finish", "unfin", "ground floor", "ground level",
    "bedroom", "bath", "kitchen", "pool", "sauna", "jacuzzi",
    "hot tub", "soak", "lot", "acre", "garage", "deck", "patio",
    "renovated", "updated", "condition", "hardwood", "central air",
    "fireplace", "attic", "laundry", "storage", "walk-out",
]


def scrape_listing_description(
    url: str,
    address: str | None = None,
    town: str | None = None,
    state: str | None = None,
    zip_code: str | None = None,
    mls_id: str | None = None,
) -> tuple[str | None, list[str]]:
    """Fetch a listing page and extract description + image URLs.

    Returns (description_text, image_urls) tuple.
    description_text is None if no useful content found.
    image_urls is a list of image URL strings (may be empty).

    Strategy varies by URL type to avoid wasting time on known failures:
    - OneHome (Angular SPA): skip straight to DuckDuckGo → Redfin fallback
    - Redfin: skip static (returns 405), go straight to Jina Reader
    - Other URLs: try static → Jina → DuckDuckGo fallback
    """
    if not url:
        return None, []

    _LAST_TRAIL.clear()
    url_lower = url.lower()

    # Trulia/Zillow first, for EVERY source. This used to sit inside the
    # OneHome branch only, so the 19 listings whose URL is a Redfin link went
    # straight down the Redfin path into the WAF and failed with an empty
    # trail — including 110 Cypress Ln, the highest-scoring live home on the
    # board. Redfin's page is unreachable from a datacenter whether we arrive
    # at it from a OneHome listing or from a Redfin one, so the source of the
    # URL was never the thing that mattered.
    if address and town:
        result = _discover_and_scrape_public_listing(
            address, town, state, zip_code, mls_id)
        if result and result[0]:
            return result

    # --- OneHome URLs: Angular SPA, static + Jina always return empty shell ---
    if "onehome.com" in url_lower:
        logger.info(f"OneHome URL detected, no page to scrape: {url[:80]}")
        # Verified Redfin discovery first. Redfin scraping works (94 of 104
        # Redfin listings enriched); OneKeyMLS's /address/ form has 404'd since
        # 2026-08-02 and only 4 of 183 OneHome listings ever got evidence, all
        # in March. So try the path with a demonstrated success rate before the
        # one with a documented failure.
        discovered = _discover_redfin_url(address, town, state, zip_code, mls_id)
        if discovered:
            result = _scrape_static(discovered)
            if result and result[0]:
                _trail("static ok")
                return result
            _trail("static empty")
            result = _scrape_with_jina(discovered)
            if result and result[0]:
                _trail("jina scrape ok")
                return result
            _trail("jina scrape empty")
            logger.info(f"Discovered Redfin URL but both scrapes came back empty: {discovered}")
        if mls_id and address and town:
            result = _try_onekeymls(address, town, state, zip_code, mls_id)
            if result and result[0]:
                return result
        return _try_redfin_fallback(address, town, state, zip_code, mls_id)

    # --- Redfin URLs: try with retries + aggressive fallbacks ---
    if "redfin.com" in url_lower:
        logger.info(f"Redfin URL detected, trying static HTTP once: {url[:80]}")
        # Single static attempt: Redfin bot-blocks cloud IPs (405) essentially
        # always, so a second UA retry just adds latency before the Jina
        # fallback that actually works.
        static_failed = False
        result = _scrape_static(url, attempt=1)
        if result and result[0]:
            return result
        if result is None:  # None = detected bot block or hard error
            static_failed = True

        if static_failed:
            logger.info(f"Static scrape failed for Redfin (likely bot block), trying Jina Reader immediately: {url[:80]}")
        else:
            logger.info(f"Static scrape returned empty content, trying Jina Reader: {url[:80]}")

        result = _scrape_with_jina(url)
        if result and result[0]:
            return result

        # Redfin blocked (common from cloud IPs) — fall back to OneKey MLS
        if mls_id and address and town:
            logger.info(f"Redfin + Jina failed, trying OneKey MLS for MLS#{mls_id}")
            result = _try_onekeymls(address, town, state, zip_code, mls_id)
            if result and result[0]:
                return result

        # No MLS ID — search OneKey MLS by address via DDG
        if address and town:
            logger.info(f"Trying OneKeyMLS address search for: {address}, {town}")
            onekeymls_url = _search_onekeymls_url(address, town, state, zip_code)
            if onekeymls_url:
                result = _scrape_static(onekeymls_url, attempt=1)
                if result and result[0]:
                    return result
                result = _scrape_with_jina(onekeymls_url)
                if result and result[0]:
                    return result

        logger.warning(f"All scraping methods exhausted for Redfin URL: {url[:80]}")
        return None, []

    # --- Other URLs: try full chain ---
    result = _scrape_static(url)
    if result and result[0]:
        return result

    logger.info(f"Static scrape found nothing, trying Jina Reader for: {url}")
    result = _scrape_with_jina(url)
    if result and result[0]:
        return result

    # Last resort: search for Redfin listing by address
    return _try_redfin_fallback(address, town, state, zip_code, mls_id)


def _try_onekeymls(
    address: str,
    town: str,
    state: str | None,
    zip_code: str | None,
    mls_id: str,
) -> tuple[str | None, list[str]]:
    """Scrape OneKey MLS (onekeymls.com) directly using MLS ID.

    OneKey MLS is the source MLS for the NY metro area. Its public listing
    pages are server-rendered with a predictable URL:
      https://www.onekeymls.com/address/{address-slug}/{mls_id}

    Where the address slug is: {street}-{town}-{state}-{zip} with spaces→hyphens.
    Works reliably from cloud IPs unlike Redfin.
    """
    def _slugify(s: str) -> str:
        return s.strip().replace(" ", "-")

    parts = [_slugify(address), _slugify(town)]
    if state:
        parts.append(_slugify(state))
    if zip_code:
        parts.append(zip_code)
    slug = "-".join(parts)
    url = f"https://www.onekeymls.com/address/{slug}/{mls_id}"

    # NOTE (2026-08-02): this /address/{slug}/{mls_id} form now 404s for every
    # listing tested — OneKeyMLS moved to /home-details/{slug}/{opaqueId}, and
    # the opaque ID isn't derivable from the MLS number. Kept behind a single
    # cheap attempt in case the route returns; the DDG-discovered
    # /home-details/ URL in _search_onekeymls_url is the path that still works.
    logger.info(f"Trying OneKey MLS: {url}")
    result = _scrape_static(url)
    if result and result[0]:
        return result

    logger.info(f"OneKey MLS static failed, trying Jina: {url}")
    result = _scrape_with_jina(url)
    if result and result[0]:
        return result

    return None, []


# --- Address-verified search discovery ----------------------------------

_REDFIN_HOME_RE = re.compile(
    r"redfin\.com/([A-Z]{2})/([^/\s]+)/([^/\s]+)/home/(\d+)", re.IGNORECASE)

_STREET_SUFFIXES = re.compile(
    r"\b(road|rd|drive|dr|street|st|avenue|ave|lane|ln|terrace|ter|court|ct|"
    r"place|pl|way|circle|cir|boulevard|blvd|trail|trl|path|run|row|loop|"
    r"heights|hts|extension|ext|turnpike|tpke|highway|hwy)\b",
    re.IGNORECASE,
)


def _street_key(text: str | None) -> str:
    """Street name reduced to comparable letters, suffix words dropped.

    "53 Tarryhill Road" and "53-Tarryhill-Rd-10591" both reduce to
    "53tarryhill", so an MLS address and a Redfin slug can be compared without
    caring which suffix abbreviation each side used.
    """
    if not text:
        return ""
    cleaned = _STREET_SUFFIXES.sub(" ", str(text))
    return re.sub(r"[^a-z0-9]", "", cleaned.lower())


def _slug_matches_address(
    slug: str, address: str | None, town: str | None, zip_code: str | None,
    slug_town: str | None = None,
) -> bool:
    """Does this URL's address slug describe the property we asked for?

    Shared by both discovery paths, so the Redfin and OneKeyMLS twins cannot
    drift apart again — they previously carried the same loose check
    independently, and fixing one left the other accepting false matches.

    Number and street must match; then the ZIP must corroborate, or the town
    if no ZIP is available. See _redfin_url_matches for why ZIP outranks town.
    """
    zip_in_slug = re.search(r"[-/](\d{5})(?:$|[-/])", slug)

    # Token-sequence match, not substring: a OneKeyMLS slug legitimately
    # embeds the town and state ("53-tarryhill-rd-tarrytown-ny-10591"), so the
    # address cannot be compared as a whole. Requiring the address tokens to
    # appear CONSECUTIVELY keeps it strict — "12 Oak" does not match
    # "12-Oakwood-Dr", which a substring test would have accepted.
    def _tokens(text: str) -> list[str]:
        cleaned = _STREET_SUFFIXES.sub(" ", str(text or ""))
        return [t for t in re.split(r"[^a-z0-9]+", cleaned.lower()) if t]

    want = _tokens(address)
    got = _tokens(slug)
    if not want or not want[0].isdigit():
        return False
    if not any(got[i:i + len(want)] == want for i in range(len(got) - len(want) + 1)):
        return False

    want_zip = (str(zip_code).strip()[:5] if zip_code else "")
    if zip_in_slug and want_zip:
        return zip_in_slug.group(1) == want_zip
    if town and slug_town:
        return _street_key(slug_town) == _street_key(town)
    if town:
        # No town segment in the URL (OneKeyMLS puts the town inside the slug)
        return _street_key(town) in _street_key(slug)
    return False


def _onekeymls_url_matches(
    url: str, address: str | None, town: str | None, zip_code: str | None,
) -> bool:
    """Same strictness as the Redfin check, for OneKeyMLS URLs.

    Its slug shape varies (/address/{slug}/{id} historically,
    /home-details/{slug}/{opaqueId} now), so verification works off the path
    rather than a fixed capture.
    """
    if not url or "onekeymls.com" not in url.lower():
        return False
    path = url.split("onekeymls.com", 1)[1]
    return _slug_matches_address(path, address, town, zip_code)


def _redfin_url_matches(
    url: str, address: str | None, town: str | None, zip_code: str | None,
) -> bool:
    """Is this discovered Redfin URL definitely the same property?

    A wrong URL here is worse than no URL: its description and photos get
    attached to someone else's listing and scored as that house's evidence.

    The check this replaces accepted a URL if ANY street word over two
    characters appeared anywhere in it — and in Westchester "Hill", "Ridge",
    "Brook" and "Lane" are in half the street names, so "12 Oak Lane" would
    match "98-Maple-Lane". Three things must now agree:

      * the street number, exactly;
      * the street name, suffix-insensitive;
      * the ZIP, or failing that the town — corroboration that it is the same
        place. ZIP is checked first because the town legitimately differs
        across sources at borders: 149 Brook Farm Rd E is a Bedford listing
        whose Redfin page is filed under Pound Ridge, same ZIP 10576.
    """
    m = _REDFIN_HOME_RE.search(url or "")
    if not m:
        return False
    slug_town, slug = m.group(2), m.group(3)
    zip_in_slug = re.search(r"-(\d{5})$", slug)
    slug_core = re.sub(r"-\d{5}$", "", slug)

    want_num = re.match(r"\s*(\d+)", address or "")
    got_num = re.match(r"(\d+)", slug_core)
    if not (want_num and got_num) or want_num.group(1) != got_num.group(1):
        return False
    if _street_key(slug_core) != _street_key(address):
        return False

    want_zip = (str(zip_code).strip()[:5] if zip_code else "")
    if zip_in_slug and want_zip:
        return zip_in_slug.group(1) == want_zip
    if town:
        return _street_key(slug_town) == _street_key(town)
    # Number and street matched but nothing corroborates the place — the one
    # case where two real properties could collide, so decline.
    return False


# Set when Jina answers 429. Cleared by clear_transport_throttle() at the top
# of each drain, so the block lasts one drain and not longer.
_JINA_THROTTLED = False


def transport_throttled() -> bool:
    """Has Jina rate-limited us during this drain?

    Worth asking before starting a scrape, because a 429 is not this
    listing's failure. Every scrape_desc attempt burns one of the job's three
    attempts, so a single throttling episode drove all 179 OneHome listings
    into 'failed' — and the gap scan then grants a failed row only one attempt
    per scan, whereas a 'done' row gets a full budget. So the throttle cost
    179 listings their retries for a condition none of them caused.
    """
    return _JINA_THROTTLED


def clear_transport_throttle() -> None:
    global _JINA_THROTTLED
    _JINA_THROTTLED = False


# Last scrape's stage trail, read by the job handler for its error message.
# A module global rather than a return value because scrape_listing_description
# has one signature used from four call sites; the alternative was threading an
# out-param through all of them for telemetry.
_LAST_TRAIL: list[str] = []


def _trail(stage: str) -> None:
    _LAST_TRAIL.append(stage)


def last_scrape_trail() -> str:
    return " -> ".join(_LAST_TRAIL) if _LAST_TRAIL else "no stages recorded"


# Pacing state for Jina. Anonymous r.jina.ai allows roughly 20 requests per
# minute per visitor IP — which a Fly app shares with other tenants — and an
# authenticated key raises that substantially.
_JINA_MIN_INTERVAL_ANON = 3.2
_JINA_MIN_INTERVAL_KEYED = 0.5
_jina_lock = threading.Lock()
_jina_last_at: float = 0.0

# Counted so the pacing and the throttle are visible on /health rather than
# inferred from a blindness number that moved the wrong way for ten days.
_JINA_STATS = {"requests": 0, "throttled": 0, "skipped": 0}


def jina_stats() -> dict:
    return {**_JINA_STATS, "key_configured": bool(settings.jina_api_key),
            "throttled_now": _JINA_THROTTLED}


def note_throttled_skip() -> None:
    _JINA_STATS["skipped"] += 1


def _pace_jina() -> None:
    """Wait until the next Jina request is due.

    Each blind listing costs TWO Jina calls — one to search, one to fetch the
    page it finds — so 264 listings is ~528 requests. Bursting those at the
    drain's own pace exceeded the anonymous quota immediately and every
    request after the first came back 429. Spread at the quota's rate the same
    work takes about half an hour and completes.
    """
    interval = (_JINA_MIN_INTERVAL_KEYED if settings.jina_api_key
                else _JINA_MIN_INTERVAL_ANON)
    global _jina_last_at
    with _jina_lock:
        wait = interval - (time.monotonic() - _jina_last_at)
        if _jina_last_at and wait > 0:
            time.sleep(wait)
        _jina_last_at = time.monotonic()


def _fetch_via_jina(url: str) -> str | None:
    """Fetch a URL's text through Jina Reader, which requests from its own IPs.

    This is the transport that makes search discovery work at all. Redfin
    bot-blocks its autocomplete API from cloud IPs (403, verified), and
    reaching a search engine directly is unreliable from a datacenter, but
    Jina fetches server-side and returns the rendered result.

    Paced: see _pace_jina. Without it the anonymous quota is spent in seconds
    and the whole backfill fails.
    """
    _pace_jina()
    _JINA_STATS["requests"] += 1
    headers = {"Accept": "text/html"}
    if settings.jina_api_key:
        # Unauthenticated r.jina.ai is metered per visitor IP, which a Fly app
        # shares with other tenants.
        headers["Authorization"] = f"Bearer {settings.jina_api_key}"
    try:
        with httpx.Client(timeout=_JINA_TIMEOUT, follow_redirects=True) as client:
            response = client.get(f"{_JINA_READER_URL}{url}", headers=headers)
        if response.status_code != 200:
            logger.info(f"Jina fetch returned {response.status_code} for {url[:80]}")
            _trail(f"jina HTTP {response.status_code}")
            if response.status_code in (429, 402):
                global _JINA_THROTTLED
                _JINA_THROTTLED = True
                _JINA_STATS["throttled"] += 1
                logger.warning(
                    "Jina returned %s — treating the transport as throttled for the "
                    "rest of this drain rather than spending every listing's retries "
                    "on it. Set JINA_API_KEY to leave the shared-IP quota.",
                    response.status_code,
                )
            return None
        _trail(f"jina ok {len(response.text)}ch")
        return response.text
    except Exception as e:
        logger.info(f"Jina fetch failed for {url[:80]}: {e}")
        _trail(f"jina error {type(e).__name__}")
        return None


def _discover_redfin_url(
    address: str | None, town: str | None, state: str | None, zip_code: str | None,
    mls_id: str | None = None,
) -> str | None:
    """Find the Redfin page for an address, and prove it is the right one.

    Why this exists: 179 of 278 listings arrive as OneHome portal URLs, which
    are an Angular SPA with nothing to scrape. Their only evidence path ran
    through OneKeyMLS's /address/{slug}/{mls_id} form, which has 404'd for
    every listing since 2026-08-02, and a search step that yields nothing from
    the deployed app. Result: 4 of 183 OneHome listings ever enriched, all four
    back in March. Redfin scraping, by contrast, works for 94 of 104 — so the
    missing piece was never the scrape, it was finding the Redfin URL.
    """
    if not address or not town:
        return None
    query = " ".join(
        [address, town, state or "NY", str(zip_code or ""), "redfin"]
    ).split()
    search = "https://lite.duckduckgo.com/lite/?q=" + "+".join(query)

    body = _fetch_via_jina(search)
    if not body:
        _trail("discovery: no search body")
        return None
    seen = list(dict.fromkeys(m.group(0) for m in _REDFIN_HOME_RE.finditer(body)))
    _trail(f"discovery: {len(seen)} candidate(s)")
    for candidate in seen:
        full = candidate if candidate.startswith("http") else f"https://www.{candidate}"
        if _redfin_url_matches(full, address, town, zip_code):
            logger.info(f"Discovered Redfin URL for {address}: {full}")
            _trail("discovery: verified")
            return full
    if seen:
        logger.info(
            f"{len(seen)} Redfin URL(s) found for {address} but none verified as "
            "the same property — declining rather than attaching another house"
        )
        _trail("discovery: none verified")
    return None


_TRULIA_URL_RE = re.compile(r"trulia\.com/(?:home|p)/[A-Za-z0-9._-]+", re.IGNORECASE)
_ZILLOW_URL_RE = re.compile(r"zillow\.com/homedetails/[A-Za-z0-9._-]+/[0-9_zpid]+", re.IGNORECASE)
_MLS_IN_TITLE_RE = re.compile(r"MLS\s*#\s*([0-9]{5,9})", re.IGNORECASE)


def _mls_matches_page(html: str, mls_id: str | None) -> bool | None:
    """Does this page's MLS number match the listing's?

    Returns True/False when both are known, None when the comparison cannot be
    made. This is the strongest verification available: Trulia puts
    "MLS# 1052438" in its title, the MLS number is the one identifier both the
    alert email and the listing page agree on exactly, and 243 of 245 blind
    listings have one. An address slug can be ambiguous across town borders;
    an MLS number cannot.
    """
    if not mls_id:
        return None
    m = _MLS_IN_TITLE_RE.search(html[:4000])
    if not m:
        return None
    return m.group(1).strip() == str(mls_id).strip()


_MD_IMAGE_RE = re.compile(
    r"https://[^\s\)\"']*(?:zillowstatic|trulia\.com/pictures)[^\s\)\"']*\.(?:jpg|jpeg|webp|png)",
    re.IGNORECASE,
)


def _description_from_markdown(markdown: str) -> str | None:
    """Pull the listing prose out of Jina's markdown rendering.

    The HTML extractors find nothing here: Jina returns markdown by default and
    Trulia's own markup is class-soup, so the reliable signal is shape. Listing
    prose is a long unbroken line that is not a nav list, a spec table, or an
    image reference — every one of which is abundant on the page.
    """
    best = None
    for raw in (markdown or "").split("\n"):
        line = raw.strip()
        if len(line) < 150 or line.count("](") > 2:
            continue
        if line[:1] in ("*", "|", "!", "#", "-", ">"):
            continue
        if "|" in line or line.lower().startswith(("skip main", "http")):
            continue
        if best is None or len(line) > len(best):
            best = line
    return best


def _images_from_markdown(markdown: str, limit: int = 40) -> list[str]:
    """Listing photos from the markdown, de-duplicated and capped."""
    return list(dict.fromkeys(_MD_IMAGE_RE.findall(markdown or "")))[:limit]


def _discover_and_scrape_public_listing(
    address: str | None, town: str | None, state: str | None,
    zip_code: str | None, mls_id: str | None = None,
) -> tuple[str | None, list[str]]:
    """Find a fetchable public listing page and scrape it.

    Not Redfin. Redfin's property pages answer any datacenter-side renderer
    with an AWS WAF "Human Verification" interstitial - 885 bytes, "Target URL
    returned error 405" - whether fetched directly or through Jina. That wall
    is why the Redfin discovery path shipped in #76 produced zero evidence in
    production: discovery found and verified the right URL, and the page behind
    it was a CAPTCHA. Nothing has gained a description since 2026-08-07.

    Trulia and Zillow render fine through the same transport. Trulia is tried
    first because it carries the full description AND the photo set; Zillow is
    the fallback and carries description but no usable images.
    """
    if not address or not town:
        return None, []
    for site, url_re in (("trulia", _TRULIA_URL_RE), ("zillow", _ZILLOW_URL_RE)):
        query = " ".join(
            [address, town, state or "NY", str(zip_code or ""), site]
        ).split()
        body = _fetch_via_jina(
            "https://lite.duckduckgo.com/lite/?q=" + "+".join(query))
        if not body:
            _trail(f"{site}: no search body")
            continue
        seen = list(dict.fromkeys(m.group(0) for m in url_re.finditer(body)))
        _trail(f"{site}: {len(seen)} candidate(s)")
        for candidate in seen:
            full = candidate if candidate.startswith("http") else f"https://www.{candidate}"
            if not _slug_matches_address(full.split(".com", 1)[1], address, town, zip_code):
                continue
            page = _fetch_via_jina(full)
            if not page:
                _trail(f"{site}: page fetch failed")
                continue
            mls_ok = _mls_matches_page(page, mls_id)
            if mls_id and mls_ok is not True:
                # These sites keep a page per ADDRESS, not per listing, so the
                # same URL may serve a years-old sold record — 28 Argyle Place
                # returns a 2022 sale whose title carries no MLS number at all.
                # Its prose would be scored as this listing's evidence. When we
                # know the MLS number, the page must state the same one;
                # absent or different means decline.
                _trail(f"{site}: MLS {'mismatch' if mls_ok is False else 'absent'}")
                continue
            desc = _description_from_markdown(page)
            imgs = _images_from_markdown(page)
            if desc or imgs:
                _trail(f"{site}: ok {len(desc or '')}ch {len(imgs)} imgs")
                return desc, imgs
            _trail(f"{site}: page had no content")
    return None, []


def _try_redfin_fallback(
    address: str | None,
    town: str | None,
    state: str | None,
    zip_code: str | None,
    mls_id: str | None = None,
) -> tuple[str | None, list[str]]:
    """Search DuckDuckGo for a Redfin URL and scrape it via static HTTP or Jina."""
    if not address or not town:
        return None, []

    logger.info(f"Searching Redfin for: {address}, {town}" + (f" MLS#{mls_id}" if mls_id else ""))
    redfin_url = _search_redfin_url(address, town, state, zip_code, mls_id)
    if not redfin_url:
        return None, []

    logger.info(f"Found Redfin URL: {redfin_url}")
    # Try static HTTP first (Redfin server-renders with a browser User-Agent)
    result = _scrape_static(redfin_url)
    if result and result[0]:
        return result
    # Jina Reader as fallback
    result = _scrape_with_jina(redfin_url)
    if result and result[0]:
        return result
    return None, []


def _is_spa_url(url: str) -> bool:
    """Check if URL is likely a JavaScript SPA that needs JS rendering."""
    return "portal.onehome.com" in url or "onehome.com" in url


def _get_rotating_user_agent() -> str:
    """Rotate User-Agent to avoid bot detection. Simulates different browsers."""
    agents = [
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.2 Safari/605.1.15",
    ]
    return random.choice(agents)


def _is_bot_block_page(html: str) -> bool:
    """Detect if Redfin returned a bot-blocking page (not a normal 404 or missing listing).

    Redfin bot blocks show specific UI patterns:
    - Minimal HTML (no listing details, no property cards)
    - "Unknown address" + error styling from Redfin error handler
    - No description selectors / address fields populated

    False positives to avoid:
    - Normal 404s (have search bar, navigation)
    - Genuinely delisted properties (have address/price, just no description)
    """
    soup = BeautifulSoup(html, "html.parser")
    html_lower = html.lower()
    text_lower = soup.get_text().lower()

    # If the page has substantial content (>5000 chars of useful text),
    # it's probably a real listing page or normal error — not a bot block
    meaningful_text = " ".join([tag.get_text(strip=True) for tag in soup.find_all(['p', 'div', 'span']) if tag.get_text(strip=True)])
    if len(meaningful_text) > 5000:
        return False

    # Redfin bot-block specific patterns (minimal error page from JS)
    # Look for: (1) "unknown address" + (2) minimal navigation + (3) no property fields
    has_unknown_address = "unknown address" in text_lower
    has_error_structure = "rf-error" in html_lower or "error" in html_lower
    has_nav = soup.find("nav") or soup.find(class_="header") or soup.find(id="header")
    has_property_fields = (
        soup.find(class_="address-street") or
        soup.find(class_="price") or
        soup.find(class_="property-details") or
        soup.find(id="listingRemarks") or
        soup.find(class_="remarksContainer")
    )

    # Bot block: "unknown address" error page with no nav/property fields
    if has_unknown_address and has_error_structure and not has_nav and not has_property_fields:
        return True

    return False


def _scrape_static(url: str, attempt: int = 1) -> tuple[str | None, list[str]] | None:
    """Fast static HTTP scrape — works for server-rendered pages (Redfin, etc.).

    Args:
        url: URL to scrape
        attempt: Attempt number (used for delays and logging)

    Returns:
        (description, images) tuple or None if failed.
        NOTE: Returns None on both errors AND detected bot blocks (caller should try Jina next).
    """
    # Add delay to avoid rate limiting (0.5-1.5s per attempt)
    if attempt > 1:
        delay = 0.5 + random.random()
        time.sleep(delay)

    try:
        with httpx.Client(
            timeout=_SCRAPE_TIMEOUT,
            follow_redirects=True,
            headers={
                "User-Agent": _get_rotating_user_agent(),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
                "Referer": "https://www.google.com/",
                "DNT": "1",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
            },
        ) as client:
            response = client.get(url)
            response.raise_for_status()

        html = response.text

        # Check for bot-block pages (Redfin returning error pages for automated requests)
        if _is_bot_block_page(html):
            logger.warning(f"Detected Redfin bot-block page for {url[:80]} (attempt {attempt}), will try Jina")
            return None

        description = _extract_description_from_html(html, url, "static")
        images = _extract_image_urls(html, url) if description else []
        return description, images

    except httpx.HTTPStatusError as e:
        logger.warning(f"HTTP {e.response.status_code} fetching listing page: {url} (attempt {attempt})")
        return None
    except Exception as e:
        logger.warning(f"Failed to scrape listing page {url} (attempt {attempt}): {e}")
        return None


_JINA_READER_URL = "https://r.jina.ai/"
_JINA_TIMEOUT = 30.0


def _scrape_with_jina(url: str) -> tuple[str | None, list[str]]:
    """Use Jina Reader API to render a page (handles SPAs) and extract content.

    Jina Reader (r.jina.ai) renders JavaScript pages server-side and returns
    clean text/markdown. Free for up to 20 req/min. No headless browser needed.
    """
    try:
        with httpx.Client(
            timeout=_JINA_TIMEOUT,
            follow_redirects=True,
        ) as client:
            # Jina Reader: prefix URL with r.jina.ai/
            response = client.get(
                f"{_JINA_READER_URL}{url}",
                headers={
                    "Accept": "text/html",
                    "X-Return-Format": "html",
                },
            )
            response.raise_for_status()

        html = response.text
        logger.info(f"Jina Reader returned {len(html)} chars for {url[:80]}")
        description = _extract_description_from_html(html, url, "jina")
        images = _extract_image_urls(html, url)
        return description, images

    except Exception as e:
        logger.warning(f"Jina Reader scrape failed for {url}: {e}")
        return None, []


# ---------------------------------------------------------------------------
# DuckDuckGo URL search — fallback when primary URL scrape fails
# ---------------------------------------------------------------------------

import time as _time

_DDG_URL = "https://lite.duckduckgo.com/lite/"
_DDG_HTML_URL = "https://html.duckduckgo.com/html/"
_DDG_LAST_CALL: float = 0.0  # monotonic timestamp of last DDG request
_DDG_MIN_INTERVAL: float = 3.0  # seconds between DDG requests to avoid rate limiting


def _ddg_rate_limit():
    """Enforce minimum interval between DuckDuckGo requests to avoid 202/403."""
    global _DDG_LAST_CALL
    now = _time.monotonic()
    elapsed = now - _DDG_LAST_CALL
    if elapsed < _DDG_MIN_INTERVAL:
        _time.sleep(_DDG_MIN_INTERVAL - elapsed)
    _DDG_LAST_CALL = _time.monotonic()
_DDG_TIMEOUT = 10.0
_REDFIN_URL_RE = re.compile(r"https?://www\.redfin\.com/[^\s\"<>&]+/home/\d+")
_ONEKEYMLS_URL_RE = re.compile(
    r"https?://(?:www\.)?onekeymls\.com/[^\s\"<>&]+"
)

# Regex patterns for extracting property stats from visible page text
# Price: allow optional space after $, require 6+ digit/comma chars (matches $1,275,000 and $ 1,275,000)
_PRICE_RE = re.compile(r"\$\s?[\d,]{6,}", re.IGNORECASE)
# \b for the same reason as the plaintext parser: scraped page text carries the
# street address, so "406 Bedford Rd" read as 406 bedrooms without it.
_STATS_BEDS_RE = re.compile(r"(\d+)\s*(?:bed(?:room)?s?|bd)\b", re.IGNORECASE)
_STATS_BATHS_RE = re.compile(r"(\d+)\s*(?:bath(?:room)?s?|ba)\b", re.IGNORECASE)
_STATS_SQFT_RE = re.compile(r"([\d,]+)\s*(?:sq\.?\s*ft|sqft|square\s*f(?:eet|oot))", re.IGNORECASE)
_MIN_HOME_PRICE = 50_000  # ignore prices below this (taxes, fees, etc.)
_YEAR_BUILT_RE = re.compile(r"(?:year\s*built|built\s*in|constructed)\s*:?\s*(\d{4})", re.IGNORECASE)
_LIST_DATE_RE = re.compile(
    r"(?:list(?:ed|ing)\s*(?:date|on|since)?|on\s*(?:the\s*)?market(?:\s*since)?|date\s*listed)\s*:?\s*"
    r"(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}|\w+\s+\d{1,2},?\s+\d{4}|\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)
# JSON-LD / structured data patterns for on-market date (OneKeyMLS embeds these)
_JSON_ON_MARKET_RE = re.compile(
    r'"[Oo]n[Mm]arket[Dd]ate"\s*:\s*"(\d{4}-\d{2}-\d{2})',
)
_JSON_LIST_DATE_RE = re.compile(
    r'"(?:[Ll]ist(?:ing)?[Dd]ate|[Dd]ate[Ll]isted)"\s*:\s*"(\d{4}-\d{2}-\d{2})',
)
_JSON_YEAR_BUILT_RE = re.compile(
    r'"[Yy]ear[Bb]uilt"\s*:\s*"?(\d{4})',
)
# Lot size patterns: "0.25 acres", "10,000 sq ft lot", JSON-LD lotSize
# Redfin structured text: "0.45 acres Lot Size" — most reliable from descriptions
_LOT_ACRES_REDFIN_RE = re.compile(
    r"([\d,.]+)\s*acres?\s*Lot\s*Size",
    re.IGNORECASE,
)
# General text: "0.25 acres", "1.22 acres"
# Uses negative lookbehind for / to avoid matching fractions like "1/4 acre"
_LOT_ACRES_TEXT_RE = re.compile(
    r"(?<!/)([\d,.]+)\s*(?:acre|ac)s?\b",
    re.IGNORECASE,
)
_LOT_SQFT_TEXT_RE = re.compile(
    r"([\d,]+)\s*(?:sq\.?\s*ft|sqft|square\s*feet)\s+lot\b",
    re.IGNORECASE,
)
# Redfin JSON-LD: "lotSize":{"@type":"QuantitativeValue","value":0.25,"unitText":"acres"}
# or "lotSize":"10890 sqft"
_JSON_LOT_SIZE_VALUE_RE = re.compile(
    r'"lotSize"\s*:\s*\{[^}]*"value"\s*:\s*([\d.]+)',
)
_JSON_LOT_SIZE_STR_RE = re.compile(
    r'"lotSize"\s*:\s*"([\d,.]+)\s*(acres?|sqft|sq\s*ft|square\s*feet)"',
    re.IGNORECASE,
)


def _search_redfin_url(
    address: str,
    town: str,
    state: str | None = None,
    zip_code: str | None = None,
    mls_id: str | None = None,
) -> str | None:
    """Search DuckDuckGo Lite for a Redfin listing page matching the given address.

    Returns the Redfin URL if found, None otherwise. Best-effort only.
    Uses DDG Lite (lite.duckduckgo.com) which returns plain HTML results
    without JavaScript requirements.
    Including the MLS ID narrows results significantly for exact matches.
    """
    parts = [f"{address}", f"{town}"]
    if state:
        parts.append(state)
    if mls_id:
        parts.append(f"MLS {mls_id}")
    parts.append("redfin")
    query = " ".join(parts)

    try:
        with httpx.Client(
            timeout=_DDG_TIMEOUT,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36"
                ),
            },
        ) as client:
            _ddg_rate_limit()
            response = client.post(_DDG_URL, data={"q": query})

        if response.status_code != 200:
            logger.warning(f"DDG Lite returned {response.status_code} for: {query}")
            return None

        # Find Redfin /home/ URLs directly in the response HTML
        matches = _REDFIN_URL_RE.findall(response.text)
        if not matches:
            logger.info(f"No Redfin URLs found in DDG results for: {query}")
            return None

        # Strict verification. This used to accept a URL if ANY street word
        # over two characters appeared anywhere in it, so "12 Oak Lane" would
        # match "98-Maple-Lane" and attach that house's description and photos.
        for redfin_url in matches:
            full = redfin_url if redfin_url.startswith("http") else f"https://www.{redfin_url}"
            if _redfin_url_matches(full, address, town, zip_code):
                return full

        logger.info(f"Redfin URLs found but none verified for address: {address}")
        return None

    except Exception as e:
        logger.warning(f"DuckDuckGo search failed for {address}: {e}")
        return None


def _search_onekeymls_url(
    address: str,
    town: str,
    state: str | None = None,
    zip_code: str | None = None,
) -> str | None:
    """Search DuckDuckGo for a OneKey MLS listing page matching the given address.

    Returns the OneKey MLS URL if found, None otherwise. Mirrors _search_redfin_url().
    Validates that the street number matches to avoid false positives.
    """
    parts = [address, town]
    if state:
        parts.append(state)
    parts.append("onekeymls")
    query = " ".join(parts)

    try:
        with httpx.Client(
            timeout=_DDG_TIMEOUT,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36"
                ),
            },
        ) as client:
            _ddg_rate_limit()
            # Use DDG HTML endpoint (Lite returns 202 from cloud IPs)
            response = client.post(_DDG_HTML_URL, data={"q": query})

        if response.status_code != 200:
            logger.warning(f"DDG HTML returned {response.status_code} for OneKeyMLS: {query}")
            return None

        matches = _ONEKEYMLS_URL_RE.findall(response.text)
        if not matches:
            logger.info(f"No OneKeyMLS URLs found in DDG results for: {query}")
            return None

        # Strict verification, shared with the Redfin path. This block used to
        # accept any street word over two characters appearing anywhere in the
        # URL — the same false-match bug its twin had.
        for url in matches:
            if _onekeymls_url_matches(url, address, town, zip_code):
                return url

        logger.info(f"OneKeyMLS URLs found but none matched address: {address}")
        return None

    except Exception as e:
        logger.warning(f"DuckDuckGo OneKeyMLS search failed for {address}: {e}")
        return None


# Compact listing stats line: "$1,275,000 3 beds 2 bath 2,167 sqft" or similar
_COMPACT_STATS_RE = re.compile(
    r"\$\s?([\d,]{6,})\s+(\d+)\s*beds?\s+(\d+)\s*(?:bath|ba)\w*\s+([\d,]+)\s*(?:sq\.?\s*ft|sqft)",
    re.IGNORECASE,
)


def _extract_property_stats(html: str) -> dict | None:
    """Extract price/beds/baths/sqft from page HTML text.

    Returns dict with integer values for found fields, or None if nothing found.
    First tries a compact stats pattern (e.g. "$1,275,000 3 beds 2 bath 2,167 sqft")
    which avoids false matches from search filter dropdowns.
    Falls back to individual regex patterns with filtering.
    """
    soup = BeautifulSoup(html, "html.parser")
    # Strip form elements to avoid search filter dropdowns polluting matches
    for tag in soup.find_all(["script", "style", "nav", "footer", "header", "form"]):
        tag.decompose()
    text = soup.get_text(separator=" ", strip=True)

    # Try compact stats pattern first (most reliable)
    result = {}
    compact = _COMPACT_STATS_RE.search(text)
    if compact:
        price = int(compact.group(1).replace(",", ""))
        if price >= _MIN_HOME_PRICE:
            result = {
                "price": price,
                "bedrooms": int(compact.group(2)),
                "bathrooms": int(compact.group(3)),
                "sqft": int(compact.group(4).replace(",", "")),
            }

    # Fallback: individual field extraction with filtering

    for price_match in _PRICE_RE.finditer(text):
        price_str = price_match.group(0).replace("$", "").replace(",", "").strip()
        try:
            price = int(price_str)
            if price >= _MIN_HOME_PRICE:
                result["price"] = price
                break
        except ValueError:
            pass

    beds_match = _STATS_BEDS_RE.search(text)
    if beds_match:
        result["bedrooms"] = int(beds_match.group(1))

    baths_match = _STATS_BATHS_RE.search(text)
    if baths_match:
        result["bathrooms"] = int(baths_match.group(1))

    # SqFt: skip round filter values; pick the largest plausible home size
    # (avoids picking lot sqft like 436 over living area like 5,850)
    _sqft_candidates = []
    for sqft_match in _STATS_SQFT_RE.finditer(text):
        sqft = int(sqft_match.group(1).replace(",", ""))
        if sqft % 500 != 0 and 500 <= sqft <= 30000:
            _sqft_candidates.append(sqft)
    if _sqft_candidates:
        result["sqft"] = max(_sqft_candidates)

    # Year built — try visible text first, then JSON-LD in raw HTML
    year_match = _YEAR_BUILT_RE.search(text)
    if year_match:
        year = int(year_match.group(1))
        if 1700 <= year <= 2030:
            result["year_built"] = year
    if "year_built" not in result:
        json_yb = _JSON_YEAR_BUILT_RE.search(html)
        if json_yb:
            year = int(json_yb.group(1))
            if 1700 <= year <= 2030:
                result["year_built"] = year

    # List date — try visible text first, then JSON-LD in raw HTML
    list_date_match = _LIST_DATE_RE.search(text)
    if list_date_match:
        result["list_date"] = list_date_match.group(1).strip()
    if "list_date" not in result:
        json_ld = _JSON_ON_MARKET_RE.search(html) or _JSON_LIST_DATE_RE.search(html)
        if json_ld:
            result["list_date"] = json_ld.group(1)

    # Lot size — try JSON-LD first (most reliable), then visible text
    # JSON-LD value (numeric, assumed acres for Redfin)
    json_lot_val = _JSON_LOT_SIZE_VALUE_RE.search(html)
    if json_lot_val:
        val = float(json_lot_val.group(1))
        if 0.01 <= val <= 1000:
            result["lot_acres"] = round(val, 4)
    # JSON-LD string form
    if "lot_acres" not in result:
        json_lot_str = _JSON_LOT_SIZE_STR_RE.search(html)
        if json_lot_str:
            val_str = json_lot_str.group(1).replace(",", "")
            unit = json_lot_str.group(2).lower()
            try:
                val = float(val_str)
                if "acre" in unit:
                    if 0.01 <= val <= 1000:
                        result["lot_acres"] = round(val, 4)
                else:
                    # sq ft → acres
                    acres = val / 43560
                    if 0.01 <= acres <= 1000:
                        result["lot_acres"] = round(acres, 4)
            except ValueError:
                pass
    # Visible text: try Redfin structured "X acres Lot Size" first (most reliable)
    if "lot_acres" not in result:
        redfin_match = _LOT_ACRES_REDFIN_RE.search(text)
        if redfin_match:
            try:
                val = float(redfin_match.group(1).replace(",", ""))
                if 0.01 <= val <= 1000:
                    result["lot_acres"] = round(val, 4)
            except ValueError:
                pass
    # Visible text: general "0.25 acres" (with fraction protection)
    if "lot_acres" not in result:
        acres_match = _LOT_ACRES_TEXT_RE.search(text)
        if acres_match:
            try:
                val = float(acres_match.group(1).replace(",", ""))
                if 0.01 <= val <= 1000:
                    result["lot_acres"] = round(val, 4)
            except ValueError:
                pass
    # Visible text: "10,890 sq ft lot"
    if "lot_acres" not in result:
        sqft_lot_match = _LOT_SQFT_TEXT_RE.search(text)
        if sqft_lot_match:
            try:
                sqft = float(sqft_lot_match.group(1).replace(",", ""))
                acres = sqft / 43560
                if 0.01 <= acres <= 1000:
                    result["lot_acres"] = round(acres, 4)
            except ValueError:
                pass

    return result if result else None


def scrape_listing_structured_data(
    address: str | None,
    town: str | None,
    state: str | None = None,
    zip_code: str | None = None,
) -> dict | None:
    """Search for a listing on OneKey MLS and extract structured property data.

    Returns dict with keys like price, bedrooms, bathrooms, sqft — or None.
    """
    if not address or not town:
        return None

    onekeymls_url = _search_onekeymls_url(address, town, state, zip_code)
    if not onekeymls_url:
        return None

    logger.info(f"Fetching structured data from OneKeyMLS: {onekeymls_url}")
    try:
        with httpx.Client(
            timeout=_SCRAPE_TIMEOUT,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/121.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
            },
        ) as client:
            response = client.get(onekeymls_url)
            response.raise_for_status()

        stats = _extract_property_stats(response.text)
        if stats:
            logger.info(f"Extracted structured data from OneKeyMLS: {stats}")
        return stats

    except Exception as e:
        logger.warning(f"Failed to fetch OneKeyMLS page {onekeymls_url}: {e}")
        return None


def _extract_description_from_html(
    html: str, url: str, method: str
) -> str | None:
    """Extract property description from HTML content.

    Shared logic for both static and Playwright-rendered HTML.
    Collects all qualifying text blocks and returns the longest one,
    which is typically the most informative (full narrative description
    rather than a short features list).
    """
    soup = BeautifulSoup(html, "html.parser")

    # Remove script/style elements that pollute text extraction
    for tag in soup.find_all(["script", "style", "nav", "footer", "header"]):
        tag.decompose()

    # Collect candidates from targeted CSS selectors first.
    # Selectors are site-specific and precise — prefer them over keyword fallback
    # so that navigation/UI text (which also contains real estate keywords) never
    # beats the actual description block.
    selector_candidates: list[tuple[str, str]] = []
    for selector in _DESCRIPTION_SELECTORS:
        elements = soup.select(selector)
        for el in elements:
            text = el.get_text(separator=" ", strip=True)
            if len(text) >= 50 and _has_useful_content(text):
                selector_candidates.append((text, f"selector: {selector}"))

    if selector_candidates:
        # Use the longest selector match — don't fall through to keyword search
        best_text, best_source = max(selector_candidates, key=lambda c: len(c[0]))
        logger.info(
            f"Scraped description ({len(best_text)} chars) from {url} "
            f"via {method}, {best_source}"
        )
        return best_text[:_MAX_DESCRIPTION_LEN]

    # Keyword fallback: scan all block-level elements for real estate content.
    # Only reached when no targeted selector matched (unknown site structure).
    keyword_candidates: list[tuple[str, str]] = []
    for tag in soup.find_all(["p", "div", "section", "td", "span"]):
        text = tag.get_text(separator=" ", strip=True)
        if len(text) >= 80 and _has_useful_content(text):
            keyword_candidates.append((text, "keyword fallback"))

    if not keyword_candidates:
        logger.warning(f"No description candidates found in {len(html)} chars from {url[:80]} via {method}")
        return None

    best_text, best_source = max(keyword_candidates, key=lambda c: len(c[0]))
    logger.info(
        f"Scraped description ({len(best_text)} chars) from {url} "
        f"via {method}, {best_source}"
    )
    return best_text[:_MAX_DESCRIPTION_LEN]


def _extract_image_urls(html: str, page_url: str) -> list[str]:
    """Extract listing photo URLs from HTML content.

    Returns a deduplicated list of image URLs (full-size photos only,
    skipping icons and thumbnails).
    """
    soup = BeautifulSoup(html, "html.parser")
    seen = set()
    images = []

    # Try targeted selectors first
    for selector in _IMAGE_SELECTORS:
        for img in soup.select(selector):
            src = img.get("src") or img.get("data-src") or ""
            if not src or not src.startswith(("http://", "https://")):
                continue

            # Skip small images (likely icons/thumbnails)
            width = img.get("width", "")
            if width and width.isdigit() and int(width) < _MIN_IMAGE_WIDTH:
                continue

            # Skip common non-photo patterns
            src_lower = src.lower()
            if any(skip in src_lower for skip in [
                "icon", "logo", "avatar", "sprite", "placeholder",
                "1x1", "pixel", "spacer", "blank",
                "badge", "flag", "footer", "app-download",
                "equal-housing",
            ]):
                continue

            if src not in seen:
                seen.add(src)
                images.append(src)

    # Enumerate full Redfin CDN image set if seed images are from Redfin
    if images and any("ssl.cdn-redfin.com" in u for u in images):
        enumerated = enumerate_redfin_images(images)
        if len(enumerated) > len(images):
            images = enumerated

    if images:
        logger.info(f"Extracted {len(images)} image URLs from {page_url}")
    return images


def _has_useful_content(text: str) -> bool:
    """Check if text contains real estate description keywords."""
    text_lower = text.lower()
    return any(kw in text_lower for kw in _DESCRIPTION_KEYWORDS)


# ---------------------------------------------------------------------------
# OneKey MLS listing status extraction
# ---------------------------------------------------------------------------

_MLS_STATUS_RE = re.compile(
    r'"(?:SaleStatus|MlsStatus)"\s*:\s*"([^"]+)"',
    re.IGNORECASE,
)


def _extract_listing_status(html: str) -> str | None:
    """Extract listing status from OneKey MLS page HTML.

    OneKey MLS pages embed structured JSON with SaleStatus/MlsStatus fields
    (e.g. "Active", "Sold", "Pending", "Closed", "Under Contract").
    Returns the first match or None.
    """
    match = _MLS_STATUS_RE.search(html)
    if match:
        return match.group(1)
    return None


def check_listing_status(
    address: str,
    town: str,
    state: str | None = None,
    zip_code: str | None = None,
) -> str | None:
    """Search OneKey MLS for a listing and return its status string.

    Uses DDG to find the OneKey MLS page, fetches it via static HTTP,
    and extracts the SaleStatus/MlsStatus field.
    Returns status string (e.g. "Active", "Sold", "Pending") or None.
    """
    if not address or not town:
        return None

    onekeymls_url = _search_onekeymls_url(address, town, state, zip_code)
    if not onekeymls_url:
        return None

    logger.info(f"Checking listing status from OneKeyMLS: {onekeymls_url}")
    try:
        with httpx.Client(
            timeout=_SCRAPE_TIMEOUT,
            follow_redirects=True,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/121.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "en-US,en;q=0.5",
            },
        ) as client:
            response = client.get(onekeymls_url)
            response.raise_for_status()

        status = _extract_listing_status(response.text)
        if status:
            logger.info(f"OneKeyMLS status for {address}, {town}: {status}")
        else:
            logger.info(f"No status found on OneKeyMLS page for {address}, {town}")
        return status

    except Exception as e:
        logger.warning(f"Failed to check OneKeyMLS status for {address}, {town}: {e}")
        return None
