# Changelog

All notable changes to Listings Analyzer are documented here.

---

## [Unreleased]

### Added
- **"Liked" status** — new listing status for marking properties to revisit or share. Independent from "Passed" and "Toured". `POST /listings/{id}/liked` endpoint (auth-only) toggles the status. Dashboard shows "Like" / "♥ Liked — click to remove" button in expanded detail, "♥ Liked" badge on card summary, and "Liked" filter chip with count. Status persists across page reloads in database.

### Changed
- **Ground-floor bedroom downgraded to nice-to-have** — No longer a hard criterion or near-disqualifying deduction. Now a +5 to +10 bonus if present, no penalty if absent. Stair lift is a viable alternative. AI system prompt updated to reflect this. PRD scoring table updated.
- **Active filter hardened** — "Active" view now always excludes passed, toured, and pending/sold/closed listings regardless of display preferences. It is strictly "listings still on the roster to check out." The display preference toggles (hide passed, hide toured, hide pending) now apply only to the "All" view. PRD updated.
- **Criteria v60: NJ hard reject + reduced sqft penalty** — NJ listings are now an explicit hard pass (Reject immediately). Borderline sqft penalty reduced from `-12 to -15` → `-5 to -7` for 2,200–2,400 sqft range (no longer "severe"). PRD updated to match.

### Added
- **NYS GIS parcel lot_acres lookup** — `fetch_lot_acres_parcel()` in `enrichment.py` queries the free NYS GIS Tax Parcels public ArcGIS REST service to backfill `lot_acres` for listings where the description contains no lot size data. Covers all Westchester (and other NY) county parcels; NJ/CT addresses are skipped. Address matching uses a 3-word prefix comparison against PARCEL_ADDR (number + first 2 street words) to disambiguate similar streets (e.g. "8 Old Roaring Brook Rd" vs "8 Old Farm Ln"). Falls back to MUNI_NAME/CITYTOWN_NAME town matching when address words are ambiguous. Rate-limited at 1.2s between calls. Wired into the `_enrich_all()` backfill pipeline for automatic coverage on new listings.
- **`POST /manage/enrich-lot-acres` endpoint** — one-shot backfill that runs the parcel lookup for all listings with `lot_acres=NULL`. Returns `{total, found, not_found, skipped_already_set}`. Authenticated via `x-manage-key`.
- **15 tests for `fetch_lot_acres_parcel`** — `TestFetchLotAcresParcel` in `test_enrichment.py`: import, missing address/town, NJ/CT state skip, NJ town skip, single-result ACRES, CALC_ACRES fallback, multi-result 3-word addr match, multi-result MUNI_NAME match, no results, network error, cache hit, town map spot-check, rounding to 4dp. 522 total tests passing.

### Added (previous)
- **`/manage/scrape-redfin` endpoint** — scrapes all Redfin listing URLs via Jina reader to backfill missing fields: `property_type`, `lot_acres`, `property_tax_json` (annual amount), `garage_count`, `garage_type`, `hoa_monthly`, `list_date`, and `price` (force-corrected when current value < $100k, indicating a data error). Runs synchronously; returns per-listing results.
- **`property_type`, `lot_acres`, `list_date` in `manage/update-listing`** — these fields now accepted by the manage update endpoint (previously only numeric/address fields were allowed).
- **`force` param in `manage/update-listing`** — pass `"force": true` in the JSON body to overwrite existing field values (default behavior still only fills null/empty fields).
- **`force` param in `db.update_listing_fields_by_id()`** — internal DB helper now accepts `force=True` to unconditionally overwrite columns.
- **`list_date` via "X days on Redfin"** — `scrape-redfin` now falls back to calculating list date from "X days on Redfin" text in Jina output when no explicit date label is found.

