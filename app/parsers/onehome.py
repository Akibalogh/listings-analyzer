"""Parser for OneHome/Matrix MLS email alerts.

Extracts listings from the HTML structure used by OneKey MLS NY
(mlsalerts.example.com). The email contains listing cards with
predictable CSS classes.
"""

import re

from bs4 import BeautifulSoup

from app.models import ParsedListing
from app.parsers.base import EmailParser

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
