"""Database layer with dual SQLite/Postgres support.

Uses sqlite3 locally, psycopg2 on Heroku (detected via DATABASE_URL).
Provides a simple connection helper and schema initialization.
"""

import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from app.config import settings
from app.models import ParsedListing, ScoringResult

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS processed_emails (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    gmail_id TEXT UNIQUE NOT NULL,
    message_id TEXT,
    sender TEXT NOT NULL,
    subject TEXT,
    received_date TEXT,
    parser_used TEXT,
    listings_found INTEGER DEFAULT 0,
    processed_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS listings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_email_id INTEGER REFERENCES processed_emails(id),
    address TEXT,
    town TEXT,
    state TEXT,
    zip_code TEXT,
    mls_id TEXT,
    price INTEGER,
    sqft INTEGER,
    bedrooms INTEGER,
    bathrooms INTEGER,
    property_type TEXT,
    listing_status TEXT,
    source_format TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS scores (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id INTEGER UNIQUE REFERENCES listings(id),
    score INTEGER,
    verdict TEXT,
    hard_results_json TEXT,
    soft_points_json TEXT,
    concerns_json TEXT,
    confidence TEXT,
    evaluation_method TEXT DEFAULT 'deterministic',
    criteria_version INTEGER,
    ai_reasoning TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS evaluation_criteria (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    instructions TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_by TEXT,
    created_at TEXT DEFAULT (datetime('now'))
);
"""

# Postgres-compatible schema (uses SERIAL, TIMESTAMP, etc.)
SCHEMA_PG = """
CREATE TABLE IF NOT EXISTS processed_emails (
    id SERIAL PRIMARY KEY,
    gmail_id TEXT UNIQUE NOT NULL,
    message_id TEXT,
    sender TEXT NOT NULL,
    subject TEXT,
    received_date TEXT,
    parser_used TEXT,
    listings_found INTEGER DEFAULT 0,
    processed_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS listings (
    id SERIAL PRIMARY KEY,
    source_email_id INTEGER REFERENCES processed_emails(id),
    address TEXT,
    town TEXT,
    state TEXT,
    zip_code TEXT,
    mls_id TEXT,
    price INTEGER,
    sqft INTEGER,
    bedrooms INTEGER,
    bathrooms INTEGER,
    property_type TEXT,
    listing_status TEXT,
    source_format TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS scores (
    id SERIAL PRIMARY KEY,
    listing_id INTEGER UNIQUE REFERENCES listings(id),
    score INTEGER,
    verdict TEXT,
    hard_results_json TEXT,
    soft_points_json TEXT,
    concerns_json TEXT,
    confidence TEXT,
    evaluation_method TEXT DEFAULT 'deterministic',
    criteria_version INTEGER,
    ai_reasoning TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS evaluation_criteria (
    id SERIAL PRIMARY KEY,
    instructions TEXT NOT NULL,
    version INTEGER NOT NULL DEFAULT 1,
    created_by TEXT,
    created_at TIMESTAMP DEFAULT NOW()
);
"""


@contextmanager
def get_connection():
    """Get a database connection (SQLite or Postgres)."""
    if settings.is_postgres:
        import psycopg2

        # Heroku uses postgres:// but psycopg2 needs postgresql://
        url = settings.database_url.replace("postgres://", "postgresql://", 1)
        conn = psycopg2.connect(
            url,
            connect_timeout=5,
            options="-c statement_timeout=30000",
        )
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    else:
        db_path = settings.database_url.replace("sqlite:///", "")
        conn = sqlite3.connect(db_path, timeout=5)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


# In-memory re-score state (single-instance app)
rescore_state = {
    "in_progress": False, "total": 0, "completed": 0,
    "skipped": 0, "criteria_version": 0, "batch_id": None,
    "started_at": None,
}


def init_db():
    """Create tables if they don't exist."""
    schema = SCHEMA_PG if settings.is_postgres else SCHEMA
    with get_connection() as conn:
        cur = conn.cursor()
        for statement in schema.split(";"):
            statement = statement.strip()
            if statement:
                cur.execute(statement)

    # Add columns to existing tables (idempotent)
    _migrate_add_columns()

    # Recompute address_key for all listings (picks up normalization changes)
    _backfill_address_keys()

    # Remove duplicates that now share the same address_key after recomputation
    _dedup_by_address_key()

    logger.info("Database initialized")


def _placeholder():
    """Return the parameter placeholder for the current DB."""
    return "%s" if settings.is_postgres else "?"


def save_processed_email(
    gmail_id: str,
    message_id: str,
    sender: str,
    subject: str,
    parser_used: str,
    listings_found: int,
) -> int:
    """Save a processed email record. Returns the email ID."""
    ph = _placeholder()
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""INSERT INTO processed_emails (gmail_id, message_id, sender, subject, parser_used, listings_found)
            VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph})
            ON CONFLICT (gmail_id) DO NOTHING""",
            (gmail_id, message_id, sender, subject, parser_used, listings_found),
        )
        if settings.is_postgres:
            cur.execute("SELECT id FROM processed_emails WHERE gmail_id = %s", (gmail_id,))
        else:
            cur.execute("SELECT id FROM processed_emails WHERE gmail_id = ?", (gmail_id,))
        row = cur.fetchone()
        return row[0] if row else 0


def is_email_processed(gmail_id: str) -> bool:
    """Check if an email has already been processed."""
    ph = _placeholder()
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT 1 FROM processed_emails WHERE gmail_id = {ph}", (gmail_id,))
        return cur.fetchone() is not None


def get_all_processed_gmail_ids() -> list[str]:
    """Get all processed email Gmail IDs for reprocessing."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT gmail_id FROM processed_emails ORDER BY id")
        return [row[0] for row in cur.fetchall()]


def update_listing_url_by_mls(mls_id: str, listing_url: str, description: str | None):
    """Update listing URL and description for a listing found by MLS ID."""
    if not mls_id:
        return
    ph = _placeholder()
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE listings SET listing_url = {ph}, description = {ph} WHERE mls_id = {ph} AND (listing_url IS NULL OR listing_url = '')",
            (listing_url, description, mls_id),
        )


def backfill_listing_address(
    mls_id: str,
    address: str | None,
    town: str | None,
    state: str | None,
    zip_code: str | None,
):
    """Backfill address fields for a listing that's missing them.

    Only updates fields that are currently NULL or empty in the DB.
    """
    if not mls_id:
        return
    updates = []
    values = []
    ph = _placeholder()
    for col, val in [
        ("address", address),
        ("town", town),
        ("state", state),
        ("zip_code", zip_code),
    ]:
        if val:
            updates.append(f"{col} = CASE WHEN {col} IS NULL OR {col} = '' THEN {ph} ELSE {col} END")
            values.append(val)
    if not updates:
        return
    values.append(mls_id)
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE listings SET {', '.join(updates)} WHERE mls_id = {ph}",
            values,
        )


