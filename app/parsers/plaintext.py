"""Parser for plain text listing alerts.

Handles emails where listings are described in plain text with
patterns like "$1,295,000", "4 bd", "3 ba", "2,437 sqft", "MLS #964038".
"""

import re

from app.models import ParsedListing
from app.parsers.base import EmailParser

PRICE_RE = re.compile(r"\$\s*([\d,]+(?:\.\d{2})?)")
BEDS_RE = re.compile(r"(\d+)\s*(?:bd|bed|bedroom)s?", re.IGNORECASE)
BATHS_RE = re.compile(r"(\d+)\s*(?:ba|bath|bathroom)s?", re.IGNORECASE)
SQFT_RE = re.compile(r"([\d,]+)\s*(?:sq\s*\.?\s*ft|sqft|SF)", re.IGNORECASE)
MLS_RE = re.compile(r"MLS\s*#?\s*(\d+)", re.IGNORECASE)
ZIP_RE = re.compile(r"\b(\d{5})(?:-\d{4})?\b")

# Street suffixes for address matching
_SUFFIXES = r"(?:Street|St|Avenue|Ave|Lane|Ln|Drive|Dr|Road|Rd|Court|Ct|Place|Pl|Way|Circle|Cir|Boulevard|Blvd|Terrace|Ter)"

# Street address on its own line: "11 Jennifer Lane"
STREET_RE = re.compile(
    rf"^\s*(\d+\s+[\w\s.]+{_SUFFIXES}\.?)\s*$",
    re.IGNORECASE | re.MULTILINE,
)

# City, State ZIP on its own line: "Rye Brook, New York 10573"
CITY_STATE_ZIP_RE = re.compile(
    r"^\s*([A-Za-z][\w\s]*?),\s*([A-Za-z][\w\s]*?)\s+(\d{5}(?:-\d{4})?)\s*$",
    re.MULTILINE,
)

# Inline address: "31 Lalli Dr, Katonah, NY 10536" (Redfin email format)
# Uses [^\n,]+ to prevent matching across lines or past commas
INLINE_ADDR_RE = re.compile(
    rf"(\d+[^\n,]+{_SUFFIXES}\.?)\s*,\s*([A-Za-z][^\n,]*?)\s*,\s*([A-Z]{{2}})\s+(\d{{5}}(?:-\d{{4}})?)",
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

# Detect if text has listing-like content
LISTING_INDICATORS = [PRICE_RE, MLS_RE, BEDS_RE]


class PlainTextParser(EmailParser):
    def can_parse(self, html: str | None, text: str | None) -> bool:
        if not text:
            return False
        matches = sum(1 for pattern in LISTING_INDICATORS if pattern.search(text))
        return matches >= 2

    def parse(self, html: str | None, text: str | None) -> list[ParsedListing]:
        if not text:
            return []

        # Try to split into listing blocks (double newline or numbered list)
        blocks = self._split_into_blocks(text)

        listings = []
        for block in blocks:
            listing = self._parse_block(block)
            if listing and (listing.price or listing.mls_id):
                listings.append(listing)

        # If no blocks found, try parsing the whole text as one listing
        if not listings:
            listing = self._parse_block(text)
            if listing and (listing.price or listing.mls_id):
                listings.append(listing)

        return listings

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

        return [text]

    def _parse_block(self, block: str) -> ParsedListing | None:
        listing = ParsedListing(source_format="plaintext")

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
                listing.address = inline_match.group(1).strip()
                listing.town = inline_match.group(2).strip()
                listing.state = inline_match.group(3).strip()
                listing.zip_code = inline_match.group(4).strip()

        # Last resort: just grab the zip code
        if not listing.town and not listing.address:
            zip_match = ZIP_RE.search(block)
            if zip_match:
                listing.zip_code = zip_match.group(1)

        return listing
