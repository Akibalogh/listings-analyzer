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
