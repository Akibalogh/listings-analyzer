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

    def test_extracts_redfin_url(self):
        text = """
11 Jennifer Lane
Rye Brook, New York 10573
$1,295,000
4 bd, 3 ba, 2,437 sqft
MLS #964038
https://www.redfin.com/NY/Rye-Brook/11-Jennifer-Ln-10573/home/22583423
        """
        listings = self.parser.parse(None, text)
        assert len(listings) == 1
        assert listings[0].listing_url == "https://www.redfin.com/NY/Rye-Brook/11-Jennifer-Ln-10573/home/22583423"

    def test_extracts_onekeymls_url(self):
        text = """
$1,400,000 - 3 bd, 3 ba
MLS #963378
https://www.onekeymls.com/listing/963378
        """
        listings = self.parser.parse(None, text)
        assert len(listings) == 1
        assert listings[0].listing_url == "https://www.onekeymls.com/listing/963378"

    def test_strips_trailing_punctuation_from_url(self):
        text = """
$1,295,000 - 4 bd
MLS #964038
Check it out: https://www.redfin.com/NY/Rye-Brook/11-Jennifer-Ln/home/123.
        """
        listings = self.parser.parse(None, text)
        assert len(listings) == 1
        assert listings[0].listing_url == "https://www.redfin.com/NY/Rye-Brook/11-Jennifer-Ln/home/123"

    def test_no_url_when_absent(self):
        text = """
$1,295,000
4 bd, 3 ba, 2,437 sqft
MLS #964038
        """
        listings = self.parser.parse(None, text)
        assert len(listings) == 1
        assert listings[0].listing_url is None

    def test_filters_redfin_tour_urls(self):
        """Tour/checkout URLs should NOT be extracted as listing URLs."""
        text = """
$1,200,000 - 4 bd, 2.5 ba, 3,034 sqft
https://www.redfin.com/tours/checkout/times?listingId=212530348
        """
        listings = self.parser.parse(None, text)
        assert len(listings) == 1
        assert listings[0].listing_url is None

    def test_inline_address_format(self):
        """Redfin-style inline address: '31 Lalli Dr, Katonah, NY 10536'."""
        text = """
$1,200,000 4 Beds 2.5 Baths 3,034 sqft
31 Lalli Dr, Katonah, NY 10536
        """
        listings = self.parser.parse(None, text)
        assert len(listings) == 1
        assert listings[0].address == "31 Lalli Dr"
        assert listings[0].town == "Katonah"
        assert listings[0].state == "NY"
        assert listings[0].zip_code == "10536"

    def test_inline_address_multi_word_town(self):
        """Inline address with multi-word town name."""
        text = """
$1,695,000 5 Beds 3.5 Baths 3,274 sqft
7 Dickson Ln, Bedford Corners, NY 10549
        """
        listings = self.parser.parse(None, text)
        assert len(listings) == 1
        assert listings[0].address == "7 Dickson Ln"
        assert listings[0].town == "Bedford Corners"
        assert listings[0].zip_code == "10549"

    def test_redfin_tour_email_two_listings(self):
        """Tour confirmation email with two inline-address listings."""
        text = """
$1,200,000 4 Beds 2.5 Baths 3,034 Sq. Ft.
31 Lalli Dr, Katonah, NY 10536

$1,695,000 5 Beds 3.5 Baths 3,274 Sq. Ft.
7 Dickson Ln, Bedford Corners, NY 10549
        """
        listings = self.parser.parse(None, text)
        assert len(listings) == 2
        assert listings[0].address == "31 Lalli Dr"
        assert listings[0].price == 1200000
        assert listings[1].address == "7 Dickson Ln"
        assert listings[1].price == 1695000
