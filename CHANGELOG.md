# Changelog

All notable changes to Listings Analyzer are documented here.

---

## [Unreleased]

### Added
- **Address-based duplicate prevention** — `normalize_address()` in `app/enrichment.py` generates normalized address keys (Avenue→Ave, Street→St, etc.); `is_listing_duplicate_by_address()` checks DB before saving; prevents "10 Sherman Avenue" (OneHome) and "10 Sherman Ave" (plaintext) from being double-ingested
- **School data enrichment** — SchoolDigger API integration (free DEV tier, 20 calls/day); fetches nearby school rankings by zip code; caches results in DB (`school_data_json`) to minimize API calls; school percentiles displayed on dashboard cards and fed into AI scoring
- **Transit commute times** — Google Routes API integration (Essentials tier, 10K free/month); calculates Metro-North + subway + walking commute to Brookfield Place NYC (next weekday 8 AM); `commute_minutes` stored in DB and displayed as badge on dashboard cards
- **AI scorer enrichment awareness** — system prompt updated to explicitly factor school quality and commute times into evaluations; mentions specific school names/percentiles and commute duration in property_summary
- **Dashboard enrichment display** — commute badge ("52min 🚆") and school score ("Schools 85%") on compact card meta line; expandable enrichment section with full school breakdown (elementary/middle/high with names, ranks, distances) and commute details; "Commute (shortest)" and "Schools (best)" sort options
- **`POST /manage/enrich` endpoint** — backfills school data + commute times for existing listings; runs in background thread to accommodate SchoolDigger's 1-call/minute rate limit; `GET /manage/enrich/status` to check progress; `?clear_bogus=true` clears obfuscated school data before re-fetching; triggers rescore after enrichment
- **`app/enrichment.py` module** — address normalization, SchoolDigger API client (v2.0), Google Routes API client
- **Town shown on listing cards** — compact card view now displays "Address, Town" instead of just the street address
- **Version history pagination** — criteria version history shows 5 per page with Newer/Older navigation instead of full unbounded list
- 36 new tests: address normalization (19), school data (5), commute time (5), state normalization (1), manage/enrich endpoint (4), DB dedup integration (2)
- 4 new DB columns: `address_key`, `school_data_json`, `commute_minutes`, `commute_data_json`
- 4 new env vars: `SCHOOLDIGGER_APP_ID`, `SCHOOLDIGGER_APP_KEY`, `GOOGLE_MAPS_API_KEY`, `COMMUTE_DESTINATION`

- **Domain-level email source matching** — `ALERT_SENDERS` supports domain-level matching (e.g., `redfin.com` catches all Redfin senders: daily alerts, tour confirmations, favorited homes, market updates)
- **Date-filtered sender support** — `SENDER_DATE_FILTERS` env var (format: `email:days,email:days`) enables time-bounded email ingestion for senders like personal contacts; separate Gmail queries with `newer_than:Nd`
- **Inline address extraction** — PlainTextParser now handles Redfin-style inline addresses (`31 Lalli Dr, Katonah, NY 10536`) via `INLINE_ADDR_RE` regex; falls back after standalone street/city patterns
- **Listing URL extraction in PlainTextParser** — `LISTING_URL_RE` extracts Redfin, OneKeyMLS, and OneHome listing URLs from plain text emails; filters out non-listing URLs (tours, checkout, blog)
- **Management endpoints** — `POST /manage/cleanup` (delete listings by ID), `POST /manage/reset-emails` (clear orphaned processed emails + remove Gmail labels for re-ingestion)
- `GET /criteria/history` endpoint — returns all saved criteria versions, newest first (public, no auth); includes version number, created_by, created_at, 80-char preview, and full instructions
- Version history panel in the AI Criteria settings overlay; click any past version to load it into the editor; "Current" badge on the active version
- `get_criteria_history()` in `db.py`
- **OneKey MLS fallback scraper** — `_try_onekeymls()` in `onehome.py`; URL constructed from `address-town-state-zip/mls_id`; works from cloud IPs where Redfin is blocked; wired into fallback chain (OneHome → OneKey MLS → Redfin DDG; Redfin → static → Jina → OneKey MLS)
- **`/manage/scrape-descriptions` endpoint** — scrapes descriptions + images for listings with URLs but no description; iterates DB directly (no email re-parsing); protected by MANAGE_KEY; triggers rescore if descriptions found
- 29 new tests: email source config (6), URL extraction (4), inline address (4), verdict/score consistency (7), manage/scrape-descriptions (8)
- **Read-only AI Criteria for anonymous users** — "✨ AI Criteria" button always visible; settings panel opens in read-only mode (textarea disabled, save/maintenance hidden) for unauthenticated users; sign in to edit

