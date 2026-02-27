"""Tests for OneHome/Matrix MLS email parser."""

from pathlib import Path

from app.parsers.onehome import OneHomeParser

FIXTURE_DIR = Path(__file__).parent / "fixtures"


def _load_fixture(name: str) -> str:
    return (FIXTURE_DIR / name).read_text()


class TestOneHomeParser:
    def setup_method(self):
        self.parser = OneHomeParser()
        self.html = _load_fixture("onehome_sample.html")

    def test_can_parse_onehome_html(self):
        assert self.parser.can_parse(self.html, None) is True

    def test_cannot_parse_plain_text(self):
        assert self.parser.can_parse(None, "some text") is False

    def test_cannot_parse_non_onehome_html(self):
        assert self.parser.can_parse("<html><body>Hello</body></html>", None) is False

    def test_extracts_correct_number_of_listings(self):
        listings = self.parser.parse(self.html, None)
        assert len(listings) == 3

    def test_first_listing_fields(self):
        listings = self.parser.parse(self.html, None)
        first = listings[0]
        assert first.address == "11 Jennifer Lane"
        assert first.town == "Rye Brook"
        assert first.state == "New York"
        assert first.zip_code == "10573"
        assert first.price == 1295000
        assert first.sqft == 2437
        assert first.bedrooms == 4
        assert first.bathrooms == 3
        assert first.mls_id == "964038"
        assert first.property_type == "Residential"
        assert first.listing_status == "New Listing"
        assert first.source_format == "onehome_html"

    def test_second_listing_price_increased(self):
        listings = self.parser.parse(self.html, None)
        second = listings[1]
        assert second.address == "234 Judson Avenue"
        assert second.town == "Dobbs Ferry"
        assert second.price == 1400000
        assert second.bedrooms == 3
        assert second.sqft == 2385
        assert second.mls_id == "963378"
        assert second.listing_status == "Price Increased"

    def test_third_listing(self):
        listings = self.parser.parse(self.html, None)
        third = listings[2]
        assert third.address == "342 Willis Avenue"
        assert third.town == "Hawthorne"
        assert third.price == 1295000
        assert third.sqft == 3164
        assert third.bedrooms == 4
        assert third.mls_id == "961200"
