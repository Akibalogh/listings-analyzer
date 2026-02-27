"""Deterministic scoring engine for listing evaluation.

Implements the scoring logic from PRD Section 5:
- Hard requirements: any fail → Reject
- Base score of 20 for passing all hard requirements
- Soft features add points up to a max of 100

In pre-screen mode (email data only), some hard requirements
can't be assessed (basement, lot, amenities). These are marked
as "unknown" and don't cause rejection.
"""

from app.models import HardResult, ParsedListing, ScoringResult

# Hard requirement thresholds
MIN_SQFT = 2600
MIN_BEDROOMS = 4
MAX_BEDROOMS = 5
MIN_PRICE = 1_250_000
MAX_PRICE = 2_000_000

# Property types that indicate attached housing
ATTACHED_TYPES = {"condo", "townhouse", "townhome", "co-op", "coop", "attached"}

# Base score for passing all hard requirements
BASE_SCORE = 20

# Soft scoring points
SOFT_SCORES = {
    "finished_basement": 20,
    "ground_floor_bedroom": 20,
    "lot_gte_03_acre": 15,
    "pool": 10,
    "sauna": 5,
    "jacuzzi": 5,
    "soak_tub": 5,
}


def score_listing(listing: ParsedListing) -> ScoringResult:
    """Score a listing against hard requirements and soft features.

    Returns a ScoringResult with verdict, score, and details.
    """
    result = ScoringResult()
    hard_pass = True
    has_unknown_hard = False

    # --- Hard Requirements ---

    # Sqft
    if listing.sqft is not None:
        passed = listing.sqft >= MIN_SQFT
        result.hard_results.append(HardResult(
            criterion="sqft",
            passed=passed,
            value=f"{listing.sqft:,}",
            reason=f">= {MIN_SQFT:,} required" if not passed else "",
        ))
        if not passed:
            hard_pass = False
    else:
        result.hard_results.append(HardResult(
            criterion="sqft",
            passed=None,
            value="unknown",
            reason="Not available from email data",
        ))
        has_unknown_hard = True

    # Bedrooms
    if listing.bedrooms is not None:
        passed = MIN_BEDROOMS <= listing.bedrooms <= MAX_BEDROOMS
        result.hard_results.append(HardResult(
            criterion="bedrooms",
            passed=passed,
            value=str(listing.bedrooms),
            reason=f"{MIN_BEDROOMS}-{MAX_BEDROOMS} required" if not passed else "",
        ))
        if not passed:
            hard_pass = False
    else:
        result.hard_results.append(HardResult(
            criterion="bedrooms",
            passed=None,
            value="unknown",
            reason="Not available from email data",
        ))
        has_unknown_hard = True

    # Price
    if listing.price is not None:
        passed = MIN_PRICE <= listing.price <= MAX_PRICE
        result.hard_results.append(HardResult(
            criterion="price",
            passed=passed,
            value=f"${listing.price:,}",
            reason=f"${MIN_PRICE:,}-${MAX_PRICE:,} required" if not passed else "",
        ))
        if not passed:
            hard_pass = False
    else:
        result.hard_results.append(HardResult(
            criterion="price",
            passed=None,
            value="unknown",
            reason="Not available from email data",
        ))
        has_unknown_hard = True

    # Detached (not townhouse/condo)
    if listing.property_type:
        is_attached = listing.property_type.lower().strip() in ATTACHED_TYPES
        result.hard_results.append(HardResult(
            criterion="detached",
            passed=not is_attached,
            value=listing.property_type,
            reason="Townhouse/condo not allowed" if is_attached else "",
        ))
        if is_attached:
            hard_pass = False
    else:
        result.hard_results.append(HardResult(
            criterion="detached",
            passed=None,
            value="unknown",
            reason="Property type not available",
        ))
        has_unknown_hard = True

    # Basement (not available from email — always unknown in pre-screen)
    result.hard_results.append(HardResult(
        criterion="basement",
        passed=None,
        value="unknown",
        reason="Requires full listing page analysis",
    ))
    has_unknown_hard = True

    # --- Verdict ---

    if not hard_pass:
        result.verdict = "Reject"
        result.score = 0
        result.confidence = "high"
        # Add specific concerns for failed hard reqs
        for hr in result.hard_results:
            if hr.passed is False:
                result.concerns.append(f"{hr.criterion}: {hr.value} ({hr.reason})")
        return result

    # --- Soft Scoring ---

    result.score = BASE_SCORE

    # In pre-screen mode, we can't assess soft features from email data alone
    # They all require full listing page analysis
    # Score stays at BASE_SCORE with low confidence

    if has_unknown_hard:
        result.confidence = "low"
        result.concerns.append("Some hard requirements need full listing analysis")
    else:
        result.confidence = "medium"

    # Determine verdict from score
    if result.score >= 80:
        result.verdict = "Strong Match"
    elif result.score >= 60:
        result.verdict = "Worth Touring"
    elif result.score >= 40:
        result.verdict = "Low Priority"
    else:
        result.verdict = "Pass"

    return result
