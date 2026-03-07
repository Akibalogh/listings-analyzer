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


# ---------------------------------------------------------------------------
# FEMA flood zone lookup
# ---------------------------------------------------------------------------


class TestFetchFloodZone:
    """Tests for fetch_flood_zone() via FEMA NFHL ArcGIS API."""

    @patch("app.enrichment._geocode_address")
    def test_geocode_failure_returns_none(self, mock_geocode):
        from app.enrichment import fetch_flood_zone

        mock_geocode.return_value = None
        result = fetch_flood_zone("123 Bad St", "Nowhere", "NY")
        assert result is None

    @patch("app.enrichment.httpx.Client")
    @patch("app.enrichment._geocode_address")
    def test_zone_x_minimal_hazard(self, mock_geocode, mock_client_cls):
        from app.enrichment import fetch_flood_zone, _flood_zone_cache

        mock_geocode.return_value = {"lat": 41.05, "lon": -73.78}
        cache_key = "41.05000|-73.78000|flood"
        _flood_zone_cache.pop(cache_key, None)

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "features": [
                {"attributes": {"FLD_ZONE": "X", "ZONE_SUBTY": "AREA OF MINIMAL FLOOD HAZARD"}}
            ]
        }
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        result = fetch_flood_zone("1 Safe St", "Scarsdale", "NY")
        assert result is not None
        assert result["fld_zone"] == "X"
        assert result["sfha"] is False
        assert result["source"] == "fema_nfhl"

    @patch("app.enrichment.httpx.Client")
    @patch("app.enrichment._geocode_address")
    def test_zone_ae_is_sfha(self, mock_geocode, mock_client_cls):
        from app.enrichment import fetch_flood_zone, _flood_zone_cache

        mock_geocode.return_value = {"lat": 41.10, "lon": -73.75}
        cache_key = "41.10000|-73.75000|flood"
        _flood_zone_cache.pop(cache_key, None)

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {
            "features": [
                {"attributes": {"FLD_ZONE": "AE", "ZONE_SUBTY": "1 PCT ANNUAL CHANCE FLOOD HAZARD"}}
            ]
        }
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        result = fetch_flood_zone("5 Flood Ln", "Ardsley", "NY")
        assert result is not None
        assert result["fld_zone"] == "AE"
        assert result["sfha"] is True

    @patch("app.enrichment.httpx.Client")
    @patch("app.enrichment._geocode_address")
    def test_empty_features_returns_none(self, mock_geocode, mock_client_cls):
        from app.enrichment import fetch_flood_zone, _flood_zone_cache

        mock_geocode.return_value = {"lat": 41.20, "lon": -73.70}
        cache_key = "41.20000|-73.70000|flood"
        _flood_zone_cache.pop(cache_key, None)

        mock_resp = MagicMock()
        mock_resp.raise_for_status = MagicMock()
        mock_resp.json.return_value = {"features": []}
        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.return_value = mock_resp
        mock_client_cls.return_value = mock_client

        result = fetch_flood_zone("9 No Zone Rd", "Yonkers", "NY")
        assert result is None

    @patch("app.enrichment.httpx.Client")
    @patch("app.enrichment._geocode_address")
    def test_uses_cache(self, mock_geocode, mock_client_cls):
        from app.enrichment import fetch_flood_zone, _flood_zone_cache

        mock_geocode.return_value = {"lat": 41.30, "lon": -73.65}
        cache_key = "41.30000|-73.65000|flood"
        _flood_zone_cache[cache_key] = {"fld_zone": "X", "sfha": False, "source": "fema_nfhl"}

        result = fetch_flood_zone("Cached St", "Cached Town", "NY")
        assert result["fld_zone"] == "X"
        mock_client_cls.assert_not_called()

    @patch("app.enrichment.httpx.Client")
    @patch("app.enrichment._geocode_address")
    def test_network_error_returns_none(self, mock_geocode, mock_client_cls):
        from app.enrichment import fetch_flood_zone, _flood_zone_cache

        mock_geocode.return_value = {"lat": 41.40, "lon": -73.60}
        cache_key = "41.40000|-73.60000|flood"
        _flood_zone_cache.pop(cache_key, None)

        mock_client = MagicMock()
        mock_client.__enter__ = MagicMock(return_value=mock_client)
        mock_client.__exit__ = MagicMock(return_value=False)
        mock_client.get.side_effect = Exception("timeout")
        mock_client_cls.return_value = mock_client

        result = fetch_flood_zone("Err St", "Errorville", "NY")
        assert result is None

    def test_sfha_zones_coverage(self):
        """All primary SFHA zone prefixes should be flagged."""
        from app.enrichment import _SFHA_ZONES

        assert "A" in _SFHA_ZONES
        assert "AE" in _SFHA_ZONES
        assert "V" in _SFHA_ZONES
        assert "VE" in _SFHA_ZONES
        assert "X" not in _SFHA_ZONES


