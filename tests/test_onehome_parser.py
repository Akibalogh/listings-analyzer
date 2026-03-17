"""Tests for OneHome/Matrix MLS email parser."""

import re
from pathlib import Path
from unittest.mock import MagicMock, patch

from app.parsers.onehome import (
    OneHomeParser,
    scrape_listing_description,
    scrape_listing_structured_data,
    check_listing_status,
    enumerate_redfin_images,
    _has_useful_content,
    _is_spa_url,
    _try_redfin_fallback,
    _try_onekeymls,
    _scrape_static,
    _search_redfin_url,
    _search_onekeymls_url,
    _extract_description_from_html,
    _extract_image_urls,
    _extract_property_stats,
    _extract_listing_status,
    _is_bot_block_page,
    _get_rotating_user_agent,
)

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
        assert first.property_type == "Single Family Residential"
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

    def test_extracts_listing_url(self):
        """Listing URL should be extracted from the OneHome portal link."""
        listings = self.parser.parse(self.html, None)
        first = listings[0]
        assert first.listing_url is not None
        assert "portal.onehome.com" in first.listing_url
        assert "abc123" in first.listing_url

    def test_all_listings_have_urls(self):
        listings = self.parser.parse(self.html, None)
        for listing in listings:
            assert listing.listing_url is not None
            assert listing.listing_url.startswith("https://")


class TestListingScraper:
    """Tests for scrape_listing_description() — returns (description, image_urls) tuple."""

    def test_scrape_returns_none_for_empty_url(self):
        desc, images = scrape_listing_description("")
        assert desc is None
        assert images == []
        desc, images = scrape_listing_description(None)
        assert desc is None
        assert images == []

    @patch("app.parsers.onehome.httpx.Client")
    def test_scrape_extracts_description_from_remarks(self, mock_client_cls):
        """Finds description using CSS selector matching."""
        html = """<html><body>
        <div class="remarks">
            Beautiful colonial with a finished basement, hardwood floors,
            and updated kitchen. The basement features a rec room, full bath,
            and plenty of storage. Large lot with pool.
        </div>
        </body></html>"""

        mock_response = MagicMock()
        mock_response.text = html
        mock_response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_response
        mock_client_cls.return_value = mock_client

        desc, images = scrape_listing_description("https://example.com/listing/123")
        assert desc is not None
        assert "finished basement" in desc.lower()
        assert "pool" in desc.lower()

    @patch("app.parsers.onehome.httpx.Client")
    def test_scrape_finds_description_by_keyword_fallback(self, mock_client_cls):
        """Falls back to keyword search when no CSS selector matches."""
        html = """<html><body>
        <div class="some-random-class">
            <p>Short text here.</p>
            <p>This spacious home features a walkout basement with finished rec room,
            three-car garage, updated kitchen with granite counters, hardwood floors
            throughout the first floor, central air conditioning, and a beautiful
            stone fireplace in the living room.</p>
        </div>
        </body></html>"""

        mock_response = MagicMock()
        mock_response.text = html
        mock_response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_response
        mock_client_cls.return_value = mock_client

        desc, images = scrape_listing_description("https://example.com/listing/456")
        assert desc is not None
        assert "basement" in desc.lower()

    @patch("app.parsers.onehome.httpx.Client")
    def test_scrape_returns_none_on_http_error(self, mock_client_cls):
        """Returns None on HTTP error, falls through to Jina which also fails."""
        import httpx

        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_response.raise_for_status.side_effect = httpx.HTTPStatusError(
            "Forbidden", request=MagicMock(), response=mock_response
        )
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_response
        mock_client_cls.return_value = mock_client

        # Static fails with HTTP error → Jina fallback also uses httpx.Client mock
        # Both will fail with the same error
        desc, images = scrape_listing_description("https://example.com/listing/789")
        assert desc is None

    @patch("app.parsers.onehome._scrape_with_jina", return_value=(None, []))
    @patch("app.parsers.onehome.httpx.Client")
    def test_scrape_returns_none_when_no_useful_content(self, mock_client_cls, mock_jina):
        """Returns None when page has no real estate keywords."""
        html = """<html><body>
        <div class="content">
            <p>Welcome to our website. Please log in to continue.</p>
        </div>
        </body></html>"""

        mock_response = MagicMock()
        mock_response.text = html
        mock_response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_response
        mock_client_cls.return_value = mock_client

        desc, images = scrape_listing_description("https://example.com/listing/000")
        assert desc is None


