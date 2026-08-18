"""Tests for the weekly Redfin search sync (app/poller.py sync_search)."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app import db
from app.config import settings
from app.main import app
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


class TestPresencePass:
    """Prune pass 3: listings absent from the filter's search results get
    flagged 'Off Market?'; restored to Active when they reappear."""

    def _make(self, home_id, status=None):
        email_id = db.save_processed_email(
            gmail_id=f"presence-{home_id}", message_id="", sender="test",
            subject="t", parser_used="test", listings_found=1,
        )
        listing = ParsedListing(
            source_format="test", address=f"{home_id} Test St", town="Testville",
            state="NY", listing_status=status,
            listing_url=f"https://www.redfin.com/NY/Testville/{home_id}-Test-St-10000/home/{home_id}",
        )
        return db.save_listing(listing, ScoringResult(score=50, verdict="Worth Touring"), email_id)

    def _run_prune(self, present_ids):
        from app.main import _prune_sold_listings
        pixel = MagicMock()
        pixel.text = "a 1x1 image, likely be a tacker probe"
        client = MagicMock()
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        client.get.return_value = pixel
        with patch("httpx.Client", return_value=client), \
             patch("app.parsers.onehome.check_listing_status", return_value=None), \
             patch("app.poller.search_presence_home_ids", return_value=(present_ids, 2)):
            return _prune_sold_listings(fix=True)

    def test_absent_listing_flagged_off_market(self, temp_db):
        lid_gone = self._make("111")
        lid_here = self._make("222")
        report = self._run_prune(present_ids={"222"})
        assert report["absent_from_search_count"] == 1
        assert db.get_listing_by_id(lid_gone)["listing_status"] == "Off Market?"
        assert db.get_listing_by_id(lid_here)["listing_status"] is None

    def test_reappearing_listing_restored_to_active(self, temp_db):
        lid = self._make("333", status="Off Market?")
        report = self._run_prune(present_ids={"333"})
        assert report["restored_count"] == 1
        assert db.get_listing_by_id(lid)["listing_status"] == "Active"

    def test_pending_status_never_overwritten(self, temp_db):
        lid = self._make("444", status="Pending")
        report = self._run_prune(present_ids=set())
        assert db.get_listing_by_id(lid)["listing_status"] == "Pending"
        assert report["absent_from_search_count"] == 0

    def test_failed_fetch_draws_no_conclusions(self, temp_db):
        from app.main import _prune_sold_listings
        lid = self._make("555")
        client = MagicMock()
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        client.get.side_effect = RuntimeError("blocked")
        with patch("httpx.Client", return_value=client), \
             patch("app.parsers.onehome.check_listing_status", return_value=None), \
             patch("app.poller.search_presence_home_ids", return_value=(set(), 0)):
            report = _prune_sold_listings(fix=True)
        assert report["absent_from_search_count"] == 0
        assert db.get_listing_by_id(lid)["listing_status"] is None

    def test_pixel_page_counts_as_error_not_active(self, temp_db):
        self._make("666")
        report = self._run_prune(present_ids={"666"})
        assert report["errors"] >= 1  # pixel page detected, not silently "checked"

    def test_presence_check_throttled_daily(self, temp_db):
        from datetime import datetime, timezone
        from app.main import _presence_check_due
        assert _presence_check_due() is True
        db.set_app_state("last_presence_check", datetime.now(timezone.utc).isoformat())
        assert _presence_check_due() is False


class TestUrlHygiene:
    def test_tracking_params_stripped_on_init(self, temp_db):
        email_id = db.save_processed_email(
            gmail_id="hygiene", message_id="", sender="test", subject="t",
            parser_used="test", listings_found=1,
        )
        listing = ParsedListing(
            source_format="test", address="9 Track St", town="Testville", state="NY",
            listing_url="http://www.redfin.com/NY/T/9-Track-St-10000/home/999?riftinfo=abc123",
        )
        lid = db.save_listing(listing, ScoringResult(score=50, verdict="Worth Touring"), email_id)
        cleaned = db._strip_redfin_url_params()
        assert cleaned == 1
        url = db.get_listing_by_id(lid)["listing_url"]
        assert url == "https://www.redfin.com/NY/T/9-Track-St-10000/home/999"
        # Idempotent
        assert db._strip_redfin_url_params() == 0


class TestListingPageClassifier:
    """Redfin pages embed their own sale history — it must never mark a live
    listing as sold (the bug that deleted 14 Briarwood Ln while it was pending)."""

    def test_pending_banner_outranks_history_sold_lines(self):
        from app.main import _classify_listing_page
        page = "44 photos\npending\n$1,395,000\nabout this home...\nsale & tax history\nsold on june 30, 2004 for $610,000"
        assert _classify_listing_page(page) == "pending"

    def test_active_page_with_sale_history_is_not_sold(self):
        from app.main import _classify_listing_page
        page = "31 photos\nfor sale\n$1,199,000\n...\nsold on may 1, 2019 for $900,000"
        assert _classify_listing_page(page) is None

    def test_genuinely_sold_page_detected(self):
        from app.main import _classify_listing_page
        page = "this home sold on february 15, 2026 for $1,500,000. last sold price..."
        assert _classify_listing_page(page) == "sold"

    def test_history_beyond_banner_window_ignored(self):
        from app.main import _classify_listing_page
        page = "x" * 2600 + " sold on june 30, 2004"
        assert _classify_listing_page(page) is None


class TestTwoStrikeSoldDeletion:
    SOLD_PAGE = ("this home sold on february 15, 2026 for $1,500,000. "
                 + "beautiful colonial with hardwood floors and a large yard. " * 10)

    def _make(self, home_id="777"):
        email_id = db.save_processed_email(
            gmail_id=f"strike-{home_id}", message_id="", sender="test",
            subject="t", parser_used="test", listings_found=1,
        )
        listing = ParsedListing(
            source_format="test", address=f"{home_id} Strike St", town="Testville",
            state="NY",
            listing_url=f"https://www.redfin.com/NY/Testville/{home_id}-Strike-St-10000/home/{home_id}",
        )
        return db.save_listing(listing, ScoringResult(score=50, verdict="Worth Touring"), email_id)

    def _run(self, page_text, present_ids=frozenset()):
        from app.main import _prune_sold_listings
        page = MagicMock()
        page.text = page_text
        client = MagicMock()
        client.__enter__ = MagicMock(return_value=client)
        client.__exit__ = MagicMock(return_value=False)
        client.get.return_value = page
        with patch("httpx.Client", return_value=client), \
             patch("app.parsers.onehome.check_listing_status", return_value=None), \
             patch("app.poller.search_presence_home_ids", return_value=(set(present_ids), 2)):
            return _prune_sold_listings(fix=True)

    def test_first_detection_flags_not_deletes(self, temp_db):
        lid = self._make()
        report = self._run(self.SOLD_PAGE)
        assert report["deleted"] == 0
        assert report["sold_flagged"] == 1
        assert db.get_listing_by_id(lid)["listing_status"] == "Sold?"

    def test_second_detection_deletes(self, temp_db):
        lid = self._make()
        self._run(self.SOLD_PAGE)
        report = self._run(self.SOLD_PAGE)
        assert report["deleted"] == 1
        assert db.get_listing_by_id(lid) is None

    def test_false_positive_self_corrects(self, temp_db):
        """A Sold?-flagged listing whose page reads active again and which is
        still in search results gets restored to Active, not deleted."""
        lid = self._make()
        self._run(self.SOLD_PAGE)
        assert db.get_listing_by_id(lid)["listing_status"] == "Sold?"
        db.delete_app_state("last_presence_check")  # un-throttle the daily check
        active_page = "31 photos\nfor sale\n$1,199,000\n" + "lovely home. " * 30
        report = self._run(active_page, present_ids={"777"})
        assert report["deleted"] == 0
        assert report["restored_count"] == 1
        assert db.get_listing_by_id(lid)["listing_status"] == "Active"


class TestIngestHealth:
    """_ingest_health distinguishes a broken pipeline from a quiet market."""

    def _health(self, **poll):
        from app.main import _ingest_health
        return _ingest_health(poll)

    def test_healthy_recent_success(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        h = self._health(last_successful_poll=now, last_error=None)
        assert h["healthy"] is True
        assert h["reason"] is None

    def test_auth_error_is_unhealthy(self):
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        h = self._health(last_successful_poll=now,
                         last_error="('invalid_grant: Token has been expired or revoked.', {})")
        assert h["healthy"] is False
        assert h["auth_expired"] is True
        assert h["reason"] == "gmail_auth_expired"

    def test_stale_success_is_unhealthy(self):
        from datetime import datetime, timedelta, timezone
        old = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat()
        h = self._health(last_successful_poll=old, last_error=None)
        assert h["healthy"] is False
        assert h["reason"] == "no_successful_poll"
        assert h["hours_since_success"] > 25

    def test_quiet_market_recent_poll_is_healthy(self):
        """Successful poll with 0 listings is healthy — not every day has new alerts."""
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        h = self._health(last_successful_poll=now, last_error=None, listings_found=0,
                         consecutive_empty=40)
        assert h["healthy"] is True

    def test_fresh_boot_never_polled_is_not_alarmed(self):
        """No successful poll yet and no auth error → don't alarm on boot."""
        h = self._health(last_successful_poll=None, last_error=None)
        assert h["healthy"] is True

    def test_health_endpoint_exposes_ingest(self, temp_db):
        from fastapi.testclient import TestClient
        client = TestClient(app)
        data = client.get("/health").json()
        assert "ingest" in data
        assert "healthy" in data["ingest"]


