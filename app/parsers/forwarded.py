"""Preprocessor for forwarded emails.

Detects Gmail-forwarded emails and extracts the inner content
before passing to the parser chain.
"""

import re

from bs4 import BeautifulSoup

FORWARDED_SUBJECT_RE = re.compile(r"^(Fwd?|FW):\s*", re.IGNORECASE)
FORWARDED_TEXT_MARKER = "---------- Forwarded message ---------"


def is_forwarded(subject: str, html: str | None, text: str | None) -> bool:
    """Check if the email is a forwarded message."""
    if FORWARDED_SUBJECT_RE.match(subject or ""):
        return True
    if html and "gmail_quote" in html:
        return True
    if text and FORWARDED_TEXT_MARKER in text:
        return True
    return False


def unwrap_html(html: str) -> str:
    """Extract the forwarded content from Gmail HTML."""
    soup = BeautifulSoup(html, "html.parser")
    quote_div = soup.find("div", class_="gmail_quote")
    if quote_div:
        return str(quote_div)
    return html


def unwrap_text(text: str) -> str:
    """Extract the forwarded content from plain text."""
    if FORWARDED_TEXT_MARKER not in text:
        return text

    parts = text.split(FORWARDED_TEXT_MARKER, 1)
    lines = parts[1].strip().split("\n")

    # Skip forwarded headers (From:, Date:, Subject:, To:) until blank line
    for i, line in enumerate(lines):
        if line.strip() == "":
            return "\n".join(lines[i + 1 :])

    return parts[1]


def unwrap(subject: str, html: str | None, text: str | None) -> tuple[str | None, str | None]:
    """Unwrap forwarded email, returning (html, text) of inner content."""
    unwrapped_html = unwrap_html(html) if html else html
    unwrapped_text = unwrap_text(text) if text else text
    return unwrapped_html, unwrapped_text
