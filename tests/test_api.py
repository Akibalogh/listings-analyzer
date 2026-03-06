"""Tests for API endpoints: scrape, reprocess, criteria, images."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def authed_client(client):
    """Client with auth bypassed via mocked session cookie."""
    with patch("app.main._get_current_user", return_value="test@example.com"):
        yield client


class TestScrapeEndpoint:
    """Tests for POST /listings/{listing_id}/scrape."""

    def test_scrape_requires_auth(self, client):
        res = client.post("/listings/1/scrape", json={"listing_url": "https://example.com"})
        assert res.status_code == 401

    def test_scrape_rejects_empty_url(self, authed_client):
        with patch("app.main.db.get_listing_by_id", return_value={"id": 1}):
            res = authed_client.post("/listings/1/scrape", json={"listing_url": ""})
            assert res.status_code == 400

    def test_scrape_rejects_invalid_url(self, authed_client):
        with patch("app.main.db.get_listing_by_id", return_value={"id": 1}):
            res = authed_client.post("/listings/1/scrape", json={"listing_url": "not-a-url"})
            assert res.status_code == 400

    def test_scrape_returns_404_for_missing_listing(self, authed_client):
        with patch("app.main.db.get_listing_by_id", return_value=None):
            res = authed_client.post("/listings/999/scrape", json={"listing_url": "https://example.com/listing"})
            assert res.status_code == 404

    @patch("app.main.db.get_active_criteria", return_value=None)
    @patch("app.main.db.update_listing_description")
    @patch("app.parsers.onehome.scrape_listing_description", return_value=("Beautiful home with finished basement", []))
    @patch("app.main.db.get_listing_by_id", return_value={"id": 1, "address": "10 Sherman Ave"})
    def test_scrape_success_no_criteria(self, mock_get, mock_scrape, mock_update, mock_criteria, authed_client):
        res = authed_client.post("/listings/1/scrape", json={"listing_url": "https://example.com/listing/123"})
        assert res.status_code == 200
        data = res.json()
        assert data["description_found"] is True
        assert data["listing_url"] == "https://example.com/listing/123"
        mock_update.assert_called_once_with(1, "https://example.com/listing/123", "Beautiful home with finished basement")

    @patch("app.main.db.get_active_criteria", return_value=None)
    @patch("app.main.db.update_listing_description")
    @patch("app.parsers.onehome.scrape_listing_description", return_value=(None, []))
    @patch("app.main.db.get_listing_by_id", return_value={"id": 1, "address": "Test"})
    def test_scrape_no_description_found(self, mock_get, mock_scrape, mock_update, mock_criteria, authed_client):
        res = authed_client.post("/listings/1/scrape", json={"listing_url": "https://example.com/listing/456"})
        assert res.status_code == 200
        data = res.json()
        assert data["description_found"] is False


class TestReprocessEndpoint:
    """Tests for POST /reprocess."""

    def test_reprocess_requires_auth(self, client):
        res = client.post("/reprocess")
        assert res.status_code == 401

    @patch("app.main.db.get_active_criteria", return_value=None)
    @patch("app.main.db.get_all_processed_gmail_ids", return_value=[])
    def test_reprocess_no_emails(self, mock_ids, mock_criteria, authed_client):
        res = authed_client.post("/reprocess")
        assert res.status_code == 200
        data = res.json()
        assert data["emails_checked"] == 0
        assert data["urls_updated"] == 0

    @patch("app.main.db.get_active_criteria", return_value=None)
    @patch("app.main.db.update_listing_url_by_mls")
    @patch("app.parsers.onehome.scrape_listing_description", return_value=("Has finished basement", []))
    @patch("app.main.db.get_all_processed_gmail_ids", return_value=["gmail_id_1"])
    def test_reprocess_finds_urls(self, mock_ids, mock_scrape, mock_update, mock_criteria, authed_client):
        from app.models import ParsedListing

        mock_email = {
            "gmail_id": "gmail_id_1",
            "html": "<html></html>",
            "text": "",
            "subject": "Test",
        }
        mock_listing = ParsedListing(
            address="10 Sherman Ave",
            mls_id="123456",
            listing_url="https://portal.onehome.com/listing/123",
        )

        with patch("app.gmail.fetch_email_by_id", return_value=mock_email):
            with patch("app.parsers.parser_chain.parse", return_value=[mock_listing]):
                res = authed_client.post("/reprocess")
                assert res.status_code == 200
                data = res.json()
                assert data["emails_checked"] == 1
                assert data["urls_updated"] == 1
                assert data["descriptions_scraped"] == 1


class TestCriteriaEndpoint:
    """Tests for GET/PUT /criteria."""

    @patch("app.main.db.get_active_criteria", return_value=None)
    def test_get_criteria_is_public(self, mock_criteria, client):
        """GET /criteria is public — no auth required."""
        res = client.get("/criteria")
        assert res.status_code == 200
        data = res.json()
        assert data["instructions"] == ""
        assert data["version"] == 0

    @patch("app.main.db.get_active_criteria", return_value={"instructions": "Test criteria", "version": 3, "created_by": "test@example.com"})
    def test_get_criteria_with_data(self, mock_criteria, client):
        res = client.get("/criteria")
        assert res.status_code == 200
        data = res.json()
        assert data["instructions"] == "Test criteria"
        assert data["version"] == 3

    def test_put_criteria_requires_auth(self, client):
        """PUT /criteria still requires auth."""
        res = client.put("/criteria", json={"instructions": "new instructions"})
        assert res.status_code == 401

    def test_put_criteria_rejects_empty(self, authed_client):
        res = authed_client.put("/criteria", json={"instructions": ""})
        assert res.status_code == 400

    @patch("app.main.db.get_criteria_history", return_value=[])
    def test_history_is_public(self, mock_history, client):
        """GET /criteria/history is public — no auth required."""
        res = client.get("/criteria/history")
        assert res.status_code == 200
        assert res.json() == []

    @patch("app.main.db.get_criteria_history", return_value=[
        {"version": 3, "instructions": "Criteria v3 text here", "created_by": "aki@example.com", "created_at": "2026-02-28"},
        {"version": 2, "instructions": "Criteria v2 text here", "created_by": "system", "created_at": "2026-02-01"},
    ])
    def test_history_returns_versions_newest_first(self, mock_history, client):
        res = client.get("/criteria/history")
        assert res.status_code == 200
        data = res.json()
        assert len(data) == 2
        assert data[0]["version"] == 3
        assert data[1]["version"] == 2

    @patch("app.main.db.get_criteria_history", return_value=[
        {"version": 1, "instructions": "A" * 100, "created_by": "aki@example.com", "created_at": "2026-01-01"},
    ])
    def test_history_preview_truncated_to_80(self, mock_history, client):
        res = client.get("/criteria/history")
        assert res.status_code == 200
        data = res.json()
        assert len(data[0]["preview"]) == 80
        assert len(data[0]["instructions"]) == 100  # full text returned


class TestImagesEndpoint:
    """Tests for POST /listings/{listing_id}/images."""

    def test_images_requires_auth(self, client):
        res = client.post("/listings/1/images", json={"image_urls": []})
        assert res.status_code == 401

    @patch("app.main.db.get_listing_by_id", return_value=None)
    def test_images_404_missing_listing(self, mock_get, authed_client):
        res = authed_client.post("/listings/999/images", json={"image_urls": ["https://example.com/img.jpg"]})
        assert res.status_code == 404

    def test_images_rejects_non_list(self, authed_client):
        with patch("app.main.db.get_listing_by_id", return_value={"id": 1}):
            res = authed_client.post("/listings/1/images", json={"image_urls": "not-a-list"})
            assert res.status_code == 400

    def test_images_rejects_invalid_url(self, authed_client):
        with patch("app.main.db.get_listing_by_id", return_value={"id": 1}):
            res = authed_client.post("/listings/1/images", json={"image_urls": ["ftp://bad"]})
            assert res.status_code == 400


class TestPublicEndpoints:
    """Tests for endpoints that should be publicly accessible (no auth)."""

    @patch("app.main.db.get_all_listings", return_value=[])
    def test_listings_is_public(self, mock_get, client):
        res = client.get("/listings")
        assert res.status_code == 200
        data = res.json()
        assert data["count"] == 0
        assert data["listings"] == []

    @patch("app.main.db.get_all_listings", return_value=[{"id": 1, "address": "123 Main St", "score": 75}])
    def test_listings_returns_data_without_auth(self, mock_get, client):
        res = client.get("/listings")
        assert res.status_code == 200
        data = res.json()
        assert data["count"] == 1
        assert data["listings"][0]["address"] == "123 Main St"

    @patch("app.main.db.get_active_criteria", return_value={"instructions": "Test", "version": 1, "created_by": "admin"})
    def test_criteria_is_public(self, mock_criteria, client):
        res = client.get("/criteria")
        assert res.status_code == 200
        data = res.json()
        assert data["instructions"] == "Test"

    def test_rescore_status_is_public(self, client):
        res = client.get("/rescore/status")
        assert res.status_code == 200

    def test_poll_still_requires_auth(self, client):
        res = client.post("/poll")
        assert res.status_code == 401

    def test_reprocess_still_requires_auth(self, client):
        res = client.post("/reprocess")
        assert res.status_code == 401


class TestTouredEndpoint:
    """Tests for POST /listings/{listing_id}/toured."""

    def test_toured_requires_auth(self, client):
        res = client.post("/listings/1/toured", json={"toured": True})
        assert res.status_code == 401

    @patch("app.main.db.get_listing_by_id", return_value=None)
    def test_toured_404_missing_listing(self, mock_get, authed_client):
        res = authed_client.post("/listings/999/toured", json={"toured": True})
        assert res.status_code == 404

    @patch("app.main.db.mark_listing_toured")
    @patch("app.main.db.get_listing_by_id", return_value={"id": 1, "address": "6 Sunset Lane"})
    def test_toured_marks_true(self, mock_get, mock_mark, authed_client):
        res = authed_client.post("/listings/1/toured", json={"toured": True})
        assert res.status_code == 200
        data = res.json()
        assert data["listing_id"] == 1
        assert data["toured"] is True
        mock_mark.assert_called_once_with(1, True)

    @patch("app.main.db.mark_listing_toured")
    @patch("app.main.db.get_listing_by_id", return_value={"id": 2, "address": "10 Sherman Ave"})
    def test_toured_unmarks(self, mock_get, mock_mark, authed_client):
        res = authed_client.post("/listings/2/toured", json={"toured": False})
        assert res.status_code == 200
        data = res.json()
        assert data["toured"] is False
        mock_mark.assert_called_once_with(2, False)

    @patch("app.main.db.mark_listing_toured")
    @patch("app.main.db.get_listing_by_id", return_value={"id": 1, "address": "Test"})
    def test_toured_defaults_to_true(self, mock_get, mock_mark, authed_client):
        """Omitting 'toured' key defaults to True."""
        res = authed_client.post("/listings/1/toured", json={})
        assert res.status_code == 200
        data = res.json()
        assert data["toured"] is True
        mock_mark.assert_called_once_with(1, True)


class TestSoldEndpoint:
    """Tests for POST /listings/{listing_id}/sold."""

    def test_sold_requires_auth(self, client):
        res = client.post("/listings/1/sold")
        assert res.status_code == 401

    @patch("app.main.db.get_listing_by_id", return_value=None)
    def test_sold_404_missing_listing(self, mock_get, authed_client):
        res = authed_client.post("/listings/999/sold")
        assert res.status_code == 404

    @patch("app.main.db.get_connection")
    @patch("app.main.db.get_listing_by_id", return_value={"id": 1, "address": "6 Sunset Ln"})
    def test_sold_deletes_listing(self, mock_get, mock_conn, authed_client):
        mock_cur = mock_conn.return_value.__enter__.return_value.cursor.return_value
        mock_cur.rowcount = 1
        res = authed_client.post("/listings/1/sold")
        assert res.status_code == 200
        data = res.json()
        assert data["listing_id"] == 1
        assert data["deleted"] is True


class TestFilteredRoutes:
    """Tests for filter URL routes — serve dashboard with pre-set filter."""

    def test_want_to_go_returns_dashboard(self, client):
        res = client.get("/want-to-go")
        assert res.status_code == 200
        assert "Listings Analyzer" in res.text
        assert "want-to-go" in res.text  # route is in _filterRoutes JS

    def test_toured_returns_dashboard(self, client):
        res = client.get("/toured")
        assert res.status_code == 200
        assert "Listings Analyzer" in res.text

    def test_passed_returns_dashboard(self, client):
        res = client.get("/passed")
        assert res.status_code == 200
        assert "Listings Analyzer" in res.text

    def test_no_auth_required(self, client):
        """All filter routes are public — no auth needed."""
        assert client.get("/want-to-go").status_code == 200
        assert client.get("/toured").status_code == 200
        assert client.get("/passed").status_code == 200

    def test_non_reject_route_serves_dashboard(self, client):
        res = client.get("/non-reject")
        assert res.status_code == 200
        assert "Listings Analyzer" in res.text


class TestAddListingFromUrl:
    """Tests for POST /listings/add."""

    def test_requires_auth(self, client):
        res = client.post("/listings/add", json={"url": "https://redf.in/abc"})
        assert res.status_code == 401

    def test_rejects_empty_url(self, authed_client):
        res = authed_client.post("/listings/add", json={"url": ""})
        assert res.status_code == 400

    @patch("app.main.db.get_active_criteria", return_value=None)
    @patch("app.main.db.save_listing", return_value=42)
    @patch("app.main.db.save_processed_email", return_value=1)
    @patch("app.main.db.is_listing_duplicate_by_address", return_value=False)
    @patch("httpx.Client")
    def test_adds_listing_from_redfin_url(
        self, mock_client_cls, mock_dedup, mock_save_email, mock_save_listing,
        mock_criteria, authed_client
    ):
        mock_response = MagicMock()
        mock_response.url = "https://www.redfin.com/NY/Croton-On-Hudson/101-Upper-North-Highland-Pl-10520/home/123"
        mock_response.status_code = 200
        mock_response.text = "<html></html>"
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.head.return_value = mock_response
        mock_client.get.return_value = mock_response
        mock_client_cls.return_value = mock_client

        res = authed_client.post("/listings/add", json={"url": "https://redf.in/qDdarb"})
        assert res.status_code == 200
        data = res.json()
        assert data["listing_id"] == 42
        assert data["address"] is not None
        assert "Upper North Highland" in data["address"]

    @patch("app.main.db.is_listing_duplicate_by_address", return_value=True)
    @patch("httpx.Client")
    def test_rejects_duplicate(self, mock_client_cls, mock_dedup, authed_client):
        mock_response = MagicMock()
        mock_response.url = "https://www.redfin.com/NY/Rye/10-Sherman-Ave-10580/home/123"
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.head.return_value = mock_response
        mock_client_cls.return_value = mock_client

        res = authed_client.post("/listings/add", json={"url": "https://redf.in/xyz"})
        assert res.status_code == 409

    def test_rejects_invalid_url(self, authed_client):
        res = authed_client.post("/listings/add", json={"url": "not-a-url"})
        assert res.status_code == 400

    @patch("app.main.db.get_active_criteria", return_value=None)
    @patch("app.main.db.save_listing", return_value=55)
    @patch("app.main.db.save_processed_email", return_value=1)
    @patch("app.main.db.is_listing_duplicate_by_address", return_value=False)
    @patch("httpx.Client")
    def test_extracts_town_and_state(
        self, mock_client_cls, mock_dedup, mock_save_email, mock_save_listing,
        mock_criteria, authed_client
    ):
        """Redfin URL path yields correct town and state."""
        mock_response = MagicMock()
        mock_response.url = "https://www.redfin.com/NY/Chappaqua/19-Georgia-Ln-10514/home/456"
        mock_response.status_code = 200
        mock_response.text = "<html></html>"
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.head.return_value = mock_response
        mock_client.get.return_value = mock_response
        mock_client_cls.return_value = mock_client

        res = authed_client.post("/listings/add", json={"url": "https://redf.in/abc123"})
        assert res.status_code == 200
        data = res.json()
        assert data["town"] == "Chappaqua"
        assert data["listing_id"] == 55

    @patch("app.main.db.get_active_criteria", return_value=None)
    @patch("app.main.db.save_listing", return_value=60)
    @patch("app.main.db.save_processed_email", return_value=1)
    @patch("httpx.Client")
    def test_non_redfin_url_no_address(
        self, mock_client_cls, mock_save_email, mock_save_listing,
        mock_criteria, authed_client
    ):
        """Non-Redfin URL without parseable address still creates a listing."""
        mock_response = MagicMock()
        mock_response.url = "https://www.zillow.com/homedetails/123"
        mock_response.status_code = 200
        mock_response.text = "<html></html>"
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.head.return_value = mock_response
        mock_client.get.return_value = mock_response
        mock_client_cls.return_value = mock_client

        res = authed_client.post("/listings/add", json={"url": "https://www.zillow.com/homedetails/123"})
        assert res.status_code == 200
        data = res.json()
        assert data["listing_id"] == 60
        assert data["address"] is None


class TestTourRequest:
    """Tests for POST /listings/{listing_id}/tour-request."""

    def test_requires_auth(self, client):
        res = client.post("/listings/1/tour-request", json={"tour_requested": True})
        assert res.status_code == 401

    @patch("app.main.db.mark_listing_tour_requested")
    @patch("app.main.db.get_listing_by_id", return_value={"id": 1, "address": "6 Sunset Lane"})
    def test_toggles_flag(self, mock_get, mock_mark, authed_client):
        res = authed_client.post("/listings/1/tour-request", json={"tour_requested": True})
        assert res.status_code == 200
        data = res.json()
        assert data["listing_id"] == 1
        assert data["tour_requested"] is True
        mock_mark.assert_called_once_with(1, True)

        mock_mark.reset_mock()
        res = authed_client.post("/listings/1/tour-request", json={"tour_requested": False})
        assert res.status_code == 200
        assert res.json()["tour_requested"] is False
        mock_mark.assert_called_once_with(1, False)

    @patch("app.main.db.get_listing_by_id", return_value=None)
    def test_404_for_missing(self, mock_get, authed_client):
        res = authed_client.post("/listings/999/tour-request", json={"tour_requested": True})
        assert res.status_code == 404

    @patch("app.main.db.mark_listing_tour_requested")
    @patch("app.main.db.get_listing_by_id", return_value={"id": 5, "address": "Test"})
    def test_defaults_to_true(self, mock_get, mock_mark, authed_client):
        """Omitting 'tour_requested' key defaults to True."""
        res = authed_client.post("/listings/5/tour-request", json={})
        assert res.status_code == 200
        assert res.json()["tour_requested"] is True
        mock_mark.assert_called_once_with(5, True)


class TestPassedEndpoint:
    """Tests for POST /listings/{listing_id}/passed."""

    def test_requires_auth(self, client):
        res = client.post("/listings/1/passed", json={"passed": True})
        assert res.status_code == 401

    @patch("app.main.db.get_listing_by_id", return_value=None)
    def test_404_for_missing(self, mock_get, authed_client):
        res = authed_client.post("/listings/999/passed", json={"passed": True})
        assert res.status_code == 404

    @patch("app.main.db.mark_listing_passed")
    @patch("app.main.db.get_listing_by_id", return_value={"id": 1, "address": "10 Main St"})
    def test_marks_passed(self, mock_get, mock_mark, authed_client):
        res = authed_client.post("/listings/1/passed", json={"passed": True})
        assert res.status_code == 200
        data = res.json()
        assert data["listing_id"] == 1
        assert data["passed"] is True
        mock_mark.assert_called_once_with(1, True)

    @patch("app.main.db.mark_listing_passed")
    @patch("app.main.db.get_listing_by_id", return_value={"id": 1, "address": "10 Main St"})
    def test_unmarks_passed(self, mock_get, mock_mark, authed_client):
        res = authed_client.post("/listings/1/passed", json={"passed": False})
        assert res.status_code == 200
        assert res.json()["passed"] is False
        mock_mark.assert_called_once_with(1, False)

    @patch("app.main.db.mark_listing_passed")
    @patch("app.main.db.get_listing_by_id", return_value={"id": 5, "address": "Test"})
    def test_defaults_to_true(self, mock_get, mock_mark, authed_client):
        """Omitting 'passed' key defaults to True."""
        res = authed_client.post("/listings/5/passed", json={})
        assert res.status_code == 200
        assert res.json()["passed"] is True
        mock_mark.assert_called_once_with(5, True)


class TestManageEndpoint:
    """Tests for POST /manage/sync-criteria."""

    def test_sync_rejects_missing_key(self, client):
        res = client.post("/manage/sync-criteria")
        assert res.status_code == 403

    def test_sync_rejects_wrong_key(self, client):
        res = client.post("/manage/sync-criteria", headers={"x-manage-key": "wrong"})
        assert res.status_code == 403

    @patch("app.main._start_rescore")
    @patch("app.main.db.get_active_criteria", return_value={"version": 7, "instructions": "test criteria"})
    @patch("app.main.settings")
    def test_sync_with_valid_key(self, mock_settings, mock_get_criteria, mock_rescore, client):
        mock_settings.manage_key = "test-secret-key"
        res = client.post("/manage/sync-criteria", headers={"x-manage-key": "test-secret-key"})
        assert res.status_code == 200
        data = res.json()
        assert data["synced"] is True
        assert data["version"] == 7
        assert data["rescore_started"] is True
        mock_get_criteria.assert_called_once()
        mock_rescore.assert_called_once()

    @patch("app.main.db.get_active_criteria", return_value=None)
    @patch("app.main.settings")
    def test_sync_no_criteria_returns_404(self, mock_settings, mock_get_criteria, client):
        mock_settings.manage_key = "test-secret-key"
        res = client.post("/manage/sync-criteria", headers={"x-manage-key": "test-secret-key"})
        assert res.status_code == 404


class TestManageScrapeDescriptions:
    """Tests for POST /manage/scrape-descriptions."""

    def test_scrape_descriptions_rejects_missing_key(self, client):
        res = client.post("/manage/scrape-descriptions")
        assert res.status_code == 403

    def test_scrape_descriptions_rejects_wrong_key(self, client):
        res = client.post("/manage/scrape-descriptions", headers={"x-manage-key": "wrong"})
        assert res.status_code == 403

    @patch("app.main.db.get_all_listing_ids", return_value=[])
    @patch("app.main.settings")
    def test_scrape_descriptions_empty_db(self, mock_settings, mock_ids, client):
        mock_settings.manage_key = "test-key"
        res = client.post("/manage/scrape-descriptions", headers={"x-manage-key": "test-key"})
        assert res.status_code == 200
        data = res.json()
        assert data["listings_checked"] == 0
        assert data["descriptions_scraped"] == 0
        assert data["skipped"] == 0

    @patch("app.main.db.get_active_criteria", return_value=None)
    @patch("app.main.db.update_listing_description")
    @patch("app.parsers.onehome.scrape_listing_description", return_value=("Beautiful 4BR colonial", ["https://img.com/1.jpg"]))
    @patch("app.main.db.add_listing_images")
    @patch("app.main.db.get_listing_by_id", return_value={
        "id": 1, "address": "10 Test St", "town": "Rye", "state": "NY",
        "zip_code": "10573", "mls_id": "123456",
        "listing_url": "https://example.com/listing", "description": None,
        "year_built": 2000,
    })
    @patch("app.main.db.get_all_listing_ids", return_value=[1])
    @patch("app.main.settings")
    def test_scrape_descriptions_scrapes_one(
        self, mock_settings, mock_ids, mock_get, mock_add_imgs, mock_scrape,
        mock_update, mock_criteria, client,
    ):
        mock_settings.manage_key = "test-key"
        res = client.post("/manage/scrape-descriptions", headers={"x-manage-key": "test-key"})
        assert res.status_code == 200
        data = res.json()
        assert data["descriptions_scraped"] == 1
        assert data["images_found"] == 1
        mock_update.assert_called_once_with(1, "https://example.com/listing", "Beautiful 4BR colonial")
        mock_add_imgs.assert_called_once()

    @patch("app.main.db.get_listing_by_id", return_value={
        "id": 2, "address": "20 Test", "listing_url": None, "description": None,
        "year_built": 2000,
    })
    @patch("app.main.db.get_all_listing_ids", return_value=[2])
    @patch("app.main.settings")
    def test_scrape_descriptions_skips_no_url_no_town(self, mock_settings, mock_ids, mock_get, client):
        """No URL and no town — can't search DDG, so skipped."""
        mock_settings.manage_key = "test-key"
        res = client.post("/manage/scrape-descriptions", headers={"x-manage-key": "test-key"})
        assert res.status_code == 200
        data = res.json()
        assert data["skipped"] == 1
        assert data["descriptions_scraped"] == 0

    @patch("app.parsers.onehome._search_redfin_url", return_value="https://www.redfin.com/NY/Scarsdale/196-Old-Army-Rd-10583/home/123")
    @patch("app.main.db.get_connection")
    @patch("app.main.db._placeholder", return_value="?")
    @patch("app.parsers.onehome.scrape_listing_description", return_value=("Nice house", ["img1.jpg"]))
    @patch("app.main.db.update_listing_description")
    @patch("app.main.db.add_listing_images")
    @patch("app.main.db.get_listing_by_id", return_value={
        "id": 5, "address": "196 Old Army Rd", "town": "Scarsdale",
        "state": "NY", "zip_code": "10583", "mls_id": None,
        "listing_url": None, "description": None,
        "year_built": 2000,
    })
    @patch("app.main.db.get_all_listing_ids", return_value=[5])
    @patch("app.main.settings")
    def test_scrape_descriptions_finds_url_via_ddg(
        self, mock_settings, mock_ids, mock_get, mock_add_imgs,
        mock_update, mock_scrape, mock_ph, mock_conn, mock_ddg, client
    ):
        """Listings without URL get one via DDG search, then get scraped."""
        mock_settings.manage_key = "test-key"
        mock_cur = mock_conn.return_value.__enter__.return_value.cursor.return_value
        res = client.post("/manage/scrape-descriptions", headers={"x-manage-key": "test-key"})
        assert res.status_code == 200
        data = res.json()
        assert data["urls_found"] == 1
        assert data["descriptions_scraped"] == 1

    @patch("app.main.db.get_listing_by_id", return_value={
        "id": 3, "address": "30 Test", "listing_url": "https://example.com",
        "description": "Already has a description",
        "year_built": 2000,
    })
    @patch("app.main.db.get_all_listing_ids", return_value=[3])
    @patch("app.main.settings")
    def test_scrape_descriptions_skips_existing_description(self, mock_settings, mock_ids, mock_get, client):
        mock_settings.manage_key = "test-key"
        res = client.post("/manage/scrape-descriptions", headers={"x-manage-key": "test-key"})
        assert res.status_code == 200
        data = res.json()
        assert data["skipped"] == 1
        assert data["descriptions_scraped"] == 0

    @patch("app.main._start_rescore")
    @patch("app.main.db.get_active_criteria", return_value={"version": 5, "instructions": "criteria"})
    @patch("app.main.db.update_listing_description")
    @patch("app.parsers.onehome.scrape_listing_description", return_value=("New description", []))
    @patch("app.main.db.get_listing_by_id", return_value={
        "id": 1, "address": "10 Test", "listing_url": "https://example.com",
        "description": None, "town": "Rye", "state": "NY", "zip_code": "10573", "mls_id": "999",
        "year_built": 2000,
    })
    @patch("app.main.db.get_all_listing_ids", return_value=[1])
    @patch("app.main.settings")
    def test_scrape_descriptions_triggers_rescore(
        self, mock_settings, mock_ids, mock_get, mock_scrape, mock_update,
        mock_criteria, mock_rescore, client,
    ):
        mock_settings.manage_key = "test-key"
        res = client.post("/manage/scrape-descriptions", headers={"x-manage-key": "test-key"})
        assert res.status_code == 200
        data = res.json()
        assert data["rescore_started"] is True
        mock_rescore.assert_called_once()

    @patch("app.main.db.get_active_criteria", return_value=None)
    @patch("app.parsers.onehome.scrape_listing_description", side_effect=Exception("Network error"))
    @patch("app.main.db.get_listing_by_id", return_value={
        "id": 1, "address": "10 Test", "listing_url": "https://example.com",
        "description": None, "town": "Rye", "state": "NY", "zip_code": "10573", "mls_id": "111",
        "price": 1000000, "year_built": 2000,
    })
    @patch("app.main.db.get_all_listing_ids", return_value=[1])
    @patch("app.main.settings")
    def test_scrape_descriptions_handles_errors(
        self, mock_settings, mock_ids, mock_get, mock_scrape, mock_criteria, client,
    ):
        mock_settings.manage_key = "test-key"
        res = client.post("/manage/scrape-descriptions", headers={"x-manage-key": "test-key"})
        assert res.status_code == 200
        data = res.json()
        assert data["descriptions_scraped"] == 0
        assert len(data["errors"]) == 1
        assert "#1" in data["errors"][0]

    @patch("app.main.db.get_active_criteria", return_value=None)
    @patch("app.main.db.update_listing_fields_by_id")
    @patch("app.parsers.onehome.scrape_listing_structured_data", return_value={
        "price": 1499000, "bedrooms": 4, "bathrooms": 3, "sqft": 2800,
    })
    @patch("app.main.db.get_listing_by_id")
    @patch("app.main.db.get_all_listing_ids", return_value=[10])
    @patch("app.main.settings")
    def test_scrape_descriptions_phase3_backfills_data(
        self, mock_settings, mock_ids, mock_get, mock_structured, mock_update_fields,
        mock_criteria, client,
    ):
        """Phase 3 backfills structured data for listings missing price/beds/baths/sqft."""
        mock_settings.manage_key = "test-key"
        # Listing has URL and description but no price/beds/baths/sqft
        mock_get.return_value = {
            "id": 10, "address": "19 Georgia Ln", "town": "Chappaqua",
            "state": "NY", "zip_code": "10514", "mls_id": None,
            "listing_url": "https://www.redfin.com/NY/Chappaqua/19-Georgia-Ln/home/123",
            "description": "Nice house", "price": None, "bedrooms": None,
            "bathrooms": None, "sqft": None, "year_built": 2000,
        }
        res = client.post("/manage/scrape-descriptions", headers={"x-manage-key": "test-key"})
        assert res.status_code == 200
        data = res.json()
        assert data["data_backfilled"] == 1
        mock_update_fields.assert_called_once_with(
            10, price=1499000, bedrooms=4, bathrooms=3, sqft=2800,
        )