class TestReauthSessionAuth:
    """The reauth endpoint accepts a signed-in session so the dashboard button
    needn't embed the manage key."""

    def test_rejected_without_key_or_session(self):
        from fastapi.testclient import TestClient
        client = TestClient(app)
        res = client.get("/manage/gmail-reauth", follow_redirects=False)
        assert res.status_code == 403

    def test_allowed_with_session(self, monkeypatch):
        from fastapi.testclient import TestClient
        # Signed-in user present; stop before the real OAuth flow by failing creds
        monkeypatch.setattr("app.main._get_current_user", lambda req: "aki@bitsafe.finance")
        monkeypatch.setattr("app.main.settings.gmail_credentials_json", "{}")
        client = TestClient(app)
        res = client.get("/manage/gmail-reauth", follow_redirects=False)
        # Passed the auth gate (would 500 on missing creds, not 403)
        assert res.status_code != 403


class TestGmailAccountGuard:
    """Reauth must not silently accept a token for the wrong Google account."""

    def _flow_returning(self, email):
        """A fake OAuth Flow whose getProfile returns the given account email."""
        flow = MagicMock()
        flow.credentials.refresh_token = "rt-123"
        prof = flow.credentials  # any object; build() is patched separately
        return flow, email

    def test_wrong_account_rejected(self, temp_db, monkeypatch):
        import app.main as m
        monkeypatch.setattr(m.settings, "gmail_account_email", "akibalogh@gmail.com")
        # OAuth state so the callback passes the CSRF check
        import json as _json
        db.set_app_state(m._GMAIL_OAUTH_STATE_KEY, _json.dumps({"state": "s1", "code_verifier": "v"}))

        flow = MagicMock()
        flow.credentials.refresh_token = "rt-123"
        monkeypatch.setattr(m, "settings", m.settings)
        monkeypatch.setattr("google_auth_oauthlib.flow.Flow.from_client_config", lambda *a, **k: flow)
        # getProfile returns the WRONG inbox
        svc = MagicMock()
        svc.users.return_value.getProfile.return_value.execute.return_value = {"emailAddress": "aki@bitsafe.finance"}
        monkeypatch.setattr("googleapiclient.discovery.build", lambda *a, **k: svc)

        from fastapi.testclient import TestClient
        client = TestClient(m.app)
        res = client.get("/manage/gmail-callback?state=s1&code=abc", follow_redirects=False)
        assert res.status_code == 400
        assert "wrong google account" in res.text.lower()
        # Token must NOT have been saved
        assert db.get_app_state("gmail_refresh_token") != "rt-123"

    def test_correct_account_accepted(self, temp_db, monkeypatch):
        import app.main as m
        monkeypatch.setattr(m.settings, "gmail_account_email", "akibalogh@gmail.com")
        import json as _json
        db.set_app_state(m._GMAIL_OAUTH_STATE_KEY, _json.dumps({"state": "s2", "code_verifier": "v"}))

        flow = MagicMock()
        flow.credentials.refresh_token = "rt-good"
        monkeypatch.setattr("google_auth_oauthlib.flow.Flow.from_client_config", lambda *a, **k: flow)
        svc = MagicMock()
        svc.users.return_value.getProfile.return_value.execute.return_value = {"emailAddress": "akibalogh@gmail.com"}
        monkeypatch.setattr("googleapiclient.discovery.build", lambda *a, **k: svc)

        from fastapi.testclient import TestClient
        client = TestClient(m.app)
        res = client.get("/manage/gmail-callback?state=s2&code=abc", follow_redirects=False)
        assert res.status_code == 200
        assert db.get_app_state("gmail_refresh_token") == "rt-good"
        assert db.get_app_state("gmail_connected_account") == "akibalogh@gmail.com"


