"""Tests for agent tagging: config resolution, API endpoints, and DB backfill."""

from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import app


@pytest.fixture
def client():
    return TestClient(app)


@pytest.fixture
def authed_client(client):
    with patch("app.main._get_current_user", return_value="test@example.com"):
        yield client


def make_settings(agent_map: str) -> Settings:
    """Create a Settings instance with a specific agent_map via env override."""
    with patch.dict("os.environ", {"AGENT_MAP": agent_map}, clear=False):
        return Settings()


# ---------------------------------------------------------------------------
# Settings.resolve_agent_name
# ---------------------------------------------------------------------------


class TestResolveAgentName:
    """Unit tests for Settings.resolve_agent_name()."""

    def test_empty_map_returns_none(self):
        s = make_settings("")
        assert s.resolve_agent_name("anyone@example.com") is None

    def test_exact_email_match(self):
        s = make_settings("jhermoza7@gmail.com:Matt Hermoza")
        assert s.resolve_agent_name("jhermoza7@gmail.com") == "Matt Hermoza"

    def test_email_match_is_case_insensitive(self):
        s = make_settings("jhermoza7@gmail.com:Matt Hermoza")
        assert s.resolve_agent_name("JHermoza7@Gmail.COM") == "Matt Hermoza"

    def test_domain_match(self):
        s = make_settings("redfin.com:Ken Wile")
        assert s.resolve_agent_name("alerts@redfin.com") == "Ken Wile"

    def test_domain_match_is_case_insensitive(self):
        s = make_settings("redfin.com:Ken Wile")
        assert s.resolve_agent_name("Alerts@Redfin.COM") == "Ken Wile"

    def test_exact_email_takes_priority_over_domain(self):
        s = make_settings("bronwyneharris@gmail.com:Bronwyn,gmail.com:Other")
        assert s.resolve_agent_name("bronwyneharris@gmail.com") == "Bronwyn"

    def test_domain_fallback_when_no_exact_match(self):
        s = make_settings("bronwyneharris@gmail.com:Bronwyn,gmail.com:Other")
        assert s.resolve_agent_name("someone_else@gmail.com") == "Other"

    def test_name_with_display_name_in_brackets(self):
        """Handles 'Display Name <email@domain.com>' sender strings."""
        s = make_settings("mhermoza@christiesrehudsonvalley.com:Matt Hermoza")
        assert s.resolve_agent_name("Matthew Hermoza <mhermoza@christiesrehudsonvalley.com>") == "Matt Hermoza"

    def test_multiple_entries(self):
        agent_map = (
            "redfin.com:Ken Wile,"
            "northeastmatrixmail.com:Ken Wile,"
            "bronwyneharris@gmail.com:Bronwyn,"
            "mhermoza@christiesrehudsonvalley.com:Matt Hermoza"
        )
        s = make_settings(agent_map)
        assert s.resolve_agent_name("alerts@redfin.com") == "Ken Wile"
        assert s.resolve_agent_name("listing@northeastmatrixmail.com") == "Ken Wile"
        assert s.resolve_agent_name("bronwyneharris@gmail.com") == "Bronwyn"
        assert s.resolve_agent_name("mhermoza@christiesrehudsonvalley.com") == "Matt Hermoza"

    def test_no_match_returns_none(self):
        s = make_settings("redfin.com:Ken Wile")
        assert s.resolve_agent_name("unknown@zillow.com") is None

    def test_empty_sender_returns_none(self):
        s = make_settings("redfin.com:Ken Wile")
        assert s.resolve_agent_name("") is None

    def test_agent_map_dict_parses_correctly(self):
        s = make_settings("redfin.com:Ken Wile,bronwyneharris@gmail.com:Bronwyn")
        d = s.agent_map_dict
        assert d == {"redfin.com": "Ken Wile", "bronwyneharris@gmail.com": "Bronwyn"}

    def test_agent_map_dict_empty_map(self):
        s = make_settings("")
        assert s.agent_map_dict == {}

    def test_agent_map_dict_whitespace_trimmed(self):
        s = make_settings("  redfin.com : Ken Wile  ,  test@example.com : Jane Doe  ")
        d = s.agent_map_dict
        assert d["redfin.com"] == "Ken Wile"
        assert d["test@example.com"] == "Jane Doe"

    def test_entry_without_colon_ignored(self):
        s = make_settings("redfin.com:Ken Wile,malformed-entry,test@x.com:Jane")
        d = s.agent_map_dict
        assert "malformed-entry" not in d
        assert d["redfin.com"] == "Ken Wile"
        assert d["test@x.com"] == "Jane"


# ---------------------------------------------------------------------------
# POST /listings/{id}/agent
# ---------------------------------------------------------------------------


