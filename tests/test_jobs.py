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

    def test_score_unblocked_after_sibling_first_attempt(self, temp_db):
        """Score waits only for enrichment to be *tried* once — not for retries
        to be exhausted. Blocking until exhaustion delayed the first score by
        hours (one retry per hourly drain) when a scrape kept failing.
        """
        lid = _make_listing()
        db.enqueue_jobs(lid, ["commute", "score"])
        # First claim: only commute (score deferred — commute never attempted)
        claimed = db.claim_pending_jobs()
        assert [j["task_type"] for j in claimed] == ["commute"]
        db.fail_job(claimed[0]["id"], "boom")
        # Commute has now been tried once and is pending a retry, but score
        # no longer has to wait for it
        claimed = db.claim_pending_jobs()
        assert "score" in [j["task_type"] for j in claimed]

    def test_score_still_waits_for_untried_enrichment(self, temp_db):
        """A brand-new listing scores only after each enrichment gets a turn,
        so the first score isn't computed on empty data."""
        lid = _make_listing()
        db.enqueue_jobs(lid, ["commute", "schools", "score"])
        claimed = db.claim_pending_jobs()
        assert set(j["task_type"] for j in claimed) == {"commute", "schools"}
        assert "score" not in [j["task_type"] for j in claimed]

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


class TestDeterministicGate:
    def test_commute_over_limit_rejects(self):
        result = deterministic_gate({"commute_minutes": 111})
        assert result is not None
        assert result.verdict == "Reject"
        assert result.score == 0
        assert result.evaluation_method == "deterministic-gate"

    def test_commute_at_limit_rejects(self):
        """The cutoff is inclusive: 110 min itself is out (user decision 2026-07-12)."""
        result = deterministic_gate({"commute_minutes": 110})
        assert result is not None
        assert result.verdict == "Reject"

    def test_commute_below_limit_passes(self):
        assert deterministic_gate({"commute_minutes": 109}) is None

    def test_unknown_commute_never_gates(self):
        assert deterministic_gate({}) is None
        assert deterministic_gate({"commute_minutes": None}) is None


class TestHighScoreSweep:
    """The drain's sweep alerts each ≥threshold listing exactly once."""

    def _make_scored(self, score, status=None, notified=False):
        email_id = db.save_processed_email(
            gmail_id=f"hs-{score}-{status}-{db.get_all_listing_ids().__len__()}",
            message_id="", sender="test", subject="t", parser_used="test", listings_found=1,
        )
        listing = ParsedListing(source_format="onehome_html", address=f"{score} Sweep St",
                                town="Katonah", state="NY", listing_status=status)
        lid = db.save_listing(listing, ScoringResult(score=score, verdict="Worth Touring" if score>=60 else "Reject"), email_id)
        if notified:
            with db.get_connection() as conn:
                conn.cursor().execute(f"UPDATE listings SET notified = TRUE WHERE id = {db._placeholder()}", (lid,))
        return lid

    def test_claim_returns_high_scores_once(self, temp_db):
        self._make_scored(72)
        self._make_scored(85)
        self._make_scored(50)  # below threshold
        rows = db.claim_unnotified_high_scores(70)
        assert {r["score"] for r in rows} == {72, 85}
        # Second claim returns nothing — already marked notified
        assert db.claim_unnotified_high_scores(70) == []

    def test_offmarket_high_scores_not_alerted(self, temp_db):
        self._make_scored(80, status="Pending")
        self._make_scored(80, status="Sold?")
        assert db.claim_unnotified_high_scores(70) == []

    def test_sweep_sends_via_notifier(self, temp_db, monkeypatch):
        self._make_scored(75)
        sent = []
        monkeypatch.setattr("app.notifier.send_high_score_alert",
                            lambda l, s, v: sent.append((l["address"], s)))
        jobs._notify_high_scores()
        assert len(sent) == 1 and sent[0][1] == 75
        # Idempotent — no re-alert on the next sweep
        jobs._notify_high_scores()
        assert len(sent) == 1