### Added (previous)
- **Split Display Settings / AI Criteria panels** — ⚙ gear now opens Display Settings only (auto-saves on toggle, no Save button). ✨ AI Criteria button opens a separate overlay for the criteria editor, version history, rescore progress, and maintenance. Prevents accidental criteria saves when changing display prefs.
- **Gear button uniform height** — settings button now matches height of AI Criteria and Check Email buttons (38px fixed height, 22px icon).
- **AI failure retry + `ai_failed` flag** — `ai_score_listing()` now retries once on `JSONDecodeError` before giving up. On persistent failure uses `evaluation_method='ai_failed'` instead of `'deterministic'`; `_should_skip()` always forces rescore for `ai_failed` listings. Dashboard shows a ⚠ Score pending badge on affected cards; data confidence tooltip includes count of pending listings.
- **`ai_failed` card visual treatment** — cards with `evaluation_method='ai_failed'` show `?` in faint gray (not `0`) for score, amber left border + reduced opacity, and a yellow "↻ Retry scoring" button in the expanded action row. Clicking it calls `POST /listings/{id}/rescore`, shows a toast, and refreshes the list.
- **Improved junk image filter** — `system_files`, `150x150`, `120x120`, `mapHomeCard`, `genMap`, `genBcs` URL patterns added to the filter list; prevents Redfin map tiles, nearby-sale thumbnails, and micro-thumbnails from being sent to the AI (was causing JSON parse failures on some listings).
- **Scorer retry robustness** — `APIError` and unexpected exceptions on the *retry* attempt are now also caught and produce `ai_failed` instead of crashing the caller.
- **Tests: `ai_score_listing()` error paths** — `TestAiScoreListingErrorPaths` (5 tests): no API key → deterministic, JSON error retries twice, API error → ai_failed, JSON error then API error on retry → ai_failed (not crash), unexpected exception → ai_failed.
- **Tests: junk image filter** — `TestJunkImageFilter` (3 tests): `system_files` URLs excluded, `genBcs` URLs excluded, badge/flag URLs excluded.
- **Tests: `_should_skip` ai_failed + scored_at=None** — 2 new cases in `TestSkipUnchanged`: `ai_failed` always rescores, `scored_at=None` always rescores.
- **Sqft hard requirement lowered to 2,200 sqft** — criteria v55. Hard reject threshold changed from 2,400 → 2,200 sqft. 2,200–2,400 sqft range carries a severe soft penalty (-12 to -15) to avoid missed borderline listings while still strongly discouraging small homes.
- **CI test gate** — GitHub Actions workflow updated to run `pytest tests/ -x -q` before deploying. Deploy job requires test job to pass. Uses `uv sync --dev` + mock env vars for API keys.
- **Hide pending/sold toggle** — third display preference in settings: "Hide pending/sold listings" (default OFF) excludes listings where `listing_status` is Pending, Sold, Under Contract, or Closed from All view. Filter counts respect this preference.
- **"Active" filter chip** — renamed from "Non-Reject" to "Active" (clearer label; same behavior: excludes Reject verdict listings).
- **Settings modal with display preferences** — gear icon (⚙) in top nav (auth-only) opens a redesigned settings modal with a "Display Preferences" section above the AI Criteria editor. Preferences are persisted to `localStorage` and apply immediately: "Hide passed listings" toggle (default ON), "Default filter on load" dropdown (All / Non-Reject), "Default sort" dropdown (all existing sort options). Filter counts respect the hide-passed preference.
- **Hide passed listings from default views** — passed listings are now excluded from All, Non-Reject, Toured, and Want to Go views by default. They remain visible via the dedicated "Passed" filter chip. Controlled by the "Hide passed" display preference (default ON).
- **Tests for `parse_list_date()`** — `TestParseListDate` (11 tests): None input, empty string, no-date description, ISO date after "listed on", ISO date after "date listed", US date after "listed", US date after "on market since", long-form month, abbreviated month, update-date non-match, return format validation.
- **Tests for `fetch_property_tax_orpts()` county fallback** — `TestFetchPropertyTaxOrpts` (8 tests): import, missing address, missing town, municipality map presence, successful pass-1 lookup, county fallback on empty pass-1, both-passes-empty returns None, network error, cache hit, address-without-number returns None.
- **Confidence tooltip with per-field breakdown** — mouseover on the `XX% data` nav pill now shows a floating panel with each field's coverage % and a color-coded mini bar chart (green ≥90%, amber ≥70%, red <70%). Built as a custom div tooltip rather than a plain `title` attribute.
- **`parse_list_date()` description parser** — extracts on-market list date from stored description text. Handles ISO (`2026-01-15`), US (`01/15/2026`), and long-form (`January 15, 2026`) date formats after "listed on", "date listed", "on market since" cues. Wired into `_enrich_all()` to backfill `list_date` for listings missing DOM data. `list_date` added to `allowed_cols` whitelist in `db.py`.
- **ORPTS county-level fallback** — `fetch_property_tax_orpts()` now does a two-pass lookup: (1) municipality-scoped (existing), then (2) county-level (`county_name='Westchester'`) if the first pass returns no results. Fixes mismatches where village/hamlet parcels are filed under a different ORPTS municipality than the mailing address (e.g. Scarsdale addresses → Greenburgh; Bedford Rd addresses → North Castle). Expected to recover ~6-8 additional listings.
- **Data confidence indicator in top nav** — weighted composite score (0–100%) shown as a pill in the dark header bar. Covers 9 fields weighted by scoring impact: description 20%, images 15%, schools 15%, commute 15%, year built 10%, lot size 8%, basement 7%, property tax 5%, garage 5%. Color-coded green/amber/red (≥80%/60–79%/<60%). Tooltip shows field list on hover.
- **Property Details section in expanded card** — new `buildPropertyDetails()` JS function renders enriched data not previously shown in the UI: lot size (acres or sqft), garage count/type, HOA fee (or "None"), pool, basement type, assessed value (ORPTS/SODA), FEMA flood zone (with ⚠️ if SFHA), power line proximity, and nearest Metro-North station + walk time. Displayed as a compact 2-column grid below the commute/schools section.

