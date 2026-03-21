# Product Requirements Document

**Product:** Listing Analyzer
**Owner:** Aki
**Objective:** Automated Gmail alert ingestion → parse listings → AI-powered scoring against editable buyer criteria → mobile-friendly dashboard.

---

## 1. Problem

You receive listing alerts via email from your real estate agent. They contain:
- Partial address
- MLS ID
- Price + basic stats
- Tokenized OneHome links (JWT, expire quickly)

Manually reviewing each listing against your criteria is tedious and error-prone.

You want:
- Automatic email ingestion (no manual pasting)
- Structured data extraction from email alerts
- AI-powered evaluation against your specific buyer criteria
- Mobile-friendly dashboard to review and filter listings
- Editable criteria that auto-re-score all listings when changed

## 2. Goals

### Primary
- Input: Gmail listing alert emails (auto-polled)
- Output: scored + evaluated listings on a mobile dashboard

### Secondary
- Store listing history with scores
- Filter and sort listings by verdict, score, price
- Editable evaluation criteria with versioning
- Description scraping from listing pages for deeper AI evaluation
- Mark listings as toured
- Flag listings as "Want to Go" for realtor tour requests
- Add listings manually from a URL (paste Redfin link → auto-scrape + score)

### Non-goals
- Agent messaging
- Offer execution
- Comps / price analysis (future)

### Data Enrichment
- **School data** — SchoolDigger API (free DEV tier, 20 calls/day) fetches nearby school rankings by zip code; cached in DB to avoid rate limit; displayed on cards and fed into AI scoring
- **Transit commute times** — Google Routes API (Essentials tier, 10K free/month) calculates commute to Brookfield Place NYC (230 Vesey St, 10281) next weekday 8 AM; drive-to-station + transit (the only mode); station overrides map towns to nearest Metro-North station (e.g., Dobbs Ferry → Dobbs Ferry, Cortlandt Manor → Croton-Harmon, Chappaqua/Millwood/New Castle → Chappaqua, Armonk/North Castle → North White Plains); displayed as commute badge on dashboard
- **Address-based dedup** — Normalized address keys (Avenue→Ave, Street→St, New York→NY, etc.) prevent duplicate listings across different email parsers; keys are recomputed on every startup so normalization improvements apply retroactively; startup dedup pass merges any duplicates that emerge, keeping the listing with toured status / most data

## 3. User Flow

1. Agent sends listing alert emails to your Gmail.
2. App auto-polls Gmail (configurable interval, default 1h) or user clicks "Check Email".
3. Parser extracts structured listing data from email HTML/text.
4. Listing URLs are extracted from email links; descriptions scraped from listing pages.
5. Enrichment: school rankings fetched via SchoolDigger API (cached by zip), commute times via Google Routes API.
6. AI evaluator (Claude Haiku) scores each listing against user-defined criteria, factoring in school quality and commute time.
7. User reviews scored listings on mobile dashboard (public read-only — no login required to view).
8. User can edit AI criteria → all listings auto-re-scored.
9. User can reprocess old emails to backfill URLs/descriptions after parser updates.
10. User can mark listings as toured to track which properties have been visited.
11. User can flag listings as "Want to Go" to signal tour interest to their realtor.
12. User can paste a listing URL to manually add a new listing (auto-scrapes, enriches, and scores).

## 4. System Architecture

### Input Layer
- **Gmail API** auto-polls for new listing alert emails
- Supports forwarded emails (detects and unwraps)
- Configurable sender list (env var `ALERT_SENDERS`) — supports domain-level matching (e.g., `redfin.com` catches all senders from that domain)
- Date-filtered senders (env var `SENDER_DATE_FILTERS`, format: `email:days,email:days`) — for personal contacts where only recent emails are relevant
- **Global email age filter** (env var `MAX_EMAIL_AGE_DAYS`, default: 21) — Gmail queries include `newer_than:{days}d` to skip stale/sold listings from old alert emails
- Separate Gmail queries per sender group with deduplication
- Processes each email once, marks with `ListingsAnalyzer/Processed` label

### Known Input Format: OneHome/Matrix MLS Email
Primary alert source is OneKey MLS NY email alerts.

**HTML structure** (reliable CSS selectors):
- Each listing wrapped in `<!--@Record:{ID}@-->` comment + `<DIV class="multiLineDisplay">`
- `.highlight-price` → `$1,295,000`
- `.highlight-title` → `Residential`
- `.highlight-description` → street address (`11 Jennifer Lane`)
- `.highlight-address` → city/state/zip (`Rye Brook, New York 10573`)
- `.highlight-specs` → `4 bd • 3 ba • 2,437 sqft` and `MLS #964038` (two separate `<p>` elements)
- `.highlight-status` → `<img alt="New Listing">` or `<img alt="Price Increased">`
- OneHome portal links extracted per listing (used for description scraping)

