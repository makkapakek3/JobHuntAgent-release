"""V2 business-store tests: temporary files, DEMO jobs, no network."""
from contextlib import closing
from datetime import timedelta
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest

from jobhunt.config import UserError
from jobhunt.v2.models import ApplicationStatus, Job, SourcePolicy
from jobhunt.v2.storage import BusinessStore, DATABASE_ID, SCHEMA_VERSION


def demo_job(**changes):
    values = dict(source="demo", external_id="p0-1", company="DEMO 公司",
                  title="DEMO Python 工程师", jd="DEMO Python SQLite",
                  url="https://example.invalid/jobs/p0-1", is_demo=True)
    return Job(**(values | changes))


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "business.sqlite3"

    def tearDown(self):
        self.tmp.cleanup()

    def store(self, demo=True):
        return BusinessStore(self.path, demo=demo)

    def test_initialize_reopen_preserves_job_task_and_audit(self):
        candidate = demo_job()
        with closing(self.store()) as store:
            identifier = store.create_application(candidate)
        with closing(self.store()) as store:
            row = store.get_application(identifier)
            self.assertEqual(row["status"], "DISCOVERED")
            self.assertEqual(row["workflow_stage"], "CREATED")
            self.assertEqual(row["job"], candidate.model_dump(mode="json"))
            self.assertEqual(row["source_policy"]["allowed_operations"], ["import"])
            self.assertEqual(store.events(identifier)[0]["event"], "TASK_CREATED")
            tables = {r[0] for r in store.conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({"jobs", "job_matches", "applications", "approval_records", "execution_logs"} <= tables)
            self.assertNotIn("checkpoints", tables)
            self.assertEqual(store.conn.execute("PRAGMA user_version").fetchone()[0], SCHEMA_VERSION)

    def test_duplicate_source_id_is_blocked_even_if_url_changed(self):
        with closing(self.store()) as store:
            identifier = store.create_application(demo_job())
            with self.assertRaisesRegex(UserError, "DUPLICATE_APPLICATION"):
                store.create_application(demo_job(url="https://example.invalid/changed"))
            self.assertEqual(len(store.list_applications()), 1)
            self.assertEqual(len(store.events(identifier)), 1)

    def test_duplicate_canonical_url_across_sources_is_blocked(self):
        with closing(self.store()) as store:
            store.create_application(demo_job())
            with self.assertRaisesRegex(UserError, "DUPLICATE_APPLICATION"):
                store.create_application(demo_job(source="other", external_id="x",
                                                  url="https://example.invalid/jobs/p0-1?utm_source=test"))

    def test_missing_source_id_uses_url_for_duplicate_detection(self):
        with closing(self.store()) as store:
            store.create_application(demo_job(external_id=""))
            with self.assertRaisesRegex(UserError, "DUPLICATE_APPLICATION"):
                store.create_application(demo_job(external_id=""))

    def test_namespace_mismatch_rejected_before_insertion(self):
        with closing(self.store()) as store:
            with self.assertRaisesRegex(UserError, "MODE_MISMATCH"):
                store.create_application(demo_job(is_demo=False))
            self.assertEqual(store.list_applications(), [])
        with self.assertRaisesRegex(UserError, "MODE_MISMATCH"):
            self.store(demo=False)

    def test_newer_schema_is_rejected_without_modifying_database(self):
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute(f"PRAGMA application_id={DATABASE_ID}")
            conn.execute("PRAGMA user_version=999")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(UserError, "SCHEMA_TOO_NEW"):
            self.store()
        self.assertEqual(self.path.read_bytes(), before)

    def test_foreign_database_is_not_claimed_or_migrated(self):
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute("CREATE TABLE old_jobs(value TEXT)")
        before = self.path.read_bytes()
        with self.assertRaisesRegex(UserError, "FOREIGN_DATABASE"):
            self.store()
        self.assertEqual(self.path.read_bytes(), before)

    def test_failed_transaction_does_not_leave_orphan_job(self):
        with closing(self.store()) as store:
            store.conn.execute("""CREATE TRIGGER fail_application BEFORE INSERT ON applications
                BEGIN SELECT RAISE(ABORT, 'test'); END""")
            with self.assertRaises(sqlite3.IntegrityError):
                store.create_application(demo_job())
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM jobs").fetchone()[0], 0)
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM execution_logs").fetchone()[0], 0)

    def test_replayed_local_effects_are_idempotent_and_not_approval(self):
        with closing(self.store()) as store:
            identifier = store.create_application(demo_job())
            store.initialize_local(identifier)
            store.initialize_local(identifier)
            store.finish_local(identifier)
            store.finish_local(identifier)
            self.assertEqual(store.get_application(identifier)["status"], "DISCOVERED")
            self.assertEqual(store.get_application(identifier)["workflow_stage"], "COMPLETE")
            self.assertEqual([r["event"] for r in store.events(identifier)],
                             ["TASK_CREATED", "LOCAL_INITIALIZED", "LOCAL_COMPLETED"])
            self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM approval_records").fetchone()[0], 0)

    def test_logs_never_contain_job_body_or_arbitrary_error_text(self):
        with closing(self.store()) as store:
            identifier = store.create_application(demo_job(jd="password=secret-marker applicant@example.invalid"))
            store.record_error(identifier, RuntimeError("secret-marker"))
            logs = json.dumps(store.events(identifier))
            self.assertNotIn("secret-marker", logs)
            self.assertNotIn("applicant@", logs)
            self.assertIn("RuntimeError", logs)

    def test_unknown_task_and_invalid_stage_fail_explicitly(self):
        with closing(self.store()) as store:
            with self.assertRaisesRegex(UserError, "APPLICATION_NOT_FOUND"):
                store.get_application("missing")
            identifier = store.create_application(demo_job())
            with self.assertRaisesRegex(UserError, "INVALID_LOCAL_STAGE"):
                store.finish_local(identifier)

    def test_incompatible_workflow_and_business_status_cannot_resume(self):
        with closing(self.store()) as store:
            identifier = store.create_application(demo_job())
            with store.conn:
                store.conn.execute("UPDATE applications SET workflow_version=999 WHERE application_id=?", (identifier,))
            with self.assertRaisesRegex(UserError, "WORKFLOW_VERSION_UNSUPPORTED"):
                store.initialize_local(identifier)
            with store.conn:
                store.conn.execute("UPDATE applications SET workflow_version=1, status='UNKNOWN' WHERE application_id=?", (identifier,))
            with self.assertRaisesRegex(UserError, "LOCAL_ONLY_STATE_REQUIRED"):
                store.initialize_local(identifier)

    def test_missing_job_fields_remain_unknown_and_policy_defaults_local(self):
        candidate = demo_job()
        self.assertIsNone(candidate.experience_years)
        self.assertEqual(candidate.minimum_degree, "")
        self.assertEqual(candidate.salary_currency, "")
        self.assertEqual(candidate.salary_period, "")
        self.assertIsNotNone(candidate.fetched_at.tzinfo)
        policy = SourcePolicy(source="demo")
        self.assertEqual(policy.allowed_operations, ("import",))
        self.assertNotIn("submit", policy.allowed_operations)
        self.assertEqual(len(ApplicationStatus), 11)

    def test_collection_time_does_not_change_inherited_content_hashes(self):
        first = demo_job()
        second = demo_job(fetched_at=first.fetched_at + timedelta(hours=1))
        self.assertEqual(first.jd_hash, second.jd_hash)
        self.assertEqual(first.content_hash, second.content_hash)
        changed = demo_job(jd="DEMO changed requirement")
        self.assertNotEqual(first.jd_hash, changed.jd_hash)
        self.assertNotEqual(first.content_hash, changed.content_hash)


if __name__ == "__main__":
    unittest.main()