_INTEGER_COLUMNS = {"price", "sqft", "bedrooms", "bathrooms", "commute_minutes", "year_built"}
_NUMERIC_COLUMNS = _INTEGER_COLUMNS | {"lot_acres"}  # columns where IS NULL check is required


def update_listing_fields_by_id(listing_id: int, **fields):
    """Update arbitrary listing columns by listing ID.

    Only updates fields whose current DB value is NULL or empty.
    """
    if not fields:
        return
    updates = []
    values = []
    ph = _placeholder()
    for col, val in fields.items():
        if val is not None:
            # Numeric columns (int/float): only check IS NULL (comparing to '' fails in Postgres)
            if col in _NUMERIC_COLUMNS:
                updates.append(
                    f"{col} = CASE WHEN {col} IS NULL THEN {ph} ELSE {col} END"
                )
            else:
                updates.append(
                    f"{col} = CASE WHEN {col} IS NULL OR {col} = '' THEN {ph} ELSE {col} END"
                )
            values.append(val)
    if not updates:
        return
    values.append(listing_id)
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE listings SET {', '.join(updates)} WHERE id = {ph}",
            values,
        )


def is_listing_duplicate(mls_id: str) -> bool:
    """Check if a listing with this MLS ID already exists."""
    if not mls_id:
        return False
    ph = _placeholder()
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT 1 FROM listings WHERE mls_id = {ph}", (mls_id,))
        return cur.fetchone() is not None


def is_listing_duplicate_by_address(address_key: str) -> bool:
    """Check if a listing with this normalized address key already exists."""
    if not address_key:
        return False
    ph = _placeholder()
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT 1 FROM listings WHERE address_key = {ph}", (address_key,))
        return cur.fetchone() is not None


