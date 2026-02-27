# Product Requirements Document

**Product:** Listing Analyzer Bot v2
**Owner:** Aki
**Objective:** Paste listing alert text → bot finds listing online → renders page → extracts data → scores against goals.

---

## 1. Problem

You receive listing alerts in email/text. They often contain:
- Partial address
- MLS ID
- Price + basic stats
- Tokenized OneHome links

Links may expire. Auth may block scraping.

You want:
- Paste raw alert text
- Bot finds canonical listing page
- Full render
- Structured evaluation
- Deterministic scoring

## 2. Goals

### Primary
- Input: raw pasted listing content
- Output: structured evaluation + scorecard

### Secondary
- Store listing history
- Compare listings
- Detect duplicates

### Non-goals
- Email automation
- Agent messaging
- Offer execution

## 3. User Flow

1. Paste alert content into web UI or Slack.
2. Bot extracts:
   - Address
   - MLS ID
   - Price
   - Town
3. Bot performs search:
   - Query: "[address] [town] NY listing"
   - Prefer Zillow/Redfin/Realtor canonical page
4. Bot selects best match.
5. Bot renders page with headless browser.
6. Bot extracts structured data.
7. Bot runs scoring engine.
8. Bot returns evaluation.

## 4. System Architecture

### Input Layer
- Web UI or Slack slash command
- Accept raw text blob (pasted email body — HTML or plain text)

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
- OneHome token links (JWT, expire) — not useful for scraping

**Parsing strategy:**
1. If input is HTML → parse with BeautifulSoup using CSS selectors above (fast, deterministic)
2. If input is plain text → regex for `$price`, `N bd`, `N ba`, `N,NNN sqft`, `MLS #NNNNNN`, address lines
3. LLM fallback for ambiguous/unknown formats

See `samples/onehome_email_alert.txt` for reference.

### Parsing Layer
- HTML parser (BeautifulSoup) for OneHome emails
- Regex for plain text alerts
- LLM fallback for unknown formats
- Extract per listing:
  - Street
  - Town
  - State
  - Zip
  - MLS ID
  - Price
  - Sqft
  - Bedrooms
  - Bathrooms
  - Property type
  - Listing status (New, Price Changed, etc.)

### Search Layer
- Web search API
- Query strategy:
  - If full address → exact match query
  - If MLS ID → "MLS 423619934"
  - If partial → fuzzy search

### Ranking Logic
- Score candidate URLs by:
  - Exact address match
  - Matching price
  - Matching sqft
- Prefer:
  1. Zillow
  2. Redfin
  3. Realtor
  4. Brokerage site

### Rendering Layer
- Playwright (Chromium)
- Wait network idle
- Scroll page
- Expand "more details"
- Save HTML snapshot
- Save screenshot

### Extraction Layer
Two-pass:

**Pass 1:** Domain-specific selectors
**Pass 2:** LLM structured extraction with strict JSON schema

Required schema:
```json
{
  "address": "",
  "price": 0,
  "sqft": 0,
  "lot_sqft": 0,
  "bedrooms": 0,
  "bathrooms": 0,
  "basement_finished": false,
  "basement_walkout": false,
  "ground_floor_bedroom": false,
  "property_type": "",
  "attached": false,
  "year_built": 0,
  "amenities": {
    "pool": false,
    "sauna": false,
    "jacuzzi": false,
    "soak_tub": false,
    "gym_space_possible": false
  }
}
```

## 5. Evaluation Engine

### Hard Requirements
- >= 2600 sqft (no upper bound)
- 4–5 bedrooms
- Detached (no townhouse/condo)
- Finished basement
- Price: $1.25M–$2M

### Scoring

Passing all hard requirements gives a **base score of 20**.
Soft features add points on top. Max possible = 100.

| Feature | Points |
|---|---|
| Base (all hard reqs pass) | +20 |
| Finished basement | +20 |
| Ground-floor bedroom | +20 |
| Lot >= 0.3 acre | +15 |
| Pool | +10 |
| Sauna | +5 |
| Jacuzzi | +5 |
| Soak tub | +5 |

**Rationale:** A base of 20 means a listing that passes hard requirements but has zero amenities lands at "Low Priority" (not "Reject"), which is correct -- it meets the basics but has nothing extra. A listing needs at least a couple of meaningful soft wins to reach "Worth Touring."

### Land Heuristic
- lot_sqft >= 10,000 = acceptable
- < 7,000 = tight
- HOA + townhouse = reject

### Verdict Logic
If any hard fail → **Reject**

Otherwise:
- Score >= 80 → **Strong Match**
- 60–79 → **Worth Touring**
- 40–59 → **Low Priority**
- < 40 → **Pass**

## 6. Output Format

```
Listing: [Address]

Hard Criteria
  Sqft: 2850 ✓
  Bedrooms: 4 ✓
  Detached: Yes ✓
  Basement: Finished ✓

Land
  Lot: 0.18 acre ✗ (tight spacing likely)

Amenities
  Pool: No
  Sauna: No
  Jacuzzi: Yes

Score: 68 / 100
Verdict: Worth Touring

Concerns:
- Small lot
- No first-floor bedroom
```

## 7. Data Model

### Table: listings
- id
- address
- town
- mls_id
- source_url
- raw_html_path
- screenshot_path
- created_at

### Table: extracted_data
- listing_id
- json_data
- extraction_confidence
- created_at

### Table: scores
- listing_id
- score
- verdict
- reasons_json

## 8. Key Technical Decisions

### Stack
- **Language:** Python
- **Framework:** FastAPI
- **DB:** SQLite (simple start, upgrade later)
- **Deployment:** Local first, cloud later

### Search API
- SerpAPI (free tier to start, cheapest option)

### Rendering
- Playwright (local, no Docker for MVP)
- Timeout 20 seconds

### LLM Extraction
- Claude Haiku (cheapest Claude model)
- Strict JSON mode
- Validate against schema
- Reject malformed output

## 9. Edge Cases

- Duplicate addresses across years
- Pending vs sold
- Missing lot size
- Split-level houses where ground floor is unclear
- Basement labeled "partially finished"

**Mitigation:** If ambiguous → mark as "uncertain" and lower score confidence.

## 10. Confidence Scoring

Add `extraction_confidence`:
- **High:** DOM selectors matched
- **Medium:** LLM extracted cleanly
- **Low:** Missing sqft or BR

Return confidence in output.

## 11. Phase Plan

### Phase 1 (MVP)
- Paste input
- Extract address
- Search
- Render Zillow
- LLM extraction
- Deterministic scoring
- Console output

### Phase 2
- DB persistence
- Slack bot
- Screenshot previews
- Compare listings

### Phase 3
- Comps engine
- Price per sqft scoring
- Metro-North proximity scoring
- Tax estimation
- Hudson-side weighting

## 12. Engineering Discipline

- PRD locked before coding
- Explicit JSON schema contracts
- Deterministic scoring logic
- Unit tests for scoring rules
- Snapshot tests for extraction
