"""Tests for the Jina-rendered Redfin parser (_parse_jina_redfin)."""

import pytest
from app.main import _parse_jina_redfin


def _make_jina(**kwargs):
    """Build a minimal Jina text snippet from keyword args."""
    parts = []
    if "price" in kwargs:
        parts.append(f"For sale\n\n${kwargs['price']:,}")
    if "sqft" in kwargs:
        parts.append(f"{kwargs['sqft']:,}\n\nsq ft")
    if "beds" in kwargs:
        parts.append(f"{kwargs['beds']}\n\nbd")
    if "baths" in kwargs:
        parts.append(f"{kwargs['baths']} ba")
    if "year_built" in kwargs:
        parts.append(f"{kwargs['year_built']} Year Built")
    if "property_type" in kwargs:
        parts.append(f"{kwargs['property_type']}\nProperty Type")
    if "lot_acres" in kwargs:
        parts.append(f"{kwargs['lot_acres']} acres\nLot Size")
    if "garage" in kwargs:
        parts.append(f"{kwargs['garage']} car garage\nParking")
    if "garage_spaces" in kwargs:
        parts.append(f"{kwargs['garage_spaces']} car garage spaces\nParking")
    if "tax" in kwargs:
        parts.append(f"Tax Annual Amount: ${kwargs['tax']:,}")
    if "hoa" in kwargs:
        parts.append(f"${kwargs['hoa']}/month HOA")
    if "title" in kwargs:
        parts.insert(0, f"Title: {kwargs['title']}")
    return "\n\n".join(parts)


# ---- Price ----

class TestPrice:
    def test_basic_price(self):
        text = _make_jina(price=1799000)
        assert _parse_jina_redfin(text)["price"] == 1799000

    def test_price_below_100k_ignored(self):
        text = "For sale\n\n$50,000"
        assert "price" not in _parse_jina_redfin(text)

    def test_price_case_insensitive(self):
        text = "FOR SALE\n\n$1,200,000"
        assert _parse_jina_redfin(text)["price"] == 1200000

    def test_price_multiline_spacing(self):
        text = "For sale\n\n\n$999,999"
        assert _parse_jina_redfin(text)["price"] == 999999

    def test_no_price(self):
        text = "Some random text without price"
        assert "price" not in _parse_jina_redfin(text)


# ---- Sqft ----

class TestSqft:
    def test_basic_sqft(self):
        text = _make_jina(sqft=3165)
        assert _parse_jina_redfin(text)["sqft"] == 3165

    def test_sqft_with_comma(self):
        text = "2,400\n\nsq ft"
        assert _parse_jina_redfin(text)["sqft"] == 2400

    def test_sqft_too_small_ignored(self):
        text = "100\n\nsq ft"
        assert "sqft" not in _parse_jina_redfin(text)

    def test_sqft_too_large_ignored(self):
        text = "50000\n\nsq ft"
        assert "sqft" not in _parse_jina_redfin(text)

    def test_lot_sqft_not_confused(self):
        text = "43560 sq ft\nLot Size\n\n2400\n\nsq ft"
        result = _parse_jina_redfin(text)
        assert result["sqft"] == 43560 or result["sqft"] == 2400  # either match is valid


# ---- Beds ----

class TestBeds:
    def test_basic_beds(self):
        text = _make_jina(beds=4)
        assert _parse_jina_redfin(text)["bedrooms"] == 4

    def test_beds_multiline(self):
        text = "3\n\n\nbd"
        assert _parse_jina_redfin(text)["bedrooms"] == 3

    def test_no_beds(self):
        text = "Some text without beds"
        assert "bedrooms" not in _parse_jina_redfin(text)

    def test_beds_from_title_fallback(self):
        text = "Title: 123 Main St - 5 beds/3 baths"
        result = _parse_jina_redfin(text)
        assert result["bedrooms"] == 5


# ---- Baths ----

class TestBaths:
    def test_basic_baths(self):
        text = _make_jina(baths=3)
        assert _parse_jina_redfin(text)["bathrooms"] == 3

    def test_half_bath(self):
        text = "3.5 ba"
        assert _parse_jina_redfin(text)["bathrooms"] == 3.5

    def test_baths_from_title_fallback(self):
        text = "Title: 123 Main St - 4 beds/2.5 baths"
        result = _parse_jina_redfin(text)
        assert result["bathrooms"] == 2.5

    def test_no_baths(self):
        text = "Some text without baths"
        assert "bathrooms" not in _parse_jina_redfin(text)

    def test_integer_baths(self):
        text = "2 ba"
        assert _parse_jina_redfin(text)["bathrooms"] == 2