class TestManageEnrichEndpoint:
    """Tests for POST /manage/enrich."""

    def test_enrich_rejects_missing_key(self, client):
        res = client.post("/manage/enrich")
        assert res.status_code == 403

    def test_enrich_rejects_wrong_key(self, client):
        res = client.post("/manage/enrich", headers={"x-manage-key": "wrong"})
        assert res.status_code == 403

    @patch("app.main.settings")
    def test_enrich_starts_background_task(self, mock_settings, client):
        mock_settings.manage_key = "test-key"
        from app.main import _enrich_state
        _enrich_state["in_progress"] = False
        res = client.post("/manage/enrich", headers={"x-manage-key": "test-key"})
        assert res.status_code == 200
        data = res.json()
        assert data["status"] == "started"

    @patch("app.main._enrich_all")
    @patch("app.main.settings")
    def test_enrich_rejects_concurrent(self, mock_settings, mock_enrich, client):
        mock_settings.manage_key = "test-key"
        from app.main import _enrich_state
        _enrich_state["in_progress"] = True
        try:
            res = client.post("/manage/enrich", headers={"x-manage-key": "test-key"})
            assert res.status_code == 200
            data = res.json()
            assert data["status"] == "already_running"
            mock_enrich.assert_not_called()
        finally:
            _enrich_state["in_progress"] = False

    def test_enrich_status_endpoint(self, client):
        res = client.get("/manage/enrich/status")
        assert res.status_code == 200
        data = res.json()
        assert "in_progress" in data