# ---------------------------------------------------------------------------
# Metro-North station proximity
# ---------------------------------------------------------------------------


class TestFetchStationProximity:
    """Tests for fetch_station_proximity() using static station dataset."""

    @patch("app.enrichment._geocode_address")
    def test_geocode_failure_returns_none(self, mock_geocode):
        from app.enrichment import fetch_station_proximity

        mock_geocode.return_value = None
        result = fetch_station_proximity("Bad St", "Nowhere", "NY")
        assert result is None

    @patch("app.enrichment._geocode_address")
    def test_scarsdale_nearest_is_scarsdale(self, mock_geocode):
        """A point near Scarsdale station should return Scarsdale as nearest."""
        from app.enrichment import fetch_station_proximity, _station_cache

        # Scarsdale station is at ~41.005, -73.7855 — put property ~300m away
        mock_geocode.return_value = {"lat": 41.007, "lon": -73.785}
        cache_key = "41.00700|-73.78500|station"
        _station_cache.pop(cache_key, None)

        result = fetch_station_proximity("1 Station Rd", "Scarsdale", "NY")
        assert result is not None
        assert result["station"] == "Scarsdale"
        assert result["distance_m"] < 500
        assert result["walk_minutes"] >= 1
        assert result["source"] == "osm_static"

    @patch("app.enrichment._geocode_address")
    def test_returns_walk_minutes(self, mock_geocode):
        """walk_minutes should be distance_m / 83 rounded."""
        from app.enrichment import fetch_station_proximity, _station_cache

        mock_geocode.return_value = {"lat": 41.063, "lon": -73.866}
        cache_key = "41.06300|-73.86600|station"
        _station_cache.pop(cache_key, None)

        result = fetch_station_proximity("1 Train Ln", "Tarrytown", "NY")
        assert result is not None
        expected_walk = round(result["distance_m"] / 83.0)
        assert result["walk_minutes"] == expected_walk

    @patch("app.enrichment._geocode_address")
    def test_uses_cache(self, mock_geocode):
        """Second call with same coords should use cache."""
        from app.enrichment import fetch_station_proximity, _station_cache

        mock_geocode.return_value = {"lat": 41.10, "lon": -73.80}
        cache_key = "41.10000|-73.80000|station"
        _station_cache[cache_key] = {
            "station": "Hawthorne",
            "distance_m": 450,
            "walk_minutes": 5,
            "source": "osm_static",
        }

        result = fetch_station_proximity("Cached St", "Hawthorne", "NY")
        assert result["station"] == "Hawthorne"
        # geocode was called once (to get coords), but no HTTP client needed
        mock_geocode.assert_called_once()

    def test_station_list_has_key_stations(self):
        """Static list should include the main Westchester stations."""
        from app.enrichment import _METRO_NORTH_STATIONS

        names = {s["name"] for s in _METRO_NORTH_STATIONS}
        assert "Scarsdale" in names
        assert "White Plains" in names
        assert "Tarrytown" in names
        assert "Dobbs Ferry" in names
        assert "Hartsdale" in names
        assert len(_METRO_NORTH_STATIONS) >= 50

    def test_all_stations_have_valid_coords(self):
        """Every station entry must have plausible lat/lon."""
        from app.enrichment import _METRO_NORTH_STATIONS

        for s in _METRO_NORTH_STATIONS:
            assert 40.0 < s["lat"] < 42.5, f"{s['name']} lat out of range"
            assert -75.0 < s["lon"] < -72.0, f"{s['name']} lon out of range"


# ---------------------------------------------------------------------------
# parse_garage_count tests
# ---------------------------------------------------------------------------