class TestPruneNeverDeletesFlagged:
    """A user-flagged listing must never be auto-deleted by the prune, even if
    it reads as sold twice (the bug that lost a Want-to-Go home)."""

    SOLD_PAGE = ("this home sold on february 15, 2026 for $1,500,000. "
                 + "beautiful colonial with hardwood floors and a large yard. " * 10)

    def _make(self, home_id, **flags):
        email_id = db.save_processed_email(
            gmail_id=f"prot-{home_id}", message_id="", sender="test", subject="t",
            parser_used="test", listings_found=1,
        )
        listing = ParsedListing(
            source_format="test", address=f"{home_id} Prot St", town="Testville", state="NY",
            listing_url=f"https://www.redfin.com/NY/Testville/{home_id}-Prot-St-10000/home/{home_id}",
        )
        lid = db.save_listing(listing, ScoringResult(score=72, verdict="Worth Touring"), email_id)
        for f, v in flags.items():
            {"toured": db.mark_listing_toured, "tour_requested": db.mark_listing_tour_requested,
             "liked": db.mark_listing_liked, "passed": db.mark_listing_passed}[f](lid, v, by="aki@x")
        return lid

    def _run(self):
        from app.main import _prune_sold_listings
        page = MagicMock(); page.text = self.SOLD_PAGE
        client = MagicMock()
        client.__enter__ = MagicMock(return_value=client); client.__exit__ = MagicMock(return_value=False)
        client.get.return_value = page
        with patch("httpx.Client", return_value=client), \
             patch("app.parsers.onehome.check_listing_status", return_value=None), \
             patch("app.poller.search_presence_home_ids", return_value=(set(), 0)):
            return _prune_sold_listings(fix=True)

    def test_want_to_go_listing_never_deleted_even_on_repeat(self, temp_db):
        lid = self._make("701", tour_requested=True)
        # Two consecutive sold detections — an unflagged listing would be deleted
        self._run()
        r = self._run()
        assert db.get_listing_by_id(lid) is not None  # survived
        assert db.get_listing_by_id(lid)["listing_status"] == "Sold?"
        assert r["protected_from_deletion"] >= 1
        assert r["deleted"] == 0

    def test_unflagged_listing_still_deleted_on_second_strike(self, temp_db):
        lid = self._make("702")  # no flags
        self._run()
        r = self._run()
        assert db.get_listing_by_id(lid) is None  # deleted as before
        assert r["deleted"] == 1


