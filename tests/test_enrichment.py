"""Tests for enrichment module: address normalization, school data, commute times."""

from unittest.mock import MagicMock, patch

import pytest

from app.enrichment import (
    fetch_commute_time,
    fetch_school_data,
    normalize_address,
)


# ---------------------------------------------------------------------------
# Address normalization
# ---------------------------------------------------------------------------


class TestNormalizeAddress:
    """Tests for normalize_address()."""

    def test_basic_normalization(self):
        key = normalize_address("10 Sherman Avenue", "Rye", "NY")
        assert key == "10 sherman ave|rye|ny"

    def test_street_suffix(self):
        key = normalize_address("25 Oak Street", "Rye", "NY")
        assert key == "25 oak st|rye|ny"

    def test_lane_suffix(self):
        key = normalize_address("5 Maple Lane", "Larchmont", "NY")
        assert key == "5 maple ln|larchmont|ny"

    def test_drive_suffix(self):
        key = normalize_address("31 Lalli Drive", "Katonah", "NY")
        assert key == "31 lalli dr|katonah|ny"

    def test_road_suffix(self):
        key = normalize_address("100 Post Road", "White Plains", "NY")
        assert key == "100 post rd|white plains|ny"

    def test_court_suffix(self):
        key = normalize_address("7 Elm Court", "Scarsdale", "NY")
        assert key == "7 elm ct|scarsdale|ny"

    def test_boulevard_suffix(self):
        key = normalize_address("200 Main Boulevard", "Harrison", "NY")
        assert key == "200 main blvd|harrison|ny"

    def test_terrace_suffix(self):
        key = normalize_address("42 Park Terrace", "Bronxville", "NY")
        assert key == "42 park ter|bronxville|ny"

    def test_circle_suffix(self):
        key = normalize_address("8 Washington Circle", "Rye Brook", "NY")
        assert key == "8 washington cir|rye brook|ny"

    def test_place_suffix(self):
        key = normalize_address("15 Central Place", "Mamaroneck", "NY")
        assert key == "15 central pl|mamaroneck|ny"

    def test_case_insensitive(self):
        key1 = normalize_address("10 Sherman Avenue", "Rye", "NY")
        key2 = normalize_address("10 SHERMAN AVENUE", "RYE", "ny")
        assert key1 == key2

    def test_strips_periods(self):
        key1 = normalize_address("10 Sherman Ave.", "Rye", "N.Y.")
        key2 = normalize_address("10 Sherman Ave", "Rye", "NY")
        assert key1 == key2

    def test_compresses_whitespace(self):
        key = normalize_address("10  Sherman   Avenue", "Rye", "NY")
        assert key == "10 sherman ave|rye|ny"

    def test_returns_none_without_address(self):
        assert normalize_address(None, "Rye", "NY") is None
        assert normalize_address("", "Rye", "NY") is None

    def test_returns_none_without_town(self):
        assert normalize_address("10 Sherman Ave", None, "NY") is None
        assert normalize_address("10 Sherman Ave", "", "NY") is None

    def test_state_optional(self):
        key = normalize_address("10 Sherman Ave", "Rye", None)
        assert key == "10 sherman ave|rye|"

    def test_avenue_vs_ave_match(self):
        """Avenue and Ave should produce the same key."""
        key1 = normalize_address("10 Sherman Avenue", "Rye", "NY")
        key2 = normalize_address("10 Sherman Ave", "Rye", "NY")
        assert key1 == key2

    def test_street_vs_st_match(self):
        """Street and St should produce the same key."""
        key1 = normalize_address("25 Oak Street", "Rye", "NY")
        key2 = normalize_address("25 Oak St", "Rye", "NY")
        assert key1 == key2

    def test_different_towns_dont_match(self):
        """Same address in different towns should NOT match."""
        key1 = normalize_address("10 Sherman Ave", "Rye", "NY")
        key2 = normalize_address("10 Sherman Ave", "Harrison", "NY")
        assert key1 != key2


# ---------------------------------------------------------------------------
# School data (mocked HTTP)
# ---------------------------------------------------------------------------


class TestFetchSchoolData:
    """Tests for fetch_school_data() with mocked API calls."""

    def test_returns_none_without_zip(self):
        result = fetch_school_data(None, "NY")
        assert result is None

    def test_returns_none_without_state(self):
        result = fetch_school_data("10573", None)
        assert result is None

    @patch("app.enrichment.settings")
    def test_returns_none_without_api_key(self, mock_settings):
        mock_settings.schooldigger_app_id = ""
        mock_settings.schooldigger_app_key = ""
        result = fetch_school_data("10573", "NY")
        assert result is None

    @patch("app.enrichment.settings")
    @patch("app.enrichment.httpx.Client")
    def test_parses_response_correctly(self, mock_client_cls, mock_settings):
        mock_settings.schooldigger_app_id = "test_id"
        mock_settings.schooldigger_app_key = "test_key"

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "schoolList": [
                {
                    "schoolName": "Osborn Elementary",
                    "schoolLevel": "Elementary",
                    "distanceMiles": 0.5,
                    "address": {"city": "Rye", "zip": "10580"},
                    "rankHistory": [
                        {"year": 2025, "rankStatewidePercentage": 85.2}
                    ],
                },
                {
                    "schoolName": "Rye Middle School",
                    "schoolLevel": "Middle",
                    "distanceMiles": 1.2,
                    "address": {"city": "Rye", "zip": "10580"},
                    "rankHistory": [
                        {"year": 2025, "rankStatewidePercentage": 78.0}
                    ],
                },
                {
                    "schoolName": "Rye High School",
                    "schoolLevel": "High",
                    "distanceMiles": 1.5,
                    "address": {"city": "Rye", "zip": "10580"},
                    "rankHistory": [
                        {"year": 2025, "rankStatewidePercentage": 91.5}
                    ],
                },
            ]
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_response
        mock_client_cls.return_value = mock_client

        result = fetch_school_data("10580", "NY")
        assert result is not None
        assert len(result["elementary"]) == 1
        assert result["elementary"][0]["name"] == "Osborn Elementary"
        assert result["elementary"][0]["rank_percentile"] == 85.2
        assert len(result["middle"]) == 1
        assert len(result["high"]) == 1

    @patch("app.enrichment.settings")
    @patch("app.enrichment.httpx.Client")
    def test_handles_api_error(self, mock_client_cls, mock_settings):
        mock_settings.schooldigger_app_id = "test_id"
        mock_settings.schooldigger_app_key = "test_key"

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.side_effect = Exception("API error")
        mock_client_cls.return_value = mock_client

        result = fetch_school_data("10580", "NY")
        assert result is None

    def test_normalizes_state_name(self):
        """Should handle full state names like 'New York' → 'NY'."""
        from app.enrichment import _normalize_state_code

        assert _normalize_state_code("New York") == "NY"
        assert _normalize_state_code("new jersey") == "NJ"
        assert _normalize_state_code("CT") == "CT"
        assert _normalize_state_code("Unknown State") is None


