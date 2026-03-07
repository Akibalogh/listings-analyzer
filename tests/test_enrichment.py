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

    def test_parkway_vs_pkwy_match(self):
        key1 = normalize_address("50 Bronx River Parkway", "Yonkers", "NY")
        key2 = normalize_address("50 Bronx River Pkwy", "Yonkers", "NY")
        assert key1 == key2

    def test_highway_vs_hwy_match(self):
        key1 = normalize_address("100 Route 9 Highway", "Croton", "NY")
        key2 = normalize_address("100 Route 9 Hwy", "Croton", "NY")
        assert key1 == key2

    def test_trail_vs_trl_match(self):
        key1 = normalize_address("8 Deer Trail", "Pound Ridge", "NY")
        key2 = normalize_address("8 Deer Trl", "Pound Ridge", "NY")
        assert key1 == key2

    def test_north_vs_n_match(self):
        """'North' and 'N' should produce the same key."""
        key1 = normalize_address("473 Winding Road North", "Ardsley", "NY")
        key2 = normalize_address("473 Winding Road N", "Ardsley", "NY")
        assert key1 == key2

    def test_south_vs_s_match(self):
        key1 = normalize_address("10 South Broadway", "Irvington", "NY")
        key2 = normalize_address("10 S Broadway", "Irvington", "NY")
        assert key1 == key2

    def test_northeast_vs_ne_match(self):
        key1 = normalize_address("5 Northeast Plaza", "Rye", "NY")
        key2 = normalize_address("5 NE Plaza", "Rye", "NY")
        assert key1 == key2

    def test_direction_doesnt_clobber_street_name(self):
        """'North' inside a street name shouldn't be shortened."""
        key = normalize_address("10 Northfield Ave", "Dobbs Ferry", "NY")
        # "north" is a whole-word match, so "northfield" stays intact
        assert "northfield" in key

    def test_state_name_normalized_to_code(self):
        """'New York' and 'NY' produce the same key."""
        key1 = normalize_address("10 Sherman Ave", "Dobbs Ferry", "New York")
        key2 = normalize_address("10 Sherman Ave", "Dobbs Ferry", "NY")
        assert key1 == key2

    def test_state_name_new_jersey(self):
        """'New Jersey' normalizes to 'nj'."""
        key = normalize_address("5 Main St", "Hoboken", "New Jersey")
        assert key.endswith("|nj")

    def test_state_name_connecticut(self):
        """'Connecticut' normalizes to 'ct'."""
        key = normalize_address("5 Main St", "Stamford", "Connecticut")
        assert key.endswith("|ct")


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
    def test_parses_drive_transit_duration_correctly(self, mock_client_cls, mock_settings):
        """drive+transit: drive 10 min to station, 55 min transit = 65 min total."""
        mock_settings.google_maps_api_key = "test_key"
        mock_settings.commute_destination = "Brookfield Place, NYC"

        station_transit_resp = MagicMock()
        station_transit_resp.json.return_value = {
            "routes": [{"duration": "3300s", "distanceMeters": 50000}]  # 55 min from station
        }
        station_transit_resp.raise_for_status = MagicMock()

        drive_resp = MagicMock()
        drive_resp.json.return_value = {
            "routes": [{"duration": "600s", "distanceMeters": 5000}]  # 10 min drive
        }
        drive_resp.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        # Call 1: TRANSIT from station → 55 min
        # Call 2: DRIVE to station → 10 min
        mock_client.post.side_effect = [station_transit_resp, drive_resp]
        mock_client_cls.return_value = mock_client

        result = fetch_commute_time("10 Sherman Ave", "Rye", "NY", "10580")
        assert result is not None
        assert result["commute_mode"] == "drive+transit"
        assert result["commute_minutes"] == 65  # 10 min drive + 55 min transit
        assert result["drive_minutes"] == 10
        assert result["transit_minutes"] == 55

    @patch("app.enrichment.settings")
    @patch("app.enrichment.httpx.Client")
    def test_returns_none_when_station_transit_fails(self, mock_client_cls, mock_settings):
        """Returns None when no transit route from station to destination."""
        mock_settings.google_maps_api_key = "test_key"
        mock_settings.commute_destination = "Brookfield Place, NYC"

        no_routes = MagicMock()
        no_routes.json.return_value = {"routes": []}
        no_routes.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = no_routes
        mock_client_cls.return_value = mock_client

        result = fetch_commute_time("31 Lalli Dr", "Katonah", "NY", "10536")
        assert result is None

    @patch("app.enrichment.settings")
    @patch("app.enrichment.httpx.Client")
    def test_returns_none_when_drive_to_station_fails(self, mock_client_cls, mock_settings):
        """Returns None when drive route to station is unavailable."""
        mock_settings.google_maps_api_key = "test_key"
        mock_settings.commute_destination = "Brookfield Place, NYC"

        station_transit = MagicMock()
        station_transit.json.return_value = {
            "routes": [{"duration": "3600s", "distanceMeters": 50000}]
        }
        station_transit.raise_for_status = MagicMock()

        no_routes = MagicMock()
        no_routes.json.return_value = {"routes": []}
        no_routes.raise_for_status = MagicMock()

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        # Call 1: TRANSIT from station → ok; Call 2: DRIVE to station → no routes
        mock_client.post.side_effect = [station_transit, no_routes]
        mock_client_cls.return_value = mock_client

        result = fetch_commute_time("31 Lalli Dr", "Katonah", "NY", "10536")
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

    def test_station_overrides_exist(self):
        """Verify key station overrides are configured."""
        from app.enrichment import _STATION_OVERRIDES
        assert _STATION_OVERRIDES["briarcliff manor"] == "Scarborough"
        assert _STATION_OVERRIDES["pound ridge"] == "Katonah"
        assert _STATION_OVERRIDES["yorktown heights"] == "Croton-Harmon"
        assert _STATION_OVERRIDES["cortlandt manor"] == "Croton-Harmon"
        assert _STATION_OVERRIDES["chappaqua"] == "Chappaqua"
        assert _STATION_OVERRIDES["armonk"] == "North White Plains"

    @patch("app.enrichment.settings")
    def test_returns_none_without_destination(self, mock_settings):
        mock_settings.google_maps_api_key = "test_key"
        mock_settings.commute_destination = ""
        result = fetch_commute_time("10 Sherman Ave", "Rye", "NY", "10580")
        assert result is None


