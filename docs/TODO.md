# Where we left off — carrying-cost work

Branch `main-dspu58`. Plan: `docs/plan-carrying-cost.md`. Analysis context is in
that plan's opening section.

## Decisions already made (don't relitigate)

- **Cost ceiling: $12,000/mo all-in**, which is a $1.5M *purchase*, not a
  search filter. List price may run above it; the ask is an opening position.
  `settings.monthly_cost_ceiling`.
- **Realistic discount off ask: 6%.** The one observed negotiation was
  $1.8M -> $1.725M, 4.2%. `settings.achievable_discount_pct`.
- **Schools: keep full weight, add a district-premium flag.** Informational,
  no scoring change.
- **No criteria change needed for price.** v79's curve (0 at <=$1.5M, -5 at
  $1.5-1.6M, -10 at $1.6-1.7M) already matches "offer $1.5M, list can run a bit
  higher". A $1.65M ask is mildly penalised, which is right: reachable, needs
  work.
- Nothing scores on an assumed discount. The score prices the ask, because the
  ask is the only price that exists yet.

## Done and pushed

- **Price history.** `price_history` table, written from `save_listing()` on
  the same cursor as the listing, and from `_update_duplicate()` where changes
  were already being detected and discarded. Existing listings get a seeded
  baseline at boot.
- **`app/market_history.py`** — days tracked, cuts, reduction from first ask,
  and `leverage_note()` ("Tracked 120 days, cut 2x, down 8.6% from the first
  ask of $1,850,000"). Returns None when a listing is fresh.
- **Payment-block extractor** (`payment_from_markdown` in the OneHome parser) —
  pulls monthly tax, HOA, insurance and the page's own mortgage rate off
  Trulia/Zillow pages the scraper was already fetching. Stored in six new
  columns. These are the first real tax figures in the project.
- **`app/carrying_cost.py`** — cash vs economic cost kept separate, every input
  preferring a measured figure over a stated assumption, `measured_inputs`
  reporting how many of each went in, and the break-even appreciation rate.
- **`offer_for_ceiling()`** — the offer needed to hit $12,000/mo and the
  discount off ask it implies, flagged against the 6% realism bar.
- **Dashboard** — uses the server's carrying cost (mortgage included, real tax
  where published) instead of guessing 2.2% of price; leverage badge on cards.

## Next, in order

1. **Surface `offer_summary()` on the cards and `/listings`.** The function
   exists and is tested; nothing displays it yet. Smallest remaining step.
2. **District-premium flag.** Per listing, roughly what the strong district is
   costing versus a comparable home in a good-but-not-top district. The ~$260-300k
   median premium came from a crude median comparison that conflates district
   with town and house size — do better than that before showing a number, or
   show a range and say it is rough.
3. **P4: make an alert mean something.** 12 live homes at >=70 is not a signal
   when the strategy is "wait for exceptional". An alert should require the
   home to clear the score bar *and* be reachable under the ceiling —
   `offer_for_ceiling(...)["achievable"]` is the test.
4. **Pool detection pass over the top board.** Still open from three weeks ago:
   8 of the top 10 live listings have unknown pool status, including both new
   arrivals. A pool is -25 now, and 7 Lorenz Dr sat at a perfect 100 with one.
   Images are already stored; this is a vision pass over what we hold.
5. **P5: sale prices for comps.** Offer strategy needs what homes sold for, not
   what they asked. The app tracks `Sold` status and no sale price.
   Feasibility unknown — check before planning.

## Watch out for

- **No venv survives a container restart.** `python3 -m venv .venv` then
  `pip install` the deps from `pyproject.toml` by hand — `pip install -e .`
  fails on this repo (flat layout, `app` and `samples` both top-level).
- **`data.ny.gov` is blocked from the dev container** (egress policy, 403 on
  CONNECT). Production reaches it fine. Don't conclude the dataset is missing.
- **Don't deploy during a criteria rescore** — a restart loses the in-flight
  state and the remainder keeps old scores at the top of the board.
- 406 Bedford's price is corrected ($1.8M) but its pool is still not in the
  record: `has_pool` is null and the field isn't in the fix-listing allowlist.