class TestManageDataQuality:
    """Tests for POST /manage/data-quality."""

    def test_data_quality_rejects_missing_key(self, client):
        res = client.post("/manage/data-quality")
        assert res.status_code == 403

    def test_data_quality_rejects_wrong_key(self, client):
        res = client.post("/manage/data-quality", headers={"x-manage-key": "wrong"})
        assert res.status_code == 403

    @patch("app.main.db.get_connection")
    @patch("app.main.settings")
    def test_data_quality_dry_run_no_bad_listings(self, mock_settings, mock_conn, client):
        mock_settings.manage_key = "test-key"
        mock_settings.is_postgres = False
        # Simulate empty result set
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        mock_conn.return_value.__enter__ = MagicMock(return_value=MagicMock(cursor=MagicMock(return_value=mock_cursor)))
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)

        res = client.post("/manage/data-quality", headers={"x-manage-key": "test-key"})
        assert res.status_code == 200
        data = res.json()
        assert data["no_address_count"] == 0
        assert data["no_url_count"] == 0
        assert data["fix"] is False

    @patch("app.main.db._placeholder", return_value="?")
    @patch("app.main.db.get_connection")
    @patch("app.main.settings")
    def test_data_quality_dry_run_finds_bad_listings(self, mock_settings, mock_conn, mock_ph, client):
        mock_settings.manage_key = "test-key"
        mock_settings.is_postgres = False

        # Simulate rows: one with no address, one with no URL, one with both missing
        mock_rows = [
            {"id": 1, "mls_id": "M001", "address": "", "listing_url": "https://example.com"},
            {"id": 2, "mls_id": "M002", "address": "10 Main St", "listing_url": ""},
            {"id": 3, "mls_id": "M003", "address": None, "listing_url": None},
        ]
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = mock_rows
        mock_connection = MagicMock()
        mock_connection.cursor.return_value = mock_cursor
        mock_conn.return_value.__enter__ = MagicMock(return_value=mock_connection)
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)

        res = client.post("/manage/data-quality", headers={"x-manage-key": "test-key"})
        assert res.status_code == 200
        data = res.json()
        assert data["no_address_count"] == 2  # IDs 1 and 3
        assert data["no_url_count"] == 2  # IDs 2 and 3
        assert data["fix"] is False
        # Should not contain fix-mode keys
        assert "deleted" not in data

    @patch("app.main.db._placeholder", return_value="?")
    @patch("app.main.poll_once", return_value=[])
    @patch("app.main.db.get_active_criteria", return_value=None)
    @patch("app.main.db.get_connection")
    @patch("app.main.settings")
    def test_data_quality_fix_mode_deletes_and_repolls(
        self, mock_settings, mock_conn, mock_criteria, mock_poll, mock_ph, client,
    ):
        mock_settings.manage_key = "test-key"
        mock_settings.is_postgres = False

        # First call: find bad listings; subsequent calls: orphan check + delete
        call_count = [0]
        mock_cursor = MagicMock()

        def fake_fetchall():
            call_count[0] += 1
            if call_count[0] == 1:
                # Bad listings query
                return [
                    {"id": 5, "mls_id": "M005", "address": None, "listing_url": None},
                ]
            else:
                # Orphan query returns empty
                return []

        mock_cursor.fetchall = fake_fetchall
        mock_cursor.rowcount = 1
        mock_connection = MagicMock()
        mock_connection.cursor.return_value = mock_cursor
        mock_conn.return_value.__enter__ = MagicMock(return_value=mock_connection)
        mock_conn.return_value.__exit__ = MagicMock(return_value=False)

        res = client.post("/manage/data-quality?fix=true", headers={"x-manage-key": "test-key"})
        assert res.status_code == 200
        data = res.json()
        assert data["fix"] is True
        assert data["deleted"] == 1
        assert data["re_polled"] == 0
        assert data["rescore_started"] is False
        mock_poll.assert_called_once()