class TestNormalizeAddressHyphen:
    """Regression tests for hyphenated town names producing duplicate listings.

    Root cause: 'Croton-On-Hudson' and 'Croton On Hudson' generated different
    address keys, bypassing dedup logic. Fix: strip hyphens during normalization.
    """

    def test_hyphenated_town_matches_spaced(self):
        """Croton-On-Hudson and Croton On Hudson should produce identical keys."""
        key1 = normalize_address("19 Georgia Ln", "Croton-On-Hudson", "NY")
        key2 = normalize_address("19 Georgia Ln", "Croton On Hudson", "NY")
        assert key1 == key2

    def test_hyphenated_town_lowercase(self):
        """Hyphen normalization works regardless of input case."""
        key1 = normalize_address("19 Georgia Ln", "croton-on-hudson", "NY")
        key2 = normalize_address("19 Georgia Ln", "Croton On Hudson", "NY")
        assert key1 == key2

    def test_hyphenated_address_matches_spaced(self):
        """Hyphens in the street address are also normalized."""
        key1 = normalize_address("10 Maple-Ridge Rd", "Bedford", "NY")
        key2 = normalize_address("10 Maple Ridge Rd", "Bedford", "NY")
        assert key1 == key2

    def test_non_hyphenated_towns_still_differ(self):
        """Unrelated towns with same address should not match after hyphen fix."""
        key1 = normalize_address("19 Georgia Ln", "Croton-On-Hudson", "NY")
        key2 = normalize_address("19 Georgia Ln", "Yorktown", "NY")
        assert key1 != key2