class TestParseGarageCount:
    """Tests for parse_garage_count()."""

    def test_none_description(self):
        from app.enrichment import parse_garage_count
        result = parse_garage_count(None)
        assert result["garage_count"] is None
        assert result["garage_type"] is None

    def test_two_car_attached_garage(self):
        from app.enrichment import parse_garage_count
        result = parse_garage_count("Beautiful home with 2-car attached garage and large yard.")
        assert result["garage_count"] == 2
        assert result["garage_type"] == "attached"

    def test_three_car_garage(self):
        from app.enrichment import parse_garage_count
        result = parse_garage_count("Oversized 3 car garage with workshop space.")
        assert result["garage_count"] == 3

    def test_no_garage(self):
        from app.enrichment import parse_garage_count
        result = parse_garage_count("Charming home with no garage but ample driveway parking.")
        assert result["garage_count"] == 0

    def test_carport_only(self):
        from app.enrichment import parse_garage_count
        result = parse_garage_count("Property features a carport and storage shed.")
        assert result["garage_count"] == 1
        assert result["garage_type"] == "carport"

    def test_generic_garage(self):
        from app.enrichment import parse_garage_count
        result = parse_garage_count("Home with detached garage on a quiet street.")
        assert result["garage_count"] == 1
        assert result["garage_type"] == "detached"

    def test_no_mention(self):
        from app.enrichment import parse_garage_count
        result = parse_garage_count("Lovely 4 bedroom colonial near town center.")
        assert result["garage_count"] is None

    def test_one_car_detached(self):
        from app.enrichment import parse_garage_count
        result = parse_garage_count("1-car detached garage with extra storage.")
        assert result["garage_count"] == 1
        assert result["garage_type"] == "detached"


# ---------------------------------------------------------------------------
# parse_hoa_amount tests
# ---------------------------------------------------------------------------


class TestParseHoaAmount:
    """Tests for parse_hoa_amount()."""

    def test_none_description(self):
        from app.enrichment import parse_hoa_amount
        result = parse_hoa_amount(None)
        assert result["hoa_monthly"] is None

    def test_no_hoa(self):
        from app.enrichment import parse_hoa_amount
        result = parse_hoa_amount("Private home, no HOA restrictions.")
        assert result["hoa_monthly"] == 0

    def test_monthly_hoa(self):
        from app.enrichment import parse_hoa_amount
        result = parse_hoa_amount("Community features include pool. HOA $350/month.")
        assert result["hoa_monthly"] == 350

    def test_hoa_fee_dollar(self):
        from app.enrichment import parse_hoa_amount
        result = parse_hoa_amount("HOA fee $275 includes landscaping and snow removal.")
        assert result["hoa_monthly"] == 275

    def test_annual_hoa(self):
        from app.enrichment import parse_hoa_amount
        result = parse_hoa_amount("Association fee of $3,600/year covers exterior maintenance.")
        assert result["hoa_annual"] == 3600

    def test_no_mention(self):
        from app.enrichment import parse_hoa_amount
        result = parse_hoa_amount("Beautiful colonial on 0.5 acres in Scarsdale.")
        assert result["hoa_monthly"] is None
        assert result["hoa_annual"] is None

    def test_hoa_zero(self):
        from app.enrichment import parse_hoa_amount
        result = parse_hoa_amount("HOA: $0 — no association fees!")
        assert result["hoa_monthly"] == 0

    def test_property_tax_not_confused_with_hoa(self):
        """Dollar amount before 'hoa' that belongs to property taxes should not be matched."""
        from app.enrichment import parse_hoa_amount
        result = parse_hoa_amount(
            "principal and interest $6,937 property taxes $1,992 hoa dues $0 home insurance $220"
        )
        assert result["hoa_monthly"] == 0  # should match "hoa dues $0", not "$1,992"

    def test_hoa_dues_zero_explicit(self):
        """'hoa dues $0' should be detected as no HOA fee."""
        from app.enrichment import parse_hoa_amount
        result = parse_hoa_amount("Monthly: $1,200 mortgage, hoa dues $0, insurance $150")
        assert result["hoa_monthly"] == 0


# ---------------------------------------------------------------------------
# parse_pool_flag tests
# ---------------------------------------------------------------------------


class TestParsePoolFlag:
    """Tests for parse_pool_flag()."""

    def test_none_description(self):
        from app.enrichment import parse_pool_flag
        result = parse_pool_flag(None)
        assert result["has_pool"] is None

    def test_inground_pool(self):
        from app.enrichment import parse_pool_flag
        result = parse_pool_flag("Backyard features an in-ground pool and patio.")
        assert result["has_pool"] is True
        assert result["pool_type"] == "inground"

    def test_above_ground_pool(self):
        from app.enrichment import parse_pool_flag
        result = parse_pool_flag("Large deck with above-ground pool.")
        assert result["has_pool"] is True
        assert result["pool_type"] == "above_ground"

    def test_community_pool(self):
        from app.enrichment import parse_pool_flag
        result = parse_pool_flag("HOA includes access to community pool and tennis courts.")
        assert result["has_pool"] is False
        assert result["pool_type"] == "community"

    def test_pool_table_false_positive(self):
        from app.enrichment import parse_pool_flag
        result = parse_pool_flag("Game room with pool table and wet bar.")
        assert result["has_pool"] is False

    def test_no_pool_mentioned(self):
        from app.enrichment import parse_pool_flag
        result = parse_pool_flag("Quiet neighborhood with mature trees and gardens.")
        assert result["has_pool"] is False

    def test_swimming_pool(self):
        from app.enrichment import parse_pool_flag
        result = parse_pool_flag("Heated swimming pool surrounded by flagstone patio.")
        assert result["has_pool"] is True
        assert result["pool_type"] == "inground"