### Changed
- **SchoolDigger API v2.0** — fixed endpoint URL from `/v2/schools` to `/v2.0/schools`; updated response parsing for `rankHistory[0].rankStatewidePercentage` (was top-level `rankStatewidePercentile`); city/zip read from nested `address` object
- **SchoolDigger rate limiting** — enforces 1-call-per-minute delay between API calls; detects obfuscated/bogus responses (daily limit exceeded) and rejects them instead of storing garbage data
- **Background enrichment** — `/manage/enrich` now runs in a daemon thread (returns immediately); two-phase bogus clearing (clears ALL bogus data first, then re-fetches) prevents zip cache from serving stale obfuscated data
- **Dynamic `update_listing_enrichment()`** — only updates columns present in the enrichment dict (was always setting all 4 columns, nulling out existing data on partial updates)
- **Email fetching refactored** — `fetch_new_emails()` now runs multiple query groups (regular senders + date-filtered senders) with deduplication via `_fetch_query()` helper; replaces single-query approach
- **ALERT_SENDERS default** updated from individual Redfin addresses to `redfin.com,alerts@mls.example.com` (domain-level matching)
- **Serialized bulk re-scoring** — `_RESCORE_WORKERS` reduced from 5 to 1; image-heavy listings (18-46 images each) exceed Anthropic's 10k tokens/minute org limit when run concurrently; serial execution avoids rate limiting
- **Verdict/score consistency enforcement** in `_validate_ai_response()` — "Reject" always forces score=0; non-Reject verdicts always re-derived from score (80+=Strong Match, 60+=Worth Touring, 40+=Low Priority, >0=Pass); prevents filter chip mismatches
- **Selector-first description extraction** — site-specific CSS selectors (`section#overview` for OneKey MLS, `div#house-info`/`.remarksContainer` for Redfin) tried before keyword-based fallback; prevents navigation/UI boilerplate from beating real descriptions
- **Browser User-Agent for static scraping** — `_scrape_static()` now uses a real Chrome UA + Accept headers to bypass basic bot detection
- **Redfin URL handling** — static HTTP attempted first, then Jina Reader (was Jina-only)
- Image selectors expanded: `img[src*="cloudfront.net"]` (OneKey MLS CDN), `img[src*="s.cbhomes.com"]` (Coldwell Banker)
- **`_MAX_IMAGES` reduced from 10 to 5** — prevents OOM on Fly.io 256MB VMs when loading images as base64; 5 images is sufficient for basement/amenity/condition assessment

---

## [2026-02-28]

### Added
- **Toured listing tracking** — `toured` boolean column on `listings` table; `POST /listings/{id}/toured` endpoint (auth-required); toured badge in compact card row; "Mark as Toured / Unmark" toggle in expanded detail (auth-only); "Toured" filter chip
- **AI-generated property summary** — `property_summary` TEXT column on `scores` table; AI scorer now generates a structured factor-by-factor analysis (headline, ✅/⚠️/❓ factor lines, conclusion); displayed as primary analysis in expanded card view; falls back to legacy checklist for older listings
- **Public read-only dashboard** — `GET /listings`, `GET /listings/{mls_id}`, `GET /criteria`, `GET /rescore/status` require no auth; write/action endpoints remain auth-gated; "Sign in" button shown to unauthenticated users

