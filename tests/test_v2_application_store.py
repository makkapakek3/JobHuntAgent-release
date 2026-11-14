"""P3 approval and receipt persistence: fictional data and isolated SQLite only."""
from contextlib import closing
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
from threading import Barrier
import unittest

from jobhunt.config import UserError
from jobhunt.models import digest
from jobhunt.v2.models import Job, SourcePolicy
from jobhunt.v2.storage import BusinessStore


APP = "APP-" + "a" * 32


def demo_job(**changes):
    return Job(**(dict(source="demo", external_id="p3-1", company="DEMO Company",
                      title="DEMO Python Engineer", jd="DEMO Python SQLite",
                      url="https://example.invalid/jobs/p3-1", is_demo=True) | changes))


def package_for(job_id, job, application_id=APP):
    return dict(scope="LOCAL_DEMO", job=job.model_dump(mode="json"), job_id=job_id,
                job_hash=job.jd_hash, profile_version="profile-v1", resume_version="resume-v1",
                entry=f"http://127.0.0.1:8765/application/{application_id}",
                fields=dict(name="Demo Applicant", email="demo@example.invalid",
                            summary="I used Python.", resume_text="DEMO resume: Python"),
                evidence=["profile.skills: Python"])


class ApplicationStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "business.sqlite3"

    def store(self, demo=True):
        self.assertIsNotNone(importlib.util.find_spec("jobhunt.v2.application_store"),
                             "P3 ApplicationStore module is required")
        from jobhunt.v2.application_store import ApplicationStore
        store = ApplicationStore(self.path, demo=demo)
        self.addCleanup(store.close)
        return store

    def prepared(self, store):
        job = demo_job()
        job_id, _, _ = store.save_job(job, SourcePolicy(source="demo"))
        package = package_for(job_id, job)
        store.create_prepared(job_id, package, APP)
        return package

    def test_prepared_package_survives_reopen_with_version_and_audit(self):
        store = self.store()
        package = self.prepared(store)
        reopened = self.store()
        self.assertEqual(reopened.package(APP), package)
        self.assertEqual(reopened.status(APP), "PREPARED")
        self.assertEqual(reopened.get_application(APP)["workflow_version"], 2)
        self.assertEqual(reopened.get_application(APP)["content_digest"], digest(package))
        self.assertEqual([r["event"] for r in reopened.events(APP)],
                         ["DISCOVERED", "MATCHED", "PREPARED"])

    def test_schema_three_migration_preserves_p0_and_idempotent_logs(self):
        with closing(BusinessStore(self.path, demo=True)) as store:
            identifier = store.create_application(demo_job())
            store.initialize_local(identifier)
            # Reconstitute schema two from known synthetic rows only.
            with store.tx():
                store.conn.execute("DROP TABLE IF EXISTS application_packages")
                store.conn.execute("DROP TABLE IF EXISTS submission_receipts")
                store.conn.execute("""CREATE TABLE execution_logs_v2 (
                    log_id INTEGER PRIMARY KEY, application_id TEXT NOT NULL REFERENCES applications(application_id),
                    event TEXT NOT NULL CHECK(event IN ('TASK_CREATED','LOCAL_INITIALIZED','LOCAL_COMPLETED','LOCAL_ERROR')),
                    error_code TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                    UNIQUE(application_id,event,error_code))""")
                store.conn.execute("INSERT INTO execution_logs_v2 SELECT * FROM execution_logs")
                store.conn.execute("DROP TABLE execution_logs")
                store.conn.execute("ALTER TABLE execution_logs_v2 RENAME TO execution_logs")
                store.conn.execute("PRAGMA user_version=2")
        store = self.store()
        self.assertEqual(store.conn.execute("PRAGMA user_version").fetchone()[0], 3)
        store.finish_local(identifier)
        store.finish_local(identifier)
        self.assertEqual(store.get_application(identifier)["workflow_version"], 1)
        self.assertEqual([r["event"] for r in store.events(identifier)],
                         ["TASK_CREATED", "LOCAL_INITIALIZED", "LOCAL_COMPLETED"])

    def test_duplicate_job_or_application_id_rolls_back(self):
        store = self.store()
        package = self.prepared(store)
        other_id = "APP-" + "b" * 32
        duplicate = package | {"entry": f"http://127.0.0.1:8765/application/{other_id}"}
        with self.assertRaisesRegex(UserError, "DUPLICATE_APPLICATION"):
            store.create_prepared(package["job_id"], duplicate, other_id)
        other_job = demo_job(external_id="p3-2", url="https://example.invalid/jobs/p3-2")
        other_job_id, _, _ = store.save_job(other_job, SourcePolicy(source="demo"))
        with self.assertRaisesRegex(UserError, "DUPLICATE_APPLICATION"):
            store.create_prepared(other_job_id, package_for(other_job_id, other_job), APP)
        self.assertEqual(len(store.list_applications()), 1)

    def test_non_demo_and_mismatched_packages_are_rejected(self):
        store = self.store()
        job = demo_job()
        job_id, _, _ = store.save_job(job, SourcePolicy(source="demo"))
        valid = package_for(job_id, job)
        variants = [valid | {"scope": "LIVE"}, valid | {"job_id": "wrong"},
                    valid | {"job_hash": "wrong"}, valid | {"entry": "https://example.invalid/form"},
                    valid | {"entry": f"http://localhost:8765/application/{APP}"},
                    valid | {"entry": f"http://127.0.0.1:8765/application/{APP}?x=1"},
                    valid | {"entry": f"http://127.0.0.1:8765/application/APP-{'b' * 32}"},
                    valid | {"fields": valid["fields"] | {"secret": "extra"}},
                    valid | {"job": demo_job(is_demo=False).model_dump(mode="json")}]
        for package in variants:
            with self.subTest(package=package), self.assertRaises(UserError):
                store.create_prepared(job_id, package, APP)
        self.assertEqual(store.list_applications(), [])
        live_path = Path(self.tmp.name) / "live.sqlite3"
        from jobhunt.v2.application_store import ApplicationStore
        with closing(ApplicationStore(live_path, demo=False)) as live:
            live_job = demo_job(is_demo=False)
            live_id, _, _ = live.save_job(live_job, SourcePolicy(source="demo"))
            with self.assertRaisesRegex(UserError, "DEMO_REQUIRED"):
                live.create_prepared(live_id, package_for(live_id, live_job), APP)

    def approved(self, store):
        package = self.prepared(store)
        store.mark_filled(APP)
        store.decide(APP, "approve", digest(package))
        return package

    def test_fill_is_idempotent_and_decisions_require_current_review(self):
        store = self.store()
        package = self.prepared(store)
        with self.assertRaisesRegex(UserError, "STATE"):
            store.decide(APP, "approve", digest(package))
        store.mark_filled(APP)
        store.mark_filled(APP)
        self.assertEqual(store.status(APP), "WAITING_APPROVAL")
        self.assertEqual([r["event"] for r in store.events(APP)].count("FORM_FILLED"), 1)
        with self.assertRaisesRegex(UserError, "DIGEST"):
            store.decide(APP, "approve", "stale")
        with self.assertRaisesRegex(UserError, "DIGEST"):
            store.decide(APP, "approve", None)
        with self.assertRaises(UserError):
            store.decide(APP, "maybe", digest(package))
        store.decide(APP, "approve", digest(package))
        self.assertEqual(store.status(APP), "APPROVED")
        with self.assertRaisesRegex(UserError, "STATE"):
            store.decide(APP, "approve", digest(package))

    def test_edit_choice_waits_then_replaces_package_and_invalidates_approval(self):
        store = self.store()
        package = self.prepared(store)
        store.mark_filled(APP)
        store.decide(APP, "edit", digest(package))
        self.assertEqual(store.status(APP), "WAITING_APPROVAL")
        replacement = deepcopy(package)
        replacement["fields"]["summary"] = "Confirmed Python project responsibility."
        store.edit_package(APP, replacement)
        self.assertEqual(store.status(APP), "PREPARED")
        self.assertEqual(store.package(APP), replacement)
        store.mark_filled(APP)
        store.decide(APP, "approve", digest(replacement))
        replacement["resume_version"] = "resume-v2"
        store.edit_package(APP, replacement)
        self.assertEqual(store.status(APP), "PREPARED")
        approvals = list(store.conn.execute("SELECT * FROM approval_records"))
        self.assertTrue(all(row["invalidated_at"] for row in approvals))
        with self.assertRaises(UserError):
            store.begin_submission(APP, digest(replacement))
        events = [r["event"] for r in store.events(APP)]
        self.assertEqual(events.count("FORM_FILLED"), 2)
        self.assertEqual(events.count("PREPARED"), 3)

    def test_rejection_is_terminal_and_cannot_reset_or_submit(self):
        store = self.store()
        package = self.prepared(store)
        store.mark_filled(APP)
        store.decide(APP, "reject", digest(package))
        self.assertEqual(store.status(APP), "REJECTED")
        for action in (lambda: store.edit_package(APP, package), lambda: store.invalidate(APP),
                       lambda: store.mark_filled(APP), lambda: store.fail(APP, "secret"),
                       lambda: store.begin_submission(APP, digest(package))):
            with self.assertRaisesRegex(UserError, "STATE"):
                action()
        self.assertEqual(store.status(APP), "REJECTED")

    def test_begin_atomically_consumes_approval_and_only_stores_token_hash(self):
        store = self.store()
        package = self.approved(store)
        with self.assertRaisesRegex(UserError, "DIGEST"):
            store.begin_submission(APP, "wrong")
        token = store.begin_submission(APP, digest(package))
        self.assertIsInstance(token, str)
        self.assertGreaterEqual(len(token), 32)
        self.assertEqual(store.status(APP), "SUBMITTING")
        record = dict(store.conn.execute("SELECT * FROM approval_records WHERE decision='approve'").fetchone())
        self.assertTrue(record["consumed_at"])
        self.assertTrue(record["token_hash"])
        self.assertNotIn(token, json.dumps(record))
        self.assertNotIn(token, json.dumps(store.events(APP)))
        self.assertNotIn(token.encode(), self.path.read_bytes())
        with self.assertRaisesRegex(UserError, "STATE"):
            self.store().begin_submission(APP, digest(package))

    def test_changed_job_or_invalidated_approval_cannot_begin(self):
        store = self.store()
        package = self.approved(store)
        store.save_job(demo_job(jd="DEMO Changed requirement"), SourcePolicy(source="demo"))
        with self.assertRaisesRegex(UserError, "CHANGED"):
            store.begin_submission(APP, digest(package))
        self.assertEqual(store.status(APP), "APPROVED")
        store.invalidate(APP)
        self.assertEqual(store.status(APP), "PREPARED")
        self.assertTrue(store.conn.execute("SELECT invalidated_at FROM approval_records").fetchone()[0])

    def test_failed_fill_can_edit_but_unknown_and_submitting_cannot_reset(self):
        store = self.store()
        package = self.prepared(store)
        store.fail(APP, "private@example.invalid token secret")
        self.assertEqual(store.status(APP), "FAILED")
        self.assertNotIn("private@", json.dumps(store.events(APP)))
        store.edit_package(APP, package)
        store.mark_filled(APP)
        store.decide(APP, "approve", digest(package))
        store.begin_submission(APP, digest(package))
        for status in ("SUBMITTING", "UNKNOWN"):
            self.assertEqual(store.status(APP), status)
            for action in (lambda: store.invalidate(APP), lambda: store.edit_package(APP, package),
                           lambda: store.begin_submission(APP, digest(package))):
                with self.assertRaisesRegex(UserError, "STATE"):
                    action()
            store.fail(APP, "SUBMISSION_UNCERTAIN", uncertain=True)
        store.fail(APP, "secret", uncertain=False)
        self.assertEqual(store.status(APP), "UNKNOWN")

    def test_server_accepts_once_and_receipt_is_durable_before_submission_finish(self):
        store = self.store()
        package = self.approved(store)
        self.assertIsNone(store.receipt(APP))
        token = store.begin_submission(APP, digest(package))
        server = self.store()
        receipt = server.accept_submission(APP, token, digest(package), package["fields"])
        self.assertEqual(set(receipt), {"application_id", "content_digest", "receipt_id", "received_at", "scope"})
        self.assertEqual(receipt["application_id"], APP)
        self.assertEqual(receipt["content_digest"], digest(package))
        self.assertEqual(receipt["scope"], "LOCAL_DEMO")
        self.assertEqual(store.status(APP), "SUBMITTING")
        self.assertEqual(self.store().receipt(APP), receipt)
        self.assertEqual(server.accept_submission(APP, token, digest(package), package["fields"]), receipt)
        self.assertEqual([r["event"] for r in store.events(APP)].count("RECEIPT_RECEIVED"), 1)
        self.assertNotIn(token, json.dumps(receipt))
        store.finish_submission(APP, receipt)
        store.finish_submission(APP, receipt)
        self.assertEqual(store.status(APP), "SUBMITTED")
        self.assertEqual([r["event"] for r in store.events(APP)].count("SUBMITTED"), 1)
        self.assertEqual(server.accept_submission(APP, token, digest(package), package["fields"]), receipt)

    def test_server_requires_consumed_token_exact_digest_and_all_fields(self):
        store = self.store()
        package = self.approved(store)
        with self.assertRaises(UserError):
            store.accept_submission(APP, "fake", digest(package), package["fields"])
        token = store.begin_submission(APP, digest(package))
        variants = [("fake", digest(package), package["fields"]),
                    (token, "wrong", package["fields"]),
                    (token, digest(package), package["fields"] | {"extra": "x"}),
                    (token, digest(package), package["fields"] | {"resume_text": "tampered"}),
                    (token, digest(package), {"name": "Demo Applicant"})]
        for bad_token, bad_digest, fields in variants:
            with self.subTest(token=bad_token == token, digest=bad_digest), self.assertRaises(UserError):
                store.accept_submission(APP, bad_token, bad_digest, fields)
        self.assertIsNone(store.receipt(APP))
        store.save_job(demo_job(jd="DEMO stale after consumption"), SourcePolicy(source="demo"))
        with self.assertRaisesRegex(UserError, "CHANGED"):
            store.accept_submission(APP, token, digest(package), package["fields"])
        self.assertIsNone(store.receipt(APP))

    def test_unknown_recovers_only_from_exact_persisted_receipt(self):
        store = self.store()
        package = self.approved(store)
        token = store.begin_submission(APP, digest(package))
        receipt = store.accept_submission(APP, token, digest(package), package["fields"])
        store.fail(APP, "SUBMISSION_UNCERTAIN", uncertain=True)
        reopened = self.store()
        with self.assertRaises(UserError):
            reopened.finish_submission(APP, receipt | {"receipt_id": "invented"})
        with self.assertRaises(UserError):
            reopened.finish_submission(APP, receipt | {"content_digest": "wrong"})
        self.assertEqual(reopened.status(APP), "UNKNOWN")
        # External source drift cannot erase a durable, already accepted submission.
        reopened.save_job(demo_job(jd="DEMO changed after receipt"), SourcePolicy(source="demo"))
        reopened.finish_submission(APP, reopened.receipt(APP))
        self.assertEqual(reopened.status(APP), "SUBMITTED")
        for action in (lambda: reopened.invalidate(APP), lambda: reopened.edit_package(APP, package),
                       lambda: reopened.begin_submission(APP, digest(package))):
            with self.assertRaisesRegex(UserError, "STATE"):
                action()

    def test_unknown_without_receipt_never_accepts_a_late_new_submission(self):
        store = self.store()
        package = self.approved(store)
        token = store.begin_submission(APP, digest(package))
        store.fail(APP, "SUBMISSION_UNCERTAIN", uncertain=True)
        with self.assertRaisesRegex(UserError, "STATE"):
            store.accept_submission(APP, token, digest(package), package["fields"])
        with self.assertRaises(UserError):
            store.finish_submission(APP, {"application_id": APP, "content_digest": digest(package)})
        self.assertIsNone(store.receipt(APP))
        self.assertEqual(store.status(APP), "UNKNOWN")

    def test_begin_rollback_keeps_approval_unconsumed(self):
        store = self.store()
        package = self.approved(store)
        store.conn.execute("""CREATE TRIGGER fail_submitting BEFORE UPDATE ON applications
            WHEN NEW.status='SUBMITTING' BEGIN SELECT RAISE(ABORT, 'synthetic fault'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            store.begin_submission(APP, digest(package))
        self.assertEqual(store.status(APP), "APPROVED")
        record = store.conn.execute("SELECT consumed_at,token_hash FROM approval_records").fetchone()
        self.assertEqual(tuple(record), (None, None))

    def test_receipt_transaction_rolls_back_on_audit_failure(self):
        store = self.store()
        package = self.approved(store)
        token = store.begin_submission(APP, digest(package))
        store.conn.execute("""CREATE TRIGGER fail_receipt_audit BEFORE INSERT ON execution_logs
            WHEN NEW.event='RECEIPT_RECEIVED' BEGIN SELECT RAISE(ABORT, 'synthetic fault'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            store.accept_submission(APP, token, digest(package), package["fields"])
        self.assertIsNone(store.receipt(APP))
        self.assertEqual(store.status(APP), "SUBMITTING")

    def test_failure_after_consumption_is_always_unknown_and_never_editable(self):
        store = self.store()
        package = self.approved(store)
        store.begin_submission(APP, digest(package))
        store.fail(APP, "SUBMISSION_FAILED", uncertain=False)
        self.assertEqual(store.status(APP), "UNKNOWN")
        with self.assertRaisesRegex(UserError, "STATE"):
            store.edit_package(APP, package)
        self.assertIsNone(store.conn.execute("SELECT invalidated_at FROM approval_records").fetchone()[0])

    def test_competing_connections_consume_only_one_approval(self):
        store = self.store()
        package = self.approved(store)
        barrier = Barrier(2)
        from jobhunt.v2.application_store import ApplicationStore

        def submit_once():
            with closing(ApplicationStore(self.path, demo=True)) as independent:
                barrier.wait(timeout=5)
                try:
                    return independent.begin_submission(APP, digest(package))
                except UserError:
                    return None

        with ThreadPoolExecutor(max_workers=2) as workers:
            first = workers.submit(submit_once)
            second = workers.submit(submit_once)
            results = [first.result(timeout=10), second.result(timeout=10)]
        self.assertEqual(sum(result is not None for result in results), 1)
        self.assertEqual([r["event"] for r in store.events(APP)].count("SUBMITTING"), 1)


if __name__ == "__main__":
    unittest.main()
