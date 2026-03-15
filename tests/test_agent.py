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


# ---------------------------------------------------------------------------
# Settings.resolve_agent_name
# ---------------------------------------------------------------------------


class TestResolveAgentName:
    """Unit tests for Settings.resolve_agent_name()."""

    def _settings(self, agent_map: str) -> Settings:
        return Settings(agent_map=agent_map)

    def test_empty_map_returns_none(self):
        s = self._settings("")
        assert s.resolve_agent_name("anyone@example.com") is None

    def test_exact_email_match(self):
        s = self._settings("jhermoza7@gmail.com:Matt Hermoza")
        assert s.resolve_agent_name("jhermoza7@gmail.com") == "Matt Hermoza"

    def test_email_match_is_case_insensitive(self):
        s = self._settings("jhermoza7@gmail.com:Matt Hermoza")
        assert s.resolve_agent_name("JHermoza7@Gmail.COM") == "Matt Hermoza"

    def test_domain_match(self):
        s = self._settings("redfin.com:Ken Wile")
        assert s.resolve_agent_name("alerts@redfin.com") == "Ken Wile"

    def test_domain_match_is_case_insensitive(self):
        s = self._settings("redfin.com:Ken Wile")
        assert s.resolve_agent_name("Alerts@Redfin.COM") == "Ken Wile"

    def test_exact_email_takes_priority_over_domain(self):
        s = self._settings("bronwyneharris@gmail.com:Bronwyn,gmail.com:Other")
        assert s.resolve_agent_name("bronwyneharris@gmail.com") == "Bronwyn"

    def test_domain_fallback_when_no_exact_match(self):
        s = self._settings("bronwyneharris@gmail.com:Bronwyn,gmail.com:Other")
        assert s.resolve_agent_name("someone_else@gmail.com") == "Other"

    def test_name_with_spaces_in_brackets_format(self):
        """Handles 'Display Name <email@domain.com>' sender strings."""
        s = self._settings("mhermoza@christiesrehudsonvalley.com:Matt Hermoza")
        assert s.resolve_agent_name("Matthew Hermoza <mhermoza@christiesrehudsonvalley.com>") == "Matt Hermoza"

    def test_multiple_entries(self):
        agent_map = (
            "redfin.com:Ken Wile,"
            "northeastmatrixmail.com:Ken Wile,"
            "bronwyneharris@gmail.com:Bronwyn,"
            "mhermoza@christiesrehudsonvalley.com:Matt Hermoza"
        )
        s = self._settings(agent_map)
        assert s.resolve_agent_name("alerts@redfin.com") == "Ken Wile"
        assert s.resolve_agent_name("listing@northeastmatrixmail.com") == "Ken Wile"
        assert s.resolve_agent_name("bronwyneharris@gmail.com") == "Bronwyn"
        assert s.resolve_agent_name("mhermoza@christiesrehudsonvalley.com") == "Matt Hermoza"

    def test_no_match_returns_none(self):
        s = self._settings("redfin.com:Ken Wile")
        assert s.resolve_agent_name("unknown@zillow.com") is None

    def test_empty_sender_returns_none(self):
        s = self._settings("redfin.com:Ken Wile")
        assert s.resolve_agent_name("") is None

    def test_agent_map_dict_parses_correctly(self):
        s = self._settings("redfin.com:Ken Wile,bronwyneharris@gmail.com:Bronwyn")
        d = s.agent_map_dict
        assert d == {"redfin.com": "Ken Wile", "bronwyneharris@gmail.com": "Bronwyn"}

    def test_agent_map_dict_empty_map(self):
        s = self._settings("")
        assert s.agent_map_dict == {}

    def test_agent_map_dict_whitespace_trimmed(self):
        s = self._settings("  redfin.com : Ken Wile  ,  test@example.com : Jane Doe  ")
        d = s.agent_map_dict
        assert d["redfin.com"] == "Ken Wile"
        assert d["test@example.com"] == "Jane Doe"

    def test_entry_without_colon_ignored(self):
        s = self._settings("redfin.com:Ken Wile,malformed-entry,test@x.com:Jane")
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
    @patch("app.main.db.get_listing_by_id", return_value={"id": 5, "address": "5 Oak St"})
    def test_sets_agent_with_manage_key(self, mock_get, mock_update, client):
        with patch("app.main.settings") as mock_settings:
            mock_settings.manage_key = "secret-key"
            mock_settings.is_postgres = False
            res = client.post(
                "/listings/5/agent",
                json={"agent_name": "Matt Hermoza"},
                headers={"x-manage-key": "secret-key"},
            )
        assert res.status_code == 200
        assert res.json()["agent_name"] == "Matt Hermoza"

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

    @patch("app.main.db.get_connection")
    def test_returns_senders_with_auth(self, mock_conn, authed_client):
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [
            {"sender": "alerts@redfin.com", "listing_count": 39},
            {"sender": "bronwyneharris@gmail.com", "listing_count": 2},
        ]
        mock_cursor.description = [("sender",), ("listing_count",)]
        mock_conn.return_value.__enter__.return_value.cursor.return_value = mock_cursor
        with patch("app.main.settings") as mock_settings:
            mock_settings.manage_key = ""
            mock_settings.is_postgres = False
        res = authed_client.get("/manage/senders")
        assert res.status_code == 200
        data = res.json()
        assert "senders" in data

    @patch("app.main.db.get_connection")
    def test_returns_senders_with_manage_key(self, mock_conn, client):
        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = []
        mock_cursor.description = [("sender",), ("listing_count",)]
        mock_conn.return_value.__enter__.return_value.cursor.return_value = mock_cursor
        with patch("app.main.settings") as mock_settings:
            mock_settings.manage_key = "mgmt-key"
            mock_settings.is_postgres = False
            res = client.get("/manage/senders", headers={"x-manage-key": "mgmt-key"})
        assert res.status_code == 200


