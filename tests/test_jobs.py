"""Tests for the persistent job queue (app/jobs.py + jobs table in app/db.py)."""

import pytest

from app import db, jobs
from app.config import settings
from app.models import ParsedListing, ScoringResult
from app.scorer import deterministic_gate


@pytest.fixture
def temp_db(tmp_path, monkeypatch):
    """Point the app at a fresh temp SQLite DB."""
    db_file = tmp_path / "test.db"
    monkeypatch.setattr(settings, "database_url", f"sqlite:///{db_file}")
    db.init_db()
    yield db_file


def _make_listing(**overrides) -> int:
    """Insert a bare listing and return its ID."""
    fields = dict(
        source_format="test",
        address="1 Test St",
        town="Testville",
        state="NY",
        zip_code="10000",
    )
    fields.update(overrides)
    listing = ParsedListing(**fields)
    placeholder = ScoringResult(score=0, verdict="Reject", concerns=["Pending enrichment"])
    email_id = db.save_processed_email(
        gmail_id=f"test-{fields['address']}", message_id="", sender="test",
        subject="test", parser_used="test", listings_found=1,
    )
    return db.save_listing(listing, placeholder, email_id)


class TestEnqueue:
    def test_enqueue_is_idempotent(self, temp_db):
        lid = _make_listing()
        db.enqueue_jobs(lid, ["commute", "schools"])
        db.enqueue_jobs(lid, ["commute", "schools"])
        counts = db.job_counts()
        assert counts["by_status"] == {"pending": 2}

    def test_force_requeues_failed_jobs(self, temp_db):
        lid = _make_listing()
        db.enqueue_jobs(lid, ["commute"])
        # Exhaust attempts
        for _ in range(db.JOB_MAX_ATTEMPTS):
            claimed = db.claim_pending_jobs()
            assert claimed
            db.fail_job(claimed[0]["id"], "boom")
        assert db.job_counts()["by_status"] == {"failed": 1}
        # Plain enqueue leaves it failed; force resets it
        db.enqueue_jobs(lid, ["commute"])
        assert db.job_counts()["by_status"] == {"failed": 1}
        db.enqueue_jobs(lid, ["commute"], force=True)
        assert db.job_counts()["by_status"] == {"pending": 1}


class TestClaim:
    def test_claim_marks_running_and_counts_attempt(self, temp_db):
        lid = _make_listing()
        db.enqueue_jobs(lid, ["commute"])
        claimed = db.claim_pending_jobs()
        assert len(claimed) == 1
        assert claimed[0]["task_type"] == "commute"
        counts = db.job_counts()
        assert counts["by_status"] == {"running": 1}
        # Nothing left to claim
        assert db.claim_pending_jobs() == []

    def test_score_deferred_until_enrichment_settles(self, temp_db):
        lid = _make_listing()
        db.enqueue_jobs(lid, ["commute", "score"])
        claimed = db.claim_pending_jobs(task_order=jobs.TASK_ORDER)
        assert [j["task_type"] for j in claimed] == ["commute"]
        db.complete_job(claimed[0]["id"])
        claimed = db.claim_pending_jobs(task_order=jobs.TASK_ORDER)
        assert [j["task_type"] for j in claimed] == ["score"]

    def test_score_unblocked_when_sibling_exhausts_attempts(self, temp_db):
        lid = _make_listing()
        db.enqueue_jobs(lid, ["commute", "score"])
        for _ in range(db.JOB_MAX_ATTEMPTS):
            claimed = db.claim_pending_jobs()
            assert [j["task_type"] for j in claimed] == ["commute"]
            db.fail_job(claimed[0]["id"], "boom")
        # Commute is terminally failed — score may now proceed
        claimed = db.claim_pending_jobs()
        assert [j["task_type"] for j in claimed] == ["score"]

    def test_score_not_starved_by_other_listings_jobs(self, temp_db):
        lid_a = _make_listing(address="1 A St")
        lid_b = _make_listing(address="2 B St")
        db.enqueue_jobs(lid_a, ["score"])
        db.enqueue_jobs(lid_b, ["commute"])
        claimed = db.claim_pending_jobs()
        assert {(j["listing_id"], j["task_type"]) for j in claimed} == {
            (lid_a, "score"), (lid_b, "commute"),
        }