class TestHasUsefulContent:
    """Tests for the _has_useful_content helper."""

    def test_detects_basement_keywords(self):
        assert _has_useful_content("This home has a finished basement") is True
        assert _has_useful_content("Unfinished basement storage") is True

    def test_detects_amenity_keywords(self):
        assert _has_useful_content("In-ground pool with deck and patio") is True
        assert _has_useful_content("Sauna and jacuzzi in the master suite") is True

    def test_detects_condition_keywords(self):
        assert _has_useful_content("Recently renovated kitchen with hardwood") is True

    def test_rejects_generic_text(self):
        assert _has_useful_content("Welcome to our website") is False
        assert _has_useful_content("Please click the button below") is False


class TestIsSpaUrl:
    """Tests for _is_spa_url helper."""

    def test_onehome_portal_url(self):
        assert _is_spa_url("https://portal.onehome.com/en-US/property/123") is True

    def test_onehome_listing_url_with_token(self):
        assert _is_spa_url("https://portal.onehome.com/en-US/listing?token=abc") is True

    def test_non_spa_url(self):
        assert _is_spa_url("https://www.redfin.com/listing/123") is False
        assert _is_spa_url("https://www.zillow.com/homedetails/123") is False


class TestExtractDescriptionFromHtml:
    """Tests for the shared HTML description extraction logic."""

    def test_extracts_from_remarks_class(self):
        html = """<html><body>
        <div class="remarks">
            Beautiful colonial with a finished basement, hardwood floors,
            and updated kitchen. The basement features a rec room and storage.
        </div>
        </body></html>"""
        result = _extract_description_from_html(html, "http://test", "test")
        assert result is not None
        assert "finished basement" in result.lower()

    def test_extracts_from_description_class(self):
        html = """<html><body>
        <div class="listing-description">
            Spacious 4 bedroom home with finished walk-out basement,
            central air, and modern kitchen with granite countertops.
            Large deck overlooking the backyard.
        </div>
        </body></html>"""
        result = _extract_description_from_html(html, "http://test", "test")
        assert result is not None
        assert "basement" in result.lower()

    def test_keyword_fallback_finds_description(self):
        html = """<html><body>
        <div class="xyz-unique-class">
            <p>This home features a walk-out basement with finished rec room,
            updated kitchen, hardwood floors throughout, central air conditioning,
            stone fireplace in living room, and a beautiful patio.</p>
        </div>
        </body></html>"""
        result = _extract_description_from_html(html, "http://test", "test")
        assert result is not None
        assert "basement" in result.lower()

    def test_returns_none_for_no_content(self):
        html = """<html><body>
        <aotf-app-root></aotf-app-root>
        <script>angular app code here</script>
        </body></html>"""
        result = _extract_description_from_html(html, "http://test", "test")
        assert result is None

    def test_truncates_long_descriptions(self):
        long_text = "bedroom " * 1000  # Well over 5000 chars
        html = f'<html><body><div class="remarks">{long_text}</div></body></html>'
        result = _extract_description_from_html(html, "http://test", "test")
        assert result is not None
        assert len(result) <= 5000

    def test_extracts_redfin_remarks(self):
        """Redfin-specific CSS selector matches."""
        html = """<html><body>
        <div id="marketing-remarks-scroll">
            Stunning colonial with finished basement, 4 bedrooms, modern kitchen
            with stainless appliances, hardwood floors, and a beautiful deck.
            Spacious lot with mature landscaping.
        </div>
        </body></html>"""
        result = _extract_description_from_html(html, "http://test", "test")
        assert result is not None
        assert "basement" in result.lower()


class TestExtractImageUrls:
    """Tests for image URL extraction."""

    def test_extracts_onehome_images(self):
        html = """<html><body>
        <img src="https://photos.onehome.com/listing/photo1.jpg" width="800">
        <img src="https://photos.onehome.com/listing/photo2.jpg" width="600">
        <img src="https://example.com/icon.png" width="16">
        </body></html>"""
        images = _extract_image_urls(html, "http://test")
        assert len(images) == 2
        assert all("photos.onehome.com" in img for img in images)

    def test_extracts_redfin_images(self):
        html = """<html><body>
        <img src="https://ssl.cdn-redfin.com/photo/listing/photo1.jpg" width="800">
        <img src="https://ssl.cdn-redfin.com/photo/listing/photo2.jpg" width="600">
        </body></html>"""
        images = _extract_image_urls(html, "http://test")
        assert len(images) == 2

    def test_skips_icons_and_logos(self):
        html = """<html><body>
        <img src="https://example.com/logo.png" width="100">
        <img src="https://example.com/icon-small.png" width="16">
        <img src="https://example.com/photos/listing-photo.jpg" class="photo" width="800">
        </body></html>"""
        images = _extract_image_urls(html, "http://test")
        # Only the photo should be extracted, not the logo or icon
        assert all("icon" not in img and "logo" not in img for img in images)

    def test_deduplicates_images(self):
        html = """<html><body>
        <img src="https://photos.onehome.com/photo1.jpg" width="800">
        <img src="https://photos.onehome.com/photo1.jpg" width="400">
        </body></html>"""
        images = _extract_image_urls(html, "http://test")
        assert len(images) == 1

    def test_empty_page_no_images(self):
        html = "<html><body><p>No images here</p></body></html>"
        images = _extract_image_urls(html, "http://test")
        assert images == []


