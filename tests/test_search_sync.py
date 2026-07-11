"""Tests for the weekly Redfin search sync (app/poller.py sync_search)."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app import db
from app.config import settings
from app.main import app, _search_sync_due
from app.models import ParsedListing, ScoringResult
from app.poller import sync_search


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{db_file}")
    db.init_db()
    yield db_file


PAGE_1 = """
Some Jina-rendered search results:
[52 Lake Rd](https://www.redfin.com/NY/Katonah/52-Lake-Rd-10536/home/20050814)
[29 Appleby Dr](https://www.redfin.com/NY/Bedford/29-Appleby-Dr-10506/home/20149537)
photo: https://ssl.cdn-redfin.com/photo/123.jpg
"""

PAGE_2 = """
[629 Scarborough Rd](https://www.redfin.com/NY/Briarcliff-Manor/629-Scarborough-Rd-10510/home/20082409)
"""


def _mock_jina(pages: list[str]):
    """Return an httpx.Client mock whose .get yields the given page bodies in order.

    status_code=200 satisfies the direct-fetch path, so each page costs one get.
    """
    client = MagicMock()
    client.__enter__ = MagicMock(return_value=client)
    client.__exit__ = MagicMock(return_value=False)
    responses = []
    for body in pages:
        resp = MagicMock()
        resp.text = body
        resp.status_code = 200
        resp.raise_for_status = MagicMock()
        responses.append(resp)
    client.get.side_effect = responses
    return client


class TestSyncSearch:
    @patch("app.jobs.kick")
    @patch("app.jobs.enqueue_listing")
    @patch("httpx.Client")
    def test_adds_new_listings_from_search(self, mock_client_cls, mock_enqueue, mock_kick, temp_db):
        # Page 2 repeats page 1's URLs → pagination stops after page 2
        mock_client_cls.return_value = _mock_jina([PAGE_1, PAGE_1])
        with patch("time.sleep"):
            report = sync_search()

        assert report["urls_found"] == 2
        assert report["added"] == 2
        assert report["skipped_existing"] == 0
        assert report["errors"] == []
        assert mock_enqueue.call_count == 2
        mock_kick.assert_called_once()

        listings = db.get_all_listings()
        addresses = {(l["address"], l["town"]) for l in listings}
        assert ("52 Lake Rd", "Katonah") in addresses
        assert ("29 Appleby Dr", "Bedford") in addresses
        assert all(l["source_format"] == "redfin-sync" for l in listings)
        assert all(l["listing_url"].startswith("https://www.redfin.com/") for l in listings)

    @patch("app.jobs.kick")
    @patch("app.jobs.enqueue_listing")
    @patch("httpx.Client")
    def test_skips_listings_already_in_db(self, mock_client_cls, mock_enqueue, mock_kick, temp_db):
        # Pre-insert 52 Lake Rd so the sync sees it as a duplicate
        from app.enrichment import normalize_address
        email_id = db.save_processed_email(
            gmail_id="pre", message_id="", sender="test", subject="t",
            parser_used="test", listings_found=1,
        )
        existing = ParsedListing(source_format="test", address="52 Lake Rd",
                                 town="Katonah", state="NY", zip_code="10536")
        db.save_listing(
            existing, ScoringResult(score=50, verdict="Worth Touring"), email_id,
            {"address_key": normalize_address("52 Lake Rd", "Katonah", "NY")},
        )

        mock_client_cls.return_value = _mock_jina([PAGE_1, PAGE_1])
        with patch("time.sleep"):
            report = sync_search()

        assert report["added"] == 1
        assert report["skipped_existing"] == 1

    @patch("app.jobs.kick")
    @patch("app.jobs.enqueue_listing")
    @patch("httpx.Client")
    def test_skips_same_home_id_with_different_town_label(
        self, mock_client_cls, mock_enqueue, mock_kick, temp_db
    ):
        """Redfin URL slugs sometimes carry a different town than the MLS
        (Mahopac vs Somers) — the stable /home/<id> must catch the dupe."""
        email_id = db.save_processed_email(
            gmail_id="pre2", message_id="", sender="test", subject="t",
            parser_used="test", listings_found=1,
        )
        existing = ParsedListing(
            source_format="redfin-csv", address="52 Lake Rd", town="Somers",
            state="NY", zip_code="10536",
            listing_url="https://www.redfin.com/NY/Somers/52-Lake-Rd-10536/home/20050814",
        )
        db.save_listing(existing, ScoringResult(score=50, verdict="Worth Touring"), email_id)

        # Search returns the same property under the Katonah slug
        mock_client_cls.return_value = _mock_jina([PAGE_1, PAGE_1])
        with patch("time.sleep"):
            report = sync_search()

        assert report["skipped_existing"] == 1  # 52 Lake Rd caught by home ID
        assert report["added"] == 1  # 29 Appleby Dr is genuinely new

    @patch("app.jobs.kick")
    @patch("app.jobs.enqueue_listing")
    @patch("httpx.Client")
    def test_paginates_until_no_new_urls(self, mock_client_cls, mock_enqueue, mock_kick, temp_db):
        mock_client_cls.return_value = _mock_jina([PAGE_1, PAGE_2, PAGE_2])
        with patch("time.sleep"):
            report = sync_search()

        assert report["pages_fetched"] == 3
        assert report["urls_found"] == 3
        assert report["added"] == 3

    @patch("httpx.Client")
    def test_fetch_failure_is_reported_not_raised(self, mock_client_cls, temp_db):
        client = MagicMock()
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        client.get.side_effect = RuntimeError("jina down")
        mock_client_cls.return_value = client

        report = sync_search()
        assert report["added"] == 0
        assert report["pages_fetched"] == 0
        assert len(report["errors"]) == 1

    def test_disabled_without_url(self, temp_db, monkeypatch):
        monkeypatch.setattr(settings, "redfin_search_url", "")
        report = sync_search()
        assert report == {"error": "REDFIN_SEARCH_URL not configured"}


class TestSearchSyncSchedule:
    def test_due_when_never_run(self, temp_db):
        assert _search_sync_due() is True

    def test_not_due_right_after_run(self, temp_db):
        from datetime import datetime, timezone
        db.set_app_state("last_search_sync", datetime.now(timezone.utc).isoformat())
        assert _search_sync_due() is False

    def test_due_after_interval_elapsed(self, temp_db):
        from datetime import datetime, timedelta, timezone
        old = datetime.now(timezone.utc) - timedelta(days=8)
        db.set_app_state("last_search_sync", old.isoformat())
        assert _search_sync_due() is True

    def test_disabled_when_interval_zero(self, temp_db, monkeypatch):
        monkeypatch.setattr(settings, "search_sync_interval_days", 0)
        assert _search_sync_due() is False


class TestFlagAttribution:
    """Flags record who set them (toured_by / tour_requested_by / passed_by / liked_by)."""

    def _make(self):
        email_id = db.save_processed_email(
            gmail_id="flag-test", message_id="", sender="test", subject="t",
            parser_used="test", listings_found=1,
        )
        listing = ParsedListing(source_format="test", address="1 Flag St",
                                town="Testville", state="NY")
        return db.save_listing(listing, ScoringResult(score=50, verdict="Worth Touring"), email_id)

    @pytest.mark.parametrize("mark,flag", [
        (db.mark_listing_toured, "toured"),
        (db.mark_listing_tour_requested, "tour_requested"),
        (db.mark_listing_passed, "passed"),
        (db.mark_listing_liked, "liked"),
    ])
    def test_flag_records_and_clears_attribution(self, temp_db, mark, flag):
        lid = self._make()
        mark(lid, True, by="bronwyneharris@gmail.com")
        row = db.get_listing_by_id(lid)
        assert row[flag]
        assert row[f"{flag}_by"] == "bronwyneharris@gmail.com"

        mark(lid, False, by="akibalogh@gmail.com")
        row = db.get_listing_by_id(lid)
        assert not row[flag]
        assert row[f"{flag}_by"] is None


class TestManageSyncSearchEndpoint:
    def test_requires_manage_key(self):
        client = TestClient(app)
        res = client.post("/manage/sync-search")
        assert res.status_code == 403

    @patch("app.main.db.set_app_state")
    @patch("app.poller.sync_search",
           return_value={"pages_fetched": 2, "added": 3, "urls_found": 10})
    @patch("app.main.settings")
    def test_triggers_sync_and_stamps(self, mock_settings, mock_sync, mock_state):
        mock_settings.manage_key = "test-key"
        client = TestClient(app)
        res = client.post("/manage/sync-search", headers={"x-manage-key": "test-key"})
        assert res.status_code == 200
        assert res.json()["added"] == 3
        mock_sync.assert_called_once()
        mock_state.assert_called_once()

    @patch("app.main.db.set_app_state")
    @patch("app.poller.sync_search",
           return_value={"pages_fetched": 0, "added": 0, "errors": ["page 1: 403"]})
    @patch("app.main.settings")
    def test_total_failure_does_not_stamp(self, mock_settings, mock_sync, mock_state):
        """A sync that fetched nothing must not consume the weekly slot —
        the next hourly tick should retry."""
        mock_settings.manage_key = "test-key"
        client = TestClient(app)
        res = client.post("/manage/sync-search", headers={"x-manage-key": "test-key"})
        assert res.status_code == 200
        mock_state.assert_not_called()
