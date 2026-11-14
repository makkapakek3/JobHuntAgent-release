"""CLI acceptance tests: real processes and persistent SQLite, no network."""
import json
from contextlib import contextmanager
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONUTF8": "1",
                    "LANGSMITH_TRACING": "false", "LANGCHAIN_TRACING_V2": "false"}

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args, ok=True, demo=True):
        argv = [sys.executable, "-m", "jobhunt.cli", "--home", str(self.home)]
        if demo:
            argv.append("--demo")
        result = subprocess.run(argv + list(args), env=self.env, capture_output=True,
                                encoding="utf-8", timeout=30)
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout)
        return result.stdout + result.stderr

    @contextmanager
    def db(self):
        conn = sqlite3.connect(self.home / "data" / "demo.sqlite3")
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def pending(self):
        with self.db() as db:
            return [r[0] for r in db.execute("SELECT job_id FROM jobs WHERE status='PENDING_REVIEW' ORDER BY job_id")]

    def test_offline_round_trip_and_incremental_run(self):
        self.cli("run-nightly", "--dry-run")
        report = self.cli("report", "latest")
        self.assertIn("DEMO", report)
        self.assertIn("LangGraph", report)
        self.assertTrue(self.pending())
        with self.db() as db:
            count = db.execute("SELECT count(*) FROM jobs").fetchone()[0]
            self.assertGreaterEqual(count, 10)
            self.assertEqual(db.execute("SELECT sum(input_tokens+output_tokens) FROM llm_usage").fetchone()[0], 0)
        self.cli("run-nightly")
        with self.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM jobs").fetchone()[0], count)
            stats = json.loads(db.execute("SELECT stats FROM run_history ORDER BY rowid DESC LIMIT 1").fetchone()[0])
            self.assertEqual(stats["new"], 0)
            self.assertEqual(stats["analyzed"], 0)
        self.assertIn("新增：0", self.cli("report", "latest"))

    def test_approval_dry_run_and_manual_result_are_separate(self):
        self.cli("run-nightly")
        job = self.pending()[0]
        self.cli("approve", job)
        self.cli("approve", job)
        with self.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM application_requests").fetchone()[0], 1)
        self.cli("apply", "--approved-only", "--dry-run")
        with self.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM application_attempts").fetchone()[0], 0)
        self.cli("apply", "--approved-only", "--execute", ok=False)
        self.cli("apply", "--approved-only", "--prepare-manual")
        self.cli("apply", "--approved-only", "--prepare-manual")
        with self.db() as db:
            self.assertEqual(db.execute("SELECT status FROM jobs WHERE job_id=?", (job,)).fetchone()[0], "MANUAL_ACTION_REQUIRED")
            self.assertEqual(db.execute("SELECT count(*) FROM application_attempts").fetchone()[0], 1)
        self.cli("confirm", job, "--result", "applied", "--evidence", "DEMO 用户确认本地模拟结果")
        self.cli("confirm", job, "--result", "applied", "--evidence", "DEMO 用户确认本地模拟结果")
        self.cli("approve", job, ok=False)
        with self.db() as db:
            self.assertEqual(db.execute("SELECT status FROM jobs WHERE job_id=?", (job,)).fetchone()[0], "APPLIED")

    def test_invalid_batch_approval_rolls_back(self):
        self.cli("run-nightly")
        job = self.pending()[0]
        self.cli("approve", job, "JOB-NOT-EXIST", ok=False)
        with self.db() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM application_requests").fetchone()[0], 0)

    def test_expired_approval_does_not_prepare_package(self):
        self.cli("run-nightly")
        job = self.pending()[0]
        self.cli("approve", job)
        with self.db() as db:
            db.execute("UPDATE application_requests SET expires_at='2000-01-01T00:00:00+00:00'")
        output = self.cli("apply", "--approved-only", "--prepare-manual")
        self.assertIn("APPROVAL_EXPIRED", output)
        self.assertFalse(list((self.home / "reports" / "demo" / "manual").glob("*.md")))

    def test_review_actions_persist_after_restart(self):
        self.cli("run-nightly")
        jobs = self.pending()
        self.cli("favorite", jobs[0])
        self.cli("defer", jobs[0])
        self.cli("reject", jobs[1], "--reason", "不考虑")
        self.cli("run-nightly")
        with self.db() as db:
            self.assertEqual(db.execute("SELECT status,favorite FROM jobs WHERE job_id=?", (jobs[0],)).fetchone()[:], ("DEFERRED", 1))
            self.assertEqual(db.execute("SELECT status FROM jobs WHERE job_id=?", (jobs[1],)).fetchone()[0], "REJECTED")

    def test_missing_real_profile_fails_without_leaking_secrets(self):
        result = self.cli("doctor", ok=False, demo=False)
        self.assertIn("PROFILE_MISSING", result)
        self.assertNotIn("Traceback", result)

    def test_demo_flag_works_after_jobs_subcommand(self):
        self.cli("run-nightly")
        result = self.cli("jobs", "list", "--status", "pending_review", "--demo", demo=False)
        self.assertIn("JOB-", result)

    def test_failed_manual_retry_keeps_current_status_and_audit(self):
        self.cli("run-nightly")
        job = self.pending()[0]
        self.cli("approve", job)
        self.cli("apply", "--approved-only", "--prepare-manual")
        self.cli("confirm", job, "--result", "failed", "--evidence", "DEMO 用户模拟失败")
        self.cli("run-nightly")
        with self.db() as db:
            self.assertEqual(db.execute("SELECT status FROM jobs WHERE job_id=?", (job,)).fetchone()[0], "FAILED")
        self.cli("approve", job)
        self.cli("apply", "--approved-only", "--prepare-manual")
        with self.db() as db:
            self.assertEqual(db.execute("SELECT status FROM application_attempts").fetchone()[0], "MANUAL_ACTION_REQUIRED")
            self.assertEqual(db.execute("SELECT count(*) FROM audit_events WHERE action='USER_CONFIRMED_FAILED'").fetchone()[0], 1)


if __name__ == "__main__":
    unittest.main()