### Removed
- **Listing description from expanded card** — marketing text removed from detail view; full listing is accessible via the "View full listing →" link.

### Added (previous)
- **`parse_year_built()` description parser** — extracts year built from Redfin-style descriptions. Handles `"YYYY year built"` (Redfin metadata format) and `"built in YYYY"` patterns. Filters out listing-update dates (2026+). Wired into `_enrich_all()` for automatic backfill on listings missing `year_built`. Expected to fill ~10 of 12 previously missing values (79% → ~97% coverage). `year_built` added to `allowed_cols` whitelist in `db.py`.
- **ORPTS municipality map expanded** — added 14 new hamlet→municipality mappings: Irvington, Dobbs Ferry, Hartsdale, Ardsley, Elmsford (Greenburgh); Thornwood (Mount Pleasant); Rye Brook, Port Chester (Rye); Cortlandt Manor (Cortlandt); Beacon (Dutchess); Highland/Lloyd (Ulster); Palisades/Orangetown (Rockland). Expected to improve property tax coverage from 37% → ~55-60% for NY listings.
- **Direct geocoder call for lat/lng** — `_enrich_all()` now calls `_geocode_address()` directly instead of relying on stale in-memory cache. Fixes 0% lat/lng coverage on previously-geocoded listings.
- **HOA re-parse on every enrichment cycle** — removes `is None` guard so stale/wrong HOA values (e.g., property-tax amounts misclassified as HOA fees) get corrected on next enrichment run.
- **9 new tests for `parse_year_built`** — `TestParseYearBuilt` in `test_enrichment.py`: Redfin metadata format, `year built: YYYY`, `built in YYYY`, `constructed in YYYY`, listing-date filter, none input, no-year description, renovation-year disambiguation. 144 total enrichment tests passing.

### Fixed
- **HOA false positive from property tax amounts** — removed greedy `$X,XXX hoa/dues` regex pattern that matched dollar amounts preceding "hoa dues $0" (e.g., "property taxes $1,992 hoa dues $0" → wrongly stored $1,992 as HOA). Four listings affected. Fixed no-HOA regex to also match `hoa dues $0` pattern. Two regression tests added.
- **lat/lng 0% coverage** — previous fix relied on in-memory geocode cache which is empty on each container start. Now calls geocoder directly; cache hit is instant if the address was already geocoded in the same enrichment run.