def get_listing_id_and_status_by_mls(mls_id: str) -> tuple[int, str | None] | None:
    """Return (id, listing_status) for a listing with this MLS ID, or None."""
    if not mls_id:
        return None
    ph = _placeholder()
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT id, listing_status FROM listings WHERE mls_id = {ph}",
            (mls_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        if settings.is_postgres:
            return (row[0], row[1])
        return (row["id"], row["listing_status"])


def get_listing_id_and_status_by_address_key(address_key: str) -> tuple[int, str | None] | None:
    """Return (id, listing_status) for a listing with this address key, or None."""
    if not address_key:
        return None
    ph = _placeholder()
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT id, listing_status FROM listings WHERE address_key = {ph}",
            (address_key,),
        )
        row = cur.fetchone()
        if not row:
            return None
        if settings.is_postgres:
            return (row[0], row[1])
        return (row["id"], row["listing_status"])


def update_listing_status(listing_id: int, status: str):
    """Unconditionally update listing_status for a listing."""
    ph = _placeholder()
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE listings SET listing_status = {ph} WHERE id = {ph}",
            (status, listing_id),
        )


def get_school_data_by_zip(zip_code: str) -> str | None:
    """Return cached school_data_json from any listing with the same zip code.

    Avoids redundant SchoolDigger API calls for listings in the same zip.
    """
    if not zip_code:
        return None
    ph = _placeholder()
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            f"SELECT school_data_json FROM listings WHERE zip_code = {ph} AND school_data_json IS NOT NULL LIMIT 1",
            (zip_code,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return row[0] if settings.is_postgres else row["school_data_json"]


def save_listing(listing: ParsedListing, score: ScoringResult, email_id: int, enrichment: dict | None = None) -> int:
    """Save a listing and its score. Returns the listing ID."""
    ph = _placeholder()
    with get_connection() as conn:
        cur = conn.cursor()

        enr = enrichment or {}
        cur.execute(
            f"""INSERT INTO listings
            (source_email_id, address, town, state, zip_code, mls_id, price, sqft,
             bedrooms, bathrooms, property_type, listing_status, source_format,
             listing_url, description, year_built, list_date, lot_acres,
             address_key, school_data_json, commute_minutes, commute_data_json)
            VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})""",
            (
                email_id,
                listing.address,
                listing.town,
                listing.state,
                listing.zip_code,
                listing.mls_id,
                listing.price,
                listing.sqft,
                listing.bedrooms,
                listing.bathrooms,
                listing.property_type,
                listing.listing_status,
                listing.source_format,
                listing.listing_url,
                listing.description,
                listing.year_built,
                listing.list_date,
                listing.lot_acres,
                enr.get("address_key"),
                enr.get("school_data_json"),
                enr.get("commute_minutes"),
                enr.get("commute_data_json"),
            ),
        )

        if settings.is_postgres:
            cur.execute("SELECT lastval()")
        else:
            cur.execute("SELECT last_insert_rowid()")
        listing_id = cur.fetchone()[0]

        # Save score
        hard_json = json.dumps([hr.model_dump() for hr in score.hard_results])
        soft_json = json.dumps(score.soft_points)
        concerns_json = json.dumps(score.concerns)

        cur.execute(
            f"""INSERT INTO scores
            (listing_id, score, verdict, hard_results_json, soft_points_json, concerns_json, confidence)
            VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})""",
            (listing_id, score.score, score.verdict, hard_json, soft_json, concerns_json, score.confidence),
        )

        return listing_id


def get_all_listings() -> list[dict]:
    """Get all listings with their scores and full scoring detail."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT l.*, s.score, s.verdict, s.hard_results_json,
                   s.soft_points_json, s.concerns_json, s.confidence,
                   s.evaluation_method, s.criteria_version, s.ai_reasoning,
                   s.property_summary
            FROM listings l
            LEFT JOIN scores s ON s.listing_id = l.id
            ORDER BY l.created_at DESC
        """)
        rows = cur.fetchall()

        if settings.is_postgres:
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, row)) for row in rows]
        else:
            return [dict(row) for row in rows]


def get_listing_by_mls(mls_id: str) -> dict | None:
    """Get a single listing by MLS ID."""
    ph = _placeholder()
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            f"""SELECT l.*, s.score, s.verdict, s.hard_results_json, s.concerns_json, s.confidence,
                       s.evaluation_method, s.criteria_version, s.ai_reasoning, s.property_summary
            FROM listings l
            LEFT JOIN scores s ON s.listing_id = l.id
            WHERE l.mls_id = {ph}""",
            (mls_id,),
        )
        row = cur.fetchone()
        if not row:
            return None

        if settings.is_postgres:
            columns = [desc[0] for desc in cur.description]
            return dict(zip(columns, row))
        else:
            return dict(row)