**Parsing strategy (parser chain):**
1. **OneHome HTML parser** (`app/parsers/onehome.py`) — BeautifulSoup CSS selectors (fast, deterministic)
2. **Plain text parser** (`app/parsers/plaintext.py`) — regex for `$price`, `N bd`, `N ba`, `N,NNN sqft`, `MLS #NNNNNN`; strips and captures status prefixes (New Listing, Pending, etc.); extracts inline addresses with directional suffixes (`101 Long Hill Rd E, Briarcliff Manor, NY 10510`) and listing URLs (Redfin, OneKeyMLS, OneHome); falls back to Redfin URL path for address extraction
3. **LLM fallback parser** (`app/parsers/llm.py`) — Claude Haiku for ambiguous/unknown formats

### Parsing Layer
- Extract per listing:
  - Street, Town, State, Zip
  - MLS ID
  - Price, Sqft, Bedrooms, Bathrooms
  - Property type, Listing status (New, Price Changed, etc.)
  - **Listing URL** (OneHome portal link)
- Duplicate detection by MLS ID
- **Validation gate:** listings with no address AND no MLS ID are rejected before saving (prevents garbage rows from bypassing dedup)

### Description + Image Scraping Layer
- After parsing, scrape each listing's URL for full property description and photos
- `scrape_listing_description()` in `app/parsers/onehome.py` — returns `(description, image_urls)` tuple
- **URL-aware scraping strategy with multi-site fallback:**
  - **OneHome URLs** (`portal.onehome.com`): Angular SPA — try OneKey MLS directly (if MLS ID known), then DuckDuckGo → Redfin
  - **Redfin URLs**: static HTTP (browser UA) → Jina Reader → OneKey MLS fallback
  - **Other URLs**: static HTTP → Jina Reader → DuckDuckGo Redfin search
- **OneKey MLS** (`onekeymls.com`) is the source MLS for the NY metro area; URL constructed directly from `address-town-state-zip/mls_id` — no DDG search needed, works reliably from cloud IPs
- Static HTTP uses a full browser User-Agent + Accept headers to bypass basic bot detection
- Jina Reader API (`r.jina.ai`) renders JavaScript SPAs server-side (fallback for Angular portals)
- **Selector-first extraction**: targeted CSS selectors (`section#overview` for OneKey, `div#house-info` / `.remarksContainer` for Redfin) are tried first; keyword-based fallback only used when no selector matches — prevents navigation/UI text from beating the real description
- Image extraction: targeted CDN selectors (Redfin CDN, CloudFront for OneKey, Coldwell Banker), skips icons/thumbnails
- Description + images fed to AI evaluator for deeper assessment (basement detection, amenities, condition)

### Reprocessing
- `POST /reprocess` re-fetches all previously processed emails from Gmail
- Re-parses with current parser (useful after parser updates add new extraction)
- Backfills listing URLs and descriptions for older listings
- Auto-triggers re-score if criteria exist

## 5. Evaluation Engine

### AI-Only Scoring

All scoring is performed by the **AI evaluator** (`app/scorer.py:ai_score_listing`):
- Uses Claude Opus (`claude-opus-4-6`, vision-capable) via Anthropic API
- Follows user-defined natural language instructions (editable in dashboard, stored in DB)
- Evaluates listing data + scraped description + optional images
- Returns structured JSON: score, verdict, hard results, soft points, concerns, reasoning, **property_summary**
- Server-side validation: score clamped 0–100, verdict/score consistency enforced (see below), confidence checked

If no API key is configured, or no criteria have been set, listings are saved with a placeholder score (0, "Weak Match", low confidence) and a concern explaining why.

### AI-Generated Property Summary
Each scored listing receives a `property_summary` — a structured factor-by-factor analysis generated by the AI:
- **Line 1:** verdict — score/100 (e.g., "Worth Touring — 68/100")
- **Factor lines:** one per evaluated criterion, prefixed with ✅ (pass), ⚠️ (concern), or ❓ (unknown)
- **Blank line + conclusion:** brief 1–2 sentence assessment
- Displayed as the primary analysis section in the expanded card view

### Unknown Penalty (Two-Tier)
The AI distinguishes two types of unknowns when evaluating criteria:
- **Verifiable unknown** — images or description are present but don't confirm the feature (e.g., multiple photos of a large house with no visible ground-floor bedroom). Apply a meaningful penalty (10–15 pts) since absence is likely.
- **Missing-data unknown** — no floor plans, sparse description; impossible to evaluate. Apply a mild penalty (3–5 pts) — penalizing missing data too heavily unfairly disadvantages listings where the information simply wasn't provided.

### Prompt Injection Defense
Listing data (address, description, etc.) could contain malicious instructions.
- Listing data wrapped in `<listing_data>` XML tags, never mixed into prompt prose
- System prompt explicitly instructs AI to treat listing data as DATA ONLY
- Server-side output validation (score clamped, verdict from allowed list)
- No code execution — AI returns JSON only

### Bulk Re-scoring
- Triggered when criteria are saved, or manually via `/manage/sync-criteria`
- Runs in a background daemon thread; progress exposed via `GET /rescore/status`
- **Sequential by default** — each listing scored one at a time via real-time API; results appear immediately as they complete; fastest path to seeing updated scores after criteria changes
- **Batch API available** via `?sequential=false` — 50% token discount but adds unpredictable async latency (Anthropic queues batch jobs); only worthwhile for very large rescores where cost matters more than speed