class TestPruneSold:
    """Tests for POST /manage/prune-sold endpoint."""

    @patch("app.main.settings")
    def test_dry_run_returns_sold_listings(self, mock_settings, client):
        mock_settings.manage_key = "test-key"
        mock_settings.is_postgres = False

        # Mock DB to return one listing with Redfin URL
        with patch("app.main.db.get_connection") as mock_conn:
            mock_cursor = MagicMock()
            mock_cursor.fetchall.return_value = [
                {"id": 1, "address": "123 Main St", "town": "Rye", "listing_url": "https://www.redfin.com/NY/Rye/123-Main-St-10580/home/123"},
            ]
            mock_connection = MagicMock()
            mock_connection.cursor.return_value = mock_cursor
            mock_conn.return_value.__enter__ = MagicMock(return_value=mock_connection)
            mock_conn.return_value.__exit__ = MagicMock(return_value=False)

            # Mock Jina Reader returning "sold" page
            with patch("httpx.Client") as mock_httpx:
                mock_response = MagicMock()
                mock_response.text = "This home sold on February 15, 2026 for $1,500,000"
                mock_client = MagicMock()
                mock_client.__enter__ = MagicMock(return_value=mock_client)
                mock_client.__exit__ = MagicMock(return_value=False)
                mock_client.get.return_value = mock_response
                mock_httpx.return_value = mock_client

                res = client.post("/manage/prune-sold", headers={"x-manage-key": "test-key"})
                assert res.status_code == 200
                data = res.json()
                assert data["sold_count"] == 1
                assert data["sold"][0]["id"] == 1
                assert data["fix"] is False
                assert "deleted" not in data

    @patch("app.main.settings")
    def test_rejects_without_key(self, mock_settings, client):
        mock_settings.manage_key = "test-key"
        res = client.post("/manage/prune-sold", headers={"x-manage-key": "wrong"})
        assert res.status_code == 403

    @patch("app.main.settings")
    def test_pending_updates_status_not_delete(self, mock_settings, client):
        """Pending listings get status updated, not deleted."""
        mock_settings.manage_key = "test-key"
        mock_settings.is_postgres = False

        with patch("app.main.db.get_connection") as mock_conn:
            mock_cursor = MagicMock()
            mock_cursor.fetchall.return_value = [
                {"id": 1, "address": "10 Test St", "town": "Rye", "state": "NY",
                 "zip_code": "10580",
                 "listing_url": "https://www.redfin.com/NY/Rye/10-Test-St-10580/home/111"},
            ]
            mock_connection = MagicMock()
            mock_connection.cursor.return_value = mock_cursor
            mock_conn.return_value.__enter__ = MagicMock(return_value=mock_connection)
            mock_conn.return_value.__exit__ = MagicMock(return_value=False)

            # Jina returns a pending page
            with patch("httpx.Client") as mock_httpx:
                mock_response = MagicMock()
                mock_response.text = '{"listingStatus":"pending"} This home is pending sale.'
                mock_client_inst = MagicMock()
                mock_client_inst.__enter__ = MagicMock(return_value=mock_client_inst)
                mock_client_inst.__exit__ = MagicMock(return_value=False)
                mock_client_inst.get.return_value = mock_response
                mock_httpx.return_value = mock_client_inst

                with patch("app.parsers.onehome.check_listing_status"), \
                     patch("app.main.db.update_listing_status") as mock_update:
                    res = client.post(
                        "/manage/prune-sold?fix=true",
                        headers={"x-manage-key": "test-key"},
                    )
                    assert res.status_code == 200
                    data = res.json()
                    assert data["sold_count"] == 0
                    assert data["pending_count"] == 1
                    assert data["pending_updated"] == 1
                    assert "deleted" not in data
                    mock_update.assert_called_once_with(1, "Pending")

    @patch("app.main.settings")
    def test_onekeymls_fallback_finds_sold(self, mock_settings, client):
        """OneKeyMLS pass detects sold listing when Redfin pass can't check it."""
        mock_settings.manage_key = "test-key"
        mock_settings.is_postgres = False

        with patch("app.main.db.get_connection") as mock_conn:
            mock_cursor = MagicMock()
            # Listing with no Redfin URL — only address
            mock_cursor.fetchall.return_value = [
                {"id": 2, "address": "55 Oak Ave", "town": "Scarsdale", "state": "NY",
                 "zip_code": "10583", "listing_url": None},
            ]
            mock_connection = MagicMock()
            mock_connection.cursor.return_value = mock_cursor
            mock_conn.return_value.__enter__ = MagicMock(return_value=mock_connection)
            mock_conn.return_value.__exit__ = MagicMock(return_value=False)

            with patch("app.parsers.onehome.check_listing_status", return_value="Sold") as mock_check:
                res = client.post(
                    "/manage/prune-sold",
                    headers={"x-manage-key": "test-key"},
                )
                assert res.status_code == 200
                data = res.json()
                assert data["sold_count"] == 1
                assert data["sold"][0]["id"] == 2
                assert data["sold"][0]["mls_status"] == "Sold"
                mock_check.assert_called_once_with("55 Oak Ave", "Scarsdale", "NY", "10583")