class TestHomeIdDedup:
    """Spelling variants of one property must not become two rows."""

    def _make(self, addr, home_id, **flags):
        email_id = db.save_processed_email(
            gmail_id=f"hid-{addr}", message_id="", sender="t", subject="t",
            parser_used="t", listings_found=1)
        lid = db.save_listing(
            ParsedListing(source_format="test", address=addr, town="Elmsford", state="NY",
                          listing_url=f"https://www.redfin.com/NY/Elmsford/{addr.replace(' ','-')}-10523/home/{home_id}"),
            ScoringResult(score=50, verdict="Low Priority"), email_id)
        for f, v in flags.items():
            {"tour_requested": db.mark_listing_tour_requested,
             "passed": db.mark_listing_passed}[f](lid, v, by="aki@x")
        return lid

    def test_dedup_keeps_flagged_row(self, temp_db):
        plain = self._make("33 Hevelyne Rd", "20131384")
        flagged = self._make("33 Hevelyn Rd", "20131384", tour_requested=True)
        db._dedup_by_home_id()
        assert db.get_listing_by_id(flagged) is not None, "flagged row must survive"
        assert db.get_listing_by_id(plain) is None
        assert db.get_listing_by_id(flagged)["tour_requested"]

    def test_distinct_properties_untouched(self, temp_db):
        a = self._make("1 A St", "111")
        b = self._make("2 B St", "222")
        db._dedup_by_home_id()
        assert db.get_listing_by_id(a) and db.get_listing_by_id(b)

    def test_urlless_listing_never_deleted(self, temp_db):
        """A listing with no URL has no home ID — it must not be swept up."""
        email_id = db.save_processed_email(gmail_id="nourl", message_id="", sender="t",
                                           subject="t", parser_used="t", listings_found=1)
        lid = db.save_listing(
            ParsedListing(source_format="plaintext", address="163 Mount Airy Rd S",
                          town="Croton-On-Hudson", state="NY"),
            ScoringResult(score=78, verdict="Worth Touring"), email_id)
        db._dedup_by_home_id()
        assert db.get_listing_by_id(lid) is not None