### Evaluation Criteria (Editable)
- Stored in `evaluation_criteria` table with versioning (each save = new version row)
- Editable via dashboard settings panel ("✨ AI Criteria") — **only authenticated users** can edit
- Saving new criteria triggers background re-score of all listings
- **No hardcoded default criteria** — criteria must be entered and saved via the dashboard on first use
- Full version history available via `GET /criteria/history`; viewable in the settings panel with one-click restore

### Buyer Criteria (Current — as configured in dashboard)

**Hard Requirements** (any failure = Reject, score 0):
- Location must be in New York State — NJ listings are a hard pass (Reject immediately, do not score)
- >= 2,200 sqft (2,200–2,400 sqft: mild soft penalty -5 to -7; no upper bound — 4,500+ sqft is a slight concern)
- 4+ bedrooms (no upper bound — but 6+ is a slight concern: unusual layout)
- Price: $1.25M–$2M (target range $1.25M–$1.5M; above $1.5M is a concern, not a reject)
- Must be detached (no townhouse, condo, co-op)
- Must have a finished basement

**Soft Features** (base score 25 + points):

| Feature | Points |
|---|---|
| Base (all hard reqs pass) | +25 |
| School district — strong (95th+ percentile) | +25 |
| School district — good (80–94th percentile) | +15 |
| School district — average (below 80th) | +5 |
| Ground-floor bedroom — confirmed present | +15 |
| Ground-floor bedroom — confirmed absent | -45 to -50 |
| Ground-floor bedroom — unknown, verifiable (floor plans present, not confirmed) | -10 to -15 |
| Ground-floor bedroom — unknown, no floor plan data | -3 to -5 |
| Ground-floor full bathroom | +15 |
| 4-bedroom layout | +10 |
| Dedicated office (beyond bedroom count) | +5 |
| Finished basement | +10 |
| Garage — two-car | +5 |
| Garage — one-car | +2 |
| Lot/drainage — elevated/hillside | +3 to +5 |
| Lot/drainage — low-lying | -3 |
| HOA present | 0 (fold into carrying cost; flag if >$500/mo) |
| Pool | -5 |
| Privacy buffer / meaningful lot | +5 to +10 |

Ground-floor bedroom is the #1 priority (buyer's parents will live on ground floor). Confirmed absent is a near-disqualifying deduction.

**Soft Warnings** (lower score slightly, never cause Reject):
- Price above $1.5M → -5 to -10 pts
- 4,000–4,500 sqft → -5 pts
- 4,500+ sqft → -12 pts
- 6+ bedrooms → -5 to -10 pts

### Verdict Logic

- Score >= 80 → **Strong Match**
- 60–79 → **Worth Touring**
- 40–59 → **Low Priority**
- < 40 → **Weak Match**
- Any hard fail → **Reject** (score 0)

**Server-side verdict/score consistency** (enforced in `_validate_ai_response`):
- If AI returns `"Reject"`, score is forced to 0 (hard fail always means 0 points)
- Otherwise verdict is always re-derived from score (ensures filter chips work correctly even if AI returns inconsistent values like "Weak Match" at score=42)

### Vision Support
- Image URLs can be attached to listings via `POST /listings/{id}/images`
- AI evaluator examines images for basement finish, condition, amenities
- Images fetched as base64, sent to Claude Haiku vision endpoint

## 6. Dashboard UI

Mobile-first single-page app served at `/` (`app/templates/dashboard.html`).

### Auth Model
- **Public (no login required):** view listings, scores, filter/sort, expand card details, view AI criteria (read-only)
- **Auth-required (Google sign-in):** Check Email, Settings (gear icon), edit AI Criteria, Reprocess, Mark as Toured, Want to Go, Mark as Sold, Add Listing, Scrape & Score
- Unauthenticated users see a "Sign in" button in the header; action buttons and settings are hidden
- AI Criteria panel: read-only for anonymous (accessible via old entry point if URL known), editable for authed; "Save & Re-score All" and Maintenance section hidden when not logged in

### Header
- App title
- Data confidence pill (e.g. "93% data") — weighted composite of 9 enrichment fields; color-coded green/amber/red; mouseover shows per-field breakdown tooltip
- ⚙ gear icon → settings modal *(auth-only)*
- "Check Email" button → triggers `POST /poll` *(auth-only)*
- "Sign out" button *(auth-only)* / "Sign in" button *(unauthenticated)*

### Listing Cards (Compact)
- Color-coded score badge (green/yellow/orange/red)
- Address (clickable link to listing URL when available)
- Price, sqft, beds/baths, $/sqft
- Verdict badge + evaluation method (AI vN)
- Listing status tag (New Listing, Price Increased, etc.)
- Toured badge (if marked as toured)
- "Want to Go" badge (blue pill, if flagged for tour request)