class TestAddressKeyBackfill:
    """Tests for address_key backfill on init and dedup prevention."""

    def test_normalize_address_ave_avenue_match(self):
        """Avenue and Ave normalize to the same key."""
        from app.enrichment import normalize_address

        key1 = normalize_address("10 Sherman Avenue", "Dobbs Ferry", "NY")
        key2 = normalize_address("10 Sherman Ave", "Dobbs Ferry", "NY")
        assert key1 == key2

    @patch("app.main.settings")
    def test_backfill_address_keys_updates_null_keys(self, mock_settings):
        """_backfill_address_keys fills NULL address_key for listings with addr+town."""
        import sqlite3

        from app.db import _backfill_address_keys
        from app.enrichment import normalize_address

        mock_settings.is_postgres = False
        mock_settings.database_url = None

        # Create in-memory DB with a listing missing address_key
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE listings (id INTEGER PRIMARY KEY, address TEXT, "
            "town TEXT, state TEXT, address_key TEXT)"
        )
        conn.execute(
            "INSERT INTO listings (id, address, town, state, address_key) "
            "VALUES (1, '10 Sherman Avenue', 'Dobbs Ferry', 'NY', NULL)"
        )
        conn.commit()

        with patch("app.db.get_connection") as mock_conn:
            mock_conn.return_value.__enter__ = MagicMock(return_value=conn)
            mock_conn.return_value.__exit__ = MagicMock(return_value=False)
            _backfill_address_keys()

        row = conn.execute("SELECT address_key FROM listings WHERE id = 1").fetchone()
        expected = normalize_address("10 Sherman Avenue", "Dobbs Ferry", "NY")
        assert row["address_key"] == expected

    @patch("app.main.settings")
    def test_backfill_recomputes_stale_state_keys(self, mock_settings):
        """Backfill updates keys where state changed from 'New York' to 'ny'."""
        import sqlite3

        from app.db import _backfill_address_keys
        from app.enrichment import normalize_address

        mock_settings.is_postgres = False
        mock_settings.database_url = None

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE listings (id INTEGER PRIMARY KEY, address TEXT, "
            "town TEXT, state TEXT, address_key TEXT)"
        )
        # Old key with full state name (stale)
        conn.execute(
            "INSERT INTO listings (id, address, town, state, address_key) "
            "VALUES (1, '10 Sherman Avenue', 'Dobbs Ferry', 'New York', "
            "'10 sherman ave|dobbs ferry|new york')"
        )
        conn.commit()

        with patch("app.db.get_connection") as mock_conn:
            mock_conn.return_value.__enter__ = MagicMock(return_value=conn)
            mock_conn.return_value.__exit__ = MagicMock(return_value=False)
            _backfill_address_keys()

        row = conn.execute("SELECT address_key FROM listings WHERE id = 1").fetchone()
        assert row["address_key"] == "10 sherman ave|dobbs ferry|ny"

    @patch("app.main.settings")
    def test_dedup_removes_duplicate_address_keys(self, mock_settings):
        """_dedup_by_address_key keeps best listing and deletes duplicates."""
        import sqlite3

        from app.db import _dedup_by_address_key

        mock_settings.is_postgres = False
        mock_settings.database_url = None

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE listings (id INTEGER PRIMARY KEY, address TEXT, "
            "town TEXT, state TEXT, address_key TEXT, toured BOOLEAN, "
            "mls_id TEXT, listing_url TEXT)"
        )
        conn.execute(
            "CREATE TABLE scores (id INTEGER PRIMARY KEY, listing_id INTEGER)"
        )
        # Listing 1: toured, has mls_id — should be kept
        conn.execute(
            "INSERT INTO listings VALUES (1, '10 Sherman Avenue', 'Dobbs Ferry', "
            "'NY', '10 sherman ave|dobbs ferry|ny', 1, '923146', 'https://example.com')"
        )
        conn.execute("INSERT INTO scores VALUES (1, 1)")
        # Listing 2: not toured, no mls_id — should be deleted
        conn.execute(
            "INSERT INTO listings VALUES (2, '10 Sherman Ave', 'Dobbs Ferry', "
            "'NY', '10 sherman ave|dobbs ferry|ny', 0, NULL, NULL)"
        )
        conn.execute("INSERT INTO scores VALUES (2, 2)")
        conn.commit()

        with patch("app.db.get_connection") as mock_conn:
            mock_conn.return_value.__enter__ = MagicMock(return_value=conn)
            mock_conn.return_value.__exit__ = MagicMock(return_value=False)
            _dedup_by_address_key()

        remaining = conn.execute("SELECT id FROM listings ORDER BY id").fetchall()
        assert [r["id"] for r in remaining] == [1]
        scores = conn.execute("SELECT listing_id FROM scores").fetchall()
        assert [r["listing_id"] for r in scores] == [1]

    @patch("app.main.settings")
    def test_dedup_keeps_toured_over_non_toured(self, mock_settings):
        """Dedup prefers the toured listing even if it has a higher id."""
        import sqlite3

        from app.db import _dedup_by_address_key

        mock_settings.is_postgres = False
        mock_settings.database_url = None

        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(
            "CREATE TABLE listings (id INTEGER PRIMARY KEY, address TEXT, "
            "town TEXT, state TEXT, address_key TEXT, toured BOOLEAN, "
            "mls_id TEXT, listing_url TEXT)"
        )
        conn.execute(
            "CREATE TABLE scores (id INTEGER PRIMARY KEY, listing_id INTEGER)"
        )
        # Listing 1: NOT toured, lower id
        conn.execute(
            "INSERT INTO listings VALUES (1, '6 Sunset Ln', 'Hartsdale', "
            "'NY', '6 sunset ln|hartsdale|ny', 0, NULL, NULL)"
        )
        # Listing 2: toured, higher id — should be kept
        conn.execute(
            "INSERT INTO listings VALUES (2, '6 Sunset Lane', 'Hartsdale', "
            "'NY', '6 sunset ln|hartsdale|ny', 1, '999999', 'https://example.com')"
        )
        conn.commit()

        with patch("app.db.get_connection") as mock_conn:
            mock_conn.return_value.__enter__ = MagicMock(return_value=conn)
            mock_conn.return_value.__exit__ = MagicMock(return_value=False)
            _dedup_by_address_key()

        remaining = conn.execute("SELECT id FROM listings").fetchall()
        assert [r["id"] for r in remaining] == [2]

    def test_normalize_address_state_name_matches_code(self):
        """'New York' and 'NY' produce the same address key."""
        from app.enrichment import normalize_address

        key1 = normalize_address("10 Sherman Ave", "Dobbs Ferry", "New York")
        key2 = normalize_address("10 Sherman Ave", "Dobbs Ferry", "NY")
        assert key1 == key2
        assert key1.endswith("|ny")

    def test_data_quality_reports_no_town(self, client):
        """Data-quality dry run includes no_town_count."""
        with patch("app.main.settings") as mock_settings, \
             patch("app.main.db.get_connection") as mock_conn, \
             patch("app.main.db._placeholder", return_value="?"):
            mock_settings.manage_key = "test-key"
            mock_settings.is_postgres = False
            mock_cursor = MagicMock()
            mock_cursor.fetchall.return_value = [
                {"id": 1, "mls_id": None, "address": "123 Main St",
                 "town": None, "listing_url": "https://redfin.com/test"},
                {"id": 2, "mls_id": None, "address": "456 Oak Ave",
                 "town": "Rye", "listing_url": ""},
            ]
            mock_connection = MagicMock()
            mock_connection.cursor.return_value = mock_cursor
            mock_conn.return_value.__enter__ = MagicMock(return_value=mock_connection)
            mock_conn.return_value.__exit__ = MagicMock(return_value=False)

            res = client.post("/manage/data-quality", headers={"x-manage-key": "test-key"})
            assert res.status_code == 200
            data = res.json()
            assert data["no_town_count"] == 1
            assert data["no_town"][0]["id"] == 1


