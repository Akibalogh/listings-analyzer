"""FastAPI app for the Listings Analyzer."""

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException

from app import db
from app.poller import poll_once

logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    logger.info("Database initialized")
    yield


app = FastAPI(
    title="Listings Analyzer",
    description="Paste listing alert text → extract data → score against goals",
    version="0.1.0",
    lifespan=lifespan,
)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/poll")
def trigger_poll():
    """Trigger a Gmail poll cycle. Returns processed listings."""
    results = poll_once()
    return {
        "listings_processed": len(results),
        "results": results,
    }


@app.get("/listings")
def list_listings():
    """Get all scored listings."""
    listings = db.get_all_listings()
    return {"count": len(listings), "listings": listings}


@app.get("/listings/{mls_id}")
def get_listing(mls_id: str):
    """Get a single listing by MLS ID."""
    listing = db.get_listing_by_mls(mls_id)
    if not listing:
        raise HTTPException(status_code=404, detail=f"Listing MLS #{mls_id} not found")
    return listing