# ---------------------------------------------------------------------------
# parse_basement tests
# ---------------------------------------------------------------------------


class TestParseBasement:
    """Tests for parse_basement()."""

    def test_none_description(self):
        from app.enrichment import parse_basement
        result = parse_basement(None)
        assert result["has_basement"] is None

    def test_finished_basement(self):
        from app.enrichment import parse_basement
        result = parse_basement("Spacious finished basement with rec room and full bath.")
        assert result["has_basement"] is True
        assert result["basement_type"] == "finished"

    def test_walkout_basement(self):
        from app.enrichment import parse_basement
        result = parse_basement("Walk-out basement opens to backyard with patio.")
        assert result["has_basement"] is True
        assert result["basement_type"] == "walk_out"

    def test_unfinished_basement(self):
        from app.enrichment import parse_basement
        result = parse_basement("Full basement with utility and laundry hookups.")
        assert result["has_basement"] is True
        assert result["basement_type"] == "unfinished"

    def test_partially_finished(self):
        from app.enrichment import parse_basement
        result = parse_basement("Partially finished basement with home office.")
        assert result["has_basement"] is True
        assert result["basement_type"] == "partially_finished"

    def test_no_basement_slab(self):
        from app.enrichment import parse_basement
        result = parse_basement("Ranch home on slab foundation with vaulted ceilings.")
        assert result["has_basement"] is False
        assert result["basement_type"] is None

    def test_crawl_space(self):
        from app.enrichment import parse_basement
        result = parse_basement("Home features crawl space and attic storage.")
        assert result["has_basement"] is False

    def test_generic_basement(self):
        from app.enrichment import parse_basement
        result = parse_basement("Large lower level with potential for additional living space.")
        assert result["has_basement"] is True

    def test_no_mention(self):
        from app.enrichment import parse_basement
        result = parse_basement("Charming Cape Cod on tree-lined street.")
        assert result["has_basement"] is None


# ---------------------------------------------------------------------------
# parse_year_built tests
# ---------------------------------------------------------------------------

class TestParseYearBuilt:
    def test_redfin_year_before(self):
        """Redfin metadata format: YYYY year built"""
        from app.enrichment import parse_year_built
        result = parse_year_built("single-family property type 1934 year built 1.34 acres lot size")
        assert result == 1934

    def test_year_built_colon(self):
        """year built: YYYY format"""
        from app.enrichment import parse_year_built
        result = parse_year_built("Year Built: 2003 Lot Size: 2.66 acres")
        assert result == 2003

    def test_redfin_year_built_inline(self):
        """Redfin inline: YYYY year built followed by other metadata"""
        from app.enrichment import parse_year_built
        result = parse_year_built("property type 1990 year built 1.12 acres lot size 2 car garage")
        assert result == 1990

    def test_built_in(self):
        """built in YYYY"""
        from app.enrichment import parse_year_built
        result = parse_year_built("This charming Colonial was built in 1955 and recently renovated.")
        assert result == 1955

    def test_constructed_in(self):
        """constructed in YYYY"""
        from app.enrichment import parse_year_built
        result = parse_year_built("Custom home constructed in 2018 with modern finishes.")
        assert result == 2018

    def test_ignores_listing_update_date(self):
        """Listing update dates (2026) should not be picked up"""
        from app.enrichment import parse_year_built
        result = parse_year_built("Beautiful home. listing updated: mar 4, 2026 at 11:45am")
        assert result is None

    def test_none_description(self):
        from app.enrichment import parse_year_built
        result = parse_year_built(None)
        assert result is None

    def test_no_year_in_description(self):
        from app.enrichment import parse_year_built
        result = parse_year_built("Lovely home with pool and 2-car garage on quiet street.")
        assert result is None

    def test_year_built_takes_priority_over_renovation(self):
        """'year built 1986' wins over renovation year 2019"""
        from app.enrichment import parse_year_built
        result = parse_year_built("property type 1986 year built 4.8 acres renovated in 2019")
        assert result == 1986