class TestManySmallEmails:
    """Ken's MLS alert is moving from a daily digest to immediate delivery, so
    the same home now arrives across many small emails instead of one big one.
    Each repeat must update the existing row, never create a second one."""

    def _poll(self, emails: list[tuple[str, ParsedListing]]):
        """Run poll_once over one parsed listing per email, with no network."""
        from app.poller import poll_once

        fetched = [{
            "gmail_id": gmail_id, "subject": f"New listing {gmail_id}",
            "sender": "KEY@northeastmatrixmail.com", "html": "<html/>", "text": "",
            "message_id": gmail_id, "label_id": "lbl",
        } for gmail_id, _ in emails]
        parsed = [[listing] for _, listing in emails]

        with patch("app.poller.fetch_new_emails", return_value=fetched), \
             patch("app.poller.mark_processed"), \
             patch("app.poller.scrape_listing_description", return_value=(None, [])), \
             patch("app.poller.scrape_listing_structured_data", return_value=None), \
             patch("app.poller._enrich_listing",
                   side_effect=lambda _l, key: {"address_key": key}), \
             patch("app.poller._evaluate_listing",
                   return_value=ScoringResult(score=70, verdict="Worth Touring")), \
             patch("app.jobs.kick"), patch("app.jobs.enqueue_listing"), \
             patch("app.poller.parser_chain") as mock_parser:
            mock_parser.parse.side_effect = parsed
            poll_once()

    @staticmethod
    def _listing(**overrides):
        fields = {
            "source_format": "onehome_html", "address": "121 Law Rd",
            "town": "Briarcliff Manor", "state": "NY", "zip_code": "10510",
            "mls_id": "888111", "price": 1750000,
        }
        fields.update(overrides)
        return ParsedListing(**fields)

    def test_same_listing_in_two_emails_stays_one_row(self, temp_db):
        self._poll([("e1", self._listing()), ("e2", self._listing())])
        listings = db.get_all_listings()
        assert len(listings) == 1
        assert listings[0]["mls_id"] == "888111"

    def test_price_change_in_second_email_updates_the_row(self, temp_db):
        self._poll([
            ("e1", self._listing(price=1750000)),
            ("e2", self._listing(price=1650000)),
        ])
        listings = db.get_all_listings()
        assert len(listings) == 1
        assert listings[0]["price"] == 1650000

    def test_repeat_without_mls_id_dedups_by_home_id(self, temp_db):
        """Redfin-sourced repeats carry a home ID but no MLS number."""
        url = "https://www.redfin.com/NY/Yorktown-Heights/2341-Blue-Spruce-Dr-10598/home/20140001"
        base = {"mls_id": None, "address": "2341 Blue Spruce Dr",
                "town": "Yorktown Heights", "listing_url": url}
        self._poll([
            ("e1", self._listing(price=1150000, **base)),
            ("e2", self._listing(price=1095000, **base)),
        ])
        listings = db.get_all_listings()
        assert len(listings) == 1
        # The home-ID branch used to drop the repeat without applying its update
        assert listings[0]["price"] == 1095000

    def test_address_spelling_variant_dedups_by_address_key(self, temp_db):
        self._poll([
            ("e1", self._listing(mls_id=None, address="163 Mount Airy Road S")),
            ("e2", self._listing(mls_id=None, address="163 Mount Airy Rd S")),
        ])
        assert len(db.get_all_listings()) == 1


