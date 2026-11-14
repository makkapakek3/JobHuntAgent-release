"""P1 persistence tests; synthetic jobs only, isolated temporary databases."""
from contextlib import closing
from pathlib import Path
import tempfile
import unittest

from jobhunt.config import UserError
from jobhunt.v2.models import Job, SourcePolicy
from jobhunt.v2.storage import BusinessStore


def candidate(**changes):
    return Job(**(dict(source="DEMO-source", external_id="1", company="DEMO 公司", title="DEMO Python",
                       jd="DEMO Python SQLite", url="https://example.invalid/jobs/1", is_demo=True) | changes))


class DiscoveryStorageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "business.sqlite3"

    def tearDown(self):
        self.tmp.cleanup()

    def store(self):
        return BusinessStore(self.path, demo=True)

    def test_discovery_and_duplicate_have_no_application_side_effect(self):
        with closing(self.store()) as store:
            first = store.save_job(candidate(), SourcePolicy(source="DEMO-source"))
            second = store.save_job(candidate(), SourcePolicy(source="DEMO-source"))
            self.assertEqual(first[1:], (True, False))
            self.assertEqual(second, (first[0], False, False))
            self.assertEqual(len(store.list_jobs()), 1)
            self.assertEqual(store.list_applications(), [])
            identifier = store.create_application(job_id=first[0])
            self.assertEqual(store.get_application(identifier)["job_id"], first[0])
            with self.assertRaisesRegex(UserError, "DUPLICATE_APPLICATION"):
                store.create_application(job_id=first[0])

    def test_alias_preserves_both_origins_without_replacing_primary(self):
        with closing(self.store()) as store:
            identifier, _, _ = store.save_job(candidate(), SourcePolicy(source="DEMO-source"))
            alias = candidate(source="DEMO-alias", external_id="other", jd="DEMO alias content")
            result = store.save_job(alias, SourcePolicy(source="DEMO-alias"))
            self.assertEqual(result, (identifier, False, False))
            row = store.get_job(identifier)
            self.assertEqual(row["job"]["jd"], "DEMO Python SQLite")
            self.assertEqual({s["source"] for s in row["sources"]}, {"DEMO-source", "DEMO-alias"})

    def test_changed_alias_url_still_deduplicates_new_sources_and_applications(self):
        with closing(self.store()) as store:
            job_id, _, _ = store.save_job(candidate(), SourcePolicy(source='DEMO-source'))
            alias = candidate(source='DEMO-alias', external_id='other')
            store.save_job(alias, SourcePolicy(source=alias.source))
            store.save_job(alias.model_copy(update={'url': 'https://example.invalid/alternate'}),
                           SourcePolicy(source=alias.source))
            incoming = candidate(source='DEMO-third', url='https://example.invalid/alternate?utm_source=demo')
            result = store.save_job(incoming, SourcePolicy(source=incoming.source))
            self.assertEqual(result, (job_id, False, False))
            store.create_application(job_id=job_id)
            with self.assertRaisesRegex(UserError, 'DUPLICATE_APPLICATION'):
                store.create_application(incoming)
            self.assertEqual(len(store.list_jobs()), 1)

    def test_primary_url_cannot_take_another_jobs_alias(self):
        with closing(self.store()) as store:
            policy = SourcePolicy(source='DEMO-source')
            store.save_job(candidate(), policy)
            other = candidate(external_id='2', url='https://example.invalid/two')
            store.save_job(other, policy)
            alias = candidate(source='DEMO-alias', url=other.url)
            store.save_job(alias, SourcePolicy(source=alias.source))
            store.save_job(alias.model_copy(update={'url': 'https://example.invalid/alternate'}),
                           SourcePolicy(source=alias.source))
            with self.assertRaisesRegex(UserError, 'IDENTITY_CONFLICT'):
                store.save_job(candidate(url='https://example.invalid/alternate'), policy)
            self.assertEqual(len(store.list_jobs()), 2)

    def test_primary_change_preserves_application_snapshot_and_invalidates_approval(self):
        with closing(self.store()) as store:
            identifier = store.create_application(candidate())
            before = store.get_application(identifier)
            with store.conn:
                store.conn.execute("""INSERT INTO approval_records(approval_id,application_id,content_digest,decision,created_at)
                                      VALUES('DEMO-approval',?,?,'approve','2026-01-01')""",
                                   (identifier, before["content_digest"]))
            changed = candidate(jd="DEMO changed requirements")
            job_id, new, updated = store.save_job(changed, SourcePolicy(source="DEMO-source"))
            self.assertEqual((new, updated), (False, True))
            self.assertEqual(store.get_job(job_id)["job"]["jd"], changed.jd)
            self.assertEqual(store.get_application(identifier)["job"]["jd"], before["job"]["jd"])
            self.assertIsNotNone(store.conn.execute("SELECT invalidated_at FROM approval_records").fetchone()[0])
            with self.assertRaisesRegex(UserError, "JOB_CHANGED_REVIEW_REQUIRED"):
                store.validate_local(identifier)

    def test_migration_preserves_existing_p0_task(self):
        with closing(self.store()) as store:
            identifier = store.create_application(candidate())
            before = store.get_application(identifier)
            # Reconstruct the prior P0 schema, preserving its actual rows and digests.
            store.conn.execute("DROP TABLE submission_receipts")
            store.conn.execute("DROP TABLE application_packages")
            store.conn.execute("DROP TABLE job_sources")
            store.conn.execute("DROP TABLE source_cache")
            store.conn.execute("ALTER TABLE applications DROP COLUMN job_snapshot")
            store.conn.execute("PRAGMA user_version=1")
        with closing(self.store()) as store:
            after = store.get_application(identifier)
            self.assertEqual(after["content_digest"], before["content_digest"])
            self.assertEqual(after["job"], before["job"])
            self.assertEqual(len(store.get_job(after["job_id"])["sources"]), 1)
            store.initialize_local(identifier)
            store.finish_local(identifier)
            self.assertEqual(len(store.events(identifier)), 3)

    def test_identity_collision_rolls_back_instead_of_merging_distinct_jobs(self):
        with closing(self.store()) as store:
            policy = SourcePolicy(source="DEMO-source")
            store.save_job(candidate(), policy)
            store.save_job(candidate(external_id="2", url="https://example.invalid/jobs/2"), policy)
            with self.assertRaisesRegex(UserError, "JOB_IDENTITY_CONFLICT"):
                store.save_job(candidate(url="https://example.invalid/jobs/2"), policy)
            self.assertEqual(len(store.list_jobs()), 2)

    def test_source_policy_mismatch_and_missing_job_fail(self):
        with closing(self.store()) as store:
            with self.assertRaisesRegex(UserError, "SOURCE_POLICY_MISMATCH"):
                store.save_job(candidate(), SourcePolicy(source="wrong"))
            with self.assertRaisesRegex(UserError, "JOB_NOT_FOUND"):
                store.create_application(job_id="missing")
            self.assertEqual(store.list_jobs(), [])

    def test_extended_source_fields_affect_hash_but_not_collection_time(self):
        first = candidate(salary_text="USD 100,000 per year", category="DEMO Development", employment_type="full_time")
        self.assertNotEqual(first.jd_hash, candidate(salary_text="USD 110,000 per year").jd_hash)
        self.assertNotEqual(first.content_hash, candidate(salary_text="USD 110,000 per year").content_hash)


if __name__ == "__main__":
    unittest.main()