# ---------------------------------------------------------------------------
# POST /manage/update-listing
# ---------------------------------------------------------------------------


class TestManageUpdateListing:
    """Tests for POST /manage/update-listing."""

    def test_requires_manage_key(self, client):
        res = client.post("/manage/update-listing", json={"listing_id": 1, "price": 500000})
        assert res.status_code == 403

    @patch("app.main.db.get_listing_by_id", return_value=None)
    def test_returns_400_without_listing_id(self, mock_get, client):
        with patch("app.main.settings") as mock_settings:
            mock_settings.manage_key = "key"
            mock_settings.is_postgres = False
            res = client.post(
                "/manage/update-listing",
                json={"price": 500000},
                headers={"x-manage-key": "key"},
            )
        assert res.status_code == 400

    @patch("app.main.db.update_listing_fields_by_id")
    @patch("app.main.db.get_listing_by_id", return_value={"id": 1, "address": "10 Oak St", "price": None})
    def test_updates_allowed_fields(self, mock_get, mock_update, client):
        with patch("app.main.settings") as mock_settings:
            mock_settings.manage_key = "key"
            mock_settings.is_postgres = False
            res = client.post(
                "/manage/update-listing",
                json={"listing_id": 1, "price": 1435000, "bedrooms": 4, "force": True},
                headers={"x-manage-key": "key"},
            )
        assert res.status_code == 200
        data = res.json()
        assert data["listing_id"] == 1
        assert "price" in data["updated"]
        assert "bedrooms" in data["updated"]

    @patch("app.main.db.update_listing_fields_by_id")
    @patch("app.main.db.get_listing_by_id", return_value={"id": 1, "address": "10 Oak St"})
    def test_rejects_disallowed_fields(self, mock_get, mock_update, client):
        with patch("app.main.settings") as mock_settings:
            mock_settings.manage_key = "key"
            mock_settings.is_postgres = False
            res = client.post(
                "/manage/update-listing",
                json={"listing_id": 1, "agent_name": "Bad Actor"},
                headers={"x-manage-key": "key"},
            )
        # agent_name is not in ALLOWED — should 400
        assert res.status_code == 400

    @patch("app.main.db.get_listing_by_id", return_value=None)
    def test_returns_404_for_missing_listing(self, mock_get, client):
        with patch("app.main.settings") as mock_settings:
            mock_settings.manage_key = "key"
            mock_settings.is_postgres = False
            with patch("app.main.db.update_listing_fields_by_id"):
                res = client.post(
                    "/manage/update-listing",
                    json={"listing_id": 999, "price": 100000},
                    headers={"x-manage-key": "key"},
                )
        assert res.status_code == 404


# ---------------------------------------------------------------------------
# Agent backfill (DB layer)
# ---------------------------------------------------------------------------


class TestAgentBackfill:
    """Unit tests for the agent_name backfill logic in db._backfill_agent_names."""

    def test_backfill_skipped_when_no_agent_map(self):
        """Backfill is a no-op when AGENT_MAP is empty."""
        from app import db as db_module

        with patch("app.db.settings") as mock_settings:
            mock_settings.agent_map = ""
            mock_settings.agent_map.strip.return_value = ""
            # Should return immediately without touching DB
            with patch("app.db.get_connection") as mock_conn:
                db_module._backfill_agent_names()
                mock_conn.assert_not_called()

    def test_backfill_updates_matched_listings(self):
        """Backfill calls UPDATE for each sender that maps to an agent name."""
        from app import db as db_module

        mock_cursor = MagicMock()
        mock_cursor.fetchall.return_value = [
            (1, "alerts@redfin.com"),
            (2, "bronwyneharris@gmail.com"),
            (3, "unknown@nowhere.com"),
        ]

        with patch("app.db.settings") as mock_settings:
            mock_settings.agent_map = "redfin.com:Ken Wile,bronwyneharris@gmail.com:Bronwyn"
            mock_settings.agent_map.strip.return_value = "nonempty"
            mock_settings.is_postgres = False
            # Provide a real Settings-like resolve_agent_name
            real_settings = Settings(agent_map="redfin.com:Ken Wile,bronwyneharris@gmail.com:Bronwyn")
            mock_settings.resolve_agent_name = real_settings.resolve_agent_name

            mock_conn_ctx = MagicMock()
            mock_conn_ctx.__enter__ = MagicMock(return_value=MagicMock(cursor=MagicMock(return_value=mock_cursor)))
            mock_conn_ctx.__exit__ = MagicMock(return_value=False)

            with patch("app.db.get_connection", return_value=mock_conn_ctx):
                db_module._backfill_agent_names()

            # Should have issued one SELECT and two UPDATEs (listings 1 & 2 matched)
            calls = mock_cursor.execute.call_args_list
            assert any("SELECT" in str(c) for c in calls)
            update_calls = [c for c in calls if "UPDATE" in str(c)]
            assert len(update_calls) == 2
