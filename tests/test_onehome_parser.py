"""Tests for OneHome/Matrix MLS email parser."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from app.parsers.onehome import (
    OneHomeParser,
    scrape_listing_description,
    _has_useful_content,
    _is_spa_url,
    _try_redfin_fallback,
    _scrape_static,
    _search_redfin_url,
    _extract_description_from_html,
    _extract_image_urls,
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