### Added
- **Description parsing: garage, HOA, pool, basement** — Four new regex-based parsers in `enrichment.py`: `parse_garage_count()` (count + attached/detached/carport), `parse_hoa_amount()` (monthly/annual fees), `parse_pool_flag()` (inground/above-ground/community), `parse_basement()` (finished/partially finished/unfinished/walk-out). All pure code, no API calls. 7 new DB columns: `garage_count`, `garage_type`, `hoa_monthly`, `has_pool`, `pool_type`, `has_basement`, `basement_type`. Wired into `_enrich_all()` for automatic backfill and `_build_listing_data()` for AI scoring.
- **Criteria v52** — structured data references for garage, pool, basement, HOA. Expanded amenities section with per-signal scoring. Basement: +5 walk-out/finished, +2 partially finished, 0 unfinished, -3 slab/no basement. Pool: -5 inground, -3 above-ground, 0 no pool/community.
- **31 new tests** — `TestParseGarageCount` (8), `TestParseHoaAmount` (7), `TestParsePoolFlag` (7), `TestParseBasement` (9). 133 total enrichment tests passing.
- **Metro-North station proximity** — `fetch_station_proximity()` in `enrichment.py` finds nearest Metro-North station using a static 61-station dataset (Harlem, Hudson, New Haven lines). No API call at runtime. Returns station name, distance_m, walk_minutes (at 5km/h). `station_json TEXT` column added. Passed to AI scorer as `nearest_metro_north`.
- **Criteria v50** — flood zone penalty adjusted: Zone AE/VE -20 (was -15), Zone X 500-year 0 (was -5).
- **Criteria v51** — Metro-North station walking distance scoring: +5 under 10 min walk, 0 at 10–20 min, -5 over 20 min.
- **6 new tests for station proximity** — `TestFetchStationProximity` in `test_enrichment.py`.
- **FEMA flood zone detection** — `fetch_flood_zone()` in `enrichment.py` queries FEMA NFHL ArcGIS REST API (free, no key) by lat/lon. Returns `fld_zone`, `zone_subty`, and `sfha` (bool). `flood_zone_json TEXT` column added to `listings` table. Passed to AI scorer as `flood_zone` structured field.
- **Criteria v47** — calibration rebalance from buyer preference study: base 25→30, school average 0 (was +5), GFBath +8 (was +15), 4BR +8 (was +10), basement +5 (was +10), commute >110 -15 (was -10).
- **Power line proximity detection** — `fetch_power_line_proximity()` queries OSM Overpass API (free) for high-voltage transmission infrastructure (power=line/cable/tower) within 300m radius. Returns nearest distance, type, voltage. `lat REAL`, `lng REAL`, `power_line_json TEXT` columns added.
- **Criteria v48** — power line scoring: -5 at 150–299m, -10 at 75–149m, -15 under 75m.
- **Criteria v49** — flood zone scoring: -15 Zone AE/VE (SFHA), -10 Zone A, -5 Zone X 500-year, 0 Zone X minimal.
- **10 new tests for power line proximity** — `TestHaversineM`, `TestGeocodeAddress`, `TestFetchPowerLineProximity` in `test_enrichment.py`.
- **7 new tests for flood zone** — `TestFetchFloodZone` in `test_enrichment.py`: zone X, zone AE (SFHA), empty features, cache, network error, SFHA zone coverage.
- **NY ORPTS property tax** — `fetch_property_tax_orpts()` in `enrichment.py` queries NY State ORPTS API (`data.ny.gov/resource/7vem-aaz7.json`) for assessed/taxable values for Westchester and other NY municipalities outside NYC. Integrated as fallback after NYC SODA. Municipality names mapped via `_ORPTS_MUNICIPALITY_MAP` (40+ hamlet→town mappings). Streets queried as UPPERCASE with suffix in same field.
- **Lot size as structured field** — `lot_acres REAL` column added to `listings` table; extracted from listing pages via JSON-LD `lotSize` (object and string forms), text "X.XX acres", and "N,NNN sq ft lot"; valid range 0.01–1000 acres; backfilled from stored description text in Phase 4 of `/manage/scrape-descriptions`.
- **`clear_bogus_commute` flag** — `POST /manage/enrich?clear_bogus_commute=true` nulls commute data for listings where `commute_mode == "transit"` (pure transit = walk/bus, unreliable for suburban areas). Forces re-enrichment with drive+transit mode.
- **Criteria v43** — garage scoring (+5 two-car, +2 one-car, 0 no garage); pool penalty reduced -15→-5; HOA folded into carrying cost (no direct penalty, flag if >$500/mo); lot/drainage scoring (+3 to +5 elevated/hillside, -3 low-lying).
- **Criteria v44** — lot_acres context added to instructions for AI scoring.
- **Criteria v45–v46** — sqft penalty restructured: 4,000–4,500 sqft → -5 pts, 4,500+ → -12 pts (starts at 4,000 per buyer preference, not 3,800).
- **7 new tests for ORPTS** — `TestFetchPropertyTaxOrpts` in `test_enrichment.py`: municipality map contents, structured data return, not-found case, exception handling, missing inputs, cache hit, NYC SODA fallthrough.
- **7 new tests for lot size extraction** — `TestLotAcresExtraction` in `test_onehome_parser.py`: JSON-LD object, JSON-LD string (acres + sqft), visible text (acres + sqft), unrealistic value rejection, priority order.