class TestUrlRouting:
    """Tests for smart URL routing — skip known-failing steps."""

    @patch("app.parsers.onehome._try_redfin_fallback", return_value=(None, []))
    @patch("app.parsers.onehome._scrape_with_jina")
    @patch("app.parsers.onehome._scrape_static")
    def test_onehome_skips_static_and_jina(self, mock_static, mock_jina, mock_fallback):
        """OneHome URLs skip static + Jina, go straight to Redfin fallback."""
        url = "https://portal.onehome.com/en-US/property/aotf~123"

        scrape_listing_description(url, address="10 Test St", town="Scarsdale")

        mock_static.assert_not_called()
        mock_jina.assert_not_called()
        mock_fallback.assert_called_once()

    @patch("app.parsers.onehome._scrape_with_jina", return_value=("Finished basement", []))
    @patch("app.parsers.onehome._scrape_static", return_value=None)
    def test_redfin_tries_static_then_jina(self, mock_static, mock_jina):
        """Redfin URLs try static HTTP first; fall back to Jina if static fails."""
        url = "https://www.redfin.com/NY/Scarsdale/10-Test-St/home/123"

        desc, images = scrape_listing_description(url)

        mock_static.assert_called_once_with(url)
        mock_jina.assert_called_once_with(url)
        assert desc is not None
        assert "basement" in desc.lower()

    @patch("app.parsers.onehome._scrape_with_jina")
    @patch("app.parsers.onehome._scrape_static")
    def test_other_urls_try_full_chain(self, mock_static, mock_jina):
        """Non-OneHome/Redfin URLs try static first, then Jina."""
        mock_static.return_value = ("Beautiful home with finished basement and pool", [])
        url = "https://example.com/listing/123"

        desc, images = scrape_listing_description(url)

        mock_static.assert_called_once_with(url)
        mock_jina.assert_not_called()
        assert "basement" in desc.lower()

    @patch("app.parsers.onehome._try_redfin_fallback")
    @patch("app.parsers.onehome._scrape_with_jina", return_value=(None, []))
    @patch("app.parsers.onehome._scrape_static", return_value=None)
    def test_other_urls_fallback_to_redfin(self, mock_static, mock_jina, mock_fallback):
        """Non-OneHome/Redfin URLs fall through to Redfin search when both fail."""
        mock_fallback.return_value = ("Colonial with garage and deck", [])
        url = "https://example.com/listing/456"

        desc, images = scrape_listing_description(
            url, address="10 Test St", town="Scarsdale"
        )

        mock_static.assert_called_once()
        mock_jina.assert_called_once()
        mock_fallback.assert_called_once()
        assert "garage" in desc.lower()