class TestRetry:
    def test_fail_returns_to_pending_then_failed(self, temp_db):
        lid = _make_listing()
        db.enqueue_jobs(lid, ["commute"])
        for attempt in range(1, db.JOB_MAX_ATTEMPTS + 1):
            claimed = db.claim_pending_jobs()
            assert claimed, f"attempt {attempt} should be claimable"
            db.fail_job(claimed[0]["id"], f"error {attempt}")
        counts = db.job_counts()
        assert counts["by_status"] == {"failed": 1}

    def test_reset_running_jobs_requeues_orphans(self, temp_db):
        lid = _make_listing()
        db.enqueue_jobs(lid, ["commute"])
        db.claim_pending_jobs()
        assert db.job_counts()["by_status"] == {"running": 1}
        reset = db.reset_running_jobs()
        assert reset == 1
        assert db.job_counts()["by_status"] == {"pending": 1}

    def test_orphan_on_final_attempt_goes_to_failed_not_zombie_pending(self, temp_db):
        """A job interrupted mid-run on its last attempt must land in 'failed' —
        a pending row at max attempts would be unclaimable forever."""
        lid = _make_listing()
        db.enqueue_jobs(lid, ["commute"])
        for _ in range(db.JOB_MAX_ATTEMPTS - 1):
            claimed = db.claim_pending_jobs()
            db.fail_job(claimed[0]["id"], "boom")
        db.claim_pending_jobs()  # final attempt, now 'running' at max attempts
        db.reset_running_jobs()  # simulated crash/deploy
        assert db.job_counts()["by_status"] == {"failed": 1}


class TestDrain:
    def test_drain_processes_and_completes(self, temp_db, monkeypatch):
        lid = _make_listing()
        db.enqueue_jobs(lid, ["commute", "score"])
        ran = []
        monkeypatch.setattr(jobs, "_HANDLERS", {
            "commute": lambda listing: ran.append("commute"),
            "score": lambda listing: ran.append("score"),
        })
        result = jobs.drain()
        assert result == {"processed": 2, "failed": 0}
        assert ran == ["commute", "score"]
        assert db.job_counts()["by_status"] == {"done": 2}

    def test_drain_retries_on_later_drains_not_immediately(self, temp_db, monkeypatch):
        lid = _make_listing()
        db.enqueue_jobs(lid, ["commute"])

        def boom(listing):
            raise RuntimeError("no route found")

        monkeypatch.setattr(jobs, "_HANDLERS", {"commute": boom})
        # One attempt per drain — transient failures aren't burned back-to-back
        for expected_status in ("pending", "pending", "failed"):
            result = jobs.drain()
            assert result == {"processed": 0, "failed": 1}
            assert db.job_counts()["by_status"] == {expected_status: 1}
        # Attempts exhausted — nothing left to claim
        assert jobs.drain() == {"processed": 0, "failed": 0}

    def test_drain_skips_deleted_listings(self, temp_db, monkeypatch):
        lid = _make_listing()
        db.enqueue_jobs(lid, ["commute"])
        with db.get_connection() as conn:
            conn.cursor().execute("DELETE FROM listings WHERE id = ?", (lid,))
        result = jobs.drain()
        assert result == {"processed": 1, "failed": 0}