### Changed
- **Removed deterministic scoring path entirely** — `score_listing()` and all hardcoded thresholds removed from `scorer.py`; `poller.py` now returns a placeholder `ScoringResult` (score=0, confidence=low) when no API key or criteria are configured
- **Removed hardcoded `DEFAULT_CRITERIA`** — no criteria are seeded on startup; criteria must be configured via the AI Criteria panel in the dashboard on first use
- `/manage/sync-criteria` repurposed: no longer pushes hardcoded criteria; now triggers a rescore with the current active criteria from DB; returns 404 if no criteria have been set
- Soft score weights updated: `ground_floor_bedroom` +25 (was +20), `lot_gte_03_acre` +10 (was +15)
- `update_score()` in `db.py` now accepts and persists `property_summary`

### Removed
- `DEFAULT_CRITERIA` string constant from `db.py`
- `_seed_default_criteria()` function from `db.py`
- `TestHardRequirements`, `TestPreScreenMode`, `TestRealListings` test classes (tested the removed deterministic scorer)

---

## [2026-02-15]

### Added
- **Jina Reader API scraping** — replaced Playwright with `r.jina.ai` for rendering JavaScript SPAs (OneHome Angular portal); no browser dependency required
- **Redfin scraping support** — static HTTP scraper with CSS selectors tuned for Redfin page structure; URL-type-aware routing in `scrape_listing_description()`
- **Email reprocessing** — `POST /reprocess` re-fetches all processed emails, extracts listing URLs, scrapes descriptions, and triggers rescore
- `/manage/reprocess` endpoint (API key protected) for server-side reprocessing
- Vision support — image URLs attachable via `POST /listings/{id}/images`; AI evaluator sends images as base64 to Claude Haiku

### Changed
- `scrape_listing_description()` returns `(description, image_urls)` tuple (was just description)
- Keyword-based content detection ensures scraped text contains real estate terms before accepting it

---

## [2026-01-20] — Phase 2 MVP

### Added
- **AI evaluation engine** (`app/scorer.py`) — Claude Haiku scores listings against user-defined natural language criteria; returns structured JSON (score, verdict, hard results, soft points, concerns, reasoning)
- **Prompt injection defense** — listing data wrapped in `<listing_data>` XML tags; system prompt instructs AI to treat it as untrusted data only; server-side output validation (score clamped 0–100, verdict from allowlist)
- **Editable evaluation criteria** — stored in `evaluation_criteria` table with versioning; editable via dashboard settings panel; saving triggers background rescore of all listings
- **Background re-scoring** — `_rescore_all()` runs in a daemon thread; progress exposed via `GET /rescore/status`
- Google Sign-In auth — OAuth2 ID token verification; HMAC-signed session cookies (7-day expiry); allowlisted emails only
- Fly.io deployment with Postgres backend
- `/manage/sync-criteria` endpoint (API key protected) for syncing hardcoded criteria to DB

### Changed
- Dashboard upgraded with settings overlay, rescore progress bar, filter chips (verdict-based), sort controls
- Listing cards show verdict badge, evaluation method (AI vN), confidence, AI reasoning section

---

## [2025-12-01] — Phase 1 MVP

### Added
- Gmail API integration — OAuth2 refresh token; auto-polls for new listing alert emails; labels processed emails to avoid re-processing
- **OneHome HTML parser** (`app/parsers/onehome.py`) — BeautifulSoup CSS selectors for OneKey MLS NY alert format
- **Plain text parser** (`app/parsers/plaintext.py`) — regex extraction for price, beds, baths, sqft, MLS ID
- **LLM fallback parser** (`app/parsers/llm.py`) — Claude Haiku parses ambiguous/unknown email formats
- SQLite schema (`processed_emails`, `listings`, `scores`, `evaluation_criteria` tables)
- Dual DB support — SQLite locally, Postgres in production (detected via `DATABASE_URL`)
- Idempotent schema migrations via `_migrate_add_columns()`
- Mobile-first dashboard — listing cards with score badges, price/sqft/beds display, expandable detail
- `GET /listings`, `GET /listings/{mls_id}` — listing data endpoints
- `POST /poll` — manual Gmail poll trigger
- `GET /health` — health check