# ---------------------------------------------------------------------------
# Commute time (mocked HTTP)
# ---------------------------------------------------------------------------


class TestFetchCommuteTime:
    """Tests for fetch_commute_time() with mocked API calls."""

    @patch("app.enrichment.settings")
    def test_returns_none_without_api_key(self, mock_settings):
        mock_settings.google_maps_api_key = ""
        result = fetch_commute_time("10 Sherman Ave", "Rye", "NY", "10580")
        assert result is None

    def test_returns_none_without_address(self):
        result = fetch_commute_time(None, "Rye", "NY", "10580")
        assert result is None

    def test_returns_none_without_town(self):
        result = fetch_commute_time("10 Sherman Ave", None, "NY", "10580")
        assert result is None

    @patch("app.enrichment.settings")
    @patch("app.enrichment.httpx.Client")
    def test_parses_transit_duration_correctly(self, mock_client_cls, mock_settings):
        mock_settings.google_maps_api_key = "test_key"
        mock_settings.commute_destination = "Brookfield Place, NYC"

        mock_response = MagicMock()
        mock_response.json.return_value = {
            "routes": [
                {
                    "duration": "3960s",  # 66 minutes
                    "distanceMeters": 45000,
                }
            ]
        }
        mock_response.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_response
        mock_client_cls.return_value = mock_client

        result = fetch_commute_time("10 Sherman Ave", "Rye", "NY", "10580")
        assert result is not None
        assert result["commute_minutes"] == 66
        assert result["route_duration_seconds"] == 3960
        assert result["commute_mode"] == "transit"

    @patch("app.enrichment.settings")
    @patch("app.enrichment.httpx.Client")
    def test_falls_back_to_drive_plus_transit(self, mock_client_cls, mock_settings):
        """When TRANSIT from address fails, compute drive-to-station + transit."""
        mock_settings.google_maps_api_key = "test_key"
        mock_settings.commute_destination = "Brookfield Place, NYC"

        no_routes = MagicMock()
        no_routes.json.return_value = {"routes": []}
        no_routes.raise_for_status = MagicMock()

        station_transit = MagicMock()
        station_transit.json.return_value = {
            "routes": [{"duration": "3600s", "distanceMeters": 50000}]  # 60 min
        }
        station_transit.raise_for_status = MagicMock()

        drive_to_station = MagicMock()
        drive_to_station.json.return_value = {
            "routes": [{"duration": "600s", "distanceMeters": 5000}]  # 10 min
        }
        drive_to_station.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        # Call 1: TRANSIT from address → no routes
        # Call 2: TRANSIT from station → 60 min
        # Call 3: DRIVE to station → 10 min
        mock_client.post.side_effect = [no_routes, station_transit, drive_to_station]
        mock_client_cls.return_value = mock_client

        result = fetch_commute_time("31 Lalli Dr", "Katonah", "NY", "10536")
        assert result is not None
        assert result["commute_minutes"] == 70  # 10 + 60
        assert result["commute_mode"] == "drive+transit"
        assert result["drive_minutes"] == 10
        assert result["transit_minutes"] == 60

    @patch("app.enrichment.settings")
    @patch("app.enrichment.httpx.Client")
    def test_handles_no_routes_any_mode(self, mock_client_cls, mock_settings):
        """Returns None when no transit routes exist (direct or via station)."""
        mock_settings.google_maps_api_key = "test_key"
        mock_settings.commute_destination = "Brookfield Place, NYC"

        mock_response = MagicMock()
        mock_response.json.return_value = {"routes": []}
        mock_response.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_response
        mock_client_cls.return_value = mock_client

        result = fetch_commute_time("Remote Island", "Nowhere", "NY", "00000")
        assert result is None

    @patch("app.enrichment.settings")
    @patch("app.enrichment.httpx.Client")
    def test_handles_api_error(self, mock_client_cls, mock_settings):
        mock_settings.google_maps_api_key = "test_key"
        mock_settings.commute_destination = "Brookfield Place, NYC"

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.side_effect = Exception("Network error")
        mock_client_cls.return_value = mock_client

        result = fetch_commute_time("10 Sherman Ave", "Rye", "NY", "10580")
        assert result is None

    @patch("app.enrichment.settings")
    def test_returns_none_without_destination(self, mock_settings):
        mock_settings.google_maps_api_key = "test_key"
        mock_settings.commute_destination = ""
        result = fetch_commute_time("10 Sherman Ave", "Rye", "NY", "10580")
        assert result is None