class TestRedfinFallback:
    """Tests for the DDG → Redfin → Jina fallback chain."""

    @patch("app.parsers.onehome._scrape_with_jina")
    @patch("app.parsers.onehome._scrape_static", return_value=None)
    @patch("app.parsers.onehome._search_redfin_url", return_value="https://www.redfin.com/NY/Scarsdale/10-Test-St/home/123")
    def test_ddg_redfin_fallback(self, mock_ddg, mock_static, mock_jina):
        """DuckDuckGo finds a Redfin URL; static fails so Jina is used."""
        mock_jina.return_value = (
            "Beautiful colonial with finished basement",
            ["https://img.redfin.com/photo1.jpg"],
        )

        desc, images = _try_redfin_fallback(
            address="10 Test St", town="Scarsdale", state="NY", zip_code="10583"
        )

        assert desc is not None
        assert "basement" in desc.lower()
        assert len(images) == 1
        mock_ddg.assert_called_once()
        mock_static.assert_called_once()
        mock_jina.assert_called_once()

    @patch("app.parsers.onehome._search_redfin_url", return_value=None)
    def test_ddg_finds_nothing(self, mock_ddg):
        """Returns (None, []) when DDG finds no Redfin URL."""
        desc, images = _try_redfin_fallback(
            address="10 Test St", town="Scarsdale", state="NY", zip_code="10583"
        )

        assert desc is None
        assert images == []

    def test_no_address_returns_none(self):
        """Returns (None, []) when no address info provided."""
        desc, images = _try_redfin_fallback(address=None, town=None, state=None, zip_code=None)
        assert desc is None
        assert images == []

    @patch("app.parsers.onehome._scrape_with_jina", return_value=(None, []))
    @patch("app.parsers.onehome._scrape_static", return_value=None)
    @patch("app.parsers.onehome._search_redfin_url", return_value="https://www.redfin.com/NY/Scarsdale/10-Test-St/home/123")
    def test_ddg_finds_url_but_both_scrapers_fail(self, mock_ddg, mock_static, mock_jina):
        """Returns (None, []) when DDG finds URL but both static and Jina fail."""
        desc, images = _try_redfin_fallback(
            address="10 Test St", town="Scarsdale", state="NY", zip_code="10583"
        )

        assert desc is None
        assert images == []
        mock_static.assert_called_once()
        mock_jina.assert_called_once()

    @patch("app.parsers.onehome._search_redfin_url", return_value="https://www.redfin.com/NY/Scarsdale/10-Test-St/home/123")
    @patch("app.parsers.onehome._scrape_with_jina")
    @patch("app.parsers.onehome._scrape_static", return_value=None)
    def test_onehome_url_uses_redfin_fallback(self, mock_static, mock_jina, mock_ddg):
        """OneHome URL goes directly to Redfin fallback; static fails, Jina succeeds."""
        mock_jina.return_value = (
            "Beautiful home with finished basement",
            ["https://photos.redfin.com/photo1.jpg", "https://photos.redfin.com/photo2.jpg"],
        )
        url = "https://portal.onehome.com/en-US/property/aotf~123"

        desc, images = scrape_listing_description(
            url, address="10 Test St", town="Scarsdale"
        )

        assert desc is not None
        assert len(images) == 2
        redfin_url = "https://www.redfin.com/NY/Scarsdale/10-Test-St/home/123"
        # Static tried first with Redfin URL (returns None), then Jina called
        mock_static.assert_called_once_with(redfin_url)
        mock_jina.assert_called_once_with(redfin_url)


class TestOneKeyMLS:
    """Tests for the OneKey MLS direct URL scraping."""

    @patch("app.parsers.onehome._scrape_static")
    def test_onekeymls_constructs_url_from_mls_id(self, mock_static):
        """_try_onekeymls builds the correct OneKey MLS URL from address parts."""
        mock_static.return_value = (
            "Colonial home with finished basement and garage",
            ["https://d36ebcehl5r1pw.cloudfront.net/images/KEY123/photo1.jpeg"],
        )

        desc, images = _try_onekeymls(
            address="342 Willis Avenue",
            town="Hawthorne",
            state="NY",
            zip_code="10532",
            mls_id="961200",
        )

        assert desc is not None
        assert "basement" in desc.lower()
        assert len(images) == 1
        expected_url = "https://www.onekeymls.com/address/342-Willis-Avenue-Hawthorne-NY-10532/961200"
        mock_static.assert_called_once_with(expected_url)

    @patch("app.parsers.onehome._scrape_with_jina", return_value=(None, []))
    @patch("app.parsers.onehome._scrape_static", return_value=None)
    def test_onekeymls_falls_back_to_jina(self, mock_static, mock_jina):
        """_try_onekeymls falls back to Jina if static fails."""
        _try_onekeymls("10 Test St", "Scarsdale", "NY", "10583", "123456")
        mock_static.assert_called_once()
        mock_jina.assert_called_once()

    @patch("app.parsers.onehome._try_onekeymls")
    @patch("app.parsers.onehome._scrape_with_jina", return_value=(None, []))
    @patch("app.parsers.onehome._scrape_static", return_value=None)
    def test_onehome_url_with_mls_id_tries_onekeymls_first(
        self, mock_static, mock_jina, mock_onekeymls
    ):
        """OneHome URL with mls_id tries OneKey MLS before Redfin DDG search."""
        mock_onekeymls.return_value = (
            "Beautiful colonial with finished basement",
            ["https://d36ebcehl5r1pw.cloudfront.net/images/KEY123/photo1.jpeg"],
        )
        url = "https://portal.onehome.com/en-US/property/aotf~123"

        desc, images = scrape_listing_description(
            url,
            address="342 Willis Avenue",
            town="Hawthorne",
            state="NY",
            zip_code="10532",
            mls_id="961200",
        )

        assert desc is not None
        mock_onekeymls.assert_called_once_with(
            "342 Willis Avenue", "Hawthorne", "NY", "10532", "961200"
        )

    @patch("app.parsers.onehome._try_onekeymls")
    @patch("app.parsers.onehome._scrape_with_jina", return_value=(None, []))
    @patch("app.parsers.onehome._scrape_static", return_value=None)
    def test_redfin_blocked_falls_back_to_onekeymls(
        self, mock_static, mock_jina, mock_onekeymls
    ):
        """Redfin URL that returns nothing falls back to OneKey MLS when mls_id provided."""
        mock_onekeymls.return_value = (
            "Stunning waterfront home with pool and finished basement",
            ["https://d36ebcehl5r1pw.cloudfront.net/images/KEY456/photo1.jpeg"],
        )
        url = "https://www.redfin.com/NY/Eastchester/34-Lakeshore-Dr-10709/home/20146668"

        desc, images = scrape_listing_description(
            url,
            address="34 Lakeshore Drive",
            town="Eastchester",
            state="NY",
            zip_code="10709",
            mls_id="928190",
        )

        assert desc is not None
        assert "waterfront" in desc.lower()
        mock_onekeymls.assert_called_once_with(
            "34 Lakeshore Drive", "Eastchester", "NY", "10709", "928190"
        )