class TestPollDuplicateStatusUpdate:
    """Tests for status/URL update on duplicate detection during poll."""

    @patch("app.poller.mark_processed")
    @patch("app.poller.fetch_new_emails")
    @patch("app.poller.ai_score_listing")
    @patch("app.poller.scrape_listing_description", return_value=(None, []))
    @patch("app.poller.db")
    def test_poll_updates_status_on_mls_duplicate(
        self, mock_db, mock_scrape, mock_ai, mock_fetch, mock_mark
    ):
        """When a duplicate MLS is found with a new status, update the existing listing."""
        from app.models import ParsedListing
        from app.poller import poll_once

        mock_db.init_db = MagicMock()
        mock_db.is_email_processed.return_value = False
        mock_db.save_processed_email.return_value = 1

        # First listing is a duplicate by MLS
        mock_db.is_listing_duplicate.return_value = True
        mock_db.get_listing_id_and_status_by_mls.return_value = (42, "New Listing")

        mock_fetch.return_value = [{
            "gmail_id": "abc123",
            "subject": "Price change",
            "sender": "noreply@redfin.com",
            "html": "",
            "text": "",
            "message_id": "msg1",
            "label_id": "lbl1",
        }]

        # The parser will return a listing with status "Pending"
        pending_listing = ParsedListing(
            address="182 Broadway",
            town="Dobbs Ferry",
            state="NY",
            mls_id="6304978",
            listing_status="Pending",
            source_format="plaintext",
        )

        with patch("app.poller.parser_chain") as mock_parser:
            mock_parser.parse.return_value = [pending_listing]
            poll_once()

        # Should have updated the status
        mock_db.update_listing_status.assert_called_once_with(42, "Pending")

    @patch("app.poller.mark_processed")
    @patch("app.poller.fetch_new_emails")
    @patch("app.poller.ai_score_listing")
    @patch("app.poller.scrape_listing_description", return_value=(None, []))
    @patch("app.poller.db")
    def test_poll_backfills_url_on_duplicate(
        self, mock_db, mock_scrape, mock_ai, mock_fetch, mock_mark
    ):
        """When a duplicate is found and the existing has no URL, backfill it."""
        from app.models import ParsedListing
        from app.poller import poll_once

        mock_db.init_db = MagicMock()
        mock_db.is_email_processed.return_value = False
        mock_db.save_processed_email.return_value = 1

        # Duplicate by MLS, existing has no URL
        mock_db.is_listing_duplicate.return_value = True
        mock_db.get_listing_id_and_status_by_mls.return_value = (7, "New Listing")

        mock_fetch.return_value = [{
            "gmail_id": "def456",
            "subject": "Listing update",
            "sender": "noreply@redfin.com",
            "html": "",
            "text": "",
            "message_id": "msg2",
            "label_id": "lbl2",
        }]

        listing_with_url = ParsedListing(
            address="10 Sherman Ave",
            town="Dobbs Ferry",
            state="NY",
            mls_id="923146",
            listing_status="New Listing",
            listing_url="https://www.redfin.com/NY/Dobbs-Ferry/10-Sherman-Ave-10522/home/123",
            source_format="plaintext",
        )

        with patch("app.poller.parser_chain") as mock_parser:
            mock_parser.parse.return_value = [listing_with_url]
            poll_once()

        # Status unchanged, but URL should be backfilled
        mock_db.update_listing_status.assert_not_called()
        mock_db.update_listing_fields_by_id.assert_called_once_with(
            7, listing_url="https://www.redfin.com/NY/Dobbs-Ferry/10-Sherman-Ave-10522/home/123"
        )


