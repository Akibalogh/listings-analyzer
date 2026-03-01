"""Parser for OneHome/Matrix MLS email alerts.

Extracts listings from the HTML structure used by OneKey MLS NY
(mlsalerts.example.com). The email contains listing cards with
predictable CSS classes.

Also provides scrape_listing_description() to fetch the full listing
page and extract the property description (basement, features, etc.).

Supports both static HTML pages and JavaScript SPAs:
- Static HTTP scrape for server-rendered pages (Redfin, etc.)
- Jina Reader API (r.jina.ai) for SPA pages (OneHome Angular portal)
"""

import logging
import re

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
            listing.property_type = title_el.get_text(strip=True)

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
    # OneHome / Matrix portal common patterns
    '[data-testid="listing-description"]',
    '[class*="description"]',
    '[class*="remarks"]',
    '[id*="description"]',
    '[id*="remarks"]',
    '[class*="property-details"]',
    '[class*="listing-detail"]',
    # Redfin-specific patterns
    '#marketing-remarks-scroll',
    '.remarks-container',
    '.keyDetailsList',
    '[data-rf-test-id="listingRemarks"]',
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
        logger.info(f"OneHome URL detected, skipping to Redfin lookup: {url[:80]}")
        return _try_redfin_fallback(address, town, state, zip_code, mls_id)

    # --- Redfin URLs: try static HTTP first (server-renders with browser UA),
    #     fall back to Jina Reader if static fails ---
    if "redfin.com" in url_lower:
        logger.info(f"Redfin URL detected, trying static HTTP: {url[:80]}")
        result = _scrape_static(url)
        if result and result[0]:
            return result
        logger.info(f"Static scrape failed for Redfin, trying Jina Reader: {url[:80]}")
        result = _scrape_with_jina(url)
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


def _try_redfin_fallback(
    address: str | None,
    town: str | None,
    state: str | None,
    zip_code: str | None,
    mls_id: str | None = None,
) -> tuple[str | None, list[str]]:
    """Search DuckDuckGo for a Redfin URL and scrape it via Jina Reader."""
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


def _scrape_static(url: str) -> tuple[str | None, list[str]] | None:
    """Fast static HTTP scrape — works for server-rendered pages (Redfin, etc.)."""
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
            response = client.get(url)
            response.raise_for_status()

        html = response.text
        description = _extract_description_from_html(html, url, "static")
        images = _extract_image_urls(html, url) if description else []
        return description, images

    except httpx.HTTPStatusError as e:
        logger.warning(f"HTTP {e.response.status_code} fetching listing page: {url}")
        return None
    except Exception as e:
        logger.warning(f"Failed to scrape listing page {url}: {e}")
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
# DuckDuckGo Redfin URL search — fallback when primary URL scrape fails
# ---------------------------------------------------------------------------

_DDG_URL = "https://lite.duckduckgo.com/lite/"
_DDG_TIMEOUT = 10.0
_REDFIN_URL_RE = re.compile(r"https?://www\.redfin\.com/[^\s\"<>&]+/home/\d+")


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

    candidates: list[tuple[str, str]] = []  # (text, source_label)

    # Collect candidates from targeted CSS selectors
    for selector in _DESCRIPTION_SELECTORS:
        elements = soup.select(selector)
        for el in elements:
            text = el.get_text(separator=" ", strip=True)
            if len(text) >= 50 and _has_useful_content(text):
                candidates.append((text, f"selector: {selector}"))

    # Collect candidates from keyword fallback (broader search)
    for tag in soup.find_all(["p", "div", "section", "td", "span"]):
        text = tag.get_text(separator=" ", strip=True)
        if len(text) >= 80 and _has_useful_content(text):
            candidates.append((text, "keyword fallback"))

    if not candidates:
        logger.warning(f"No description candidates found in {len(html)} chars from {url[:80]} via {method}")
        return None

    # Pick the longest qualifying block — longer text is typically the
    # full narrative description rather than a short features list
    best_text, best_source = max(candidates, key=lambda c: len(c[0]))
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
            ]):
                continue

            if src not in seen:
                seen.add(src)
                images.append(src)

    if images:
        logger.info(f"Extracted {len(images)} image URLs from {page_url}")
    return images


def _has_useful_content(text: str) -> bool:
    """Check if text contains real estate description keywords."""
    text_lower = text.lower()
    return any(kw in text_lower for kw in _DESCRIPTION_KEYWORDS)
