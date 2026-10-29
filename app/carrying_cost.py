"""What a listing actually costs per month, and how that compares to renting.

The app ranked list price. List price is not the cost: on this board the
correlation between price and price-neutral house quality is -0.09, so the
number being scored carried almost no information about the house, while the
number that decides the purchase — monthly outlay against a $5,000 rent — was
never computed at all.

Two figures, deliberately kept apart:

  cash      what leaves the account each month. Mortgage payment, tax,
            insurance, maintenance, HOA, incremental utilities.
  economic  what it actually costs. Mortgage INTEREST only (principal is
            savings), plus the same carrying items, plus the return given up
            on the down payment and closing costs.

Cash is what a budget has to absorb; economic is what the decision turns on.
Reporting one as the other is how "$9,300 more per month" gets quoted — a
figure that counts principal as a cost and ignores the opportunity cost on
~$395,000, two errors in opposite directions.

Every default here is a stated assumption, not a measurement, and is overridden
by a real figure whenever the listing page published one.
"""

from app.config import settings

# Fallbacks, in order of preference, when a listing page published nothing.
#
# 1.74% is not the Westchester "average" the dashboard used to assume (2.2%,
# which overstated every listing by about a quarter). It is what the two
# listing pages that published an actual bill imply: $1,992/mo on $1,390,000
# is 1.72%, and $2,088/mo on $1,400,000 is 1.79%. Two data points is thin, and
# it is still a guess — it is just a guess with evidence behind it now.
DEFAULT_TAX_RATE = 0.0174
DEFAULT_INSURANCE_RATE = 0.0042      # ~$600/mo on $1.725M
DEFAULT_MAINTENANCE_RATE = 0.01      # 1%/yr, the conventional reserve
DEFAULT_UTILITIES_MONTHLY = 300      # incremental vs an apartment
CLOSING_COST_RATE = 0.029


def _monthly_payment(principal: float, annual_rate: float, years: int = 30) -> float:
    r = annual_rate / 12
    n = years * 12
    if r <= 0:
        return principal / n
    return principal * r * (1 + r) ** n / ((1 + r) ** n - 1)


def _first_year_interest(principal: float, annual_rate: float, years: int = 30) -> float:
    r = annual_rate / 12
    pay = _monthly_payment(principal, annual_rate, years)
    balance, interest = principal, 0.0
    for _ in range(12):
        charge = balance * r
        interest += charge
        balance -= pay - charge
    return interest


def carrying_cost(listing: dict, *, rate: float | None = None) -> dict | None:
    """Monthly cash and economic cost for one listing, or None without a price.

    `sources` names where each input came from, because a number assembled from
    one measured figure and four assumptions should not be presented with the
    same confidence as one assembled from five measured figures.
    """
    price = listing.get("price")
    if not price or price < 100_000:
        return None

    sources = {}
    annual_rate = rate or listing.get("page_mortgage_rate")
    if annual_rate and annual_rate > 1:       # pages quote 6.38, not 0.0638
        annual_rate /= 100
    if annual_rate:
        sources["rate"] = "listing page" if not rate else "caller"
    else:
        annual_rate = settings.mortgage_rate_assumed
        sources["rate"] = "assumed"

    tax = listing.get("tax_monthly")
    if tax:
        sources["tax"] = listing.get("payment_source") or "listing page"
    else:
        tax = price * DEFAULT_TAX_RATE / 12
        sources["tax"] = "assumed"

    insurance = listing.get("insurance_monthly")
    if insurance:
        sources["insurance"] = "listing page"
    else:
        insurance = price * DEFAULT_INSURANCE_RATE / 12
        sources["insurance"] = "assumed"

    hoa = listing.get("hoa_monthly") or listing.get("hoa_monthly_page") or 0
    maintenance = price * DEFAULT_MAINTENANCE_RATE / 12
    utilities = DEFAULT_UTILITIES_MONTHLY

    down = price * settings.down_payment_fraction
    loan = price - down
    payment = _monthly_payment(loan, annual_rate)
    interest = _first_year_interest(loan, annual_rate) / 12
    capital = down + price * CLOSING_COST_RATE
    opportunity = capital * settings.opportunity_cost_rate / 12

    carry = tax + insurance + maintenance + utilities + hoa
    cash = payment + carry
    economic = interest + carry + opportunity

    rent = settings.current_rent_monthly
    appreciation_needed = None
    if price:
        # The annual appreciation that makes owning cost the same as renting.
        appreciation_needed = round(((economic - rent) * 12) / price * 100, 1)

    return {
        "price": price,
        "mortgage_rate": round(annual_rate * 100, 2),
        "monthly_payment": round(payment),
        "monthly_interest": round(interest),
        "tax_monthly": round(tax),
        "insurance_monthly": round(insurance),
        "maintenance_monthly": round(maintenance),
        "utilities_monthly": utilities,
        "hoa_monthly": round(hoa),
        "cash_monthly": round(cash),
        "economic_monthly": round(economic),
        "capital_required": round(capital),
        "vs_rent_monthly": round(economic - rent),
        "appreciation_needed_pct": appreciation_needed,
        "sources": sources,
        "measured_inputs": sum(1 for v in sources.values() if v != "assumed"),
    }