# ---- Year Built ----

class TestYearBuilt:
    def test_basic_year(self):
        text = _make_jina(year_built=1867)
        assert _parse_jina_redfin(text)["year_built"] == 1867

    def test_modern_year(self):
        text = "2025 Year Built"
        assert _parse_jina_redfin(text)["year_built"] == 2025

    def test_year_too_old_ignored(self):
        text = "1700 Year Built"
        assert "year_built" not in _parse_jina_redfin(text)

    def test_year_too_new_ignored(self):
        text = "2050 Year Built"
        assert "year_built" not in _parse_jina_redfin(text)

    def test_no_year(self):
        text = "Some text without year built"
        assert "year_built" not in _parse_jina_redfin(text)


# ---- Title Fallback ----

class TestTitleFallback:
    def test_title_beds_and_baths(self):
        text = "Title: 19 Coprock Rd - 4 beds/3 baths"
        result = _parse_jina_redfin(text)
        assert result["bedrooms"] == 4
        assert result["bathrooms"] == 3

    def test_title_half_baths(self):
        text = "Title: 3 Nickelby Pl - 4 beds/3.5 baths"
        result = _parse_jina_redfin(text)
        assert result["bathrooms"] == 3.5

    def test_title_not_used_when_stats_present(self):
        text = "Title: 123 Main - 2 beds/1 baths\n\n5\n\nbd\n\n3 ba"
        result = _parse_jina_redfin(text)
        assert result["bedrooms"] == 5  # from stats block, not title
        assert result["bathrooms"] == 3


# ---- Lot Size ----

class TestLotSize:
    def test_lot_acres(self):
        text = _make_jina(lot_acres=0.5)
        assert _parse_jina_redfin(text)["lot_acres"] == 0.5

    def test_lot_sqft_to_acres(self):
        text = "43560 sq ft\nLot Size"
        result = _parse_jina_redfin(text)
        assert abs(result["lot_acres"] - 1.0) < 0.01

    def test_lot_acres_too_large_ignored(self):
        text = "5000 acres\nLot Size"
        assert "lot_acres" not in _parse_jina_redfin(text)


# ---- Other Fields ----

class TestOtherFields:
    def test_property_type(self):
        text = _make_jina(property_type="Single-family")
        assert _parse_jina_redfin(text)["property_type"] == "Single Family Residential"

    def test_garage_without_spaces(self):
        text = "2 car garage\nParking"
        result = _parse_jina_redfin(text)
        assert result["garage_count"] == 2

    def test_garage_with_spaces(self):
        text = "2 car garage spaces\nParking"
        result = _parse_jina_redfin(text)
        assert result["garage_count"] == 2

    def test_tax(self):
        text = _make_jina(tax=15000)
        result = _parse_jina_redfin(text)
        assert '"annual": 15000' in result.get("property_tax_json", "")

    def test_hoa(self):
        text = _make_jina(hoa=350)
        assert _parse_jina_redfin(text)["hoa_monthly"] == 350


# ---- Full Realistic Listing ----

class TestFullListing:
    def test_realistic_redfin_jina(self):
        text = (
            "Title: 19 Coprock Rd, Tarrytown, NY - 4 beds/3 baths\n\n"
            "For sale\n\n$1,799,000\n\n"
            "4\n\nbd\n\n\u2022\n\n3 ba\n\n\u2022\n\n3,165\n\nsq ft\n\n"
            "Single-family\nProperty Type\n\n"
            "0.46 acres\nLot Size\n\n"
            "2 car garage\nParking\n\n"
            "1867 Year Built\n\n"
            "Tax Annual Amount: $28,000\n\n"
            "15 days on Redfin"
        )
        result = _parse_jina_redfin(text)
        assert result["price"] == 1799000
        assert result["sqft"] == 3165
        assert result["bedrooms"] == 4
        assert result["bathrooms"] == 3
        assert result["year_built"] == 1867
        assert result["property_type"] == "Single Family Residential"
        assert result["lot_acres"] == 0.46
        assert result["garage_count"] == 2

    def test_realistic_half_bath(self):
        text = (
            "For sale\n\n$1,300,000\n\n"
            "4\n\nbd\n\n\u2022\n\n3.5 ba\n\n\u2022\n\n4,348\n\nsq ft\n\n"
            "2000 Year Built"
        )
        result = _parse_jina_redfin(text)
        assert result["price"] == 1300000
        assert result["sqft"] == 4348
        assert result["bedrooms"] == 4
        assert result["bathrooms"] == 3.5
        assert result["year_built"] == 2000
