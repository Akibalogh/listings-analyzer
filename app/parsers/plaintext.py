"""Parser for plain text listing alerts.

Handles emails where listings are described in plain text with
patterns like "$1,295,000", "4 bd", "3 ba", "2,437 sqft", "MLS #964038".
"""

import re
from datetime import datetime

from app.models import ParsedListing
from app.parsers.base import EmailParser

PRICE_RE = re.compile(r"\$\s*([\d,]+(?:\.\d{2})?)")
BEDS_RE = re.compile(r"(\d+)\s*(?:bd|bed|bedroom)s?", re.IGNORECASE)
BATHS_RE = re.compile(r"(\d+)\s*(?:ba|bath|bathroom)s?", re.IGNORECASE)
SQFT_RE = re.compile(r"([\d,]+)\s*(?:sq\s*\.?\s*ft|sqft|SF)", re.IGNORECASE)
MLS_RE = re.compile(r"MLS\s*#?\s*(\d+)", re.IGNORECASE)
ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")

# ---------------------------------------------------------------------------
# Property type extraction
# ---------------------------------------------------------------------------

# Redfin / MLS plaintext patterns:
#   "Style: Colonial"  "Type: Single Family Residential"
#   "Single Family"  "Condo"  "Townhouse"  in standalone or labeled lines
_PROPERTY_TYPE_LABELED_RE = re.compile(
    r"(?:style|type|property\s+type|home\s+type)\s*:\s*"
    r"([\w][\w\s/\-]{1,50})",
    re.IGNORECASE,
)
# Standalone mention (whole line or comma-separated field) — only match known terms
# to avoid false positives from description prose
_PROPERTY_TYPE_STANDALONE_RE = re.compile(
    r"(?m)^\s*"
    r"(single[\s\-]family(?:\s+residential)?|single[\s\-]family\s+home|"
    r"condo(?:minium)?|co[\s\-]?op|townhouse|townhome|"
    r"multi[\s\-]?family|multifamily|two[\s\-]family|"
    r"mobile[\s\-]home|manufactured[\s\-]home|"
    r"residential|colonial|ranch|cape\s+cod|split[\s\-]level|"
    r"contemporary|victorian|tudor|farmhouse|craftsman)"
    r"\s*$",
    re.IGNORECASE,
)

# Canonical mapping: normalize raw extracted strings → standard values
_PROPERTY_TYPE_CANONICAL = {
    # Single family variants
    "single family residential": "Single Family Residential",
    "single family home": "Single Family Residential",
    "single family": "Single Family Residential",
    "single-family residential": "Single Family Residential",
    "single-family home": "Single Family Residential",
    "single-family": "Single Family Residential",
    "residential": "Single Family Residential",
    # Condo / Co-op
    "condominium": "Condo/Co-op",
    "condo": "Condo/Co-op",
    "co-op": "Condo/Co-op",
    "co op": "Condo/Co-op",
    "coop": "Condo/Co-op",
    # Townhouse
    "townhouse": "Townhouse",
    "townhome": "Townhouse",
    # Multi-family
    "multi-family": "Multi-Family",
    "multi family": "Multi-Family",
    "multifamily": "Multi-Family",
    "two-family": "Multi-Family",
    "two family": "Multi-Family",
    # Style labels — map to Single Family as best approximation unless more info
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
    # Mobile/manufactured
    "mobile home": "Mobile/Manufactured Home",
    "manufactured home": "Mobile/Manufactured Home",
    "mobile-home": "Mobile/Manufactured Home",
    "manufactured-home": "Mobile/Manufactured Home",
}


def _canonicalize_property_type(raw: str) -> str | None:
    """Normalize a raw property type string to a canonical value.

    Returns a canonical string (e.g. "Single Family Residential") or the
    title-cased raw value if no mapping is found but the string is not blank.
    """
    if not raw:
        return None
    normalized = raw.strip().lower()
    # Try exact match first
    if normalized in _PROPERTY_TYPE_CANONICAL:
        return _PROPERTY_TYPE_CANONICAL[normalized]
    # Try prefix match (handles "Single Family Residential - Detached" etc.)
    for key, canonical in _PROPERTY_TYPE_CANONICAL.items():
        if normalized.startswith(key):
            return canonical
    # Return title-cased raw value — better than None for coverage
    return raw.strip().title()


# ---------------------------------------------------------------------------
# List date extraction
# ---------------------------------------------------------------------------

