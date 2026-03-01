"""LLM fallback parser using Claude Haiku for structured extraction.

Used when no deterministic parser can handle the email format.
"""

import json
import logging

import anthropic

from app.config import settings
from app.models import ParsedListing
from app.parsers.base import EmailParser

logger = logging.getLogger(__name__)

EXTRACTION_PROMPT = """Extract all real estate listings from this email content.
For each listing, return a JSON object with these fields (use null if not found):

- address: street address
- town: city/town name
- state: state name
- zip_code: ZIP code
- mls_id: MLS number (digits only)
- price: price in dollars (integer, no commas)
- sqft: square footage (integer)
- bedrooms: number of bedrooms (integer)
- bathrooms: number of bathrooms (integer)
- property_type: e.g. "Residential", "Condo", "Townhouse"
- listing_status: e.g. "New Listing", "Price Increased", "Active"
- listing_url: URL to the full listing page (if present)
- description: full property description text including features, basement info, lot details, amenities (if present)

Return a JSON array of listing objects. If no listings found, return [].
Do NOT include any text outside the JSON array."""


class LLMFallbackParser(EmailParser):
    def can_parse(self, html: str | None, text: str | None) -> bool:
        # Always available as last resort, but only if we have an API key
        return bool(settings.anthropic_api_key)

    def parse(self, html: str | None, text: str | None) -> list[ParsedListing]:
        content = text or html or ""
        if not content.strip():
            return []

        # Truncate to avoid excessive token usage
        if len(content) > 50000:
            content = content[:50000]

        try:
            client = anthropic.Anthropic(api_key=settings.anthropic_api_key)
            response = client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=4096,
                messages=[
                    {
                        "role": "user",
                        "content": f"{EXTRACTION_PROMPT}\n\n---\n\n{content}",
                    }
                ],
            )
            response_text = response.content[0].text.strip()

            # Parse JSON response
            listings_data = json.loads(response_text)
            if not isinstance(listings_data, list):
                listings_data = [listings_data]

            listings = []
            for item in listings_data:
                listing = ParsedListing(
                    address=item.get("address"),
                    town=item.get("town"),
                    state=item.get("state"),
                    zip_code=item.get("zip_code"),
                    mls_id=str(item["mls_id"]) if item.get("mls_id") else None,
                    price=int(item["price"]) if item.get("price") else None,
                    sqft=int(item["sqft"]) if item.get("sqft") else None,
                    bedrooms=int(item["bedrooms"]) if item.get("bedrooms") else None,
                    bathrooms=int(item["bathrooms"]) if item.get("bathrooms") else None,
                    property_type=item.get("property_type"),
                    listing_status=item.get("listing_status"),
                    listing_url=item.get("listing_url"),
                    description=item.get("description"),
                    source_format="llm",
                )
                listings.append(listing)

            return listings

        except (json.JSONDecodeError, anthropic.APIError, KeyError) as e:
            logger.error(f"LLM fallback parser failed: {e}")
            return []