class TestSearchOneKeyMLSUrl:
    """Tests for DDG-based OneKey MLS URL search."""

    @patch("app.parsers.onehome.httpx.Client")
    def test_search_onekeymls_url_finds_listing(self, mock_client_cls):
        """DDG returns a OneKey MLS URL matching the street number."""
        ddg_html = """<html><body>
        <a href="https://www.onekeymls.com/address/19-Georgia-Ln-Chappaqua-NY-10514/970123">
            19 Georgia Ln, Chappaqua
        </a>
        </body></html>"""

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = ddg_html
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_response
        mock_client_cls.return_value = mock_client

        url = _search_onekeymls_url("19 Georgia Ln", "Chappaqua", "NY", "10514")
        assert url is not None
        assert "onekeymls.com" in url
        assert "19-Georgia" in url

    @patch("app.parsers.onehome.httpx.Client")
    def test_search_onekeymls_url_no_results(self, mock_client_cls):
        """Returns None when DDG has no OneKey MLS URLs."""
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.text = "<html><body>No results</body></html>"
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_response
        mock_client_cls.return_value = mock_client

        url = _search_onekeymls_url("999 Fake St", "Nowhere", "NY")
        assert url is None


class TestExtractPropertyStats:
    """Tests for _extract_property_stats — regex-based stat extraction from HTML."""

    def test_extracts_all_stats(self):
        html = """<html><body>
        <div class="price">$1,499,000</div>
        <div class="specs">4 Bedrooms | 3 Bathrooms | 2,800 Sq Ft</div>
        </body></html>"""
        stats = _extract_property_stats(html)
        assert stats is not None
        assert stats["price"] == 1499000
        assert stats["bedrooms"] == 4
        assert stats["bathrooms"] == 3
        assert stats["sqft"] == 2800

    def test_extracts_partial_stats(self):
        html = """<html><body>
        <div>$1,250,000</div>
        <div>3 bd</div>
        </body></html>"""
        stats = _extract_property_stats(html)
        assert stats is not None
        assert stats["price"] == 1250000
        assert stats["bedrooms"] == 3
        assert "bathrooms" not in stats
        assert "sqft" not in stats

    def test_returns_none_for_empty_page(self):
        html = "<html><body><p>Login to continue</p></body></html>"
        stats = _extract_property_stats(html)
        assert stats is None

    def test_ignores_stats_in_scripts(self):
        """Script content should be stripped before regex matching."""
        html = """<html><body>
        <script>var price = "$999,000"; var beds = "5 Beds";</script>
        <p>Welcome to our site</p>
        </body></html>"""
        stats = _extract_property_stats(html)
        assert stats is None

    def test_extracts_year_built_from_text(self):
        html = """<html><body>
        <div>$1,250,000</div>
        <div>4 Bedrooms | 2 Bathrooms | 2,800 Sq Ft</div>
        <div>Year Built: 1958</div>
        </body></html>"""
        stats = _extract_property_stats(html)
        assert stats is not None
        assert stats["year_built"] == 1958

    def test_extracts_year_built_from_json_ld(self):
        """YearBuilt in JSON-LD inside <script> tags should still be found."""
        html = """<html><body>
        <script type="application/ld+json">{"YearBuilt": "1972"}</script>
        <div>$1,499,000 4 beds 3 bath 2,800 sqft</div>
        </body></html>"""
        stats = _extract_property_stats(html)
        assert stats is not None
        assert stats["year_built"] == 1972

    def test_extracts_list_date_from_json_ld(self):
        """OnMarketDate in JSON-LD should be extracted as list_date."""
        html = """<html><body>
        <script>{"OnMarketDate": "2026-02-15T00:00:00"}</script>
        <div>$1,300,000 3 beds 2 bath 2,100 sqft</div>
        </body></html>"""
        stats = _extract_property_stats(html)
        assert stats is not None
        assert stats["list_date"] == "2026-02-15"

    def test_extracts_list_date_from_visible_text(self):
        html = """<html><body>
        <div>$1,100,000</div>
        <div>3 bd 2 ba 1,800 sqft</div>
        <div>Listed on: 01/15/2026</div>
        </body></html>"""
        stats = _extract_property_stats(html)
        assert stats is not None
        assert stats["list_date"] == "01/15/2026"

    def test_year_built_rejects_invalid_years(self):
        html = """<html><body>
        <div>$1,250,000</div>
        <div>Built in 1650</div>
        </body></html>"""
        stats = _extract_property_stats(html)
        assert stats is not None
        assert "year_built" not in stats

    def test_compact_stats_still_extracts_year_built(self):
        """Compact stats pattern should not prevent year_built extraction."""
        html = """<html><body>
        <div>$1,275,000 3 beds 2 bath 2,167 sqft</div>
        <script>{"YearBuilt": "2005", "OnMarketDate": "2026-01-20T00:00:00"}</script>
        </body></html>"""
        stats = _extract_property_stats(html)
        assert stats is not None
        assert stats["price"] == 1275000
        assert stats["year_built"] == 2005
        assert stats["list_date"] == "2026-01-20"