### Changed
- **Phase 4 backfill in `/manage/scrape-descriptions`** — now also backfills `lot_acres` from stored description text for listings missing the field (in addition to year_built/list_date).
- **`_NUMERIC_COLUMNS`** added in `db.py` — ensures `lot_acres` (REAL) uses `IS NULL` checks instead of `= ''` in `update_listing_fields_by_id`.

### Fixed
- **API tests broken by lot_acres Phase 4** — mock listings in `test_api.py` updated to include `lot_acres: 0.5`, preventing Phase 4 from hitting unmocked DB calls.
- **`test_reprocess_finds_urls`** — added mock for `db.backfill_listing_address` (was calling unmocked DB function).

### Added (previous session)
- **GFB 4-signal inference** — scorer system prompt expanded with explicit 4-signal framework for ground-floor bedroom determination: (1) floor plan image labels (Den/Study/Guest Room on ground floor), (2) description text patterns ("first floor bedroom", "in-law suite", "master on main"), (3) photo examination (beds visible at ground level), (4) property type + age inference (ranch = always GFB, Colonial/Tudor = rarely). Opus commits at 60%+ confidence; only returns "unknown" if all four signals are absent/contradictory.
- **Criteria v42** — tiered school scoring (+25 strong/95th+ percentile, +15 good/80–94th, +5 average/below 80th; previously flat +35); steeper price taper (-5 at $1.5–1.65M, -10 at $1.65–1.8M, -15 above $1.8M; previously -5 to -10 flat); price/sqft signal doubled (±10, was ±5); old "Condition" section collapsed into "Age & Physical Condition" — eliminates double-counting.
- **`/manage/update-listing` endpoint** — update individual listing fields (year_built, price, sqft, bedrooms, bathrooms, address, town, state, zip_code) by listing ID without re-scraping. Protected by MANAGE_KEY.
- **year_built backfill** — scraped Redfin via Jina Reader for 3 listings missing the field: #62 (31 Lalli Dr, Katonah) → 1994, #483 (175 Palmer Ln, Thornwood) → 1948.

### Added
- **Age/condition scoring** — `score_age_condition()` in `enrichment.py` computes age tier adjustment (pre-1940 → -22 pts up to 2005+ → 0) and keyword scan of listing description (e.g. "new roof" +6, "sold as is" -12, "fixer-upper" -12). Result passed to AI scorer as `age_condition` signal. No external API required.
- **Price/sqft market benchmark** — `get_price_per_sqft_signal()` loads Zillow ZHVI CSV at startup (~5MB), computes listing $/sqft vs ZIP-level median benchmark, returns `below_market`/`at_market`/`above_market` signal. Passed to AI as `price_per_sqft_signal`.
- **Property tax enrichment** — `fetch_property_tax()` queries NY Open Data SODA API (free, no key) for NYC assessed/market values. Stored as `property_tax_json` column. Passed to AI as `property_tax`.
- **Scoring criteria v41** — updated instructions to use all 3 new structured signals.
- **DB migration** — `property_tax_json TEXT` column added to listings table.
- **Image audit endpoint** — `GET /manage/image-audit` reports image coverage stats: total listings, listings with/without images, listings with unknowns in scoring, sample of high-priority targets needing images; protected by MANAGE_KEY
- **Force rescrape unknowns** — `POST /manage/rescrape-unknowns` targets listings with "Unknown" hard requirements and insufficient images (<3); re-scrapes listing URLs for images, updates DB, re-scores with images; protected by MANAGE_KEY