class TestEnqueueMissing:
    def test_detects_gaps(self, temp_db):
        # Listing missing everything enrichable
        lid = _make_listing(listing_url="https://www.redfin.com/NY/T/1-Test-St/home/1")
        counts = jobs.enqueue_missing()
        assert counts["scrape_desc"] == 1
        assert counts["stats"] == 1
        assert counts["commute"] == 1
        assert counts["schools"] == 1
        assert counts["score"] == 1
        pending = db.job_counts()["by_status"]["pending"]
        assert pending == 5

    def test_gap_scan_resurrects_done_jobs_while_gap_persists(self, temp_db):
        """A done job must not block repair when its data gap still exists."""
        lid = _make_listing(listing_url="https://www.redfin.com/NY/T/1-Test-St/home/1")
        jobs.enqueue_missing()
        # Simulate every job running but producing nothing (done, gap remains).
        # Two claim rounds: 'score' is deferred until its siblings settle.
        for _ in range(2):
            for job in db.claim_pending_jobs(limit=50):
                db.complete_job(job["id"])
        assert db.job_counts()["by_status"] == {"done": 5}
        counts = jobs.enqueue_missing()
        assert counts["scrape_desc"] == 1
        assert db.job_counts()["by_status"]["pending"] == 5

    def test_gap_scan_gives_failed_jobs_one_attempt_per_scan(self, temp_db):
        lid = _make_listing(listing_url="https://www.redfin.com/NY/T/1-Test-St/home/1")
        db.enqueue_jobs(lid, ["commute"])
        for _ in range(db.JOB_MAX_ATTEMPTS):
            claimed = db.claim_pending_jobs()
            db.fail_job(claimed[0]["id"], "boom")
        assert db.job_counts()["by_task"]["commute"] == {"failed": 1}
        jobs.enqueue_missing()
        # Resurrected with exactly one attempt left
        assert db.job_counts()["by_task"]["commute"] == {"pending": 1}
        claimed = [j for j in db.claim_pending_jobs(limit=50) if j["task_type"] == "commute"]
        assert len(claimed) == 1
        db.fail_job(claimed[0]["id"], "boom again")
        assert db.job_counts()["by_task"]["commute"] == {"failed": 1}

    def test_gap_scan_skips_score_during_active_rescore(self, temp_db, monkeypatch):
        _make_listing(listing_url="https://www.redfin.com/NY/T/1-Test-St/home/1")
        monkeypatch.setitem(db.rescore_state, "in_progress", True)
        counts = jobs.enqueue_missing()
        assert counts["score"] == 0
        assert counts["scrape_desc"] == 1  # enrichment still enqueued

    def test_gap_scan_deletes_orphan_jobs(self, temp_db):
        lid = _make_listing()
        db.enqueue_jobs(lid, ["commute"])
        with db.get_connection() as conn:
            conn.cursor().execute("DELETE FROM listings WHERE id = ?", (lid,))
        jobs.enqueue_missing()
        assert db.job_counts()["by_status"] == {}

    def test_complete_listing_gets_no_jobs(self, temp_db):
        lid = _make_listing(
            price=1_000_000, sqft=3000, bedrooms=4, bathrooms=3, year_built=1990,
            description="A lovely home",
        )
        db.add_listing_images(lid, ["https://example.com/1.jpg"])
        db.update_listing_enrichment(lid, {
            "commute_minutes": 75,
            "school_data_json": '{"high": []}',
        })
        criteria_version = db.save_criteria("test criteria", created_by="test")
        score = ScoringResult(score=80, verdict="Worth Touring", evaluation_method="ai")
        db.update_score(
            listing_id=lid, score=score, method="ai",
            criteria_version=criteria_version, reasoning="fine",
        )
        counts = jobs.enqueue_missing()
        assert all(v == 0 for v in counts.values()), counts


class TestScoreNotification:
    def _score_listing(self, lid, monkeypatch):
        from unittest.mock import MagicMock
        scored = MagicMock()
        scored.score, scored.verdict, scored.evaluation_method = 80, "Worth Touring", "ai"
        rescore = MagicMock(return_value=scored)
        notify = MagicMock()
        monkeypatch.setattr("app.main._rescore_one_listing", rescore)
        monkeypatch.setattr("app.notifier.notify_new_listing", notify)
        db.save_criteria("test criteria", created_by="test")
        jobs._handle_score(db.get_listing_by_id(lid))
        return notify

    def test_manual_add_first_score_notifies(self, temp_db, monkeypatch):
        lid = _make_listing(source_format="manual")
        notify = self._score_listing(lid, monkeypatch)
        notify.assert_called_once()

    def test_csv_import_never_notifies(self, temp_db, monkeypatch):
        """The old import path scored without notifying — a 59-row CSV must not
        burst-send Slack messages."""
        lid = _make_listing(source_format="redfin-csv")
        notify = self._score_listing(lid, monkeypatch)
        notify.assert_not_called()

    def test_search_sync_find_notifies(self, temp_db, monkeypatch):
        """Weekly-search discoveries notify on first scoring — hearing about
        new matches is the point of the sync."""
        lid = _make_listing(source_format="redfin-sync")
        notify = self._score_listing(lid, monkeypatch)
        notify.assert_called_once()


class TestDeterministicGate:
    def test_commute_over_limit_rejects(self):
        result = deterministic_gate({"commute_minutes": 111})
        assert result is not None
        assert result.verdict == "Reject"
        assert result.score == 0
        assert result.evaluation_method == "deterministic-gate"

    def test_commute_at_limit_passes(self):
        assert deterministic_gate({"commute_minutes": 110}) is None

    def test_unknown_commute_never_gates(self):
        assert deterministic_gate({}) is None
        assert deterministic_gate({"commute_minutes": None}) is None
