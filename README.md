# Listings Analyzer

Real estate listing analyzer that ingests Gmail alerts, parses listings, and scores them with AI against editable buyer criteria. Mobile-friendly dashboard deployed on Fly.io.

## How It Works

1. **Polls Gmail** on a schedule for listing alert emails (Redfin, OneKey MLS, forwarded links)
2. **Parses** emails with specialized parsers (OneHome HTML, plaintext regex, LLM fallback)
3. **Enriches** with school ratings (SchoolDigger) and transit commute times (Google Routes)
4. **Scores** each listing against your criteria using Claude Haiku
5. **Displays** results on a mobile-friendly dashboard with filters, sorting, and shareable URLs

## Quick Start

### Prerequisites

- Python 3.12+
- [uv](https://docs.astral.sh/uv/) package manager
- Gmail account with API access
- Anthropic API key

### 1. Clone and install

```bash
git clone https://github.com/YOUR_USER/listings-analyzer.git
cd listings-analyzer
uv sync
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env` with your credentials:

| Variable | Required | Description |
|----------|----------|-------------|
| `GMAIL_CREDENTIALS_JSON` | Yes | Gmail OAuth credentials JSON (from Google Cloud Console) |
| `GMAIL_REFRESH_TOKEN` | Yes | Gmail refresh token (run `uv run python scripts/gmail_auth.py` to obtain) |
| `ANTHROPIC_API_KEY` | Yes | Anthropic API key for AI scoring |
| `ALLOWED_EMAILS` | Yes | Comma-separated emails allowed to sign in |
| `ALERT_SENDERS` | Yes | Comma-separated sender emails/domains to poll (e.g. `redfin.com`) |
| `DATABASE_URL` | No | Postgres URL for production; defaults to SQLite locally |
| `MANAGE_KEY` | No | Secret key for `/manage/*` admin endpoints |
| `POLL_INTERVAL_HOURS` | No | Auto-poll interval (default: 1 hour, 0 to disable) |
| `SCHOOLDIGGER_APP_ID` | No | SchoolDigger API credentials for school ratings |
| `SCHOOLDIGGER_APP_KEY` | No | (free dev tier: 20 calls/day) |
| `GOOGLE_MAPS_API_KEY` | No | Google Routes API for commute times |
| `COMMUTE_DESTINATION` | No | Commute target address (default: Brookfield Place, NYC) |
| `SENDER_DATE_FILTERS` | No | Date-filter senders, e.g. `friend@example.com:21` (only last 21 days) |
| `MAX_EMAIL_AGE_DAYS` | No | Ignore emails older than N days (default: 21) |

### 3. Set up Gmail OAuth

```bash
# Create OAuth credentials at https://console.cloud.google.com/apis/credentials
# Download the JSON and set GMAIL_CREDENTIALS_JSON in .env

uv run python scripts/gmail_auth.py
# Follow the browser flow, then copy the refresh token to .env
```

### 4. Run locally

```bash
uv run uvicorn app.main:app --reload --port 8000
```

Open http://localhost:8000 — sign in with a Google account listed in `ALLOWED_EMAILS`.

### 5. Run tests

```bash
uv run pytest tests/ -x -q
```

## Project Structure

```
app/
├── main.py            # FastAPI app, all API endpoints, scheduler
├── config.py          # Pydantic settings (env vars)
├── db.py              # Database layer (SQLite + Postgres)
├── gmail.py           # Gmail API client
├── poller.py          # Poll pipeline (fetch → parse → score → save)
├── scorer.py          # AI evaluation (Claude Haiku)
├── enrichment.py      # Schools (SchoolDigger) + commute (Google Routes)
├── auth.py            # Google Sign-In + session cookies
├── models.py          # Pydantic models
├── parsers/
│   ├── onehome.py     # OneHome/OneKey MLS HTML parser + scraper
│   ├── plaintext.py   # Regex-based plaintext parser
│   ├── llm_fallback.py# Claude-based fallback parser
│   ├── forwarded.py   # Forwarded email handler
│   └── base.py        # Base parser interface
└── templates/
    └── dashboard.html # Single-page mobile dashboard

tests/                 # Pytest suite (280+ tests)
scripts/
└── gmail_auth.py      # Gmail OAuth setup helper
samples/               # Sample email fixtures
```

## Dashboard Routes

| Route | Description |
|-------|-------------|
| `/` | Dashboard (all listings) |
| `/non-reject` | Non-reject listings only |
| `/strong-match` | Strong matches only |
| `/worth-touring` | Worth touring only |
| `/reject` | Rejects only |
| `/toured` | Toured listings |
| `/want-to-go` | Flagged "Want to Go" |

All routes are shareable links. Filter chips update the URL via `pushState`.

## API Endpoints

| Method | Path | Auth | Description |
|--------|------|------|-------------|
| `GET` | `/listings` | No | List all scored listings |
| `POST` | `/listings/add` | Yes | Create listing from URL |
| `POST` | `/listings/{id}/toured` | Yes | Toggle toured flag |
| `POST` | `/listings/{id}/tour-request` | Yes | Toggle "Want to Go" flag |
| `POST` | `/listings/{id}/sold` | Yes | Mark listing as sold (deletes it) |
| `POST` | `/listings/{id}/url` | Yes | Attach URL to existing listing |
| `GET` | `/criteria` | No | Get current scoring criteria |
| `POST` | `/criteria` | Yes | Update criteria + trigger rescore |
| `POST` | `/manage/poll` | Key | Trigger email poll |
| `POST` | `/manage/scrape-descriptions` | Key | Backfill URLs + descriptions |
| `POST` | `/manage/enrich` | Key | Backfill schools + commute |
| `POST` | `/manage/data-quality` | Key | Audit/fix data issues |

## Deploying to Fly.io

```bash
fly launch          # First time
fly deploy          # Subsequent deploys

# Set secrets (never commit these)
fly secrets set GMAIL_CREDENTIALS_JSON='...'
fly secrets set GMAIL_REFRESH_TOKEN='...'
fly secrets set ANTHROPIC_API_KEY='...'
fly secrets set ALLOWED_EMAILS='...'
fly secrets set ALERT_SENDERS='...'
fly secrets set MANAGE_KEY='...'
fly secrets set DATABASE_URL='...'
```

## License

MIT
