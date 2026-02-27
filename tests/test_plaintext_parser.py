"""Tests for plain text listing parser."""

from app.parsers.plaintext import PlainTextParser


class TestPlainTextParser:
    def setup_method(self):
        self.parser = PlainTextParser()

    def test_can_parse_with_listing_indicators(self):
        text = "Check out this listing: $1,295,000, MLS #964038, 4 bd"
        assert self.parser.can_parse(None, text) is True

    def test_cannot_parse_random_text(self):
        text = "Hey, want to grab lunch tomorrow?"
        assert self.parser.can_parse(None, text) is False

    def test_cannot_parse_none(self):
        assert self.parser.can_parse(None, None) is False

    def test_parse_single_listing(self):
        text = """
11 Jennifer Lane
Rye Brook, New York 10573
$1,295,000
4 bd, 3 ba, 2,437 sqft
MLS #964038
        """
        listings = self.parser.parse(None, text)
        assert len(listings) == 1
        listing = listings[0]
        assert listing.price == 1295000
        assert listing.bedrooms == 4
        assert listing.bathrooms == 3
        assert listing.sqft == 2437
        assert listing.mls_id == "964038"
        assert listing.address == "11 Jennifer Lane"
        assert listing.town == "Rye Brook"
        assert listing.state == "New York"
        assert listing.zip_code == "10573"
        assert listing.source_format == "plaintext"

    def test_parse_multiple_listings(self):
        text = """
11 Jennifer Lane
Rye Brook, New York 10573
$1,295,000
4 bd, 3 ba, 2,437 sqft
MLS #964038

234 Judson Avenue
Dobbs Ferry, New York 10522
$1,400,000
3 bd, 3 ba, 2,385 sqft
MLS #963378
        """
        listings = self.parser.parse(None, text)
        assert len(listings) == 2
        assert listings[0].mls_id == "964038"
        assert listings[1].mls_id == "963378"

    def test_parse_partial_info(self):
        text = "New listing: $1,500,000 - 4 bedrooms, MLS #123456"
        listings = self.parser.parse(None, text)
        assert len(listings) == 1
        assert listings[0].price == 1500000
        assert listings[0].bedrooms == 4
        assert listings[0].mls_id == "123456"
