# Backlog

Things decided but not built. Each entry says what the buyer asked for and
what it would take, so a later session does not have to re-derive it.

## Score financial commitment, not asking price

**Asked for:** 2026-09-20 — "in general lets factor in property tax in
addition to house asking price to determine financial commitment. lets add
that feature later."

Today price and property tax are scored as two unrelated soft factors: the
price curve prices the ask, and a separate `-3 slightly high / -6 very high
for area` prices the tax. Nothing combines them, so a $1.6M home with $42k
taxes and a $1.75M home with $18k taxes rank as if the cheaper one is the
cheaper one. In Westchester that difference is roughly $2,000/month, which
dwarfs most of the soft points the score currently moves on.

**What it would take**

- A single carrying-cost number per listing: mortgage on the ask at a stated
  rate and down payment, plus monthly property tax, plus HOA. All three inputs
  already exist — `price`, `property_tax_json`, `hoa_monthly` — but tax is
  populated on only 39% of listings, so the feature needs the tax coverage gap
  closed first or it will rank on absence of data.
- Replace the price curve's bands with bands over the carrying-cost number,
  anchored the same way the v78 curve is anchored: state the comfortable
  monthly figure, the realistic top, and the ceiling. The buyer should give
  those three numbers in dollars per month; do not infer them from the price
  bands, since the whole point is that the two are not interchangeable.
- Keep the hard price band as-is. It is a gate against listings that are the
  wrong market entirely, not an affordability statement.
- Missing tax data must cost nothing (the criteria's standing rule), which
  means the carrying-cost band can only apply where tax is known; everything
  else falls back to the price curve and says so in confidence.

**Why it is worth doing:** the scores derive from the ledger now, so a band
the buyer sets moves the board by exactly what he set it to. A carrying-cost
band is the highest-leverage weight he could give the system.