### Changed
- **Enhanced AI prompt for unknowns** — scorer system prompt now explicitly instructs AI to penalize unknowns heavily: each unknown hard requirement reduces score by 10-15 points, unknown basement by 15-20 points; 3+ unknowns should result in Weak Match or Low Priority (30-50 range), not 60+; floor plans are critical for determining basement finish, ground-floor bedrooms, detached status

### Added
- **"Passed" status** — `passed` boolean column on listings; `POST /listings/{id}/passed` toggle endpoint (auth-required); orange "Passed" badge in compact card row; "Pass" / "Passed — click to undo" button in detail actions; "Passed" filter chip with count; `/passed` URL route
- **Redfin CDN image enumeration** — `enumerate_redfin_images()` in `onehome.py` probes sequential Redfin CDN photo indices (HEAD requests, 0–80) to discover all listing photos; static HTML scraping captures ~7 but listings often have 30–50+; called automatically from `_extract_image_urls()` when Redfin CDN URLs detected
- **Smart image selection for scoring** — `_select_scoring_images()` in `scorer.py` picks a representative blend of 8 images: 3 from start (hero/kitchen), 2 from middle, 3 from end (floor plans/basement); ensures floor plans (typically last images) are always seen by the AI scorer
- **Phase 2.5 in `/manage/scrape-descriptions`** — re-enumerates Redfin CDN images for existing listings with fewer than 10 images; fills in the full photo set without re-scraping

### Changed
- **Removed verdict tags from compact cards** — "Strong Match", "Worth Touring", etc. pills removed from compact card row (score number + color already conveys the tier); removed `Strong Match`, `Worth Touring`, `Reject` filter chips and their `/strong-match`, `/worth-touring`, `/reject` URL routes; kept `Non-Reject` filter and verdict CSS for detail view
- **`_MAX_IMAGES` bumped from 5 → 8** — peak memory ~52MB per 5-listing chunk, safe on 1024MB Fly.io
- **Scorer image hint updated** — now mentions floor plans, office/den, and room layout in the image examination prompt

### Added
- **"Want to Go" flag** — `tour_requested` boolean column on listings; `POST /listings/{id}/tour-request` toggle endpoint (auth-required); blue "Want to Go" badge in compact card row; "Want to Go" / "✓ Want to Go — click to cancel" button in detail actions; "Want to Go" filter chip with count; mirrors the existing Toured pattern
- **Add listing from URL** — `POST /listings/add` endpoint creates a new listing from a pasted URL; resolves short URLs (redf.in), extracts address from Redfin URL path, scrapes description/images, extracts structured data (price/beds/baths/sqft), enriches (schools/commute), scores with AI; dashboard shows URL input bar when signed in; duplicate detection by address key
- **Filter chip routes** — each filter has its own URL: `/non-reject`, `/strong-match`, `/worth-touring`, `/reject`, `/toured`, `/want-to-go`; shareable links that pre-filter the dashboard; browser back/forward supported via `pushState`
- **Public repo hardening** — removed hardcoded personal emails from config defaults and `.env.example`; all personal config now env-var-only

### Changed
- **Auto-update listing status on duplicate detection** — when poller encounters a duplicate listing (by MLS ID or address key), it now updates the existing listing's `listing_status` if changed (e.g., "New Listing" → "Pending") and backfills `listing_url` if the existing record has none; previously duplicates were silently skipped
- **Improved sold/pending detection via OneKey MLS** — `_prune_sold_listings()` now has two passes: (1) Redfin URLs via Jina Reader (existing), (2) OneKey MLS status check via DDG search for all remaining listings; OneKey MLS pages include structured `SaleStatus`/`MlsStatus` JSON fields accessible from cloud IPs (unlike Redfin which blocks Jina from Fly.io)
- **Pending listings preserved** — listings detected as "Pending" or "Under Contract" now get `listing_status` updated to "Pending" instead of being deleted; sold indicators and pending indicators are now separate lists

