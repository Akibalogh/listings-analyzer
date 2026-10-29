"""Negotiating position, derived from what a listing has done over time.

The board knew what a home costs today and nothing about how it got there. A
home listed at $1.85M in March, cut twice, sitting 94 days, is a different
negotiation from one listed at $1.69M last week — and the app presented them
identically. That gap is what the $1.725M offer on 406 Bedford was made into:
the broker's advice was "best and highest immediately", and there was no
evidence on hand to argue with it.

Nothing here scores a listing. It reports facts about the listing's history so
a human can judge the offer. Whether days on market should cost or earn points
is a buyer's judgement, not a parser's.
"""

from datetime import datetime, timezone

# Below this, a "cut" is a rounding artifact or a parse wobble, not a signal.
_MIN_MEANINGFUL_CUT = 0.005  # 0.5%


def _parse_ts(value) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def market_history(
    first_seen,
    history: list[tuple[int, str]] | None,
    current_price: int | None,
    now: datetime | None = None,
) -> dict:
    """Days on market, price cuts, and total reduction from the first ask.

    `history` is (price, seen_at) oldest first. `first_seen` is when the app
    first saw the listing, which is a floor on days-on-market, not the true
    list date — the app only knows what arrived in an alert email. Reported as
    `days_tracked` for that reason: naming it days_on_market would claim
    knowledge of a date nobody here has.
    """
    now = now or datetime.now(timezone.utc)
    first = _parse_ts(first_seen)
    days_tracked = max(0, (now - first).days) if first else None

    rows = [(p, _parse_ts(t)) for p, t in (history or []) if p]
    prices = [p for p, _ in rows]
    if current_price and (not prices or prices[-1] != current_price):
        prices.append(current_price)
        rows.append((current_price, None))

    cuts, raises, last_change_at = 0, 0, None
    for (prev, _), (curr, when) in zip(rows, rows[1:]):
        if not prev:
            continue
        swing = (curr - prev) / prev
        if swing <= -_MIN_MEANINGFUL_CUT:
            cuts += 1
            last_change_at = when or last_change_at
        elif swing >= _MIN_MEANINGFUL_CUT:
            raises += 1
            last_change_at = when or last_change_at

    first_ask = prices[0] if prices else None
    latest = prices[-1] if prices else current_price
    reduction_pct = None
    if first_ask and latest and first_ask > 0 and latest != first_ask:
        reduction_pct = round((first_ask - latest) / first_ask * 100, 1)

    last_change = _parse_ts(last_change_at) if last_change_at else None
    return {
        "days_tracked": days_tracked,
        "first_ask": first_ask,
        "current_price": latest,
        "price_cuts": cuts,
        "price_raises": raises,
        "reduction_pct": reduction_pct,
        "days_since_last_change": (
            max(0, (now - last_change).days) if last_change else None),
        "observations": len(prices),
    }


def leverage_note(h: dict) -> str | None:
    """One line a human can take into an offer, or None when there is nothing.

    Deliberately states only what was observed. "Listed 94 days, cut twice,
    down 8% from the first ask" is an argument; "the seller is motivated" is a
    guess wearing an argument's clothes.
    """
    if not h:
        return None
    parts = []
    days = h.get("days_tracked")
    if days is not None and days >= 30:
        parts.append(f"tracked {days} days")
    cuts = h.get("price_cuts") or 0
    if cuts:
        parts.append(f"cut {cuts}x" if cuts > 1 else "cut once")
    red = h.get("reduction_pct")
    if red and red > 0:
        parts.append(f"down {red:g}% from the first ask of ${h['first_ask']:,}")
    if (h.get("price_raises") or 0) and not cuts:
        parts.append("raised since listing")
    return ", ".join(parts).capitalize() if parts else None
