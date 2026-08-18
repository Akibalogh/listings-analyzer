# Criteria v76 draft — make the score arithmetic honest

**Status: DRAFT — not active. Saving this as v76 triggers a full-corpus rescore
and reprices the board, so it ships only on Aki's sign-off.**

The code side (PR: arithmetic contract, validation, telemetry) works with v75
but is calibrated for these text changes. Four edits to v75, everything else
unchanged:

## 1. One school-district adjustment

v75 awards "+25 strong school district" once, but 64 of 112 live breakdowns
scored elementary, middle, and high separately (max +75 for one district) —
the single largest reason breakdowns summed outside 0–100. Replace the school
lines with:

> Score the school district ONCE, judged on the best-ranked elementary school
> (the same measure code uses to validate school rejections):
> +25 strong district (best elementary at 95th percentile or higher)
> +10 good district (80th–94th)
> −20 mediocre district (50th–79th)
> −35 weak district (below 50th — near-dealbreaker; a home in a weak district
>     should not reach Worth Touring regardless of other strengths)
> Exactly one school entry in soft_points. Never one per school level.

(The −35 stays a penalty, not a Reject — v75 already says so; the hardcoded
prompt used to disagree and has been fixed to defer to this table.)

## 2. Restate the arithmetic as a contract

After "Base score: 30", add:

> The final score IS the arithmetic: 30 + the sum of every adjustment listed
> in soft_points, clamped to 0–100. soft_points is the complete ledger — every
> adjustment applied appears there exactly once. A score that disagrees with
> its own ledger is invalid.

## 3. Rename the bottom verdict band

v75 says "Below 40 Pass". Code and dashboard say "Weak Match", and "Pass"
reads dangerously close to "passes". Change to:

> Below 40 Weak Match

## 4. No other weight changes

Deliberately. The stacked-school fix removes the systematic out-of-range
driver; the integrity endpoint's `score_vs_breakdown` section measures the
residual after v76's rescore. Recalibrate further only on that evidence.

## What repricing to expect

Unknowable precisely before the rescore, but directionally: listings whose 72
rode a holistic bump over a weak breakdown will drop; listings with strong
single-district schools keep their +25. The alert latch is safe — a criteria
rescore never re-arms it (app/db.py, criteria_rescore=True).