### Added
- **`check_listing_status()` in `onehome.py`** — searches DDG for OneKey MLS page and extracts `SaleStatus`/`MlsStatus` from page JSON; returns status string ("Active", "Sold", "Pending", "Closed") or None; reuses existing DDG rate limiting
- **`_extract_listing_status()` in `onehome.py`** — regex extraction of listing status from OneKey MLS HTML embedded JSON
- **OneKey MLS address search fallback** — when Redfin scraping fails and no MLS ID is available (bare Redfin URLs), DDG searches for the address on OneKey MLS; wired into the Redfin scraping chain after existing fallbacks
- **Structured data extraction from scraped pages** — `_extract_property_stats()` regex-extracts price/beds/baths/sqft from any listing page HTML; `scrape_listing_structured_data()` combines DDG search + fetch + extraction
- **Structured data backfill in poller** — before scoring, bare-URL listings missing price/beds/baths/sqft get data backfilled from OneKey MLS; prevents "missing data" rejections
- **Phase 3 in `/manage/scrape-descriptions`** — after URL search and description scraping, backfills structured property data for listings missing all of price/beds/baths/sqft; triggers rescore if data found
- **HTML URL backfill in PlainTextParser** — `parse()` now extracts Redfin listing URLs from the HTML portion of emails and matches them to text-parsed listings by address; fills in `listing_url` for listings that had no URL in the plain text body
- **DB connection timeouts** — `psycopg2.connect()` now uses `connect_timeout=5` and `statement_timeout=30000` (30s); `sqlite3.connect()` uses `timeout=5`; prevents indefinite hangs when Postgres is unreachable
- **Chunked batch rescore** — `_rescore_all()` now processes listings in chunks of 5 (`_BATCH_CHUNK_SIZE`) instead of building all batch requests in memory at once; peak memory drops from ~193MB to ~33MB; Batch API 50% discount preserved
- **`POST /manage/data-quality` endpoint** — audits listings with missing address, URL, or town (dry-run by default); with `?fix=true`: deletes no-address listings, backfills missing towns from Redfin URLs, resets orphaned emails, triggers re-poll + rescore; protected by MANAGE_KEY
- **Listing validation gate** — `poll_once()` now rejects listings with no address AND no MLS ID before saving; prevents garbage rows from bypassing both dedup checks
- **Town backfill from Redfin URLs** — data-quality fix mode extracts town/state/zip from Redfin URL paths for listings missing town data
- **Tour header stripping** — plaintext parser now strips Redfin tour headers ("N homes on this tour") before address parsing; prevents tour count from being concatenated into listing address
- **Address key backfill on startup** — `init_db()` now backfills `address_key` for any listing with address+town but NULL key; prevents Ave/Avenue-style duplicates from bypassing dedup
- **Expanded address normalization** — added parkway/pkwy, highway/hwy, trail/trl, crossing/xing, turnpike/tpke, expressway/expy suffix mappings; added directional word normalization (north→n, south→s, east→e, west→w, northeast→ne, etc.)
- **Global email age filter** — `MAX_EMAIL_AGE_DAYS` env var (default: 21); Gmail queries now include `newer_than:{days}d` to skip stale emails; prevents ingesting sold/expired listings from months-old alerts
- **`POST /manage/prune-sold` endpoint** — checks Redfin listing URLs via Jina Reader for sold/off-market status; dry-run by default, `?fix=true` deletes sold listings; protected by MANAGE_KEY
- **Bare Redfin URL parsing** — plaintext parser now handles emails with only Redfin URLs (no price/beds data); extracts address/town/state/zip from URL path; enables ingesting listings forwarded by personal contacts
- **`POST /listings/{id}/sold` endpoint** — deletes a listing and its score; auth-required; called from new "Mark as Sold" button on dashboard
- **"Mark as Sold" button** — red-themed button at top of expanded card detail; confirms before deleting; removes card from UI immediately
- **Action buttons row** — toured and sold buttons now displayed together at top of card detail section (were at bottom)