class TestListingUpdatesFromEmails:
    """Redfin price-drop / sold emails about already-tracked homes must update
    the record instead of being discarded as duplicates."""

    def _make(self, price=1500000, status="Active"):
        email_id = db.save_processed_email(
            gmail_id=f"upd-{price}-{status}", message_id="", sender="t", subject="t",
            parser_used="t", listings_found=1)
        return db.save_listing(
            ParsedListing(source_format="plaintext", address="110 Oliver Rd",
                          town="Bedford", state="NY", price=price, listing_status=status),
            ScoringResult(score=60, verdict="Worth Touring"), email_id)

    def _apply(self, lid, **fields):
        from app.poller import _update_duplicate
        row = db.get_listing_by_id(lid)
        with patch("app.jobs.kick"), patch("app.jobs.enqueue_listing") as enq:
            _update_duplicate((lid, row.get("listing_status")),
                              ParsedListing(source_format="plaintext", **fields))
        return enq

    def test_price_drop_applied_and_rescored(self, temp_db):
        lid = self._make(price=2146000)
        enq = self._apply(lid, address="110 Oliver Rd", town="Bedford", price=1800000)
        assert db.get_listing_by_id(lid)["price"] == 1800000
        enq.assert_called_once()  # re-scored, since price is a scored factor
        assert enq.call_args[1]["tasks"] == ["score"]

    def test_price_increase_also_applied(self, temp_db):
        lid = self._make(price=1500000)
        self._apply(lid, address="110 Oliver Rd", town="Bedford", price=1600000)
        assert db.get_listing_by_id(lid)["price"] == 1600000

    def test_implausible_price_change_ignored(self, temp_db):
        """A monthly payment parsed as a price must not overwrite the real one."""
        lid = self._make(price=1500000)
        enq = self._apply(lid, address="110 Oliver Rd", town="Bedford", price=14493)
        assert db.get_listing_by_id(lid)["price"] == 1500000
        enq.assert_not_called()

    def test_unchanged_price_does_not_rescore(self, temp_db):
        lid = self._make(price=1500000)
        enq = self._apply(lid, address="110 Oliver Rd", town="Bedford", price=1500000)
        enq.assert_not_called()

    def test_sold_email_flags_rather_than_deletes(self, temp_db):
        """Sold from an email is a suspicion — flag Sold? and let the prune's
        two-strike logic confirm, so a misparse can't destroy a listing."""
        lid = self._make(status="Active")
        self._apply(lid, address="110 Oliver Rd", town="Bedford", listing_status="Sold")
        assert db.get_listing_by_id(lid) is not None
        assert db.get_listing_by_id(lid)["listing_status"] == "Sold?"

    def test_normal_status_passes_through(self, temp_db):
        lid = self._make(status="Active")
        self._apply(lid, address="110 Oliver Rd", town="Bedford", listing_status="Pending")
        assert db.get_listing_by_id(lid)["listing_status"] == "Pending"

    def test_missing_price_backfilled(self, temp_db):
        lid = self._make(price=None)
        self._apply(lid, address="110 Oliver Rd", town="Bedford", price=1450000)
        assert db.get_listing_by_id(lid)["price"] == 1450000


