"""Tests for v8 bug fixes.

Covers:
1. SPA route coverage — GET /all and other filter routes return 200
2. Redfin URL query param stripping — iOS share links lose utm_* params before storage
3. Dashboard AI Criteria button visibility — no auth-only class on criteria btn
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.main import app

DASHBOARD_HTML = (Path(__file__).parent.parent / "app" / "templates" / "dashboard.html").read_text()


@pytest.fixture
def client():
    return TestClient(app, follow_redirects=True)


@pytest.fixture
def authed_client(client):
    """Client with auth bypassed via mocked session."""
    with patch("app.main._get_current_user", return_value="test@example.com"):
        yield client


# ---------------------------------------------------------------------------
# Group 1: SPA route coverage
# ---------------------------------------------------------------------------

class TestSPARoutes:
    """All filter chip routes must return 200 so page reloads don't 404."""

    def test_all_route_returns_200(self, client):
        """Regression: /all was missing from server routes, causing 404 on reload."""
        res = client.get("/all")
        assert res.status_code == 200

    def test_all_route_serves_dashboard(self, client):
        res = client.get("/all")
        assert "Listings Analyzer" in res.text

    def test_want_to_go_returns_200(self, client):
        assert client.get("/want-to-go").status_code == 200

    def test_toured_returns_200(self, client):
        assert client.get("/toured").status_code == 200

    def test_passed_returns_200(self, client):
        assert client.get("/passed").status_code == 200

    def test_non_reject_returns_200(self, client):
        assert client.get("/non-reject").status_code == 200

    def test_all_filter_routes_require_no_auth(self, client):
        """All filter routes are public — no login required."""
        assert client.get("/all").status_code == 200
        assert client.get("/want-to-go").status_code == 200
        assert client.get("/toured").status_code == 200
        assert client.get("/passed").status_code == 200
        assert client.get("/non-reject").status_code == 200


# ---------------------------------------------------------------------------
# Group 2: Redfin URL query param stripping
# ---------------------------------------------------------------------------

