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

### Non-goals
- Agent messaging
- Offer execution
- Comps / price analysis (future)

### Data Enrichment
- **School data** — SchoolDigger API (free DEV tier, 20 calls/day) fetches nearby school rankings by zip code; cached in DB to avoid rate limit; displayed on cards and fed into AI scoring
- **Transit commute times** — Google Routes API (Essentials tier, 10K free/month) calculates Metro-North + subway + walking commute to Brookfield Place NYC (next weekday 8 AM); displayed as badge on dashboard cards
- **Address-based dedup** — Normalized address keys (Avenue→Ave, Street→St, etc.) prevent duplicate listings across different email parsers

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

## 4. System Architecture

### Input Layer
- **Gmail API** auto-polls for new listing alert emails
- Supports forwarded emails (detects and unwraps)
- Configurable sender list (env var `ALERT_SENDERS`) — supports domain-level matching (e.g., `redfin.com` catches all senders from that domain)
- Date-filtered senders (env var `SENDER_DATE_FILTERS`, format: `email:days,email:days`) — for personal contacts where only recent emails are relevant
- Separate Gmail queries per sender group with deduplication
- Processes each email once, marks with `ListingsAnalyzer/Processed` label

### Known Input Format: OneHome/Matrix MLS Email
Primary alert source is OneKey MLS NY via `mlsalerts.example.com`.

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
- Uses Claude Haiku (vision-capable) via Anthropic API
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

### Prompt Injection Defense
Listing data (address, description, etc.) could contain malicious instructions.
- Listing data wrapped in `<listing_data>` XML tags, never mixed into prompt prose
- System prompt explicitly instructs AI to treat listing data as DATA ONLY
- Server-side output validation (score clamped, verdict from allowed list)
- No code execution — AI returns JSON only

### Bulk Re-scoring
- Triggered when criteria are saved, or manually via `/manage/sync-criteria`
- Runs in a background daemon thread; progress exposed via `GET /rescore/status`
- **Chunked batch processing** — listings processed in chunks of 10 (`_BATCH_CHUNK_SIZE`) to keep peak memory under control; each listing can include ~6.6MB of base64-encoded images; building all requests at once caused OOM on 512MB Fly.io machines
- **Batch API** — each chunk submitted as a Message Batch (50% token discount); poll every 30s until complete; results processed and memory freed before next chunk
- Sequential fallback preserved if batch API fails on any chunk

### Evaluation Criteria (Editable)
- Stored in `evaluation_criteria` table with versioning (each save = new version row)
- Editable via dashboard settings panel ("✨ AI Criteria") — **only authenticated users** can edit
- Saving new criteria triggers background re-score of all listings
- **No hardcoded default criteria** — criteria must be entered and saved via the dashboard on first use
- Full version history available via `GET /criteria/history`; viewable in the settings panel with one-click restore

### Buyer Criteria (Current — as configured in dashboard)

**Hard Requirements** (any failure = Reject, score 0):
- >= 2,400 sqft (no upper bound — but 4,500+ sqft is a slight concern)
- 4+ bedrooms (no upper bound — but 6+ is a slight concern: unusual layout)
- Price: $1.25M–$2M (target range $1.25M–$1.5M; above $1.5M is a concern, not a reject)
- Must be detached (no townhouse, condo, co-op)
- Must have a finished basement

**Soft Features** (base score 20 + points):

| Feature | Points |
|---|---|
| Base (all hard reqs pass) | +20 |
| Ground-floor bedroom | +25 (highest priority) |
| Finished basement | +20 |
| Lot >= 0.3 acre | +10 |
| Pool | +10 |
| Sauna | +5 |
| Jacuzzi/hot tub | +5 |
| Soaking tub | +5 |