class TestRedinFallsBackToOneKeyMLSSearch:
    """Tests for Redfin URLs falling back to OneKey MLS DDG search when no MLS ID."""

    @patch("app.parsers.onehome._search_onekeymls_url",
           return_value="https://www.onekeymls.com/address/19-Georgia-Ln-Chappaqua-NY/970123")
    @patch("app.parsers.onehome._scrape_static")
    @patch("app.parsers.onehome._scrape_with_jina", return_value=(None, []))
    def test_redfin_falls_back_to_onekeymls_search(self, mock_jina_outer, mock_static, mock_onekeymls_search):
        """Redfin URL with no MLS ID falls back to OneKeyMLS DDG address search."""
        # First static call (Redfin) fails, Jina fails, no MLS ID so _try_onekeymls skipped,
        # then DDG search finds OneKeyMLS URL, second static call succeeds
        mock_static.side_effect = [
            None,  # Redfin static fails
            ("Charming colonial with finished basement and pool", []),  # OneKeyMLS static succeeds
        ]
        url = "https://www.redfin.com/NY/Chappaqua/19-Georgia-Ln-10514/home/123456"

        desc, images = scrape_listing_description(
            url, address="19 Georgia Ln", town="Chappaqua", state="NY", zip_code="10514"
        )

        assert desc is not None
        assert "basement" in desc.lower()
        mock_onekeymls_search.assert_called_once_with("19 Georgia Ln", "Chappaqua", "NY", "10514")


class TestExtractListingStatus:
    """Tests for _extract_listing_status — regex extraction of SaleStatus/MlsStatus."""

    def test_extract_listing_status_sold(self):
        html = '<script>{"SaleStatus":"Sold","ListPrice":1200000}</script>'
        assert _extract_listing_status(html) == "Sold"

    def test_extract_listing_status_active(self):
        html = '<div data-props=\'{"MlsStatus":"Active","beds":4}\'></div>'
        assert _extract_listing_status(html) == "Active"

    def test_extract_listing_status_missing(self):
        html = "<html><body><p>No structured data here</p></body></html>"
        assert _extract_listing_status(html) is None

    def test_extract_listing_status_pending(self):
        html = '{"SaleStatus":"Pending","MlsStatus":"Pending"}'
        assert _extract_listing_status(html) == "Pending"

    def test_extract_listing_status_closed(self):
        html = '{"SaleStatus":"Closed"}'
        assert _extract_listing_status(html) == "Closed"


