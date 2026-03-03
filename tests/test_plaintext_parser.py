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

    def test_address_from_redfin_url_fallback(self):
        """When no address is in the text, extract it from the Redfin URL."""
        text = """
$1,200,000 4 Beds 2.5 Baths 3,034 sqft
https://www.redfin.com/NY/Briarcliff-Manor/101-Long-Hill-Rd-E-10510/home/20082263
        """
        listings = self.parser.parse(None, text)
        assert len(listings) == 1
        assert listings[0].address == "101 Long Hill Rd E"
        assert listings[0].town == "Briarcliff Manor"
        assert listings[0].state == "NY"
        assert listings[0].zip_code == "10510"
        assert "redfin.com" in listings[0].listing_url

    def test_address_from_redfin_url_no_zip(self):
        """Redfin URL without embedded zip still extracts address and town."""
        text = """
$900,000 3 Beds 2 Baths
https://www.redfin.com/NY/Rye-Brook/11-Jennifer-Ln/home/22583423
        """
        listings = self.parser.parse(None, text)
        assert len(listings) == 1
        assert listings[0].address == "11 Jennifer Ln"
        assert listings[0].town == "Rye Brook"
        assert listings[0].state == "NY"

    def test_redfin_url_does_not_override_parsed_address(self):
        """If address was already parsed from text, Redfin URL should not override it."""
        text = """
$1,200,000 4 Beds 2.5 Baths 3,034 sqft
31 Lalli Dr, Katonah, NY 10536
https://www.redfin.com/NY/Katonah/31-Lalli-Dr-10536/home/12345
        """
        listings = self.parser.parse(None, text)
        assert len(listings) == 1
        assert listings[0].address == "31 Lalli Dr"
        assert listings[0].town == "Katonah"

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

    # --- Status prefix stripping ---

    def test_status_prefix_stripped_new_listing(self):
        """Status prefix 'New Listing' stripped and captured in listing_status."""
        text = """
New Listing
$1,200,000 4 Beds 2.5 Baths 3,034 sqft
101 Long Hill Rd E, Briarcliff Manor, NY 10510
        """
        listings = self.parser.parse(None, text)
        assert len(listings) == 1
        assert listings[0].listing_status == "New Listing"
        assert listings[0].address == "101 Long Hill Rd E"
        assert listings[0].town == "Briarcliff Manor"

    def test_status_prefix_stripped_pending(self):
        """Status prefix 'Pending' stripped and captured."""
        text = """
Pending
31 Lalli Dr, Katonah, NY 10536
$1,200,000 4 Beds 2.5 Baths 3,034 sqft
        """
        listings = self.parser.parse(None, text)
        assert len(listings) == 1
        assert listings[0].listing_status == "Pending"
        assert listings[0].address == "31 Lalli Dr"

    def test_status_prefix_stripped_coming_soon(self):
        """Status prefix 'Coming Soon' stripped and captured."""
        text = """
Coming Soon
$1,500,000 5 Beds 3 Baths 3,500 sqft
MLS #123456
        """
        listings = self.parser.parse(None, text)
        assert len(listings) == 1
        assert listings[0].listing_status == "Coming Soon"
        assert listings[0].mls_id == "123456"

    def test_no_status_prefix_leaves_status_none(self):
        """Without a status prefix, listing_status should be None."""
        text = """
$1,200,000 4 Beds 2.5 Baths 3,034 sqft
31 Lalli Dr, Katonah, NY 10536
        """
        listings = self.parser.parse(None, text)
        assert len(listings) == 1
        assert listings[0].listing_status is None

    def test_multiple_listings_with_status_prefixes(self):
        """Multiple listing blocks with different status prefixes."""
        text = """
New Listing
$1,200,000 4 Beds 2.5 Baths 3,034 sqft
31 Lalli Dr, Katonah, NY 10536

Price Drop
$1,400,000 5 Beds 3.5 Baths 3,274 sqft
7 Dickson Ln, Bedford Corners, NY 10549
        """
        listings = self.parser.parse(None, text)
        assert len(listings) == 2
        assert listings[0].listing_status == "New Listing"
        assert listings[0].address == "31 Lalli Dr"
        assert listings[1].listing_status == "Price Drop"
        assert listings[1].address == "7 Dickson Ln"

    def test_status_prefix_stripped_updated_mls_listing(self):
        """Status prefix 'Updated MLS Listing' stripped and captured."""
        text = """
Updated MLS Listing
$1,200,000 4 Beds 2.5 Baths 3,034 sqft
10 Barnes Ln, Chappaqua, NY 10514
        """
        listings = self.parser.parse(None, text)
        assert len(listings) == 1
        assert listings[0].listing_status == "Updated MLS Listing"
        assert listings[0].address == "10 Barnes Ln"
        assert listings[0].town == "Chappaqua"

    def test_status_prefix_stripped_new_tour_insight(self):
        """Status prefix 'New Tour Insight' stripped and captured."""
        text = """
New Tour Insight
$1,500,000 5 Beds 3 Baths 3,500 sqft
145 Cedar Ln, Ossining, NY 10562
        """
        listings = self.parser.parse(None, text)
        assert len(listings) == 1
        assert listings[0].listing_status == "New Tour Insight"
        assert listings[0].address == "145 Cedar Ln"
        assert listings[0].town == "Ossining"

    # --- Directional suffixes ---

    def test_inline_address_directional_suffix(self):
        """Inline address with directional: '101 Long Hill Rd E, ...'."""
        text = """
$1,200,000 4 Beds 2.5 Baths 3,034 sqft
101 Long Hill Rd E, Briarcliff Manor, NY 10510
        """
        listings = self.parser.parse(None, text)
        assert len(listings) == 1
        assert listings[0].address == "101 Long Hill Rd E"
        assert listings[0].town == "Briarcliff Manor"
        assert listings[0].state == "NY"
        assert listings[0].zip_code == "10510"

    def test_standalone_address_directional_suffix(self):
        """Standalone street line with directional: '101 Long Hill Rd E'."""
        text = """
101 Long Hill Rd E
Briarcliff Manor, New York 10510
$1,200,000
4 Beds 3 Baths 3,034 sqft
        """
        listings = self.parser.parse(None, text)
        assert len(listings) == 1
        assert listings[0].address == "101 Long Hill Rd E"
        assert listings[0].town == "Briarcliff Manor"

    def test_status_prefix_with_directional_address(self):
        """Combined: status prefix + directional suffix."""
        text = """
New Listing
$1,200,000 4 Beds 2.5 Baths 3,034 sqft
101 Long Hill Rd E, Briarcliff Manor, NY 10510
https://www.redfin.com/NY/Briarcliff-Manor/101-Long-Hill-Rd-E-10510/home/20082263
        """
        listings = self.parser.parse(None, text)
        assert len(listings) == 1
        assert listings[0].listing_status == "New Listing"
        assert listings[0].address == "101 Long Hill Rd E"
        assert listings[0].town == "Briarcliff Manor"

    def test_tour_header_stripped(self):
        """Redfin tour header '2 homes on this tour' stripped from block."""
        text = """
2 homes on this tour

31 Lalli Dr, Katonah, NY 10536
$1,200,000 4 Beds 2.5 Baths 3,034 sqft
        """
        listings = self.parser.parse(None, text)
        assert len(listings) == 1
        assert listings[0].address == "31 Lalli Dr"
        assert listings[0].town == "Katonah"
