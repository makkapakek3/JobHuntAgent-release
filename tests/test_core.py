import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from jobhunt.config import Context, UserError
from jobhunt.models import Job, MatchResult, canonical_url
from jobhunt.storage import Store, run_lock


def sample(**changes):
    return Job.model_validate({"source": "test", "external_id": "1", "company": "示例公司",
        "title": "Python Agent 工程师", "city": "上海", "jd": "Python LangGraph SQLite",
        "url": "https://example.invalid/jobs/1", "is_demo": True, **changes})


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.db = Store(self.home / "test.sqlite3")

    def tearDown(self):
        self.db.close()
        self.tmp.cleanup()

    def test_normalized_url_and_cross_source_dedup(self):
        a, new, _ = self.db.upsert(sample(url="https://example.invalid/jobs/1?utm_source=a#x"))
        b, new2, _ = self.db.upsert(sample(source="second", external_id="22"))
        self.assertTrue(new)
        self.assertFalse(new2)
        self.assertEqual(a, b)
        self.assertEqual(self.db.conn.execute("SELECT count(*) FROM job_sources").fetchone()[0], 2)
        self.assertEqual(canonical_url("https://e.invalid/a/?id=2&utm_x=y"), "https://e.invalid/a?id=2")

    def test_jd_update_keeps_id_and_changes_hash(self):
        a, _, _ = self.db.upsert(sample())
        b, new, changed = self.db.upsert(sample(jd="Python Rust 新要求"))
        self.assertEqual(a, b)
        self.assertFalse(new)
        self.assertTrue(changed)

    def test_filter_unknown_is_not_pass(self):
        from jobhunt.filters import evaluate
        ctx = Context(self.home, True).load()
        decision = evaluate(sample(city=""), ctx)
        self.assertEqual(decision["decision"], "UNKNOWN")
        self.assertIn("city", decision["unknown"])

    def test_explicit_hard_constraints(self):
        from jobhunt.filters import evaluate
        ctx = Context(self.home, True).load()
        cases = [dict(city="北京"), dict(salary_max=1000), dict(minimum_degree="博士"),
                 dict(campus=False), dict(experience_years=6), dict(graduation_year=2026),
                 dict(company="黑名单示例公司"), dict(jd="Python 长期出差")]
        for case in cases:
            with self.subTest(case=case):
                self.assertEqual(evaluate(sample(**case), ctx)["decision"], "REJECT")

    def test_non_cny_salary_is_unknown(self):
        from jobhunt.filters import evaluate
        ctx = Context(self.home, True).load()
        outcome = evaluate(sample(salary_max=200, salary_currency="USD", salary_period="year"), ctx)
        self.assertNotEqual(outcome["decision"], "REJECT")
        self.assertIn("salary", outcome["unknown"])

    def test_schema_rejects_invalid_model_output(self):
        with self.assertRaises(ValueError):
            MatchResult.model_validate({"score": 101, "reasons": [], "gaps": [], "risks": [], "confidence": 2})
        with self.assertRaises(ValueError):
            MatchResult.model_validate({"score": 80, "reasons": ["x"], "gaps": [], "risks": [], "confidence": .5, "send": True})

    def test_profile_allowlist_and_redaction(self):
        ctx = Context(self.home, True).load()
        self.assertNotIn("contact", ctx.safe_profile)
        self.assertTrue(ctx.resume_version)

    def test_lock_blocks_parallel_run(self):
        with run_lock(self.home / "run.lock"):
            with self.assertRaises(UserError):
                with run_lock(self.home / "run.lock"):
                    self.fail("must not acquire a second lock")

    def test_csv_json_imports_and_invalid_rows(self):
        from jobhunt.sources import FileSource
        p = self.home / "jobs.json"
        p.write_text(json.dumps([sample().model_dump(), {"company": "broken"}]), encoding="utf-8")
        result = FileSource(p).fetch(self.db, 100)
        self.assertEqual(len(result.jobs), 1)
        self.assertTrue(result.errors)
        p = self.home / "jobs.csv"
        p.write_text('company,title,city,jd,url,campus,salary_max\n示例,Python,上海,Python,https://example.invalid/j/2,true,20000\n', encoding="utf-8-sig")
        self.assertTrue(FileSource(p).fetch(self.db, 100).jobs[0].campus)

    def test_remotive_mapping_and_persistent_rate_limit(self):
        from jobhunt.sources import RemotiveSource
        payload = {"jobs": [{"id": 12, "company_name": "Example", "title": "Python Engineer",
                "candidate_required_location": "Worldwide", "description": "<p>Python</p>",
                "url": "https://remotive.com/remote-jobs/software-dev/python-12"}]}
        with patch("jobhunt.sources.fetch_json", return_value=payload) as fetch:
            first = RemotiveSource().fetch(self.db, 100)
            second = RemotiveSource().fetch(self.db, 100)
        self.assertEqual(first.jobs[0].source, "Remotive")
        self.assertFalse(first.jobs[0].is_demo)
        self.assertEqual(first.jobs[0].jd, "Python")
        self.assertEqual(fetch.call_count, 1)
        self.assertIn("RATE_LIMIT", second.errors[0])

    def test_source_timeout_is_reportable(self):
        from jobhunt.sources import RemotiveSource
        with patch("jobhunt.sources.fetch_json", side_effect=TimeoutError("SECRET")):
            result = RemotiveSource().fetch(self.db, 100)
        self.assertIn("TimeoutError", result.errors[0])
        self.assertNotIn("SECRET", result.errors[0])


if __name__ == "__main__":
    unittest.main()