### Listing Cards (Expanded)
- Stats row: Price, Size, Beds, Baths, **Year Built**, **Days on Market (DOM)**, $/sqft
- Year Built extracted from listing pages or OneKeyMLS JSON-LD (`YearBuilt` field)
- DOM calculated from `list_date` (real on-market date from OneKeyMLS `OnMarketDate`)
- AI-generated **property summary** (structured factor analysis with ✅/⚠️/❓ indicators)
- Falls back to hard results checklist + concerns + AI reasoning for older listings without property_summary
- "Want to Go" / "✓ Want to Go — click to cancel" toggle *(auth-only)*
- "Mark as Toured" / "Unmark as Toured" toggle *(auth-only)*
- "Mark as Sold" button *(auth-only)*

### Filters & Sorting
- Filter chips: All, Active (non-reject), **Toured**, **Want to Go**, **Passed**
- Sort: Score (high→low/low→high), Price, Sqft, $/sqft, Newest first, Commute, Schools
- Filter counts shown on chips: Toured and Want to Go counts are always accurate (include pending/passed listings); All/Active counts respect display preferences

### Display Settings Panel (overlay, auth-only via ⚙ gear icon)
- "Hide passed listings" toggle (default ON) — excludes passed from All/Active view; **does not affect** Passed, Toured, or Want to Go filter chips
- "Hide pending/sold listings" toggle (default OFF) — excludes Pending/Sold/Under Contract/Closed from All/Active view; **does not affect** Toured or Want to Go filter chips
- "Hide low-scoring listings" toggle (default OFF) — excludes Weak Match and Reject from All/Active view; **does not affect** Toured or Want to Go filter chips
- "Default sort" dropdown — persisted across sessions
- All preferences auto-saved to `localStorage` on toggle change (no Save button)

### AI Criteria Panel (overlay, auth-only via ✨ AI Criteria button)
- Textarea with current AI evaluation instructions *(read-only for anonymous, editable for authed)*
- Version indicator (version number + created_by)
- "Save & Re-score All" button — saves new criteria version and triggers background rescore
- Re-score progress bar (polls `GET /rescore/status` every 2s)
- **Version history** — lists all past versions with date and preview; click any row to load into textarea; "Current" badge on active version; one-click restore before saving
- Maintenance section: "Reprocess Emails" button

### Add Listing from URL
- "**+ Add Listing**" bar at top of dashboard *(auth-only)* — paste any listing URL (Redfin, short redf.in links, etc.)
- Resolves short URLs, extracts address from Redfin URL path, scrapes description/images, extracts structured data (price/beds/baths/sqft), enriches with schools + commute, scores with AI
- Duplicate detection by normalized address key — rejects if listing already exists
- Loading state: button shows "Adding…", input disables during scrape; Enter key submits
- Source format stored as `"manual"` to distinguish from email-ingested listings

### URL/Description Management
- Listings without URLs show input field + "Scrape & Score" button *(auth-only)*
- Scraping fetches description from listing page and auto-re-scores

## 7. Data Model

### Table: processed_emails
- id, gmail_id (unique), message_id, sender, subject
- received_date, parser_used, listings_found, processed_at

### Table: listings
- id, source_email_id (FK → processed_emails)
- address, town, state, zip_code, mls_id
- price, sqft, bedrooms, bathrooms
- property_type, listing_status, source_format
- listing_url (TEXT, nullable — scraped from email)
- description (TEXT, nullable — scraped from listing page)
- image_urls_json (TEXT, nullable — JSON array)
- **toured (BOOLEAN DEFAULT FALSE)**
- **tour_requested (BOOLEAN DEFAULT FALSE)** — "Want to Go" flag
- **year_built (INTEGER, nullable)** — extracted from listing page or OneKeyMLS JSON-LD
- **list_date (TEXT, nullable)** — on-market date (YYYY-MM-DD); extracted from OneKeyMLS JSON-LD `OnMarketDate`
- **lot_acres (REAL, nullable)** — extracted from JSON-LD `lotSize` field, or visible text ("0.25 acres", "10,890 sq ft lot"); valid range 0.01–1000
- **lat (REAL, nullable)**, **lng (REAL, nullable)** — geocoded coordinates (Nominatim); used for OSM and FEMA lookups
- **power_line_json (TEXT, nullable)** — OSM Overpass result: nearest transmission line/tower distance/type/voltage within 300m
- **flood_zone_json (TEXT, nullable)** — FEMA NFHL result: fld_zone, zone_subty, sfha flag
- **station_json (TEXT, nullable)** — nearest Metro-North station: name, distance_m, walk_minutes
- **garage_count (INTEGER, nullable)** — parsed from description: number of garage stalls (0 = explicitly no garage)
- **garage_type (TEXT, nullable)** — "attached", "detached", "carport", or null
- **hoa_monthly (INTEGER, nullable)** — monthly HOA fee in dollars, parsed from description (0 = no HOA)
- **has_pool (BOOLEAN, nullable)** — pool on property, parsed from description
- **pool_type (TEXT, nullable)** — "inground", "above_ground", "community", or null
- **has_basement (BOOLEAN, nullable)** — basement present, parsed from description
- **basement_type (TEXT, nullable)** — "finished", "partially_finished", "unfinished", "walk_out", or null
- created_at

> **Note on `year_built` enrichment:** This column is populated from three sources in priority order: (1) OneKeyMLS JSON-LD `YearBuilt` field during scraping, (2) Redfin metadata text in descriptions (`"YYYY year built"` pattern via `parse_year_built()`), (3) manual backfill via `/manage/update-listing`. Coverage ~97% across active listings.

