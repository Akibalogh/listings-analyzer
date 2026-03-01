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
    })
    @patch("app.main.db.get_all_listing_ids", return_value=[2])
    @patch("app.main.settings")
    def test_scrape_descriptions_skips_no_url(self, mock_settings, mock_ids, mock_get, client):
        mock_settings.manage_key = "test-key"
        res = client.post("/manage/scrape-descriptions", headers={"x-manage-key": "test-key"})
        assert res.status_code == 200
        data = res.json()
        assert data["skipped"] == 1
        assert data["descriptions_scraped"] == 0

    @patch("app.main.db.get_listing_by_id", return_value={
        "id": 3, "address": "30 Test", "listing_url": "https://example.com",
        "description": "Already has a description",
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