def offer_for_ceiling(listing: dict, ceiling: int | None = None) -> dict | None:
    """The purchase price whose all-in monthly cost hits the buyer's ceiling.

    The ceiling is a PURCHASE price, not a search filter. The buyer's position
    is "list can run a bit above $1.5M, we offer $1.5M" — so the useful number
    is not whether the ask fits the budget but what offer would make it fit,
    and whether that discount is one anyone actually gets.

    No assumed discount goes into the score. The score prices the ask, because
    the ask is the only price that exists yet. This is reported alongside it.

    Returns None without a price. `achievable` is deliberately conservative:
    the buyer's one observed negotiation was $1.8M -> $1.725M, 4.2% off ask.
    """
    price = listing.get("price")
    if not price or price < 100_000:
        return None
    ceiling = ceiling or settings.monthly_cost_ceiling
    if not ceiling:
        return None

    # Bisect on price: every cost component is monotonic in it.
    lo, hi = 100_000, price
    at_ask = carrying_cost(listing)
    if not at_ask:
        return None
    if at_ask["cash_monthly"] <= ceiling:
        return {
            "ceiling_monthly": ceiling,
            "offer_needed": price,
            "discount_pct": 0.0,
            "within_budget_at_ask": True,
            "achievable": True,
        }
    for _ in range(40):
        mid = (lo + hi) / 2
        probe = dict(listing)
        probe["price"] = int(mid)
        # A published tax bill belongs to the house at its assessed value, not
        # to a hypothetical purchase price; scale it so the probe is coherent.
        if listing.get("tax_monthly"):
            probe["tax_monthly"] = round(listing["tax_monthly"] * mid / price)
        c = carrying_cost(probe)
        if not c:
            break
        if c["cash_monthly"] > ceiling:
            hi = mid
        else:
            lo = mid
    offer = int(lo)
    discount = round((price - offer) / price * 100, 1)
    return {
        "ceiling_monthly": ceiling,
        "offer_needed": offer,
        "discount_pct": discount,
        "within_budget_at_ask": False,
        "achievable": discount <= settings.achievable_discount_pct,
    }


def offer_summary(listing: dict) -> str | None:
    """One line about the gap between the ask and what the buyer would pay."""
    o = offer_for_ceiling(listing)
    if not o:
        return None
    if o["within_budget_at_ask"]:
        return f"Within budget at the ${listing['price']:,} ask"
    verdict = "a realistic cut" if o["achievable"] else "more than anyone usually gets off ask"
    return (
        f"Needs an offer near ${o['offer_needed']:,} "
        f"({o['discount_pct']:g}% off ask) to stay under "
        f"${o['ceiling_monthly']:,}/mo — {verdict}"
    )


def cost_summary(listing: dict) -> str | None:
    """One line for a listing card."""
    c = carrying_cost(listing)
    if not c:
        return None
    return (
        f"${c['cash_monthly']:,}/mo all-in · ${c['vs_rent_monthly']:,}/mo more "
        f"than renting · needs {c['appreciation_needed_pct']:g}%/yr to break even"
    )
