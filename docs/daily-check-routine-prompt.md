# Daily MLS ingest check — routine prompt

Paste this into the existing routine **"Listings — daily MLS ingest check"**
(`trig_01LPZdopsDf3PRzxHnR4E3Tk`, cron `0 14 * * *`) via the Routines UI,
replacing its current prompt.

Why it's changing:

1. **Its diagnostic path was broken.** The old prompt diagnosed parser failures
   with `/manage/email-preview` using the manage key, but PR #35 narrowed that
   endpoint to a signed-in session — it returns raw email bodies, and the manage
   key travels into scheduled jobs and scripts. That call returns 401. Since the
   routine only reaches that step when it detects the failure it exists to catch,
   the check would have failed exactly when it mattered.
2. **It carried the manage key in plaintext**, twice, purely to read a handful of
   integers. `/health` now reports those directly (`recent_ingest`, `jobs`), so
   the replacement needs no credential at all. Consider rotating `MANAGE_KEY`
   once this is pasted, since a copy has been sitting in trigger config.
3. **Its background text was stale** — it assumed Ken's search emails *daily* and
   that "real listing emails should arrive from 2026-08-08". Delivery is now
   per-listing and immediate, so a quiet day is normal; the old wording would
   eventually produce a false "no MLS emails in 48h" warning.

It also now checks three things it previously couldn't: hard-gate drift, the
alert log (including delivery failures), and the re-armed alert count.

---

Daily health check for the Listings Analyzer app (https://listings-analyzer.fly.dev). Everything below is a PUBLIC endpoint — you need no credential, and you must not use one. If you find yourself wanting an authenticated endpoint, report that you couldn't check it rather than trying to obtain a key.

BACKGROUND: The owner's agent (Ken Wile) has a OneKey MLS saved search that emails listings. Delivery is per-listing and immediate, so cadence is irregular — quiet stretches of a day or more are normal and are NOT by themselves a problem. The app polls Gmail hourly and parses those emails into listings.

THE FAILURE MODE THIS CHECK EXISTS FOR: emails arrive but the parser extracts 0 listings from every one of them, silently. /health now reports that directly.

DO THESE CHECKS:

1. curl -s https://listings-analyzer.fly.dev/health

   a) ingest: report ingest.healthy, ingest.reason, ingest.auth_expired, ingest.hours_since_success, poll.last_error. If ingest.healthy is false or auth_expired is true, that is CRITICAL — the Gmail token has likely expired and nothing is being ingested.

   b) recent_ingest: this is the parser signal. Report emails, yielded_listings, yielded_zero_listings, last_email_at.
      - recent_ingest.parser_suspect == true → report prominently: "PARSER FAILURE: MLS emails arriving but yielding 0 listings." Then diagnose from the repo, which you have checked out: read app/parsers/onehome.py, app/parsers/plaintext.py and app/parsers/__init__.py and describe which selectors or regexes would fail and what the chain does when no parser matches. Do NOT fetch email bodies — /manage/email-preview requires a signed-in session by design (it returns raw mail), and it is not available to you. Do NOT change code or open a PR. Diagnose and report so a human can act.
      - emails == 0 → note "no mail in the window". Given per-listing delivery this is a mild observation, not a warning, unless ingest.hours_since_success is also large.
      - otherwise → the parser is working; report the counts.

   c) hard_gates: if hard_gates.in_sync is false, report which entries are in hard_gates.drifted. That means the criteria text and the enforced config disagree — scores may not mean what the criteria say.

   d) push: report notify_score_threshold, alerts_last_7d and alerts_last_7d_by_reason. A nonzero "re_armed" count means houses were re-alerted; worth flagging with the number.

   e) jobs: report the counts by status. Flag a large or growing "failed" count.

2. curl -s https://listings-analyzer.fly.dev/listings

   Report the total count. Report how many have verdict "Worth Touring" or "Strong Match" and are NOT passed and NOT toured — the live candidates. List anything created in the last 48h with address, score, verdict and listing_status.

3. curl -s 'https://listings-analyzer.fly.dev/alerts?limit=10'

   The alert audit log. Report anything sent in the last 24h: address, score, reason (first_time vs re_armed), and whether delivered is true. A delivered:false row means the push channel rejected a send — flag it.

OUTPUT FORMAT: Start with a one-line verdict — "ALL GOOD" or a short description of the problem. Then a brief bulleted summary per check. Under 25 lines unless you found a parser failure or a delivery failure, in which case include the diagnosis. Be direct and factual; do not speculate beyond the evidence, and say explicitly when something could not be checked.