# ---------------------------------------------------------------------------
# Age / condition scoring tests
# ---------------------------------------------------------------------------

class TestScoreAgeCondition:
    """Tests for score_age_condition() — age tiers and keyword scanning."""

    def test_import(self):
        from app.enrichment import score_age_condition
        assert callable(score_age_condition)

    def test_pre1940_age_penalty(self):
        from app.enrichment import score_age_condition
        result = score_age_condition(1935, None)
        assert result["age_adjustment"] == -22
        assert result["age_tier"] == "pre-1940"
        assert result["condition_adjustment"] == 0

    def test_1940s_age_penalty(self):
        from app.enrichment import score_age_condition
        result = score_age_condition(1955, None)
        assert result["age_adjustment"] == -18
        assert result["age_tier"] == "1940-1959"

    def test_1975_1989_age_penalty(self):
        from app.enrichment import score_age_condition
        result = score_age_condition(1982, None)
        assert result["age_adjustment"] == -6
        assert result["age_tier"] == "1975-1989"

    def test_2005_plus_no_penalty(self):
        from app.enrichment import score_age_condition
        result = score_age_condition(2010, None)
        assert result["age_adjustment"] == 0
        assert result["age_tier"] == "2005+"

    def test_unknown_year_no_penalty(self):
        from app.enrichment import score_age_condition
        result = score_age_condition(None, None)
        assert result["age_adjustment"] == 0
        assert result["age_tier"] == "unknown"

    def test_positive_keywords(self):
        from app.enrichment import score_age_condition
        result = score_age_condition(1990, "Fully renovated with new roof and new HVAC.")
        assert result["condition_adjustment"] > 0
        assert "new roof" in result["keywords_matched"]
        assert "fully renovated" in result["keywords_matched"]

    def test_negative_keywords_as_is(self):
        from app.enrichment import score_age_condition
        result = score_age_condition(1980, "Sold as is. Needs TLC.")
        assert result["condition_adjustment"] < 0
        assert "sold as is" in result["keywords_matched"]

    def test_condition_clamped_positive(self):
        from app.enrichment import score_age_condition
        desc = " ".join(["new construction newly built gut renovated new roof new hvac new windows turnkey"] * 5)
        result = score_age_condition(2020, desc)
        assert result["condition_adjustment"] <= 15

    def test_condition_clamped_negative(self):
        from app.enrichment import score_age_condition
        desc = " ".join(["sold as is fixer-upper needs tlc cesspool knob and tube major repairs"] * 5)
        result = score_age_condition(1940, desc)
        assert result["condition_adjustment"] >= -25

    def test_case_insensitive(self):
        from app.enrichment import score_age_condition
        result = score_age_condition(2000, "NEW ROOF installed last year. TURNKEY.")
        assert "new roof" in result["keywords_matched"]


# ---------------------------------------------------------------------------
# Price per sqft signal tests
# ---------------------------------------------------------------------------