# --- Schema migrations (idempotent) ---


def _migrate_add_columns():
    """Add new columns to existing tables. Safe to run multiple times.

    Uses per-statement connections for Postgres compatibility — a failed
    ALTER TABLE (column already exists) aborts the transaction in Postgres,
    so each attempt needs its own connection.
    """
    alterations = [
        ("listings", "listing_url", "TEXT"),
        ("listings", "image_urls_json", "TEXT"),
        ("listings", "description", "TEXT"),
        ("listings", "toured", "BOOLEAN DEFAULT FALSE"),
        ("listings", "address_key", "TEXT"),
        ("listings", "school_data_json", "TEXT"),
        ("listings", "commute_minutes", "INTEGER"),
        ("listings", "commute_data_json", "TEXT"),
        ("listings", "enriched_at", "TEXT"),
        ("listings", "tour_requested", "BOOLEAN DEFAULT FALSE"),
        ("listings", "passed", "BOOLEAN DEFAULT FALSE"),
        ("listings", "year_built", "INTEGER"),
        ("listings", "list_date", "TEXT"),
        ("listings", "property_tax_json", "TEXT"),
        ("listings", "lot_acres", "REAL"),
        ("listings", "lat", "REAL"),
        ("listings", "lng", "REAL"),
        ("listings", "power_line_json", "TEXT"),
        ("listings", "flood_zone_json", "TEXT"),
        ("listings", "station_json", "TEXT"),
        ("listings", "garage_count", "INTEGER"),
        ("listings", "garage_type", "TEXT"),
        ("listings", "hoa_monthly", "INTEGER"),
        ("listings", "has_pool", "BOOLEAN"),
        ("listings", "pool_type", "TEXT"),
        ("listings", "has_basement", "BOOLEAN"),
        ("listings", "basement_type", "TEXT"),
        ("scores", "evaluation_method", "TEXT DEFAULT 'deterministic'"),
        ("scores", "criteria_version", "INTEGER"),
        ("scores", "ai_reasoning", "TEXT"),
        ("scores", "property_summary", "TEXT"),
        ("scores", "scored_at", "TEXT"),
    ]
    for table, column, col_type in alterations:
        try:
            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {column} {col_type}")
        except Exception:
            pass  # Column already exists


def _backfill_address_keys():
    """Recompute address_key for all listings with address+town.

    Runs on every startup so normalization changes (e.g. state name → code)
    are applied to existing listings, not just new ones.
    """
    from app.enrichment import normalize_address

    ph = _placeholder()
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, address, town, state, address_key FROM listings "
            "WHERE address IS NOT NULL AND town IS NOT NULL"
        )
        if settings.is_postgres:
            columns = [desc[0] for desc in cur.description]
            rows = [dict(zip(columns, r)) for r in cur.fetchall()]
        else:
            rows = [dict(r) for r in cur.fetchall()]

    updated = 0
    for row in rows:
        key = normalize_address(row["address"], row["town"], row["state"])
        if key and key != row.get("address_key"):
            with get_connection() as conn:
                cur = conn.cursor()
                cur.execute(
                    f"UPDATE listings SET address_key = {ph} WHERE id = {ph}",
                    (key, row["id"]),
                )
                updated += 1

    if updated:
        logger.info(f"Recomputed address_key for {updated} listing(s)")


def _dedup_by_address_key():
    """Remove duplicate listings that share the same address_key.

    Keeps the listing with the most data (prefers: has toured, has mls_id,
    has listing_url, lowest id as tiebreaker). Deletes the rest.
    """
    ph = _placeholder()
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, address_key, toured, mls_id, listing_url FROM listings "
            "WHERE address_key IS NOT NULL ORDER BY address_key, id"
        )
        if settings.is_postgres:
            columns = [desc[0] for desc in cur.description]
            rows = [dict(zip(columns, r)) for r in cur.fetchall()]
        else:
            rows = [dict(r) for r in cur.fetchall()]

    # Group by address_key
    from collections import defaultdict

    groups: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        groups[row["address_key"]].append(row)

    delete_ids: list[int] = []
    for key, listings in groups.items():
        if len(listings) < 2:
            continue
        # Sort: toured first, then has mls_id, then has url, then lowest id
        listings.sort(
            key=lambda r: (
                not r.get("toured"),
                not r.get("mls_id"),
                not r.get("listing_url"),
                r["id"],
            )
        )
        # Keep first (best), delete rest
        delete_ids.extend(r["id"] for r in listings[1:])

    if delete_ids:
        with get_connection() as conn:
            cur = conn.cursor()
            for lid in delete_ids:
                cur.execute(f"DELETE FROM scores WHERE listing_id = {ph}", (lid,))
                cur.execute(f"DELETE FROM listings WHERE id = {ph}", (lid,))
            conn.commit()
        logger.info(f"Deduped {len(delete_ids)} listing(s) by address_key")