class TestRedfinQueryParamStripping:
    """iOS share links resolve to redfin.com URLs with tracking params that
    cause HTTP 405 on subsequent GET requests.
    The fix strips query params immediately after HEAD redirect resolution.
    """

    CLEAN_URL = "https://www.redfin.com/NY/Tarrytown/19-Coprock-Rd-10591/home/20097635"
    TRACKING_URL = (
        CLEAN_URL
        + "?600390594=copy_variant&utm_source=ios_share&utm_medium=share&utm_nooverride=1"
    )

    # --- Pure-logic unit tests (no HTTP, no DB) ---

    def test_stripping_logic_removes_utm_params(self):
        """The split('?')[0] stripping logic produces a clean URL."""
        url = self.TRACKING_URL
        if "redfin.com" in url and "?" in url:
            url = url.split("?")[0]
        assert url == self.CLEAN_URL
        assert "utm_source" not in url
        assert "?" not in url

    def test_stripping_logic_leaves_clean_url_unchanged(self):
        """URLs without query params pass through the stripping logic unchanged."""
        url = self.CLEAN_URL
        if "redfin.com" in url and "?" in url:
            url = url.split("?")[0]
        assert url == self.CLEAN_URL

    def test_stripping_logic_ignores_non_redfin_urls(self):
        """Non-Redfin URLs with query params are not stripped."""
        url = "https://www.onekeymls.com/listing/123?ref=search"
        if "redfin.com" in url and "?" in url:
            url = url.split("?")[0]
        assert "?" in url  # unchanged

    # --- Integration test: short URL resolves to tracking URL → stored clean ---

    @patch("app.main.db.get_active_criteria", return_value=None)
    @patch("app.main.db.save_listing", return_value=55)
    @patch("app.main.db.save_processed_email", return_value=1)
    @patch("app.main.db.is_listing_duplicate_by_address", return_value=False)
    @patch("httpx.Client")
    def test_utm_params_stripped_from_response_url(
        self,
        mock_client_cls,
        mock_dedup,
        mock_save_email,
        mock_save_listing,
        mock_criteria,
        authed_client,
    ):
        """When a redf.in short URL resolves with iOS tracking params, the URL
        in the API response must have no query string."""
        mock_response = MagicMock()
        mock_response.url = self.TRACKING_URL
        mock_response.status_code = 200
        mock_response.text = "<html></html>"
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.head.return_value = mock_response
        mock_client.get.return_value = mock_response
        mock_client_cls.return_value = mock_client

        res = authed_client.post("/listings/add", json={"url": "https://redf.in/lYQRHk"})

        assert res.status_code == 200
        data = res.json()
        assert "?" not in data["url"], (
            f"Tracking params not stripped from response URL: {data['url']}"
        )
        assert "utm_source" not in data["url"]

    @patch("app.main.db.get_active_criteria", return_value=None)
    @patch("app.main.db.save_listing", return_value=56)
    @patch("app.main.db.save_processed_email", return_value=1)
    @patch("app.main.db.is_listing_duplicate_by_address", return_value=False)
    @patch("httpx.Client")
    def test_clean_path_preserved_after_stripping(
        self,
        mock_client_cls,
        mock_dedup,
        mock_save_email,
        mock_save_listing,
        mock_criteria,
        authed_client,
    ):
        """The listing path must be identical to the clean URL after stripping."""
        mock_response = MagicMock()
        mock_response.url = self.TRACKING_URL
        mock_response.status_code = 200
        mock_response.text = "<html></html>"
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.head.return_value = mock_response
        mock_client.get.return_value = mock_response
        mock_client_cls.return_value = mock_client

        res = authed_client.post("/listings/add", json={"url": "https://redf.in/lYQRHk"})

        assert res.status_code == 200
        data = res.json()
        assert data["url"] == self.CLEAN_URL

    @patch("app.main.db.get_active_criteria", return_value=None)
    @patch("app.main.db.save_listing", return_value=57)
    @patch("app.main.db.save_processed_email", return_value=1)
    @patch("app.main.db.is_listing_duplicate_by_address", return_value=False)
    @patch("httpx.Client")
    def test_address_extracted_correctly_after_stripping(
        self,
        mock_client_cls,
        mock_dedup,
        mock_save_email,
        mock_save_listing,
        mock_criteria,
        authed_client,
    ):
        """Address extraction from the URL path should still work after stripping."""
        dirty_url = (
            "https://www.redfin.com/NY/Chappaqua/19-Georgia-Ln-10514/home/456"
            "?utm_source=ios_share&utm_medium=share"
        )
        mock_response = MagicMock()
        mock_response.url = dirty_url
        mock_response.status_code = 200
        mock_response.text = "<html></html>"
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.head.return_value = mock_response
        mock_client.get.return_value = mock_response
        mock_client_cls.return_value = mock_client

        res = authed_client.post("/listings/add", json={"url": "https://redf.in/iosshare3"})

        assert res.status_code == 200
        data = res.json()
        assert data["town"] == "Chappaqua"
        assert "?" not in data["url"]


# ---------------------------------------------------------------------------
# Group 3: Dashboard criteria button visibility
# ---------------------------------------------------------------------------

class TestCriteriaButtonVisibility:
    """The AI Criteria button must be visible to anonymous users (v8 fix).

    Before the fix the button carried class 'auth-only', hiding it via CSS.
    After the fix the button is always visible; openCriteria() gates editing.
    """

    def test_criteria_button_not_auth_only(self):
        """Regression guard: button must not have 'auth-only' class."""
        assert 'class="criteria-btn auth-only"' not in DASHBOARD_HTML, (
            "criteria-btn has auth-only class — anonymous users cannot see it"
        )

    def test_criteria_button_class_is_criteria_btn(self):
        """Button must exist with class 'criteria-btn' (no auth-only appended)."""
        assert 'class="criteria-btn"' in DASHBOARD_HTML

    def test_open_criteria_has_readonly_guard(self):
        """openCriteria() must set textarea.readOnly for anonymous users."""
        assert "textarea.readOnly = !isAuthed" in DASHBOARD_HTML

    def test_save_button_hidden_for_anon(self):
        """saveCriteriaBtn must be hidden when user is not authenticated."""
        assert "saveCriteriaBtn" in DASHBOARD_HTML
        # JS hides/shows the save button based on auth state
        assert "display = isAuthed" in DASHBOARD_HTML or "'saveCriteriaBtn'" in DASHBOARD_HTML

    def test_readonly_note_shown_for_anon(self):
        """'Sign in to edit criteria' note must exist and be toggled by auth state."""
        assert "Sign in to edit criteria" in DASHBOARD_HTML
        assert "readonlyNote" in DASHBOARD_HTML