class TestCheckListingStatus:
    """Tests for check_listing_status — DDG search + status extraction."""

    @patch("app.parsers.onehome.httpx.Client")
    @patch("app.parsers.onehome._search_onekeymls_url",
           return_value="https://www.onekeymls.com/address/123-Main-St-Rye-NY-10580/999999")
    def test_returns_sold_status(self, mock_search, mock_client_cls):
        mock_response = MagicMock()
        mock_response.text = '<script>{"SaleStatus":"Sold","ListPrice":1500000}</script>'
        mock_response.raise_for_status = MagicMock()
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_response
        mock_client_cls.return_value = mock_client

        status = check_listing_status("123 Main St", "Rye", "NY", "10580")
        assert status == "Sold"
        mock_search.assert_called_once()

    @patch("app.parsers.onehome._search_onekeymls_url", return_value=None)
    def test_returns_none_when_no_mls_page(self, mock_search):
        status = check_listing_status("999 Fake St", "Nowhere", "NY")
        assert status is None

    def test_returns_none_for_missing_address(self):
        assert check_listing_status(None, None) is None
        assert check_listing_status("", "") is None


class TestEnumerateRedfinImages:
    """Tests for enumerate_redfin_images() — CDN image enumeration via HEAD requests."""

    def test_returns_seeds_when_no_redfin_pattern(self):
        """Non-Redfin URLs are returned unchanged."""
        seeds = ["https://photos.onehome.com/photo1.jpg", "https://example.com/photo2.jpg"]
        assert enumerate_redfin_images(seeds) == seeds

    def test_returns_seeds_for_empty_list(self):
        assert enumerate_redfin_images([]) == []

    @patch("app.parsers.onehome.httpx.Client")
    def test_enumerates_sequential_images(self, mock_client_cls):
        """Finds additional images by probing sequential indices."""
        seeds = [
            "https://ssl.cdn-redfin.com/photo/42/genMid.12345_3_z.jpg",
        ]

        def head_side_effect(url):
            # Images exist at indices 0-9, 404 after that
            m = re.search(r"genMid\.12345_(\d+)_z\.jpg", url)
            resp = MagicMock()
            if m and int(m.group(1)) <= 9:
                resp.status_code = 200
            else:
                resp.status_code = 404
            return resp

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.head.side_effect = head_side_effect
        mock_client_cls.return_value = mock_client

        result = enumerate_redfin_images(seeds)
        assert len(result) == 10  # indices 0-9
        assert result[0] == "https://ssl.cdn-redfin.com/photo/42/genMid.12345_0_z.jpg"
        assert result[9] == "https://ssl.cdn-redfin.com/photo/42/genMid.12345_9_z.jpg"

    @patch("app.parsers.onehome.httpx.Client")
    def test_stops_after_consecutive_misses(self, mock_client_cls):
        """Stops probing after 3 consecutive 404s."""
        seeds = [
            "https://ssl.cdn-redfin.com/photo/42/genMid.99999_0_z.jpg",
        ]

        call_count = 0

        def head_side_effect(url):
            nonlocal call_count
            call_count += 1
            resp = MagicMock()
            # Only index 0 exists
            if "_0_z.jpg" in url:
                resp.status_code = 200
            else:
                resp.status_code = 404
            return resp

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.head.side_effect = head_side_effect
        mock_client_cls.return_value = mock_client

        result = enumerate_redfin_images(seeds)
        assert len(result) == 1
        # Should have stopped at index 3 (0=hit, 1=miss, 2=miss, 3=miss → stop)
        assert call_count == 4

    @patch("app.parsers.onehome.httpx.Client")
    def test_returns_seeds_on_network_error(self, mock_client_cls):
        """Falls back to seeds if network fails entirely."""
        seeds = [
            "https://ssl.cdn-redfin.com/photo/42/genMid.12345_3_z.jpg",
        ]
        mock_client_cls.side_effect = Exception("Connection refused")

        result = enumerate_redfin_images(seeds)
        assert result == seeds


