"""Tests for the scoring engine."""

from app.models import ParsedListing
from app.scorer import score_listing


class TestHardRequirements:
    def test_reject_low_sqft(self):
        listing = ParsedListing(sqft=2000, bedrooms=4, price=1_500_000, property_type="Residential")
        result = score_listing(listing)
        assert result.verdict == "Reject"
        assert result.score == 0
        assert any(hr.criterion == "sqft" and hr.passed is False for hr in result.hard_results)

    def test_reject_too_few_bedrooms(self):
        listing = ParsedListing(sqft=3000, bedrooms=3, price=1_500_000, property_type="Residential")
        result = score_listing(listing)
        assert result.verdict == "Reject"

    def test_reject_too_many_bedrooms(self):
        listing = ParsedListing(sqft=3000, bedrooms=7, price=1_500_000, property_type="Residential")
        result = score_listing(listing)
        assert result.verdict == "Reject"

    def test_reject_over_budget(self):
        listing = ParsedListing(sqft=3000, bedrooms=4, price=2_500_000, property_type="Residential")
        result = score_listing(listing)
        assert result.verdict == "Reject"

    def test_reject_under_budget(self):
        listing = ParsedListing(sqft=3000, bedrooms=4, price=900_000, property_type="Residential")
        result = score_listing(listing)
        assert result.verdict == "Reject"

    def test_reject_townhouse(self):
        listing = ParsedListing(sqft=3000, bedrooms=4, price=1_500_000, property_type="Townhouse")
        result = score_listing(listing)
        assert result.verdict == "Reject"

    def test_reject_condo(self):
        listing = ParsedListing(sqft=3000, bedrooms=4, price=1_500_000, property_type="Condo")
        result = score_listing(listing)
        assert result.verdict == "Reject"

    def test_pass_all_hard_requirements(self):
        listing = ParsedListing(sqft=3000, bedrooms=4, price=1_500_000, property_type="Residential")
        result = score_listing(listing)
        assert result.verdict != "Reject"
        assert result.score >= 20

    def test_no_upper_sqft_bound(self):
        listing = ParsedListing(sqft=5000, bedrooms=4, price=1_500_000, property_type="Residential")
        result = score_listing(listing)
        assert result.verdict != "Reject"


class TestPreScreenMode:
    def test_base_score_when_passing(self):
        listing = ParsedListing(sqft=3000, bedrooms=4, price=1_500_000, property_type="Residential")
        result = score_listing(listing)
        assert result.score == 20  # base score only, no soft features from email

    def test_unknown_hard_reqs_noted(self):
        listing = ParsedListing(sqft=3000, bedrooms=4, price=1_500_000, property_type="Residential")
        result = score_listing(listing)
        # Basement is always unknown in pre-screen
        basement = next(hr for hr in result.hard_results if hr.criterion == "basement")
        assert basement.passed is None

    def test_low_confidence_with_unknowns(self):
        listing = ParsedListing(sqft=3000, bedrooms=4, price=1_500_000, property_type="Residential")
        result = score_listing(listing)
        assert result.confidence == "low"

    def test_reject_is_high_confidence(self):
        listing = ParsedListing(sqft=2000, bedrooms=4, price=1_500_000, property_type="Residential")
        result = score_listing(listing)
        assert result.confidence == "high"

    def test_missing_sqft_is_unknown(self):
        listing = ParsedListing(bedrooms=4, price=1_500_000, property_type="Residential")
        result = score_listing(listing)
        sqft = next(hr for hr in result.hard_results if hr.criterion == "sqft")
        assert sqft.passed is None
        assert result.verdict != "Reject"  # don't reject on unknown


class TestRealListings:
    """Test with real listings from the sample email."""

    def test_jennifer_lane_rejects_on_sqft(self):
        # 11 Jennifer Lane: 2,437 sqft, 4 bd, $1,295,000
        listing = ParsedListing(sqft=2437, bedrooms=4, price=1_295_000, property_type="Residential")
        result = score_listing(listing)
        assert result.verdict == "Reject"
        assert any("sqft" in c for c in result.concerns)

    def test_judson_ave_rejects_on_bedrooms(self):
        # 234 Judson Avenue: 2,385 sqft, 3 bd, $1,400,000
        listing = ParsedListing(sqft=2385, bedrooms=3, price=1_400_000, property_type="Residential")
        result = score_listing(listing)
        assert result.verdict == "Reject"

    def test_willis_ave_passes_prescreen(self):
        # 342 Willis Avenue: 3,164 sqft, 4 bd, $1,295,000
        listing = ParsedListing(sqft=3164, bedrooms=4, price=1_295_000, property_type="Residential")
        result = score_listing(listing)
        assert result.verdict != "Reject"
        assert result.score == 20

    def test_sunset_lane_passes_prescreen(self):
        # 6 Sunset Lane: 4,200 sqft, 6 bd, $1,390,000
        # Note: 6 bedrooms exceeds max of 5
        listing = ParsedListing(sqft=4200, bedrooms=6, price=1_390_000, property_type="Residential")
        result = score_listing(listing)
        assert result.verdict == "Reject"

    def test_sherman_ave_passes_prescreen(self):
        # 10 Sherman Avenue: 2,862 sqft, 4 bd, $1,950,000
        listing = ParsedListing(sqft=2862, bedrooms=4, price=1_950_000, property_type="Residential")
        result = score_listing(listing)
        assert result.verdict != "Reject"