### Table: scores
- id, listing_id (unique FK → listings)
- score, verdict, hard_results_json, soft_points_json, concerns_json
- confidence, evaluation_method ('ai')
- criteria_version (FK → evaluation_criteria.version)
- ai_reasoning (TEXT, nullable)
- **property_summary (TEXT, nullable — AI-generated structured factor analysis)**
- created_at

### Table: evaluation_criteria
- id, instructions (TEXT), version (INT), created_by, created_at
- Versioned: highest version = active criteria
- **Not seeded on startup** — user must configure via dashboard on first use

## 8. API Endpoints

### Auth
- `GET /auth/config` — Google client ID for sign-in button
- `POST /auth/login` — verify Google ID token, create session cookie
- `POST /auth/logout` — clear session cookie
- `GET /auth/me` — current user email

### Listings (public — no auth required)
- `GET /listings` — all listings with scores
- `GET /listings/{mls_id}` — single listing by MLS ID

### Criteria (public read, auth to write)
- `GET /criteria` — active instructions + version *(public)*
- `GET /criteria/history` — all saved versions, newest first; includes preview + full instructions *(public)*
- `PUT /criteria` — save new instructions, trigger background re-score *(auth-required)*

### Actions (auth-required)
- `POST /poll` — trigger Gmail poll, process new emails
- `POST /reprocess` — re-fetch + re-parse all processed emails (backfill URLs/descriptions)
- `POST /listings/{id}/scrape` — set listing URL, scrape description, auto re-score
- `POST /listings/{id}/images` — attach image URLs to listing
- `POST /listings/{id}/rescore` — re-score single listing with current criteria
- `POST /listings/{id}/toured` — mark/unmark listing as toured
- `POST /listings/{id}/tour-request` — flag/unflag listing as "Want to Go" (body: `{"tour_requested": bool}`)
- `POST /listings/{id}/sold` — delete listing and its score (removes from dashboard immediately)
- `POST /listings/add` — create new listing from URL; resolves short links, scrapes, enriches, scores (body: `{"url": "https://..."}`); returns 409 if duplicate

### Status
- `GET /health` — health check
- `GET /rescore/status` — background re-score progress *(public)*

### Management (API key protected, `MANAGE_KEY` env var)
- `POST /manage/sync-criteria` — trigger rescore with current active criteria from DB
- `POST /manage/reprocess` — re-fetch emails, extract URLs, scrape descriptions, rescore
- `POST /manage/poll` — trigger email poll without Google auth
- `POST /manage/cleanup` — delete listings by ID list (body: `{"listing_ids": [1,2,3]}`)
- `POST /manage/reset-emails` — clear orphaned processed_emails and remove Gmail labels for re-ingestion
- `POST /manage/data-quality` — audit listings with missing address or URL (dry-run by default); with `?fix=true`: delete bad listings, reset orphaned emails, trigger re-poll + rescore
- `POST /manage/scrape-descriptions` — two-phase: (1) search DuckDuckGo for Redfin URLs for listings with address+town but no URL; (2) scrape descriptions + images for listings with URL but no description; triggers rescore if descriptions found
- `POST /manage/enrich` — backfill school data + commute times; `?clear_bogus=true` to re-fetch; runs in background
- `POST /manage/prune-sold` — check Redfin URLs via Jina Reader for sold/off-market status; dry-run by default, `?fix=true` deletes sold listings
- `POST /manage/update-criteria` — save new criteria instructions and trigger re-score without Google OAuth session (body: `{"instructions": "...", "created_by": "..."}`, `?sequential=true` for sequential mode)
- `POST /manage/update-listing` — update individual listing fields by ID without re-scraping (body: `{"listing_id": 62, "year_built": 1994}`); allowed fields: year_built, price, sqft, bedrooms, bathrooms, address, town, state, zip_code, property_type, lot_acres, list_date, listing_status; add `"force": true` to overwrite non-null values
- `POST /listings/{id}/agent` — set agent_name on a listing (body: `{"agent_name": "Matt Hermoza"}`); accepts auth session or MANAGE_KEY
- `GET /manage/senders` — list distinct email senders with listing counts; requires auth or MANAGE_KEY

## 9. Key Technical Decisions

### Stack
- **Language:** Python 3.12+
- **Framework:** FastAPI
- **DB:** SQLite (local) / Postgres (production, detected via `DATABASE_URL`)
- **Deployment:** Fly.io (Docker)
- **Dependencies:** managed with `uv`
- **Scraping:** httpx (static) + Jina Reader API (SPA rendering, no browser needed)

### AI Evaluation
- Claude Opus (`claude-opus-4-6`) — most capable, vision-capable; used for scoring quality
- LLM fallback parser uses Claude Haiku (`claude-haiku-4-5-20251001`) — fast/cheap for email parsing
- AI-only scoring (no deterministic fallback)
- Structured JSON output with server-side validation
- AI generates both a score + a `property_summary` narrative per listing

### Email Integration
- Gmail API with OAuth2 (refresh token)
- Background polling thread (configurable interval)
- Labels processed emails to avoid re-processing