class TestBotDetectionMitigation:
    """Tests for Redfin bot detection avoidance."""

    def test_rotating_user_agent_returns_valid_browser(self):
        """_get_rotating_user_agent returns a realistic User-Agent."""
        ua = _get_rotating_user_agent()
        assert "Mozilla" in ua
        assert "Chrome" in ua or "Firefox" in ua or "Safari" in ua

    def test_rotating_user_agent_varies(self):
        """_get_rotating_user_agent returns different agents."""
        agents = {_get_rotating_user_agent() for _ in range(20)}
        assert len(agents) > 1, "Should rotate through multiple User-Agents"

    def test_detects_redfin_bot_block_page(self):
        """_is_bot_block_page detects Redfin 'unknown address' bot-block errors."""
        # Redfin bot-block: minimal error page with "unknown address" and error styling
        bot_block_html = (
            '<html><body class="rf-error">'
            '<h1>Unknown Address</h1>'
            '<p>We cannot find this address.</p>'
            '</body></html>'
        )
        assert _is_bot_block_page(bot_block_html) is True

    def test_normal_404_with_nav_not_bot_block(self):
        """_is_bot_block_page does not flag normal 404 pages that have navigation."""
        normal_404 = (
            "<html><body>"
            "<nav><a href='/'>Home</a></nav>"
            "<h1>Property Not Found</h1>"
            "<p>The address you're looking for is not available.</p>"
            "</body></html>"
        )
        assert _is_bot_block_page(normal_404) is False

    def test_delisted_property_not_bot_block(self):
        """_is_bot_block_page does not flag delisted properties (have details, just no description)."""
        delisted_html = (
            "<html><body>"
            "<div class='property-details'>"
            "<span>123 Main Street</span>"
            "<span class='price'>$1,500,000</span>"
            "</div>"
            "<p>This property is no longer on the market.</p>"
            "</body></html>"
        )
        assert _is_bot_block_page(delisted_html) is False

    def test_substantial_content_not_bot_block(self):
        """_is_bot_block_page ignores 'unknown address' if page has substantial content (>5000 chars)."""
        # Real listing page with lots of details that happens to mention "unknown address"
        substantial_html = (
            "<html><body>"
            + ("<p>Home details: " + "real estate description " * 500 + "</p>")
            + "<p>The unknown address field has been processed.</p>"
            + "</body></html>"
        )
        assert _is_bot_block_page(substantial_html) is False

    def test_normal_page_with_listing_details_not_bot_block(self):
        """_is_bot_block_page does not flag normal listing pages."""
        normal_html = (
            "<html><body>"
            "<h1>123 Main St</h1>"
            "<p>Beautiful home with finished basement and nice address.</p>"
            "<div id='listingRemarks'>"
            "<p>This 3-bed, 2-bath home features a modern kitchen.</p>"
            "</div>"
            "</body></html>"
        )
        assert _is_bot_block_page(normal_html) is False

    @patch("app.parsers.onehome.time.sleep")
    @patch("app.parsers.onehome.httpx.Client")
    def test_scrape_static_retries_with_delay(self, mock_client_cls, mock_sleep):
        """_scrape_static adds delay on retry attempts."""
        mock_response = MagicMock()
        mock_response.text = "<html><body><p>Real description here with real estate content</p></body></html>"
        mock_response.raise_for_status = MagicMock()
        mock_client_cls.return_value.__enter__.return_value.get.return_value = mock_response

        # First attempt (no sleep)
        _scrape_static("http://example.com", attempt=1)
        mock_sleep.assert_not_called()

        # Second attempt (sleeps)
        _scrape_static("http://example.com", attempt=2)
        assert mock_sleep.called

    @patch("app.parsers.onehome.httpx.Client")
    def test_scrape_static_detects_bot_block_and_returns_none(self, mock_client_cls):
        """_scrape_static returns None when bot-block page detected."""
        mock_response = MagicMock()
        mock_response.text = "<html><body>Unknown Address</body></html>"
        mock_response.raise_for_status = MagicMock()
        mock_client_cls.return_value.__enter__.return_value.get.return_value = mock_response

        result = _scrape_static("http://example.com", attempt=1)
        assert result is None

    @patch("app.parsers.onehome.httpx.Client")
    def test_scrape_static_includes_referer_and_other_headers(self, mock_client_cls):
        """_scrape_static sends realistic headers including Referer, DNT."""
        mock_response = MagicMock()
        mock_response.text = "<html><body><p>Listing details here for real estate evaluation</p></body></html>"
        mock_response.raise_for_status = MagicMock()
        mock_client_cls.return_value.__enter__.return_value.get.return_value = mock_response

        _scrape_static("http://example.com", attempt=1)

        # Check that headers include realistic fields
        call_kwargs = mock_client_cls.return_value.__enter__.return_value.get.call_args[1]
        headers = call_kwargs.get("headers", {})
        assert "Referer" in headers
        assert headers["Referer"] == "https://www.google.com/"
        assert "DNT" in headers
        assert headers["DNT"] == "1"
        assert "Connection" in headers
        assert "User-Agent" in headers