class TestGetPricePerSqftSignal:
    """Tests for get_price_per_sqft_signal() — Zillow CSV benchmark."""

    def test_import(self):
        from app.enrichment import get_price_per_sqft_signal
        assert callable(get_price_per_sqft_signal)

    def test_returns_none_no_price(self):
        from app.enrichment import get_price_per_sqft_signal
        assert get_price_per_sqft_signal(None, 2000, "10528") is None

    def test_returns_none_no_sqft(self):
        from app.enrichment import get_price_per_sqft_signal
        assert get_price_per_sqft_signal(1500000, None, "10528") is None

    def test_returns_none_no_zip(self):
        from app.enrichment import get_price_per_sqft_signal
        assert get_price_per_sqft_signal(1500000, 2000, None) is None

    def test_returns_none_unknown_zip(self):
        """ZIP not in Zillow data → None."""
        from app.enrichment import get_price_per_sqft_signal, _zillow_median
        # Inject fake data
        _zillow_median["99999"] = 600000.0
        result = get_price_per_sqft_signal(1500000, 2000, "00001")
        assert result is None

    def test_below_market_signal(self):
        """Listing well below market should return below_market."""
        from app.enrichment import get_price_per_sqft_signal, _zillow_median
        _zillow_median["10001"] = 1200000.0  # benchmark ~$800/sqft
        result = get_price_per_sqft_signal(600000, 2000, "10001")  # $300/sqft
        assert result is not None
        assert result["signal"] == "below_market"
        assert result["listing_price_per_sqft"] == 300.0

    def test_above_market_signal(self):
        """Listing well above market should return above_market."""
        from app.enrichment import get_price_per_sqft_signal, _zillow_median
        _zillow_median["10002"] = 600000.0  # benchmark ~$400/sqft
        result = get_price_per_sqft_signal(2000000, 1500, "10002")  # ~$1333/sqft
        assert result is not None
        assert result["signal"] == "above_market"

    def test_at_market_signal(self):
        """Listing near market should return at_market."""
        from app.enrichment import get_price_per_sqft_signal, _zillow_median
        _zillow_median["10003"] = 900000.0  # benchmark $600/sqft
        result = get_price_per_sqft_signal(900000, 1500, "10003")  # exactly $600/sqft
        assert result is not None
        assert result["signal"] == "at_market"
        assert result["ratio"] == pytest.approx(1.0)

    def test_result_keys(self):
        """Result dict should have expected keys."""
        from app.enrichment import get_price_per_sqft_signal, _zillow_median
        _zillow_median["10004"] = 750000.0
        result = get_price_per_sqft_signal(1200000, 2000, "10004")
        assert result is not None
        assert "listing_price_per_sqft" in result
        assert "zillow_median_home_value" in result
        assert "implied_benchmark_per_sqft" in result
        assert "ratio" in result
        assert "signal" in result


# ---------------------------------------------------------------------------
# Property tax fetch tests (NYC SODA API)
# ---------------------------------------------------------------------------

class TestFetchPropertyTax:
    """Tests for fetch_property_tax() — NY Open Data SODA API."""

    def test_import(self):
        from app.enrichment import fetch_property_tax
        assert callable(fetch_property_tax)

    def test_returns_none_no_address(self):
        from app.enrichment import fetch_property_tax
        assert fetch_property_tax(None) is None

    @patch("app.enrichment.httpx.Client")
    def test_successful_fetch(self, mock_client_cls):
        from app.enrichment import fetch_property_tax, _tax_cache
        # Clear cache for this address
        cache_key = "123 Main St|Manhattan|None"
        _tax_cache.pop(cache_key, None)

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = [{
            "assessed_value_total": "150000",
            "market_value_total": "1200000",
            "tax_class_at_present": "1",
            "address": "123 MAIN ST",
        }]
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        result = fetch_property_tax("123 Main St", borough="Manhattan")
        assert result is not None
        assert result["assessed_value"] == 150000
        assert result["market_value"] == 1200000
        assert result["tax_class"] == "1"

    @patch("app.enrichment.httpx.Client")
    def test_empty_response_returns_none(self, mock_client_cls):
        from app.enrichment import fetch_property_tax, _tax_cache
        _tax_cache.pop("456 Oak Ave|Brooklyn|None", None)

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = []
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        result = fetch_property_tax("456 Oak Ave", borough="Brooklyn")
        assert result is None

    @patch("app.enrichment.httpx.Client")
    def test_network_error_returns_none(self, mock_client_cls):
        from app.enrichment import fetch_property_tax, _tax_cache
        _tax_cache.pop("789 Pine St|None|None", None)

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.side_effect = Exception("timeout")
        mock_client_cls.return_value = mock_client

        result = fetch_property_tax("789 Pine St")
        assert result is None

    @patch("app.enrichment.httpx.Client")
    def test_caches_result(self, mock_client_cls):
        """Second call with same args should use cache, not make another HTTP request."""
        from app.enrichment import fetch_property_tax, _tax_cache

        cache_key = "100 Elm St|Queens|None"
        _tax_cache[cache_key] = {
            "assessed_value": 80000,
            "market_value": 700000,
            "tax_class": "1",
            "address": "100 ELM ST",
        }

        result = fetch_property_tax("100 Elm St", borough="Queens")
        assert result is not None
        assert result["assessed_value"] == 80000
        # HTTP client should NOT have been called (cache hit)
        mock_client_cls.assert_not_called()