# Patterns for extracting list date from email body text
# Covers "Listed: 01/15/2025", "Date Listed: January 15, 2025",
# "Active since: Jan 15, 2025", "On market since: 01/15/2025"
_LIST_DATE_EMAIL_RE = re.compile(
    r"(?:listed\s*(?:date|on|since)?|date\s+listed|active\s+since|"
    r"on\s+(?:the\s+)?market\s*(?:since)?)\s*:?\s*"
    r"(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}"          # MM/DD/YYYY or MM-DD-YYYY
    r"|[A-Za-z]+\.?\s+\d{1,2},?\s+\d{4}"        # January 15, 2025 or Jan 15 2025
    r"|\d{4}-\d{2}-\d{2})",                       # YYYY-MM-DD
    re.IGNORECASE,
)

# Redfin compact metadata style: "Jan 15" (no year) — needs special handling
_LIST_DATE_SHORT_RE = re.compile(
    r"(?:listed\s*(?:on|since)?|active\s+since|date\s+listed)\s*:?\s*"
    r"([A-Za-z]+\.?\s+\d{1,2})\b(?!\s*,?\s*\d{4})",
    re.IGNORECASE,
)

_MONTH_ABBREVS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
    "january": 1, "february": 2, "march": 3, "april": 4, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


def _parse_list_date(raw: str) -> str | None:
    """Convert a raw date string captured from email body to ISO YYYY-MM-DD.

    Handles:
    - YYYY-MM-DD  → returned as-is
    - MM/DD/YYYY or MM-DD-YYYY
    - MM/DD/YY (2-digit year, assumed 20xx)
    - Month DD, YYYY or Mon DD, YYYY
    Returns None on parse failure.
    """
    raw = raw.strip()
    if not raw:
        return None

    # Already ISO
    if re.match(r"^\d{4}-\d{2}-\d{2}$", raw):
        return raw

    # Numeric: MM/DD/YYYY or MM-DD-YYYY or MM/DD/YY
    m = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})$", raw)
    if m:
        mm, dd, yy = int(m.group(1)), int(m.group(2)), int(m.group(3))
        yyyy = yy + 2000 if yy < 100 else yy
        try:
            return datetime(yyyy, mm, dd).strftime("%Y-%m-%d")
        except ValueError:
            return None

    # Text: "January 15, 2025" or "Jan 15, 2025" or "Jan 15 2025"
    for fmt in ("%B %d, %Y", "%B %d %Y", "%b %d, %Y", "%b %d %Y",
                "%B. %d, %Y", "%b. %d, %Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            pass

    return None


# ---------------------------------------------------------------------------
# Lot size extraction from email body
# ---------------------------------------------------------------------------

# Patterns for lot size in email text: "0.25 acres", "0.25 ac", "10,890 sq ft lot"
# Uses negative lookbehind for / to avoid matching fractions like "1/4 acre" → 4
_LOT_ACRES_EMAIL_RE = re.compile(
    r"(?<!/)([\d,.]+)\s*(?:acre|ac)s?\b",
    re.IGNORECASE,
)
_LOT_SQFT_EMAIL_RE = re.compile(
    r"([\d,]+)\s*(?:sq\.?\s*ft|sqft|square\s+feet)\s+lot\b",
    re.IGNORECASE,
)

# Street suffixes for address matching
_SUFFIXES = r"(?:Street|St|Avenue|Ave|Lane|Ln|Drive|Dr|Road|Rd|Court|Ct|Place|Pl|Way|Circle|Cir|Boulevard|Blvd|Terrace|Ter)"
_DIRECTIONS = r"(?:\s+(?:N|S|E|W|NE|NW|SE|SW))?"

# Listing status labels (Redfin alert prefixes)
_STATUS_LABELS = (
    "New Listing", "Pending", "Coming Soon", "New Favorite",
    "Price Drop", "Price Decreased", "Price Increased",
    "Back on Market", "Sold", "Contingent", "Under Contract",
    "Active", "Open House",
    "New Tour Insight", "Updated MLS Listing",
)
STATUS_PREFIX_RE = re.compile(
    r"^\s*(" + "|".join(re.escape(s) for s in _STATUS_LABELS) + r")\s*$",
    re.IGNORECASE | re.MULTILINE,
)
# Redfin tour headers: "2 homes on this tour", "3 homes on this tour", etc.
TOUR_HEADER_RE = re.compile(
    r"^\s*\d+\s+homes?\s+on\s+this\s+tour\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# Street address on its own line: "11 Jennifer Lane" or "101 Long Hill Rd E"
STREET_RE = re.compile(
    rf"^\s*(\d+\s+[\w\s.]+{_SUFFIXES}\.?{_DIRECTIONS})\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# City, State ZIP on its own line: "Rye Brook, New York 10573"
CITY_STATE_ZIP_RE = re.compile(
    r"^\s*([A-Za-z][\w\s]*?),\s*([A-Za-z][\w\s]*?)\s+(\d{5}(?:-\d{4})?)\s*$",
    re.MULTILINE,
)

# Inline address: "31 Lalli Dr, Katonah, NY 10536" or "101 Long Hill Rd E, ..."
# Uses [^\n,]+ to prevent matching across lines or past commas
INLINE_ADDR_RE = re.compile(
    rf"(\d+[^\n,]+{_SUFFIXES}\.?{_DIRECTIONS})\s*,\s*([A-Za-z][^\n,]*?)\s*,\s*([A-Z]{{2}})\s+(\d{{5}}(?:-\d{{4}})?)",
    re.IGNORECASE,
)

# Listing URLs — only actual listing/property pages, not tours/checkout/blog
LISTING_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:"
    r"redfin\.com/[A-Z]{2}/[^/]+/[^/]+/\S+"  # redfin.com/NY/City/Address/home/123
    r"|onekeymls\.com/listing/\S+"
    r"|onehome\.com/\S+"
    r")",
    re.IGNORECASE,
)

# Extract address from Redfin URL: /NY/Briarcliff-Manor/101-Long-Hill-Rd-E-10510/home/123
REDFIN_URL_ADDR_RE = re.compile(
    r"redfin\.com/([A-Z]{2})/([^/]+)/([^/]+?)(?:-(\d{5}))?/home/",
    re.IGNORECASE,
)

# Detect if text has listing-like content
LISTING_INDICATORS = [PRICE_RE, MLS_RE, BEDS_RE]


class PlainTextParser(EmailParser):
    def can_parse(self, html: str | None, text: str | None) -> bool:
        if not text:
            return False
        # Accept emails with 2+ listing indicators (price, MLS, beds)
        matches = sum(1 for pattern in LISTING_INDICATORS if pattern.search(text))
        if matches >= 2:
            return True
        # Also accept emails with bare Redfin/listing URLs
        if LISTING_URL_RE.search(text):
            return True
        return False

    def parse(self, html: str | None, text: str | None) -> list[ParsedListing]:
        if not text:
            return []

        # Try to split into listing blocks (double newline or numbered list)
        blocks = self._split_into_blocks(text)

        listings = []
        for block in blocks:
            listing = self._parse_block(block)
            if listing and (listing.price or listing.mls_id or listing.address):
                listings.append(listing)

        # If no blocks found, try parsing the whole text as one listing
        if not listings:
            listing = self._parse_block(text)
            if listing and (listing.price or listing.mls_id or listing.address):
                listings.append(listing)

        # Backfill missing listing URLs from HTML
        if html:
            available = self._extract_urls_from_html(html)
            for listing in listings:
                if listing.listing_url or not listing.address or not available:
                    continue
                addr_slug = listing.address.strip().replace(".", "").replace(" ", "-")
                for url in available:
                    if addr_slug.lower() in url.lower():
                        listing.listing_url = url
                        available.remove(url)
                        break

        return listings

    @staticmethod
    def _extract_urls_from_html(html: str) -> list[str]:
        """Extract unique listing URLs from HTML content."""
        seen: set[str] = set()
        result: list[str] = []
        for m in LISTING_URL_RE.finditer(html):
            clean = m.group(0).split('"')[0].split("'")[0].split(">")[0].rstrip(".,;:)")
            if clean not in seen:
                seen.add(clean)
                result.append(clean)
        return result

    def _split_into_blocks(self, text: str) -> list[str]:
        """Split text into potential listing blocks."""
        # Try splitting by double newline
        blocks = re.split(r"\n\s*\n", text)
        # Filter to blocks that have at least a price or MLS
        listing_blocks = [
            b for b in blocks
            if PRICE_RE.search(b) or MLS_RE.search(b)
        ]
        if len(listing_blocks) > 1:
            return listing_blocks

        # Try splitting by numbered list (1. or 1))
        blocks = re.split(r"\n\s*\d+[.)]\s+", text)
        listing_blocks = [
            b for b in blocks
            if PRICE_RE.search(b) or MLS_RE.search(b)
        ]
        if len(listing_blocks) > 1:
            return listing_blocks

        # Bare URL list: split by newline, keep lines that are just URLs
        lines = [ln.strip() for ln in text.split("\n") if ln.strip()]
        url_lines = [ln for ln in lines if LISTING_URL_RE.search(ln)]
        if len(url_lines) > 1:
            return url_lines

        return [text]

    def _parse_block(self, block: str) -> ParsedListing | None:
        listing = ParsedListing(source_format="plaintext")

        # Strip status prefix (e.g., "New Listing", "Pending") before parsing
        status_match = STATUS_PREFIX_RE.search(block)
        if status_match:
            listing.listing_status = status_match.group(1).strip()
            block = STATUS_PREFIX_RE.sub("", block).strip()

        # Strip tour headers (e.g., "2 homes on this tour")
        block = TOUR_HEADER_RE.sub("", block).strip()

        price_match = PRICE_RE.search(block)
        if price_match:
            listing.price = int(price_match.group(1).replace(",", "").split(".")[0])

        beds_match = BEDS_RE.search(block)
        if beds_match:
            listing.bedrooms = int(beds_match.group(1))

        baths_match = BATHS_RE.search(block)
        if baths_match:
            listing.bathrooms = int(baths_match.group(1))

        sqft_match = SQFT_RE.search(block)
        if sqft_match:
            listing.sqft = int(sqft_match.group(1).replace(",", ""))

        mls_match = MLS_RE.search(block)
        if mls_match:
            listing.mls_id = mls_match.group(1)

        # Try to find listing URL
        url_match = LISTING_URL_RE.search(block)
        if url_match:
            listing.listing_url = url_match.group(0).rstrip(".,;:)")

        # Try to find address — first try standalone line, then inline format
        street_match = STREET_RE.search(block)
        if street_match:
            listing.address = street_match.group(1).strip()

        city_match = CITY_STATE_ZIP_RE.search(block)
        if city_match:
            listing.town = city_match.group(1).strip()
            listing.state = city_match.group(2).strip()
            listing.zip_code = city_match.group(3).strip()

        # Fallback: inline address format ("31 Lalli Dr, Katonah, NY 10536")
        if not listing.address:
            inline_match = INLINE_ADDR_RE.search(block)
            if inline_match:
                raw_addr = inline_match.group(1).strip()
                raw_town = inline_match.group(2).strip()

                # Fix pipe-separator: "Suite 100| White Plains" → address gets ", Suite 100",
                # town gets "White Plains". The pipe occurs when a suite/unit qualifier
                # is appended to the address with a pipe before the town name.
                if "|" in raw_town:
                    suite_part, _, town_part = raw_town.partition("|")
                    suite_part = suite_part.strip()
                    town_part = town_part.strip()
                    if suite_part:
                        raw_addr = f"{raw_addr}, {suite_part}"
                    raw_town = town_part

                listing.address = raw_addr
                listing.town = raw_town
                listing.state = inline_match.group(3).strip()
                listing.zip_code = inline_match.group(4).strip()

        # Fallback: extract address from Redfin URL path
        if not listing.address and listing.listing_url:
            redfin_match = REDFIN_URL_ADDR_RE.search(listing.listing_url)
            if redfin_match:
                listing.state = listing.state or redfin_match.group(1).upper()
                listing.town = listing.town or redfin_match.group(2).replace("-", " ").title()
                # Address slug: "101-Long-Hill-Rd-E-10510" → "101 Long Hill Rd E"
                addr_slug = redfin_match.group(3).replace("-", " ")
                # Strip trailing zip if present in slug
                if redfin_match.group(4):
                    listing.zip_code = listing.zip_code or redfin_match.group(4)
                listing.address = addr_slug.title()

        # Last resort: just grab the zip code
        if not listing.town and not listing.address:
            zip_match = ZIP_RE.search(block)
            if zip_match:
                listing.zip_code = zip_match.group(1)

        # Property type — labeled first ("Style: Colonial"), then standalone line
        pt_match = _PROPERTY_TYPE_LABELED_RE.search(block)
        if pt_match:
            listing.property_type = _canonicalize_property_type(pt_match.group(1))
        else:
            pt_standalone = _PROPERTY_TYPE_STANDALONE_RE.search(block)
            if pt_standalone:
                listing.property_type = _canonicalize_property_type(pt_standalone.group(1))

        # List date — look for "Listed:", "Date Listed:", "Active since:", etc.
        ld_match = _LIST_DATE_EMAIL_RE.search(block)
        if ld_match:
            parsed_date = _parse_list_date(ld_match.group(1))
            if parsed_date:
                listing.list_date = parsed_date

        # Lot size — extract from email body text if present
        if listing.lot_acres is None:
            lot_acres_match = _LOT_ACRES_EMAIL_RE.search(block)
            if lot_acres_match:
                try:
                    val = float(lot_acres_match.group(1).replace(",", ""))
                    if 0.01 <= val <= 1000:
                        listing.lot_acres = round(val, 4)
                except ValueError:
                    pass
        if listing.lot_acres is None:
            lot_sqft_match = _LOT_SQFT_EMAIL_RE.search(block)
            if lot_sqft_match:
                try:
                    sqft = float(lot_sqft_match.group(1).replace(",", ""))
                    acres = sqft / 43560
                    if 0.01 <= acres <= 1000:
                        listing.lot_acres = round(acres, 4)
                except ValueError:
                    pass

        return listing
