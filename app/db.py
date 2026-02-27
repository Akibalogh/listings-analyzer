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
        conn = psycopg2.connect(url)
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
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()


def init_db():
    """Create tables if they don't exist."""
    schema = SCHEMA_PG if settings.is_postgres else SCHEMA
    with get_connection() as conn:
        cur = conn.cursor()
        for statement in schema.split(";"):
            statement = statement.strip()
            if statement:
                cur.execute(statement)
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


def is_listing_duplicate(mls_id: str) -> bool:
    """Check if a listing with this MLS ID already exists."""
    if not mls_id:
        return False
    ph = _placeholder()
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute(f"SELECT 1 FROM listings WHERE mls_id = {ph}", (mls_id,))
        return cur.fetchone() is not None


def save_listing(listing: ParsedListing, score: ScoringResult, email_id: int) -> int:
    """Save a listing and its score. Returns the listing ID."""
    ph = _placeholder()
    with get_connection() as conn:
        cur = conn.cursor()

        cur.execute(
            f"""INSERT INTO listings
            (source_email_id, address, town, state, zip_code, mls_id, price, sqft,
             bedrooms, bathrooms, property_type, listing_status, source_format)
            VALUES ({ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph}, {ph})""",
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
    """Get all listings with their scores."""
    with get_connection() as conn:
        cur = conn.cursor()
        cur.execute("""
            SELECT l.*, s.score, s.verdict, s.concerns_json, s.confidence
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
            f"""SELECT l.*, s.score, s.verdict, s.hard_results_json, s.concerns_json, s.confidence
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
