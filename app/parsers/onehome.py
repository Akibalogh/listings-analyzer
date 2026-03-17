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

import httpx
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

    url_lower = url.lower()

    # --- OneHome URLs: Angular SPA, static + Jina always return empty shell ---
    if "onehome.com" in url_lower:
        logger.info(f"OneHome URL detected, skipping to MLS lookup: {url[:80]}")
        # Try OneKey MLS directly (constructs URL from MLS ID — no DDG needed)
        if mls_id and address and town:
            result = _try_onekeymls(address, town, state, zip_code, mls_id)
            if result and result[0]:
                return result
        return _try_redfin_fallback(address, town, state, zip_code, mls_id)

    # --- Redfin URLs: try with retries + better fallbacks ---
    if "redfin.com" in url_lower:
        logger.info(f"Redfin URL detected, trying static HTTP with retries: {url[:80]}")
        # Try static with 2 attempts (different User-Agent each time)
        for attempt in range(1, 3):
            result = _scrape_static(url, attempt=attempt)
            if result and result[0]:
                return result

        logger.info(f"Static scrape failed for Redfin, trying Jina Reader: {url[:80]}")
        result = _scrape_with_jina(url)
        if result and result[0]:
            return result

        # Redfin blocked (common from cloud IPs) — fall back to OneKey MLS
        if mls_id and address and town:
            logger.info(f"Redfin blocked, trying OneKey MLS for MLS#{mls_id}")
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

    logger.info(f"Trying OneKey MLS: {url}")
    result = _scrape_static(url)
    if result and result[0]:
        return result

    logger.info(f"OneKey MLS static failed, trying Jina: {url}")
    result = _scrape_with_jina(url)
    if result and result[0]:
        return result

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
    """Detect if Redfin returned a bot-blocking page (e.g., 'Unknown address')."""
    soup = BeautifulSoup(html, "html.parser")
    text_lower = soup.get_text().lower()
    # Check for common bot-block indicators
    indicators = [
        "unknown address",
        "address not found",
        "not found",
        "access denied",
        "please try again",
        "bot",
        "automated",
    ]
    for indicator in indicators:
        if indicator in text_lower:
            return True
    return False


def _scrape_static(url: str, attempt: int = 1) -> tuple[str | None, list[str]] | None:
    """Fast static HTTP scrape — works for server-rendered pages (Redfin, etc.).

    Args:
        url: URL to scrape
        attempt: Attempt number (used for delays and logging)

    Returns:
        (description, images) tuple or None if failed
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

        # Check for bot-block pages
        if _is_bot_block_page(html):
            logger.warning(f"Detected bot-block page for {url[:80]} (attempt {attempt})")
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
_STATS_BEDS_RE = re.compile(r"(\d+)\s*(?:bed(?:room)?s?|bd)", re.IGNORECASE)
_STATS_BATHS_RE = re.compile(r"(\d+)\s*(?:bath(?:room)?s?|ba)", re.IGNORECASE)
_STATS_SQFT_RE = re.compile(r"([\d,]+)\s*(?:sq\.?\s*ft|sqft|square\s*feet)", re.IGNORECASE)
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

        # Verify the URL contains a word from the street address (avoid false matches)
        # Use the street number as the most specific identifier
        street_parts = address.split() if address else []
        for redfin_url in matches:
            url_lower = redfin_url.lower()
            # Check street number match (most reliable)
            if street_parts and street_parts[0].isdigit():
                if f"/{street_parts[0]}-" in url_lower:
                    return redfin_url
            # Check street name match (fallback)
            for part in street_parts[1:]:
                if len(part) > 2 and part.lower() in url_lower:
                    return redfin_url

        logger.info(f"Redfin URLs found but none matched address: {address}")
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

        # Validate street number match
        street_parts = address.split() if address else []
        for url in matches:
            url_lower = url.lower()
            if street_parts and street_parts[0].isdigit():
                if f"/{street_parts[0]}-" in url_lower or f"/{street_parts[0]}." in url_lower:
                    return url
            # Fallback: check street name
            for part in street_parts[1:]:
                if len(part) > 2 and part.lower() in url_lower:
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

    # SqFt: skip round filter values
    for sqft_match in _STATS_SQFT_RE.finditer(text):
        sqft = int(sqft_match.group(1).replace(",", ""))
        if sqft % 500 != 0:
            result["sqft"] = sqft
            break

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
