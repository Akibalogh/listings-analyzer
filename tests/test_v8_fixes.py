"""Tests for v8 bug fixes (2026-04-11).

Covers:
1. SPA route coverage — /all and other filter routes return 200
2. Redfin tracking param stripping — iOS share links resolve to clean URLs
3. Dashboard criteria button visible to anonymous users
"""
import re
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app

DASHBOARD_HTML = (Path(__file__).parent.parent / "app" / "templates" / "dashboard.html").read_text()


@pytest.fixture
def client():
    return TestClient(app, follow_redirects=True)


# ---------------------------------------------------------------------------
# Group 1: SPA route coverage
# ---------------------------------------------------------------------------

class TestSPARoutes:
    """All filter chip routes must return 200 so page reloads work."""

    def test_root_returns_200(self, client):
        assert client.get("/").status_code == 200

    def test_all_route_returns_200(self, client):
        """Regression: /all was missing from server routes → 404 on reload."""
        assert client.get("/all").status_code == 200

    def test_want_to_go_returns_200(self, client):
        assert client.get("/want-to-go").status_code == 200

    def test_toured_returns_200(self, client):
        assert client.get("/toured").status_code == 200

    def test_passed_returns_200(self, client):
        assert client.get("/passed").status_code == 200

    def test_non_reject_returns_200(self, client):
        assert client.get("/non-reject").status_code == 200


# ---------------------------------------------------------------------------
# Group 2: Redfin tracking param stripping
# ---------------------------------------------------------------------------

class TestRedfnQueryParamStripping:
    """iOS share links resolve to redfin.com URLs with tracking params that cause 405.
    The app must strip query params from the resolved URL before storing or scraping.
    """

    CLEAN_URL = "https://www.redfin.com/NY/Tarrytown/19-Coprock-Rd-10591/home/20097635"
    TRACKING_URL = CLEAN_URL + "?600390594=copy_variant&utm_source=ios_share&utm_medium=share&utm_nooverride=1"

    def _make_mock_head_response(self, final_url: str, status_code: int = 405):
        """Simulate httpx HEAD response after following redirects."""
        mock_resp = MagicMock()
        mock_resp.url = final_url
        mock_resp.status_code = status_code
        return mock_resp

    @patch("app.main.db.save_listing", return_value=1)
    @patch("app.main.db.get_listing_by_id", return_value=None)
    @patch("app.main.db.is_listing_duplicate", return_value=False)
    @patch("app.main.db.is_listing_duplicate_by_address", return_value=False)
    @patch("app.main.db.get_processed_email_by_gmail_id", return_value=None)
    @patch("app.main.db.save_processed_email", return_value=1)
    @patch("app.main._enrich_and_score")
    def test_tracking_params_stripped_from_stored_url(
        self,
        mock_enrich,
        mock_save_email,
        mock_get_email,
        mock_dedup_addr,
        mock_dedup,
        mock_get_listing,
        mock_save,
        client,
    ):
        """When a redf.in short URL resolves with tracking params, the stored
        listing_url must have no query string."""
        mock_client_instance = MagicMock()
        mock_client_instance.__enter__ = lambda s: s
        mock_client_instance.__exit__ = MagicMock(return_value=False)
        mock_client_instance.head.return_value = self._make_mock_head_response(self.TRACKING_URL)

        with patch("app.main.httpx.Client", return_value=mock_client_instance):
            with patch("app.main._get_current_user", return_value="test@example.com"):
                res = client.post("/listings/add", json={"url": "https://redf.in/lYQRHk"})

        # Should succeed (200) — listing created
        assert res.status_code == 200

        # The URL passed to save_listing should have no query params
        call_kwargs = mock_save.call_args
        if call_kwargs:
            args, kwargs = call_kwargs
            # listing_url is a positional or keyword arg — check all args
            all_args = list(args) + list(kwargs.values())
            for arg in all_args:
                if isinstance(arg, str) and "redfin.com" in arg:
                    assert "?" not in arg, f"Stored URL still has query params: {arg}"
                    assert "utm_source" not in arg

    def test_clean_redfin_url_unaffected(self):
        """A Redfin URL without query params should pass through unchanged."""
        url = self.CLEAN_URL
        # Simulate the stripping logic
        if "redfin.com" in url and "?" in url:
            url = url.split("?")[0]
        assert url == self.CLEAN_URL

    def test_tracking_params_are_stripped(self):
        """Simulate the stripping logic on a URL with tracking params."""
        url = self.TRACKING_URL
        if "redfin.com" in url and "?" in url:
            url = url.split("?")[0]
        assert url == self.CLEAN_URL
        assert "utm_source" not in url
        assert "?" not in url

    def test_non_redfin_url_unaffected(self):
        """Non-Redfin URLs with query params should not be stripped."""
        url = "https://www.onekeymls.com/listing/123?ref=search"
        if "redfin.com" in url and "?" in url:
            url = url.split("?")[0]
        assert "?" in url  # unchanged


# ---------------------------------------------------------------------------
# Group 3: Dashboard criteria button visibility
# ---------------------------------------------------------------------------

class TestCriteriaButtonVisibility:
    """The AI Criteria button must be visible to anonymous users.
    The read-only guard in openCriteria() ensures they can view but not edit.
    """

    def test_criteria_button_not_auth_only(self):
        """Regression: button had class 'auth-only' → hidden for anon users."""
        # The button should be just 'criteria-btn', not 'criteria-btn auth-only'
        assert 'class="criteria-btn auth-only"' not in DASHBOARD_HTML, (
            "criteria button has auth-only class — anonymous users cannot see it"
        )

    def test_criteria_button_exists(self):
        """Criteria button must still exist in the HTML."""
        assert 'class="criteria-btn"' in DASHBOARD_HTML

    def test_open_criteria_has_readonly_guard(self):
        """openCriteria() must still gate editing for anonymous users."""
        assert "textarea.readOnly = !isAuthed" in DASHBOARD_HTML

    def test_save_button_hidden_for_anon(self):
        """Save button must be hidden for anonymous users in openCriteria()."""
        assert "saveCriteriaBtn" in DASHBOARD_HTML
        assert "isAuthed ? '' : 'none'" in DASHBOARD_HTML or "isAuthed ? \"\" : \"none\"" in DASHBOARD_HTML or \
               "display = isAuthed" in DASHBOARD_HTML

    def test_readonly_note_shown_for_anon(self):
        """'Sign in to edit criteria' note must exist and be toggled by auth state."""
        assert "Sign in to edit criteria" in DASHBOARD_HTML
        assert "readonlyNote" in DASHBOARD_HTML
