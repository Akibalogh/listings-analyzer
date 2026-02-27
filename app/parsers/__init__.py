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
                    return listings
                logger.info(f"{name} matched but extracted 0 listings, trying next")

        logger.warning("No parser could extract listings from this email")
        return []


# Singleton for convenience
parser_chain = ParserChain()
