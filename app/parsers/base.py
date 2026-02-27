"""Base class for email parsers."""

from abc import ABC, abstractmethod

from app.models import ParsedListing


class EmailParser(ABC):
    @abstractmethod
    def can_parse(self, html: str | None, text: str | None) -> bool:
        """Return True if this parser can handle the given email content."""
        ...

    @abstractmethod
    def parse(self, html: str | None, text: str | None) -> list[ParsedListing]:
        """Extract listings from the email content."""
        ...