class TestDateFilteredSenderConfig:
    """Tests for SENDER_DATE_FILTERS config parsing."""

    def test_parses_single_entry(self):
        from app.config import Settings
        s = Settings(sender_date_filters="bronwyn@gmail.com:21")
        assert s.date_filtered_sender_list == [("bronwyn@gmail.com", 21)]

    def test_parses_multiple_entries(self):
        from app.config import Settings
        s = Settings(sender_date_filters="a@b.com:21,c@d.com:7")
        assert s.date_filtered_sender_list == [("a@b.com", 21), ("c@d.com", 7)]

    def test_empty_string_returns_empty(self):
        from app.config import Settings
        s = Settings(sender_date_filters="")
        assert s.date_filtered_sender_list == []

    def test_whitespace_only_returns_empty(self):
        from app.config import Settings
        s = Settings(sender_date_filters="   ")
        assert s.date_filtered_sender_list == []

    def test_ignores_invalid_entries(self):
        from app.config import Settings
        s = Settings(sender_date_filters="a@b.com:21,bad_entry,c@d.com:abc")
        # "bad_entry" has no colon → skipped; "c@d.com:abc" has non-int → skipped
        assert s.date_filtered_sender_list == [("a@b.com", 21)]

    def test_domain_in_sender_list(self):
        """Verify domain-level entries in alert_senders work."""
        from app.config import Settings
        s = Settings(alert_senders="redfin.com,alerts@mls.example.com")
        assert "redfin.com" in s.sender_list
        assert "alerts@mls.example.com" in s.sender_list