**Soft Warnings** (lower score slightly, never cause Reject):
- Price above $1.5M → -5 to -10 pts
- 4,500+ sqft → -5 to -10 pts
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
- **Auth-required (Google sign-in):** Check Email, edit AI Criteria, Reprocess, Mark as Toured, Scrape & Score
- Unauthenticated users see a "Sign in" button in the header; action buttons are hidden
- AI Criteria panel: always accessible (read-only for anonymous, editable for authed); "Save & Re-score All" and Maintenance section hidden when not logged in

### Header
- App title
- "✨ AI Criteria" button → settings overlay *(always visible — read-only when not logged in)*
- "Check Email" button → triggers `POST /poll` *(auth-only)*
- "Sign out" button *(auth-only)* / "Sign in" button *(unauthenticated)*

### Listing Cards (Compact)
- Color-coded score badge (green/yellow/orange/red)
- Address (clickable link to listing URL when available)
- Price, sqft, beds/baths, $/sqft
- Verdict badge + evaluation method (AI vN)
- Listing status tag (New Listing, Price Increased, etc.)
- Toured badge (if marked as toured)

### Listing Cards (Expanded)
- AI-generated **property summary** (structured factor analysis with ✅/⚠️/❓ indicators)
- Falls back to hard results checklist + concerns + AI reasoning for older listings without property_summary
- "Mark as Toured" / "Unmark as Toured" toggle *(auth-only)*

### Filters & Sorting
- Filter chips: All, Non-Reject, Strong Match, Worth Touring, Reject, **Toured**
- Sort: Score (high→low), Price (low→high), $/sqft (low→high)
- Filter counts shown on chips

### Settings Panel (overlay)
- Textarea with current AI evaluation instructions *(always visible; read-only for anonymous, editable for authed)*
- Version indicator (version number + created_by)
- "Save & Re-score All" button
- Re-score progress bar (polls `GET /rescore/status` every 2s)
- **Version history** — lists all past versions with date and preview; click any row to load into textarea; "Current" badge on active version; one-click restore before saving
- Maintenance section: "Reprocess Emails" button

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
- created_at

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

## 9. Key Technical Decisions

### Stack
- **Language:** Python 3.12+
- **Framework:** FastAPI
- **DB:** SQLite (local) / Postgres (production, detected via `DATABASE_URL`)
- **Deployment:** Fly.io (Docker)
- **Dependencies:** managed with `uv`
- **Scraping:** httpx (static) + Jina Reader API (SPA rendering, no browser needed)

### AI Evaluation
- Claude Haiku (`claude-haiku-4-5-20251001`) — cheap, fast, supports vision
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
- **Directional address suffixes** — Street addresses may end with a cardinal direction (N, S, E, W, NE, NW, SE, SW) after the street type suffix (e.g., "101 Long Hill Rd E"). Both `STREET_RE` and `INLINE_ADDR_RE` patterns handle optional directional suffixes.
- **Address-less listings** — Listings without an extractable address fall back to Redfin URL parsing, then zip-code-only. Address-less listings can be cleaned up via `/manage/data-quality?fix=true` (or manually via `/manage/cleanup`) and re-ingested via reset + poll.
- **DB connection timeouts** — `psycopg2` connections use `connect_timeout=5` (5s TCP connect) and `statement_timeout=30000` (30s query); SQLite uses `timeout=5` (5s lock wait). Prevents indefinite hangs when Postgres is temporarily unreachable.

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
- AI-generated property summary (structured factor analysis per listing)
- Removed deterministic scoring path — AI-only evaluation
- No hardcoded default criteria — user configures via dashboard

### Phase 3 (Future)
- Comps engine
- Price per sqft analysis
- Metro-North proximity scoring
- Tax estimation
- Slack notifications

## 12. Engineering Discipline

- PRD maintained as source of truth
- Automated tests (parsers, scraper, image extraction, scorer, AI validation, API endpoints, management)
- Dual DB support (SQLite local / Postgres production)
- Idempotent schema migrations
- Structured logging throughout
- Session-based auth with allowlisted users