# ---------------------------------------------------------------------------
# Power line proximity
# ---------------------------------------------------------------------------


class TestHaversineM:
    """Tests for _haversine_m distance calculation."""

    def test_same_point_is_zero(self):
        from app.enrichment import _haversine_m

        assert _haversine_m(40.0, -73.0, 40.0, -73.0) == pytest.approx(0.0, abs=0.01)

    def test_known_distance(self):
        """~111 km per degree latitude at equator."""
        from app.enrichment import _haversine_m

        d = _haversine_m(0.0, 0.0, 1.0, 0.0)
        assert 110_000 < d < 112_000

    def test_short_distance(self):
        """Two points ~200m apart should return ~200m."""
        from app.enrichment import _haversine_m

        # ~0.002 degrees lat ≈ 222m
        d = _haversine_m(41.0, -73.8, 41.002, -73.8)
        assert 200 < d < 250


class TestGeocodeAddress:
    """Tests for _geocode_address() via Nominatim."""

    def test_missing_address_returns_none(self):
        from app.enrichment import _geocode_address

        assert _geocode_address(None, "Scarsdale", "NY") is None
        assert _geocode_address("", "Scarsdale", "NY") is None

    def test_missing_town_returns_none(self):
        from app.enrichment import _geocode_address

        assert _geocode_address("123 Main St", None, "NY") is None

    @patch("app.enrichment.httpx.Client")
    def test_successful_geocode(self, mock_client_cls):
        from app.enrichment import _geocode_address, _geocode_cache

        cache_key = "999 maple ave|scarsdale|ny"
        _geocode_cache.pop(cache_key, None)

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = [{"lat": "41.0050", "lon": "-73.7854"}]
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        result = _geocode_address("999 Maple Ave", "Scarsdale", "NY")
        assert result is not None
        assert result["lat"] == pytest.approx(41.005)
        assert result["lon"] == pytest.approx(-73.7854)

    @patch("app.enrichment.httpx.Client")
    def test_geocode_uses_cache(self, mock_client_cls):
        from app.enrichment import _geocode_address, _geocode_cache

        cache_key = "1 cached ln|newtown|ny"
        _geocode_cache[cache_key] = {"lat": 41.1, "lon": -73.9}

        result = _geocode_address("1 Cached Ln", "Newtown", "NY")
        assert result == {"lat": 41.1, "lon": -73.9}
        mock_client_cls.assert_not_called()

    @patch("app.enrichment.httpx.Client")
    def test_geocode_empty_response_returns_none(self, mock_client_cls):
        from app.enrichment import _geocode_address, _geocode_cache

        cache_key = "unknown place rd|nowhere|ny"
        _geocode_cache.pop(cache_key, None)

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = []
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        result = _geocode_address("Unknown Place Rd", "Nowhere", "NY")
        assert result is None

    @patch("app.enrichment.httpx.Client")
    def test_geocode_network_error_returns_none(self, mock_client_cls):
        from app.enrichment import _geocode_address, _geocode_cache

        cache_key = "error st|errorville|ny"
        _geocode_cache.pop(cache_key, None)

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.side_effect = Exception("network error")
        mock_client_cls.return_value = mock_client

        result = _geocode_address("Error St", "Errorville", "NY")
        assert result is None


