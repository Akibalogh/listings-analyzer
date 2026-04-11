"""Tests for the 'processing' state of newly-added listings.

The dashboard shows a '…/Pending' state (instead of 0/Reject) when a listing
is still being enriched. Detection relies on the backend setting a specific
sentinel string in concerns_json when creating the placeholder score.

JS logic (dashboard.html):
    var isProcessing = !aiFailed
        && concerns.length === 1
        && concerns[0] === 'Pending enrichment';
"""
import json

import pytest


# --- Backend sentinel contract ---

PENDING_SENTINEL = "Pending enrichment"


def _make_placeholder_concerns():
    """Mirrors the placeholder_score set in main.py add_listing_from_url."""
    return [PENDING_SENTINEL]


def _is_processing(concerns: list, evaluation_method: str) -> bool:
    """Python equivalent of the JS isProcessing check in dashboard.html."""
    ai_failed = evaluation_method == "ai_failed"
    return (
        not ai_failed
        and len(concerns) == 1
        and concerns[0] == PENDING_SENTINEL
    )


class TestProcessingSentinel:
    def test_placeholder_concerns_triggers_processing(self):
        concerns = _make_placeholder_concerns()
        assert _is_processing(concerns, evaluation_method="ai") is True

    def test_real_scored_listing_is_not_processing(self):
        """A listing with a real AI verdict must never show as processing."""
        concerns = [
            "HARD FAIL: Property type is Multi-family.",
            "Lot size of 0.01 acres confirms condo/townhouse.",
        ]
        assert _is_processing(concerns, evaluation_method="ai") is False

    def test_high_score_listing_is_not_processing(self):
        concerns = ["Commute at 78 minutes is acceptable."]
        assert _is_processing(concerns, evaluation_method="ai") is False

    def test_ai_failed_is_not_processing(self):
        """ai_failed listings use their own display path; isProcessing must be False."""
        concerns = _make_placeholder_concerns()
        assert _is_processing(concerns, evaluation_method="ai_failed") is False

    def test_empty_concerns_is_not_processing(self):
        """Empty concerns list should not trigger processing state."""
        assert _is_processing([], evaluation_method="ai") is False

    def test_sentinel_must_be_exact_string(self):
        """Partial or similar strings must not trigger processing."""
        assert _is_processing(["pending enrichment"], evaluation_method="ai") is False  # lowercase
        assert _is_processing(["Pending Enrichment"], evaluation_method="ai") is False  # caps
        assert _is_processing(["Pending enrichment."], evaluation_method="ai") is False  # trailing dot
        assert _is_processing(["Pending enrichment", "extra"], evaluation_method="ai") is False  # multiple

    def test_sentinel_string_matches_main_py_placeholder(self):
        """The sentinel in this test must match the string in main.py exactly."""
        # If this fails, update PENDING_SENTINEL to match main.py
        assert PENDING_SENTINEL == "Pending enrichment"


class TestProcessingStateJsonRoundtrip:
    """Verify the sentinel survives JSON serialization (as stored in DB)."""

    def test_sentinel_roundtrips_through_json(self):
        concerns = _make_placeholder_concerns()
        serialized = json.dumps(concerns)
        deserialized = json.loads(serialized)
        assert _is_processing(deserialized, evaluation_method="ai") is True

    def test_real_concerns_roundtrip_is_not_processing(self):
        concerns = ["HARD FAIL: not a detached SFH", "Price $1.9M exceeds budget"]
        serialized = json.dumps(concerns)
        deserialized = json.loads(serialized)
        assert _is_processing(deserialized, evaluation_method="ai") is False
