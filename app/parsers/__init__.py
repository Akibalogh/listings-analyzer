"""Parser chain for multi-format email parsing.

Tries parsers in order: OneHome HTML → Plain Text → LLM Fallback.
Forwarded emails are unwrapped before entering the chain.
"""

import logging

from app.models import ParsedListing
from app.parsers.base import EmailParser
from app.parsers.forwarded import is_forwarded, unwrap
from app.parsers.llm_fallback import LLMFallbackParser
from app.parsers.onehome import OneHomeParser
from app.parsers.plaintext import PlainTextParser

logger = logging.getLogger(__name__)


# The Matrix MLS alerts name their saved search in the subject, and the search
# is filtered by status: "Only sold", "Only pending", "Active and Contract
# only". That is the status of every listing in the email, and it was the only
# place status appeared for this sender — the portal links are an Angular shell
# and the MLS lookups return nothing from a cloud IP. 516 Bellwood Avenue was
# pushed as a new Worth Touring while sitting in an email titled "Only sold".
#
# Only unambiguous subjects map. "Active and Contract only" mixes two statuses
# in one email with no way to tell which listing is which, so it sets none —
# guessing "Active" there would reintroduce the same false-availability bug in
# the other direction.
_SUBJECT_STATUS = {
    "only sold": "Sold",
    "only pending": "Pending",
    "only active": "Active",
}


def status_from_subject(subject: str | None) -> str | None:
    """Listing status implied by an alert email's subject, if unambiguous."""
    s = (subject or "").strip().lower()
    if not s:
        return None
    for phrase, status in _SUBJECT_STATUS.items():
        if phrase in s:
            return status
    return None


class ParserChain:
    def __init__(self):
        self.parsers: list[EmailParser] = [
            OneHomeParser(),
            PlainTextParser(),
            LLMFallbackParser(),
        ]

    def parse(
        self, html: str | None, text: str | None, subject: str = ""
    ) -> list[ParsedListing]:
        subject_status = status_from_subject(subject)

        # Unwrap forwarded emails first
        if is_forwarded(subject, html, text):
            logger.info("Detected forwarded email, unwrapping")
            html, text = unwrap(subject, html, text)

        # Try each parser in order
        for parser in self.parsers:
            if parser.can_parse(html, text):
                name = parser.__class__.__name__
                logger.info(f"Using parser: {name}")
                listings = parser.parse(html, text)
                if listings:
                    logger.info(f"{name} extracted {len(listings)} listing(s)")
                    if subject_status:
                        applied = 0
                        for listing in listings:
                            if not (listing.listing_status or "").strip():
                                listing.listing_status = subject_status
                                applied += 1
                        if applied:
                            logger.info(
                                "Applied status %r from the subject to %d listing(s)",
                                subject_status, applied,
                            )
                    return listings
                logger.info(f"{name} matched but extracted 0 listings, trying next")

        logger.warning("No parser could extract listings from this email")
        return []


# Singleton for convenience
parser_chain = ParserChain()
