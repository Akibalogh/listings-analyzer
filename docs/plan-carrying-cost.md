# Plan: make the bot answer the actual question

The app answers *"is this a good house?"* The decision is *"is this house worth
leaving a $5,000/month rent for?"* Those are different questions, and on the
current board almost nothing passes the second one. Five workstreams, ordered
by dependency.

## What the analysis established

Facts the build rests on, with their sources:

- Rent is $5,000/mo, capped at 3% to 2032. Owning at $1.725M costs ~$14,300/mo
  in cash and ~$10,500/mo in true economic terms (interest + tax + insurance +
  maintenance + opportunity cost on ~$395k, crediting 3% appreciation).
- Ownership at $1.725M matches that rent only at a **2.48%** mortgage rate
  (3.09% at $1.5M, 4.27% at $1.2M). At 7.32% the house must appreciate
  **6.9%/yr** to break even; at 6.38%, 6.1%/yr.
- Effective property tax, measured from two listing pages that published it:
  **1.72% and 1.79% of price**. That validates the 1.74% assumption the
  analysis used, and is the first tax figure in this project grounded in data.
- Across 106 live listings with a ledger, correlation between list price and
  price-neutral house quality is **-0.09**. Money does not buy a better house,
  except in the $1.65-1.80M band, which holds the best median merit (63) but
  only 13 homes.

## P1 — Record price history and days on market

The negotiation problem, and the cheapest fix. `_update_duplicate()` in
`app/poller.py` ALREADY detects every price change and applies it; nothing
records that it happened. So the board knows a home costs $1.69M and cannot
say it was listed at $1.85M in March and cut twice.

- New `price_history` table (listing_id, price, seen_at, source). A row on
  first save, a row at each change detected in `_update_duplicate`.
- Derived per listing: days on market (from `created_at`), cut count, total
  reduction %, days since the last cut.
- Exposed on `/listings` and as a dashboard badge.

No new data source, no new network call. This is the signal that would have
informed the $1.725M offer.

## P2 — Capture the payment block the listing pages already publish

Trulia/Zillow pages carry a payment calculator the scraper currently throws
away. Two listings captured it by accident, which is how it was found:

    Payment calculator $9,149 per month
    Principal and interest $6,937   Property taxes $1,992
    HOA dues $0                     Home insurance $220
    Home price $1,390,000           Loan details 30-yr fixed, 6.38%

That is monthly tax, HOA, insurance, and the current market mortgage rate —
every input P3 needs, from a fetch already being made.

Why not the obvious alternative: the app stores NY ORPTS *fractional
assessments* with no millage rate, so it cannot produce a tax bill from them
(139 of 485 listings have an assessment; zero have a bill). Whether NY
publishes a tax-rate dataset could not be checked from the dev container —
the egress policy denies data.ny.gov — and an integration will not be written
against a dataset nobody has confirmed exists. Description text is not an
option either: 3 of 319 descriptions mention a tax figure.

- `_payment_from_markdown()` beside the existing description/image extractors.
- Columns: `tax_monthly`, `hoa_monthly_page`, `insurance_monthly`,
  `page_mortgage_rate`, `payment_total_monthly`, `payment_source`.
- Tested against saved fixtures, so it is verifiable without the network.

## P3 — Make carrying cost the scored quantity

Replaces the price curve, which ranks a number that is not the cost.

- `app/carrying_cost.py`: monthly economic cost from price, tax, HOA,
  insurance, maintenance, rate, down payment and opportunity cost.
- Per listing: monthly all-in, the delta against $5,000 rent, and the
  appreciation rate needed to break even.
- Criteria v80 scores bands over monthly cost instead of list price.
- **Fails safe**: no tax figure means no carrying cost, and the listing falls
  back to the v79 price curve rather than being scored on a guess.

## P4 — Make an alert mean something

12 live homes at >=70 is not a signal when the strategy is "wait for
exceptional". An alert should require the home to be exceptional *and* within
a stated monthly ceiling.

## P5 — Sale prices, for comps

Offer strategy needs what homes actually sold for, not what they asked. The
app tracks `Sold` status but no sale price. Feasibility unknown; last.

## Business decisions — the only things to come back for

Technical choices are not on this list; these are not technical.

1. **The monthly all-in ceiling.** $5,000 is today, $14,300 was too much. The
   number in between is the one that sets the curve, and it is a judgement
   about their life, not their data.
2. **Down payment and opportunity cost.** 20% and 5% are assumed throughout.
   A founder holding cash for runway may price that capital higher.
3. **School timing.** Schools are the heaviest factor (+18 to -30) and top
   districts carry a ~$260-300k median premium on this board. There is no
   child yet, and enrollment is ~5 years after there is one. Whether to
   time-phase that weight is a family decision.
