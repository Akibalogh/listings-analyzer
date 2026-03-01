"""Pydantic models for the listings analyzer."""

from pydantic import BaseModel


class ParsedListing(BaseModel):
    address: str | None = None
    town: str | None = None
    state: str | None = None
    zip_code: str | None = None
    mls_id: str | None = None
    price: int | None = None
    sqft: int | None = None
    bedrooms: int | None = None
    bathrooms: int | None = None
    property_type: str | None = None
    listing_status: str | None = None
    listing_url: str | None = None
    description: str | None = None  # Full listing description (scraped)
    source_format: str = "unknown"


class HardResult(BaseModel):
    criterion: str
    passed: bool | None = None  # None = unknown/can't assess
    value: str = ""
    reason: str = ""


class ScoringResult(BaseModel):
    score: int = 0
    verdict: str = "Unknown"
    hard_results: list[HardResult] = []
    soft_points: dict[str, int] = {}
    concerns: list[str] = []
    confidence: str = "low"  # "high", "medium", "low"
    reasoning: str | None = None  # AI reasoning text
    property_summary: str | None = None  # AI-generated structured factor analysis
    evaluation_method: str = "deterministic"  # "deterministic" or "ai"
    criteria_version: int | None = None