class TestMaxEmailAgeConfig:
    """Tests for MAX_EMAIL_AGE_DAYS config and Gmail query construction."""

    def test_default_is_21_days(self):
        from app.config import Settings
        s = Settings()
        assert s.max_email_age_days == 21

    def test_custom_value(self):
        from app.config import Settings
        s = Settings(max_email_age_days=7)
        assert s.max_email_age_days == 7

    def test_zero_disables_filter(self):
        from app.config import Settings
        s = Settings(max_email_age_days=0)
        assert s.max_email_age_days == 0

    @patch("app.gmail._build_service")
    @patch("app.gmail._get_or_create_label", return_value="label_123")
    @patch("app.gmail.settings")
    def test_query_includes_newer_than(self, mock_settings, mock_label, mock_service):
        """Gmail query should include newer_than when max_email_age_days > 0."""
        mock_settings.sender_list = ["redfin.com"]
        mock_settings.date_filtered_sender_list = []
        mock_settings.max_email_age_days = 21

        mock_svc = MagicMock()
        mock_svc.users().messages().list().execute.return_value = {"messages": []}
        mock_service.return_value = mock_svc

        from app.gmail import fetch_new_emails
        fetch_new_emails()

        # Verify the query string passed to Gmail API contains newer_than
        call_args = mock_svc.users().messages().list.call_args
        query = call_args[1].get("q", "") if call_args[1] else call_args[0][0] if call_args[0] else ""
        # The query is passed via keyword arg 'q'
        assert "newer_than:21d" in query

    @patch("app.gmail._build_service")
    @patch("app.gmail._get_or_create_label", return_value="label_123")
    @patch("app.gmail.settings")
    def test_query_omits_newer_than_when_zero(self, mock_settings, mock_label, mock_service):
        """Gmail query should NOT include newer_than when max_email_age_days = 0."""
        mock_settings.sender_list = ["redfin.com"]
        mock_settings.date_filtered_sender_list = []
        mock_settings.max_email_age_days = 0

        mock_svc = MagicMock()
        mock_svc.users().messages().list().execute.return_value = {"messages": []}
        mock_service.return_value = mock_svc

        from app.gmail import fetch_new_emails
        fetch_new_emails()

        call_args = mock_svc.users().messages().list.call_args
        query = call_args[1].get("q", "") if call_args[1] else ""
        assert "newer_than" not in query