# --- Evaluation criteria CRUD ---


def get_active_criteria() -> dict | None:
    """Get the current evaluation criteria (highest version)."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT * FROM evaluation_criteria ORDER BY version DESC LIMIT 1")
        row = cur.fetchone()
        if not row:
            return None
        if settings.is_postgres:
            columns = [desc[0] for desc in cur.description]
            return dict(zip(columns, row))
        return dict(row)


def get_criteria_history() -> list[dict]:
    """Get all evaluation criteria versions, newest first."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, version, instructions, created_by, created_at FROM evaluation_criteria ORDER BY version DESC"
        )
        rows = cur.fetchall()
        if settings.is_postgres:
            columns = [desc[0] for desc in cur.description]
            return [dict(zip(columns, row)) for row in rows]
        return [dict(row) for row in rows]


def save_criteria(instructions: str, created_by: str) -> int:
    """Save new evaluation criteria. Returns the new version number."""
    ph = _placeholder()
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT COALESCE(MAX(version), 0) FROM evaluation_criteria")
        max_version = cur.fetchone()[0]
        new_version = max_version + 1
        cur.execute(
            f"INSERT INTO evaluation_criteria (instructions, version, created_by) VALUES ({ph}, {ph}, {ph})",
            (instructions, new_version, created_by),
        )
        return new_version


# --- Score upsert ---


def update_score(
    listing_id: int,
    score: ScoringResult,
    method: str,
    criteria_version: int | None,
    reasoning: str | None,
    property_summary: str | None = None,
):
    """Upsert a score for a listing. Automatically sets scored_at timestamp."""
    ph = _placeholder()
    hard_json = json.dumps([hr.model_dump() for hr in score.hard_results])
    soft_json = json.dumps(score.soft_points)
    concerns_json = json.dumps(score.concerns)
    scored_at = datetime.now(timezone.utc).isoformat()

    with get_connection() as conn:
        cur = conn.cursor()
        if settings.is_postgres:
            cur.execute(
                """INSERT INTO scores
                (listing_id, score, verdict, hard_results_json, soft_points_json,
                 concerns_json, confidence, evaluation_method, criteria_version,
                 ai_reasoning, property_summary, scored_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (listing_id) DO UPDATE SET
                    score = EXCLUDED.score, verdict = EXCLUDED.verdict,
                    hard_results_json = EXCLUDED.hard_results_json,
                    soft_points_json = EXCLUDED.soft_points_json,
                    concerns_json = EXCLUDED.concerns_json,
                    confidence = EXCLUDED.confidence,
                    evaluation_method = EXCLUDED.evaluation_method,
                    criteria_version = EXCLUDED.criteria_version,
                    ai_reasoning = EXCLUDED.ai_reasoning,
                    property_summary = EXCLUDED.property_summary,
                    scored_at = EXCLUDED.scored_at""",
                (listing_id, score.score, score.verdict, hard_json, soft_json,
                 concerns_json, score.confidence, method, criteria_version,
                 reasoning, property_summary, scored_at),
            )
        else:
            cur.execute(
                """INSERT INTO scores
                (listing_id, score, verdict, hard_results_json, soft_points_json,
                 concerns_json, confidence, evaluation_method, criteria_version,
                 ai_reasoning, property_summary, scored_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (listing_id) DO UPDATE SET
                    score = excluded.score, verdict = excluded.verdict,
                    hard_results_json = excluded.hard_results_json,
                    soft_points_json = excluded.soft_points_json,
                    concerns_json = excluded.concerns_json,
                    confidence = excluded.confidence,
                    evaluation_method = excluded.evaluation_method,
                    criteria_version = excluded.criteria_version,
                    ai_reasoning = excluded.ai_reasoning,
                    property_summary = excluded.property_summary,
                    scored_at = excluded.scored_at""",
                (listing_id, score.score, score.verdict, hard_json, soft_json,
                 concerns_json, score.confidence, method, criteria_version,
                 reasoning, property_summary, scored_at),
            )