### Auth
- Google Sign-In (OAuth2 ID token verification)
- Session cookies (HMAC-signed, 7-day expiry)
- Allowlisted emails only
- **Dashboard is public (read-only)** — auth only required for write actions

## 10. Edge Cases

- Duplicate MLS IDs → deduplicated on insert
- Missing description → AI evaluates with available data, marks unknown criteria
- Forwarded emails → detected and unwrapped before parsing
- Parser updates → reprocess endpoint backfills data from existing emails
- No API key configured → placeholder score saved (score=0, confidence=low, reason in concerns)
- No criteria set → placeholder score saved (score=0, confidence=low, reason in concerns)
- Prompt injection in listing data → XML-tagged isolation + system prompt defense
- Multi-story / non-standard layouts (e.g., 34 Lakeshore Drive — upper/lower floors, no clear basement) → AI may Reject on "finished basement" hard requirement; these should be reviewed manually for future AI criteria refinement
- **Status prefix stripping** — Redfin alert emails prepend status labels ("New Listing", "Pending", "Coming Soon", "Price Drop", etc.) as plain text before listing data. The plaintext parser strips these and captures them in `listing_status`. Known labels: New Listing, Pending, Coming Soon, New Favorite, Price Drop, Price Decreased, Price Increased, Back on Market, Sold, Contingent, Under Contract, Active, Open House, New Tour Insight, Updated MLS Listing.
- **Tour header stripping** — Redfin tour emails prepend "N homes on this tour" before listing blocks. The plaintext parser strips these via `TOUR_HEADER_RE` to prevent the tour count from being concatenated into the listing address.
- **Directional address suffixes** — Street addresses may end with a cardinal direction (N, S, E, W, NE, NW, SE, SW) after the street type suffix (e.g., "101 Long Hill Rd E"). Both `STREET_RE` and `INLINE_ADDR_RE` patterns handle optional directional suffixes.
- **Address-less listings** — Listings without an extractable address fall back to Redfin URL parsing, then zip-code-only. Address-less listings can be cleaned up via `/manage/data-quality?fix=true` (or manually via `/manage/cleanup`) and re-ingested via reset + poll.
- **DB connection timeouts** — `psycopg2` connections use `connect_timeout=5` (5s TCP connect) and `statement_timeout=30000` (30s query); SQLite uses `timeout=5` (5s lock wait). Prevents indefinite hangs when Postgres is temporarily unreachable.
- **Listings without clickable links** — Some listings ingested from Redfin daily alert emails have address/price data but no listing URL. This happens when the plaintext email body contains inline addresses without accompanying Redfin links (URLs were only in the HTML portion, which the plaintext parser doesn't process). `/manage/scrape-descriptions` attempts to find Redfin URLs via DuckDuckGo search; remaining no-URL listings are typically off-market, have unusual address formats DDG can't match, or are in areas Redfin doesn't cover. As of 2026-03-04, 35 listings have no URL:
  - **Parsing artifacts (1):** id=498 "44 S. Broadway, Suite 100| White Plains" — pipe-delimited town field indicates a parsing bug; MLS #3030
  - **New Jersey listings (3):** ids 506-508 — Basking Ridge, Bernardsville area; likely from a forwarded NJ search; DDG couldn't find Redfin pages
  - **Westchester NY (28):** ids 510-544, 720-723 — Chappaqua, Briarcliff Manor, Ossining, Pleasantville, Sleepy Hollow, Scarsdale, Ardsley; many are likely off-market or have addresses DDG couldn't match to Redfin listings
  - **Connecticut (1):** id=725 "332 Riversville Rd, Greenwich" — DDG didn't find a Redfin page
  - **Future mitigation:** Parsing the HTML portion of Redfin emails would capture URLs directly; alternatively, constructing Redfin URLs from address slugs (address-town-state-zip format) could bypass DDG search

## 11. Phase Plan

### Phase 1 (MVP) ✅ Complete
- Gmail auto-polling
- OneHome HTML parser + plain text + LLM fallback
- Deterministic scoring engine
- Mobile dashboard with Google auth

### Phase 2 ✅ Complete
- AI-powered evaluation (Claude Haiku)
- Editable natural language criteria with versioning
- Background re-scoring on criteria change
- Listing URL extraction + description/image scraping (static HTTP + Jina Reader SPA rendering)
- Email reprocessing for backfill
- Vision support (image URLs)
- Fly.io deployment with Postgres
- Management API (trigger rescore, reprocess) protected by API key
- Soft warnings for high sqft (4,500+), many bedrooms (6+), above-target price ($1.5M+)
- Public read-only dashboard (auth only for write actions)
- Toured listing tracking (badge, filter chip, toggle)
- Mark as Sold button (deletes listing from dashboard)
- AI-generated property summary (structured factor analysis per listing)
- Removed deterministic scoring path — AI-only evaluation
- No hardcoded default criteria — user configures via dashboard
- URL backfill via DuckDuckGo search for listings without links
- Automatic sold-listing pruning via Jina Reader (runs hourly in scheduler)
- Address dedup: state name normalization, startup key recomputation, automatic duplicate merging
- "Want to Go" tour request flag (blue badge, filter chip, toggle button)
- Manual listing add from URL (paste Redfin link → scrape + enrich + score)
- Duplicate status updates on re-encounter (status change + URL backfill)
- OneKey MLS DDG address search, listing status extraction, structured data extraction
- HTML URL backfill in plaintext parser (matches Redfin URLs from HTML to text-parsed listings)
- Structured data backfill for bare-URL listings (OneKey MLS) before scoring
- Commute is drive-to-station + transit only: pure transit (walking/bus) removed entirely for consistent, comparable commute times across all listings
- Station overrides expanded: Cortlandt Manor → Croton-Harmon; Chappaqua/Millwood/New Castle → Chappaqua; Armonk/North Castle → North White Plains
- `POST /manage/update-criteria` endpoint for criteria updates without Google OAuth (protected by MANAGE_KEY)
- **Agent tagging** — `AGENT_MAP` env var maps email senders / domains to agent names (e.g. `redfin.com:Ken Wile,bronwyneharris@gmail.com:Bronwyn`); auto-applied at poll time and backfilled on startup; shown as "Shared by {name}" note in listing detail card
- **Filter isolation fix** — Toured and Want to Go filter chips bypass all display preferences (hidePending, hidePassed, hideLowScore); regression test added
- **Filter count fix** — Toured and Want to Go chip counts now include listings with any status (pending/passed); previously pending listings with tour_requested=True were excluded from the count due to early-return in display prefs logic
- **Mobile header compact mode** — header collapses to icon-only buttons on screens ≤640px
- **Redfin bot detection + rate-limit mitigation** — scraper now: (1) rotates User-Agent across 5 browser signatures to avoid fingerprinting, (2) adds Referer, DNT, and Connection headers for realism, (3) retries Redfin URLs with exponential backoff delays (1-2s, 3-5s, 5-8s per retry), (4) detects "unknown address" bot-block pages and switches to fallback sources (OneKey MLS, Jina Reader) earlier, (5) bulk scrape operations add 2-3s delays between listing requests to respect Redfin rate limits
- Ground-floor bedroom scoring changed from binary reject to point-based: +15 confirmed, -20/-25 confirmed absent, -10/-15 verifiable unknown, -3/-5 missing-data unknown
- Two-tier unknown penalty in AI prompt: verifiable unknowns (images present, feature unconfirmed) = 10–15 pt deduction; missing-data unknowns (no floor plan) = 3–5 pt mild deduction
- Address normalization: hyphens stripped from both address and town (fixes Croton-On-Hudson duplication)

### Phase 3 ✅ Complete
- **Age/condition scoring** — deterministic from `year_built` (age tiers: pre-1940 → -22 pts, 2005+ → 0) plus keyword scan of description (e.g. "new roof" +6, "sold as is" -12). Passed to AI as `age_condition` signal. No external API.
- **Price/sqft benchmark** — Zillow ZHVI CSV loaded at startup (~5MB, monthly zip-level medians). Computes `below_market` / `at_market` / `above_market` signal. Passed to AI as `price_per_sqft_signal`.
- **Property tax — NYC SODA** — NY Open Data SODA API (free, no key required). Fetches assessed/market value for NYC boroughs during enrichment. Stored as `property_tax_json`. Passed to AI as `property_tax`.
- **Property tax — NY ORPTS** — NY State ORPTS API (`data.ny.gov/resource/7vem-aaz7.json`) fetches assessed/taxable values for all NY municipalities outside NYC (Westchester, Rockland, etc.). Integrated as fallback after NYC SODA. Municipality name mapped via `_ORPTS_MUNICIPALITY_MAP` (40+ Westchester hamlet→town mappings, e.g., Armonk→North Castle, Chappaqua→New Castle). Streets stored UPPERCASE with suffix in same field (e.g., "PHEASANT DR").
- **GFB inference improvements** — scorer system prompt expanded with 4-signal framework (floor plan labels, description text, photo examination, property type/age inference). Opus now commits at 60%+ confidence instead of defaulting to "unknown". Only returns unknown if all four signals are truly absent or contradictory.
- **Criteria v42** — schools tiered (+25 strong/95th+, +15 good/80–94th, +5 average/below 80th); price taper steeper (-5 at $1.5–1.65M, -10 at $1.65–1.8M, -15 above $1.8M); price/sqft signal doubled (±10 from ±5); Condition section collapsed into Age & Physical Condition to eliminate double-counting.
- **Criteria v43–v46** — garage scoring (+5 two-car, +2 one-car); pool reduced -15→-5 (maintenance burden, not desired but not a dealbreaker); HOA folded into carrying cost (no direct penalty); hill/drainage added (+3 to +5 elevated, -3 low-lying); sqft penalty starts at 4,000 sqft (4,000–4,500: -5, 4,500+: -12); lot_acres field added to criteria context.
- **`/manage/update-listing` endpoint** — update individual listing fields (year_built, price, sqft, beds, baths, address) without re-scraping; used for manual data backfill.
- **`clear_bogus_commute` flag** — `POST /manage/enrich?clear_bogus_commute=true` nulls commute data for listings where `commute_mode == "transit"` (transit-only = unreliable for suburban drives); forces re-enrichment with drive+transit as intended.
- **Lot size extraction** — `lot_acres` added as structured field; extracted from JSON-LD `lotSize` (object and string forms) and visible page text ("0.25 acres", "10,890 sq ft lot"); stored as `REAL` column; backfilled from stored description text during Phase 4 of `/manage/scrape-descriptions`.
- **year_built backfill** — scraped from Redfin via Jina Reader for 3 listings missing the field (#62 → 1994, #483 → 1948).

### Phase 4 (Current — Data Quality)
- **Power line proximity** — OSM Overpass API, 300m radius, stores `power_line_json` (96.5% coverage)
- **FEMA flood zone** — NFHL ArcGIS REST API (free, no key), stores `flood_zone_json` (96.5% coverage)
- **Metro-North station proximity** — static 61-station dataset (Harlem/Hudson/New Haven lines), stores `station_json` (96.5% coverage)
- **Description parsers** — `parse_garage_count()`, `parse_hoa_amount()`, `parse_pool_flag()`, `parse_basement()`, `parse_year_built()` — pure regex, no API calls; wired into `_enrich_all()` for automatic backfill
- **Criteria v47–v52** — calibration rebalance, commute scoring, power line/flood zone/station scoring, structured data references for garage/pool/basement/HOA

### Phase 4.1 (v7 Data Quality Fixes — Mar 21, 2026)
**Commute Calculation Fix**
- **Issue:** Dobbs Ferry listings (and other Westchester towns) returning 122+ minute commute times to Brookfield Place
- **Root cause:** Missing station override for Dobbs Ferry in `_STATION_OVERRIDES` dict; system generated invalid station string `"dobbs ferry train station, NY"` that Google Routes API couldn't geocode
- **Fix:** Added `"dobbs ferry": "Dobbs Ferry"` override (commit c418efa)
- **Impact:** Dobbs Ferry now correctly uses Dobbs Ferry Metro-North (Hudson Line), restoring realistic ~35–40 minute commute estimates

**AI Scorer Ground-Floor Bedroom Detection**
- **Issue:** Listings with clear first-floor bedrooms in floor plans marked as "not confirmed" (Cherry Hill Ct, Law Rd, etc.)
- **Root cause:** AI example output template showed bedroom as "unknown", anchoring scorer to reject floor plan evidence; prompt lacked explicit enforcement
- **Fix:** Updated example to show confirmed bedroom; rephrased critical instructions: "If floor plan shows bedroom on main floor = CONFIRMATION" (commit 20f45fb)
- **Impact:** Scorer now reliably detects and confirms ground-floor bedrooms from floor plan images; de-risks "parents' floor" criterion evaluation

### Phase 5 (Future)
- Comps engine
- Slack notifications

## 12. Known Data Gaps

This section documents enrichment fields that cannot reach 100% coverage and why.

| Field | Coverage | Reason for gap |
|---|---|---|
| `property_type` | ~12% | Comes from email parser only; not extractable from descriptions. Redfin alerts include it in HTML emails (parsed by OneHome parser) but not in plaintext emails. Affects ~88% of listings ingested from plaintext sources. |
| `has_basement` | ~68% | Regex-based: can only detect when description explicitly mentions "basement", "lower level", "crawl space", etc. Many descriptions simply omit this information. ~32% natural ambiguity ceiling. |
| `property_tax_json` | ~55-60% (after ORPTS expansion) | Hard floor ~37% before fix. NJ listings (Harding Township, Basking Ridge, Bernardsville area, ~9%) have no supported tax source. CT listing (Greenwich, ~2%) not covered. Some NY listings fail ORPTS address matching due to address format mismatches between listing data and parcel records. |
| `lat`, `lng` | ~85-90% (after fix) | Listings with no address or no town cannot be geocoded. 2 listings (3.5%) have no address data. |
| `year_built` | ~97% (after description parsing) | Hard floor: listings with no description and no JSON-LD metadata. 1 listing has neither. |
| `basement_type` | ~32% | Subset of `has_basement`: only populated when description is specific enough to indicate finish level. "Large basement" → `has_basement=true`, `basement_type=null`. |
| `garage_type` | ~12% | Only populated when description specifies "attached", "detached", or "carport". Generic "garage" mentions set `garage_count` but leave `garage_type` null. |
| `hoa_monthly` | ~14% | Correct behavior: most single-family homes in Westchester have no HOA. Only HOA listings (typically newer developments, condos mislabeled as SFH, or golf course communities) have this data. |

### Non-Enrichable Fields
These fields require human input or external data sources not currently integrated:
- `property_type` — requires HTML email parsing (plaintext emails lack it) or manual entry
- Floor plan data — not available in any listing source; AI must infer from description/images
- Interior condition rating — AI inference only; no structured source

## 13. Engineering Discipline

- PRD maintained as source of truth
- Automated tests (parsers, scraper, image extraction, scorer, AI validation, API endpoints, management)
- Dual DB support (SQLite local / Postgres production)
- Idempotent schema migrations
- Structured logging throughout
- Session-based auth with allowlisted users
