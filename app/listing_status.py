"""What a listing's status tells us about whether the home is still for sale.

`listings.listing_status` holds two different kinds of value, and conflating them
is why a sold house got pushed as a new listing:

  * **Market states**, from an MLS lookup or a filtered-search subject line —
    "Active", "Pending", "Sold". These are facts about availability.
  * **Event labels**, from Redfin's alert prefixes and OneHome's badge images —
    "Price Drop", "Open House", "Updated MLS Listing", "New Favorite". These say
    why an email was sent. Some imply the home was on the market when it went out
    (nobody holds an open house for a closed sale); others say nothing at all.

And the column can simply be NULL: OneHome alerts carry no status.

Two questions get asked of a status, and they need different answers:

  * *Good enough to push on?* — `is_live`. An open-house notice counts, because
    it could only have been sent about a live listing.
  * *Authoritative enough to overwrite a known market state?* — `is_market_state`.
    An open-house notice does not count, because it is weak evidence next to an
    MLS "Pending", and letting it overwrite one is what made a gone house look
    available again.

The alert path asks `is_live`, never `not is_off_market`. An allowlist means an
unrecognised or missing value fails closed — silence, rather than a push about a
house that may already be gone. Matching is case-insensitive throughout because
both "Back On Market" and "Back on Market" occur in the corpus.
"""

# Market states: what an MLS or a filtered saved search reports.
MARKET_LIVE_STATUSES = frozenset({
    "active",
    "new listing",
    "coming soon",
    "pre on-market",
    "back on market",
})

# "Sold?" and "Off Market?" are this app's own suspicion flags, set by an email
# notice or by absence from a search and pending confirmation by the prune's
# two-strike logic. A suspicion is still reason enough not to push.
OFF_MARKET_STATUSES = frozenset({
    "pending",
    "sold",
    "under contract",
    "contingent",
    "closed",
    "off market",
    "off market?",
    "sold?",
})

# Event labels that could only describe a listing that was on the market at the
# time. Enough to alert on; not enough to overturn a market state.
LIVE_EVENT_LABELS = frozenset({
    "open house",
    "price drop",
    "price decreased",
    "price increased",
})

# Everything the alert path will act on.
LIVE_STATUSES = MARKET_LIVE_STATUSES | LIVE_EVENT_LABELS


def _norm(status: str | None) -> str:
    return (status or "").strip().lower()


def is_live(status: str | None) -> bool:
    """True only when the status positively says the home is on the market."""
    return _norm(status) in LIVE_STATUSES


def is_off_market(status: str | None) -> bool:
    """True when the status positively says it is not available."""
    return _norm(status) in OFF_MARKET_STATUSES


def is_unknown(status: str | None) -> bool:
    """True when the status settles nothing — missing, or an event label like
    "Updated MLS Listing" that carries no market state."""
    norm = _norm(status)
    return norm not in LIVE_STATUSES and norm not in OFF_MARKET_STATUSES


def is_market_state(status: str | None) -> bool:
    """True for a reported market state, false for an event label.

    Only a market state may overwrite another. "Price Drop" landing on top of an
    MLS "Pending" erased the one fact keeping that home out of the alert path.
    """
    norm = _norm(status)
    return norm in MARKET_LIVE_STATUSES or norm in OFF_MARKET_STATUSES