# --- Image management ---


def add_listing_images(listing_id: int, image_urls: list[str]):
    """Set image URLs for a listing."""
    ph = _placeholder()
    urls_json = json.dumps(image_urls)
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE listings SET image_urls_json = {ph} WHERE id = {ph}",
            (urls_json, listing_id),
        )


def update_listing_description(listing_id: int, listing_url: str, description: str | None):
    """Update the listing URL and scraped description."""
    ph = _placeholder()
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE listings SET listing_url = {ph}, description = {ph} WHERE id = {ph}",
            (listing_url, description, listing_id),
        )


def update_listing_enrichment(listing_id: int, enrichment: dict):
    """Update enrichment data (address key, school data, commute) for a listing.

    Only updates columns present in the enrichment dict (supports partial updates).
    Automatically sets enriched_at timestamp.
    """
    allowed_cols = {
        "address_key", "school_data_json", "commute_minutes", "commute_data_json",
        "property_tax_json", "power_line_json", "flood_zone_json", "station_json",
        "lat", "lng", "garage_count", "garage_type", "hoa_monthly",
        "has_pool", "pool_type", "has_basement", "basement_type",
        "year_built", "list_date",
    }
    cols_to_update = {k: v for k, v in enrichment.items() if k in allowed_cols}
    if not cols_to_update:
        return

    # Always set enriched_at when enrichment data changes
    now = datetime.now(timezone.utc).isoformat()
    cols_to_update["enriched_at"] = now

    ph = _placeholder()
    set_clauses = [f"{col} = {ph}" for col in cols_to_update]
    values = list(cols_to_update.values()) + [listing_id]

    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE listings SET {', '.join(set_clauses)} WHERE id = {ph}",
            tuple(values),
        )


def mark_listing_toured(listing_id: int, toured: bool):
    """Mark (or un-mark) a listing as toured."""
    ph = _placeholder()
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE listings SET toured = {ph} WHERE id = {ph}",
            (toured, listing_id),
        )


def mark_listing_tour_requested(listing_id: int, tour_requested: bool):
    """Mark (or un-mark) a listing as tour requested."""
    ph = _placeholder()
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE listings SET tour_requested = {ph} WHERE id = {ph}",
            (tour_requested, listing_id),
        )


def mark_listing_passed(listing_id: int, passed: bool):
    """Mark (or un-mark) a listing as passed (chose not to pursue)."""
    ph = _placeholder()
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(
            f"UPDATE listings SET passed = {ph} WHERE id = {ph}",
            (passed, listing_id),
        )


# --- Listing queries for re-scoring ---


def get_all_listing_ids() -> list[int]:
    """Get all listing IDs for re-scoring."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT id FROM listings ORDER BY id")
        return [row[0] for row in cur.fetchall()]


def get_all_score_metadata() -> dict[int, dict]:
    """Get score metadata for all listings (for skip-unchanged logic).

    Returns {listing_id: {"criteria_version": int, "scored_at": str, "evaluation_method": str}}
    for every listing that has a score. Used to determine which listings can
    be skipped during a rescore.
    """
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("SELECT listing_id, criteria_version, scored_at, evaluation_method FROM scores")
        rows = cur.fetchall()
        result = {}
        if settings.is_postgres:
            for row in rows:
                result[row[0]] = {
                    "criteria_version": row[1],
                    "scored_at": row[2],
                    "evaluation_method": row[3],
                }
        else:
            for row in rows:
                result[row["listing_id"]] = {
                    "criteria_version": row["criteria_version"],
                    "scored_at": row["scored_at"],
                    "evaluation_method": row["evaluation_method"],
                }
        return result


def get_listing_by_id(listing_id: int) -> dict | None:
    """Get a single listing by internal ID (for re-scoring)."""
    ph = _placeholder()
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT * FROM listings WHERE id = {ph}", (listing_id,))
        row = cur.fetchone()
        if not row:
            return None
        if settings.is_postgres:
            columns = [desc[0] for desc in cur.description]
            return dict(zip(columns, row))
        return dict(row)