class TestSetAgentEndpoint:
    """Tests for POST /listings/{listing_id}/agent."""

    def test_requires_auth_without_manage_key(self, client):
        res = client.post("/listings/1/agent", json={"agent_name": "Ken"})
        assert res.status_code == 401

    @patch("app.main.db.update_listing_fields_by_id")
    @patch("app.main.db.get_listing_by_id", return_value={"id": 1, "address": "10 Sherman Ave"})
    def test_sets_agent_name_with_auth(self, mock_get, mock_update, authed_client):
        res = authed_client.post("/listings/1/agent", json={"agent_name": "Ken Wile"})
        assert res.status_code == 200
        data = res.json()
        assert data["agent_name"] == "Ken Wile"
        assert data["listing_id"] == 1
        mock_update.assert_called_once_with(1, force=True, agent_name="Ken Wile")

    @patch("app.main.db.update_listing_fields_by_id")
    @patch("app.main.db.get_listing_by_id", return_value={"id": 1, "address": "10 Sherman Ave"})
    def test_clears_agent_name_with_empty_string(self, mock_get, mock_update, authed_client):
        res = authed_client.post("/listings/1/agent", json={"agent_name": ""})
        assert res.status_code == 200
        assert res.json()["agent_name"] is None
        mock_update.assert_called_once_with(1, force=True, agent_name=None)

    @patch("app.main.db.update_listing_fields_by_id")
    @patch("app.main.db.get_listing_by_id", return_value={"id": 1, "address": "10 Sherman Ave"})
    def test_clears_agent_name_with_null(self, mock_get, mock_update, authed_client):
        res = authed_client.post("/listings/1/agent", json={"agent_name": None})
        assert res.status_code == 200
        assert res.json()["agent_name"] is None

    @patch("app.main.db.get_listing_by_id", return_value=None)
    def test_returns_404_for_missing_listing(self, mock_get, authed_client):
        res = authed_client.post("/listings/999/agent", json={"agent_name": "Anyone"})
        assert res.status_code == 404

    @patch("app.main.db.update_listing_fields_by_id")
    @patch("app.main.db.get_listing_by_id", return_value={"id": 2, "address": "5 Elm St"})
    def test_whitespace_trimmed_from_agent_name(self, mock_get, mock_update, authed_client):
        res = authed_client.post("/listings/2/agent", json={"agent_name": "  Ken Wile  "})
        assert res.status_code == 200
        assert res.json()["agent_name"] == "Ken Wile"
        mock_update.assert_called_once_with(2, force=True, agent_name="Ken Wile")


# ---------------------------------------------------------------------------
# GET /manage/senders
# ---------------------------------------------------------------------------


class TestManageSendersEndpoint:
    """Tests for GET /manage/senders."""

    def test_requires_auth_without_manage_key(self, client):
        res = client.get("/manage/senders")
        assert res.status_code == 401

    def test_returns_senders_with_auth(self, authed_client):
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [
            {"sender": "alerts@redfin.com", "listing_count": 39},
        ]
        mock_cursor.description = [("sender",), ("listing_count",)]
        mock_conn_ctx = MagicMock()
        mock_conn_ctx.__enter__ = MagicMock(return_value=MagicMock(cursor=MagicMock(return_value=mock_cursor)))
        mock_conn_ctx.__exit__ = MagicMock(return_value=False)
        with patch("app.main.db.get_connection", return_value=mock_conn_ctx):
            with patch("app.main.settings") as mock_s:
                mock_s.manage_key = ""
                mock_s.is_postgres = False
                res = authed_client.get("/manage/senders")
        assert res.status_code == 200
        assert "senders" in res.json()


# ---------------------------------------------------------------------------
# Filter chip counts
# ---------------------------------------------------------------------------


class TestFilterCounts:
    """Tests for updateFilterCounts JS logic (via HTML snapshot).

    Verifies that Toured and Want to Go counts are NOT filtered by display
    preferences (hidePending, hidePassed) — a pending listing with
    tour_requested=True must still be counted in Want to Go.
    """

    @pytest.fixture
    def dashboard_html(self):
        from pathlib import Path
        path = Path(__file__).parent.parent / "app" / "templates" / "dashboard.html"
        return path.read_text()

    def test_toured_count_not_filtered_by_display_prefs(self, dashboard_html):
        """Toured count increments before hidePending check."""
        lines = dashboard_html.splitlines()
        toured_idx = next(
            i for i, ln in enumerate(lines) if "counts['Toured']++" in ln
        )
        hide_pending_idx = next(
            i for i, ln in enumerate(lines)
            if "hidePending && isPending" in ln and "return" in ln
        )
        assert toured_idx < hide_pending_idx, (
            "Toured count must be incremented before the hidePending early-return"
        )

    def test_want_to_go_count_not_filtered_by_display_prefs(self, dashboard_html):
        """Want to Go count increments before hidePending check."""
        lines = dashboard_html.splitlines()
        wtg_idx = next(
            i for i, ln in enumerate(lines) if "counts['Want to Go']++" in ln
        )
        hide_pending_idx = next(
            i for i, ln in enumerate(lines)
            if "hidePending && isPending" in ln and "return" in ln
        )
        assert wtg_idx < hide_pending_idx, (
            "Want to Go count must be incremented before the hidePending early-return"
        )

    def test_want_to_go_count_not_filtered_by_hide_passed(self, dashboard_html):
        """Want to Go count increments before hidePassed check."""
        lines = dashboard_html.splitlines()
        wtg_idx = next(
            i for i, ln in enumerate(lines) if "counts['Want to Go']++" in ln
        )
        hide_passed_idx = next(
            i for i, ln in enumerate(lines)
            if "hidePassed" in ln and "return" in ln
        )
        assert wtg_idx < hide_passed_idx, (
            "Want to Go count must be incremented before the hidePassed early-return"
        )