class TestFetchPowerLineProximity:
    """Tests for fetch_power_line_proximity()."""

    @patch("app.enrichment._geocode_address")
    def test_geocode_failure_returns_none(self, mock_geocode):
        from app.enrichment import fetch_power_line_proximity

        mock_geocode.return_value = None
        result = fetch_power_line_proximity("123 Bad St", "Nowhere", "NY")
        assert result is None

    @patch("app.enrichment.httpx.Client")
    @patch("app.enrichment._geocode_address")
    def test_no_power_lines_returns_none(self, mock_geocode, mock_client_cls):
        from app.enrichment import fetch_power_line_proximity, _power_line_cache

        mock_geocode.return_value = {"lat": 41.0, "lon": -73.8}
        cache_key = "41.00000|-73.80000"
        _power_line_cache.pop(cache_key, None)

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"elements": []}
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        result = fetch_power_line_proximity("1 Safe St", "Scarsdale", "NY")
        assert result is None

    @patch("app.enrichment.httpx.Client")
    @patch("app.enrichment._geocode_address")
    def test_power_line_way_detected(self, mock_geocode, mock_client_cls):
        from app.enrichment import fetch_power_line_proximity, _power_line_cache

        mock_geocode.return_value = {"lat": 41.0, "lon": -73.8}
        cache_key = "41.00000|-73.80000"
        _power_line_cache.pop(cache_key, None)

        overpass_response = {
            "elements": [
                {
                    "type": "way",
                    "id": 12345,
                    "tags": {"power": "line", "voltage": "115000"},
                    "geometry": [
                        {"lat": 41.0009, "lon": -73.8},  # ~100m north
                        {"lat": 41.0010, "lon": -73.8},
                    ],
                }
            ]
        }
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = overpass_response
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        result = fetch_power_line_proximity("1 Power St", "Ardsley", "NY")
        assert result is not None
        assert result["nearest_distance_m"] < 200  # ~100m
        assert result["nearest_type"] == "line"
        assert result["voltage"] == "115000"
        assert result["count_within_300m"] == 1
        assert result["source"] == "osm_overpass"

    @patch("app.enrichment.httpx.Client")
    @patch("app.enrichment._geocode_address")
    def test_power_tower_node_detected(self, mock_geocode, mock_client_cls):
        from app.enrichment import fetch_power_line_proximity, _power_line_cache

        mock_geocode.return_value = {"lat": 41.0, "lon": -73.8}
        cache_key = "41.00000|-73.80000"
        _power_line_cache.pop(cache_key, None)

        overpass_response = {
            "elements": [
                {
                    "type": "node",
                    "id": 99999,
                    "lat": 41.0018,  # ~200m north
                    "lon": -73.8,
                    "tags": {"power": "tower"},
                }
            ]
        }
        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = overpass_response
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        result = fetch_power_line_proximity("2 Tower Rd", "Ardsley", "NY")
        assert result is not None
        assert 150 < result["nearest_distance_m"] < 250
        assert result["nearest_type"] == "tower"

    @patch("app.enrichment.httpx.Client")
    @patch("app.enrichment._geocode_address")
    def test_power_line_uses_cache(self, mock_geocode, mock_client_cls):
        from app.enrichment import fetch_power_line_proximity, _power_line_cache

        mock_geocode.return_value = {"lat": 41.5, "lon": -73.5}
        cache_key = "41.50000|-73.50000"
        cached_result = {
            "nearest_distance_m": 120.5,
            "nearest_type": "line",
            "voltage": "230000",
            "count_within_300m": 3,
            "source": "osm_overpass",
        }
        _power_line_cache[cache_key] = cached_result

        result = fetch_power_line_proximity("Cached Address", "Cached Town", "NY")
        assert result == cached_result
        mock_client_cls.assert_not_called()

    @patch("app.enrichment.httpx.Client")
    @patch("app.enrichment._geocode_address")
    def test_network_error_returns_none(self, mock_geocode, mock_client_cls):
        from app.enrichment import fetch_power_line_proximity, _power_line_cache

        mock_geocode.return_value = {"lat": 41.2, "lon": -73.6}
        cache_key = "41.20000|-73.60000"
        _power_line_cache.pop(cache_key, None)

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.post.side_effect = Exception("timeout")
        mock_client_cls.return_value = mock_client

        result = fetch_power_line_proximity("3 Error Ave", "Errorville", "NY")
        assert result is None