class TestTheAlertLogAnswersWhatWasSent:
    """Aki asked whether a week of ntfy alerts was real and there was nothing to
    read. `notified` is a latch: no timestamp, no score, no channel, cleared and
    re-set as scores move — and the scores table upserts one row per listing, so
    score history is overwritten too. The only record was Fly's log retention.

    This table is the answer to that question next time.
    """

    def _make_scored(self, score, status="Active"):
        email_id = db.save_processed_email(
            gmail_id=f"al-{score}-{status}-{len(db.get_all_listing_ids())}",
            message_id="", sender="test", subject="t", parser_used="test", listings_found=1,
        )
        listing = ParsedListing(source_format="onehome_html", address=f"{score} Log St",
                                town="Katonah", state="NY", listing_status=status)
        return db.save_listing(
            listing, ScoringResult(score=score, verdict="Worth Touring"), email_id,
        )

    def test_one_row_per_channel(self, temp_db):
        lid = self._make_scored(80)
        db.log_alert({"id": lid, "listing_status": "Active"}, 80, "Worth Touring",
                     {"ntfy": True, "slack": False})
        rows = db.get_recent_alerts()
        assert {(r["channel"], bool(r["delivered"])) for r in rows} == {("ntfy", True), ("slack", False)}

    def test_a_rejected_send_is_logged_not_hidden(self, temp_db):
        """A publish ntfy refused is exactly what you want to be able to see."""
        lid = self._make_scored(80)
        db.log_alert({"id": lid}, 80, "Worth Touring", {"ntfy": False})
        assert bool(db.get_recent_alerts()[0]["delivered"]) is False

    def test_the_address_comes_back_with_it(self, temp_db):
        lid = self._make_scored(80)
        db.log_alert({"id": lid}, 80, "Worth Touring", {"ntfy": True})
        assert db.get_recent_alerts()[0]["address"] == "80 Log St"

    def test_logging_never_breaks_a_send(self, temp_db, monkeypatch):
        """An unlogged alert is bad; an unsent one is worse."""
        def boom(*a, **k):
            raise RuntimeError("db gone")
        monkeypatch.setattr(db, "get_connection", boom)
        db.log_alert({"id": 1}, 80, "Worth Touring", {"ntfy": True})  # must not raise

    def test_empty_delivery_still_records_the_attempt(self, temp_db):
        """No channel configured is itself worth knowing about."""
        lid = self._make_scored(80)
        db.log_alert({"id": lid}, 80, "Worth Touring", {})
        assert db.get_recent_alerts()[0]["channel"] == "none"

    def test_the_sweep_logs_what_it_sends(self, temp_db, monkeypatch):
        self._make_scored(80)
        monkeypatch.setattr("app.notifier.send_high_score_alert",
                            lambda listing, s, v: {"ntfy": True})
        jobs._notify_high_scores()
        rows = db.get_recent_alerts()
        assert len(rows) == 1
        assert rows[0]["score"] == 80 and rows[0]["reason"] == "first_time"

    def test_a_repeat_is_labelled_as_one(self, temp_db, monkeypatch):
        """The distinction the v75 burst needed: a repeat alert and a new one
        looked identical in the logs."""
        lid = self._make_scored(80)
        monkeypatch.setattr("app.notifier.send_high_score_alert",
                            lambda listing, s, v: {"ntfy": True})
        jobs._notify_high_scores()
        # Re-arm the latch by hand (a real collapse-and-recover) and sweep again
        with db.get_connection() as conn:
            conn.cursor().execute(
                f"UPDATE listings SET notified = FALSE WHERE id = {db._placeholder()}", (lid,))
        jobs._notify_high_scores()
        assert [r["reason"] for r in db.get_recent_alerts()] == ["re_armed", "first_time"]

    def test_claim_stamps_notified_at(self, temp_db):
        lid = self._make_scored(80)
        db.claim_unnotified_high_scores(70)
        listing = db.get_listing_by_id(lid)
        assert listing["notified_at"] and listing["notified_at"].startswith("20")

    def test_counts_split_first_time_from_repeat(self, temp_db):
        lid = self._make_scored(80)
        db.log_alert({"id": lid, "alert_reason": "first_time"}, 80, "V", {"ntfy": True})
        db.log_alert({"id": lid, "alert_reason": "re_armed"}, 80, "V", {"ntfy": True})
        out = db.count_alerts_since("1970-01-01T00:00:00+00:00")
        assert out["counts"] == {"first_time": 1, "re_armed": 1}
        assert out["total"] == 2 and out["last_alert_at"]

    def test_counts_respect_the_window(self, temp_db):
        lid = self._make_scored(80)
        db.log_alert({"id": lid}, 80, "V", {"ntfy": True})
        assert db.count_alerts_since("2999-01-01T00:00:00+00:00")["total"] == 0


class TestPreLogAlertsAreMarkedNotInvented:
    """24 listings were already alerted before the log existed. Their real send
    times are unrecoverable, so they get a 1970 sentinel rather than a plausible
    made-up date — its only job is to make the next alert for them read as a
    repeat instead of a first-time push.
    """

    def test_previously_notified_rows_get_the_sentinel(self, temp_db):
        lid = _make_listing(address="9 Sentinel St")
        with db.get_connection() as conn:
            conn.cursor().execute(
                f"UPDATE listings SET notified = TRUE, notified_at = NULL "
                f"WHERE id = {db._placeholder()}", (lid,))
        db.set_app_state("notified_at_backfill_done", "")
        db._backfill_notified_at()
        assert db.get_listing_by_id(lid)["notified_at"] == db.NOTIFIED_AT_UNKNOWN

    def test_the_sentinel_makes_the_next_alert_a_repeat(self, temp_db):
        lid = _make_listing(address="10 Sentinel St", listing_status="Active")
        db.update_score(lid, ScoringResult(score=80, verdict="Worth Touring"), "ai", 75, None)
        with db.get_connection() as conn:
            conn.cursor().execute(
                f"UPDATE listings SET notified_at = {db._placeholder()} "
                f"WHERE id = {db._placeholder()}", (db.NOTIFIED_AT_UNKNOWN, lid))
        rows = db.claim_unnotified_high_scores(70)
        assert [r["alert_reason"] for r in rows] == ["re_armed"]

    def test_it_runs_once(self, temp_db):
        assert db.get_app_state("notified_at_backfill_done") == "1"