### Changed
- **Removed image management and add-URL sections** from expanded card detail; these are machine tasks, not manual ones
- **Commute: pick shortest of two strategies** — `fetch_commute_time()` now tries both direct transit (walk to station) and drive-to-station + transit, returning whichever is shorter; previously only tried drive+transit when walk-to-transit returned no routes, causing inflated times (e.g. 152 min for Briarcliff Manor when drive+transit would be ~70 min)
- **Station overrides** — added Briarcliff Manor → Scarborough, Ossining, Pleasantville to `_STATION_OVERRIDES`
- **"Pass" verdict renamed to "Weak Match"** — clearer label for AI-scored listings with score 1-39; updated in scorer, poller, dashboard CSS/filter chips, tests, and docs
- **"Pass+" filter chip renamed to "Non-Reject"** — shows all non-rejected listings regardless of score tier
- **State normalization in address keys** — `normalize_address()` now converts full state names to 2-letter codes ("New York"→"NY", "New Jersey"→"NJ", etc.); prevents duplicates when same listing arrives with "New York" from one source and "NY" from another
- **Address key recomputation on startup** — `_backfill_address_keys()` now recomputes keys for ALL listings (not just NULL), so normalization improvements apply retroactively
- **Startup dedup pass** — `_dedup_by_address_key()` runs after key recomputation; merges duplicates by keeping the listing with toured status, MLS ID, and listing URL (in priority order)
- **URL backfill via DuckDuckGo** — `/manage/scrape-descriptions` now searches DuckDuckGo for Redfin URLs for listings that have address+town but no URL; found URLs are saved before description scraping begins
- **Data-quality fix mode** — no longer deletes no-URL listings (only no-address); no-URL listings are real but just lack a clickable link
- `_rescore_all()` refactored: first pass collects IDs needing rescore (lightweight, no images loaded), then processes in chunks — build → submit → poll → process → free memory per chunk

---

## [2026-03-03]

### Added
- **Anthropic prompt caching** — system prompt now returns `cache_control: {"type": "ephemeral"}` block; cached across all scoring calls for ~90% savings on system prompt tokens
- **Batch API for rescoring** — `_rescore_all()` now uses Anthropic Message Batches API (50% discount on all tokens); polls batch status every 30s; falls back to sequential scoring if batch submission fails
- **Skip-unchanged listings** — `_should_skip()` checks `criteria_version` + `enriched_at`/`scored_at` timestamps; listings with same criteria and no new enrichment are skipped during rescore (instant completion on re-rescore)
- **Score metadata tracking** — `enriched_at` (listings) and `scored_at` (scores) columns auto-set on update; `get_all_score_metadata()` fetches all score metadata in one query for skip logic
- `build_batch_request()` and `parse_batch_result()` helpers in `app/scorer.py`
- `rescore_state` now includes `skipped` count and `batch_id` for monitoring
- 12 new tests: batch request construction (3), batch result parsing (4), skip-unchanged logic (5)
- **Commute DRIVE fallback** — `fetch_commute_time()` now falls back to Google Routes DRIVE mode when TRANSIT returns no routes (common for suburban addresses far from train stations); `commute_mode` ("transit"|"drive") tracked in `commute_data_json`
- **Parallel commute enrichment** — `/manage/enrich` runs commute API calls in parallel (5 workers via ThreadPoolExecutor); school data calls remain serial (SchoolDigger rate limit)
- **Commute mode display** — dashboard enrichment card shows 🚆 Transit or 🚗 Driving label based on which mode was used
- 2 new tests: DRIVE fallback (1), both-modes-fail (1)

### Changed
- **Compact card layout** — cards show address on line 1, town+state on line 2; removed price/beds/sqft/badge meta row from compact view
- **Card height fix** — `.card-address` and `.card-meta` now use `-webkit-line-clamp: 2` for multi-line wrapping instead of single-line truncation; cards display ~3 visible rows
- Extracted `_build_listing_data()` and `_get_image_urls()` helpers from `_rescore_one_listing()` for reuse by both single and batch scoring paths
- Removed `ThreadPoolExecutor` from rescoring (replaced by Batch API)

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
- **ALERT_SENDERS default** updated from individual Redfin addresses to domain-level matching (e.g. `redfin.com`)
- **Serialized bulk re-scoring** — `_RESCORE_WORKERS` reduced from 5 to 1; image-heavy listings (18-46 images each) exceed Anthropic's 10k tokens/minute org limit when run concurrently; serial execution avoids rate limiting
- **Verdict/score consistency enforcement** in `_validate_ai_response()` — "Reject" always forces score=0; non-Reject verdicts always re-derived from score (80+=Strong Match, 60+=Worth Touring, 40+=Low Priority, >0=Weak Match); prevents filter chip mismatches
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