class TestAnEventLabelCannotResurrectAnOffMarketListing:
    """`listing_status` holds both market states and email event labels, and
    _update_duplicate wrote any non-sold value over whatever was there. So a
    Pending or Sold?-flagged home that got a later "Open House" or "Price Drop"
    email came back looking live, and the alert filter had nothing left to object
    to. That is how a gone house reached the phone.

    An event label carries no market state, so it no longer overwrites one.
    """

    def _make(self, status):
        email_id = db.save_processed_email(
            gmail_id=f"res-{status}", message_id="", sender="t", subject="t",
            parser_used="t", listings_found=1)
        return db.save_listing(
            ParsedListing(source_format="plaintext", address="7 Resurrect Rd",
                          town="Bedford", state="NY", price=1500000,
                          listing_status=status),
            ScoringResult(score=80, verdict="Worth Touring"), email_id)

    def _apply(self, lid, status):
        from app.poller import _update_duplicate
        row = db.get_listing_by_id(lid)
        with patch("app.jobs.kick"), patch("app.jobs.enqueue_listing"):
            _update_duplicate(
                (lid, row.get("listing_status")),
                ParsedListing(source_format="plaintext", address="7 Resurrect Rd",
                              town="Bedford", listing_status=status),
            )
        return db.get_listing_by_id(lid)["listing_status"]

    def test_an_event_label_does_not_overwrite_pending(self, temp_db):
        lid = self._make("Pending")
        assert self._apply(lid, "Open House") == "Pending"

    def test_nor_a_sold_suspicion(self, temp_db):
        lid = self._make("Sold?")
        assert self._apply(lid, "Updated MLS Listing") == "Sold?"

    def test_nor_an_off_market_flag(self, temp_db):
        lid = self._make("Off Market?")
        assert self._apply(lid, "Price Drop") == "Off Market?"

    def test_and_the_listing_stays_unpushable(self, temp_db):
        lid = self._make("Pending")
        self._apply(lid, "Open House")
        assert db.claim_unnotified_high_scores(70) == []

    def test_a_real_market_status_still_wins(self, temp_db):
        """Back On Market is a genuine state change and must land."""
        lid = self._make("Pending")
        assert self._apply(lid, "Back On Market") == "Back On Market"

    def test_an_event_label_still_lands_on_an_unknown_status(self, temp_db):
        """Nothing is being lost there, and it is better than blank."""
        lid = self._make(None)
        assert self._apply(lid, "Price Drop") == "Price Drop"

    def test_an_event_label_does_not_overwrite_a_live_market_state_either(self, temp_db):
        """Keeping "Active" over "Price Drop" loses nothing and keeps the column
        meaning one thing. Both are pushable, so the alert is unaffected."""
        lid = self._make("Active")
        assert self._apply(lid, "Price Drop") == "Active"

    def test_a_sold_email_still_flags_over_an_event_label(self, temp_db):
        lid = self._make("Open House")
        assert self._apply(lid, "Sold") == "Sold?"
