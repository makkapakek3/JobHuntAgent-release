"""Fictional preference cases; no personal documents or network."""
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from jobhunt.config import Context
from jobhunt.filters import evaluate
from jobhunt.models import Job, Preferences


class PreferenceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ctx = Context(Path(self.tmp.name), True).load()
        self.ctx.preferences = Preferences.model_validate({
            "job_titles": ["Python", "Agent", "售前"],
            "secondary_job_titles": ["售前"],
            "job_types": ["campus_recruiting", "internship"],
            "locations": {"preferred": ["上海"]}, "allow_remote": True,
            "compensation": {"minimum_monthly_cny": 9000, "minimum_daily_cny": 180},
            "expected_graduation_year": 2030,
            "internship_start_month": "2030-02", "internship_max_days_per_week": 4,
            "internship_min_months": 2,
            "review_notes": ["DEMO 到岗和出差需本人决定"],
        })

    def job(self, **changes):
        return Job.model_validate({"source": "DEMO", "company": "虚构公司",
            "title": "Python 实习生", "city": "上海", "jd": "Python LangGraph SQLite",
            "url": "https://example.invalid/demonstration", "is_demo": True,
            "job_type": "internship", "campus": False, "minimum_degree": "本科",
            "salary_min": 180, "salary_max": 200, "salary_period": "day",
            "experience_years": 0, **changes})

    def test_internship_is_allowed_and_daily_floor_is_enforced(self):
        self.assertNotEqual(evaluate(self.job(), self.ctx)["decision"], "REJECT")
        low = evaluate(self.job(salary_min=120, salary_max=150), self.ctx)
        self.assertIn("薪资低于下限", low["reasons"])
        monthly = evaluate(self.job(salary_period="month", salary_min=4000, salary_max=5000), self.ctx)
        self.assertNotEqual(monthly["decision"], "REJECT")
        self.assertIn("salary", monthly["unknown"])

    def test_campus_monthly_floor_and_graduation_still_apply(self):
        job = self.job(title="Python 校招", job_type="campus_recruiting", campus=True,
                       salary_period="month", salary_min=6000, salary_max=8000,
                       graduation_year=2029)
        outcome = evaluate(job, self.ctx)
        self.assertIn("薪资低于下限", outcome["reasons"])
        self.assertIn("毕业年份不符", outcome["reasons"])

    def test_remote_does_not_assume_geographic_eligibility(self):
        job = self.job(city="Worldwide", remote=True)
        outcome = evaluate(job, self.ctx)
        self.assertNotIn("地点不符", outcome["reasons"])
        self.assertIn("remote_eligibility", outcome["unknown"])
        self.assertIn("地点不符", evaluate(self.job(city="北京", remote=False), self.ctx)["reasons"])

    def test_early_start_and_longer_duration_need_review_not_rejection(self):
        job = self.job(start_month="2030-01", duration_months=6, days_per_week=4)
        result = evaluate(job, self.ctx)
        self.assertNotEqual(result["decision"], "REJECT")
        self.assertIn("internship_start", result["unknown"])
        self.assertIn("internship_duration", result["unknown"])
        too_many = evaluate(self.job(days_per_week=5), self.ctx)
        self.assertIn("每周出勤超过上限", too_many["reasons"])

    def test_experienced_role_is_not_internship_or_campus(self):
        result = evaluate(self.job(title="Python 工程师", job_type="experienced"), self.ctx)
        self.assertIn("用工类型不符", result["reasons"])

    def test_report_keeps_conditions_and_secondary_roles_after_primary(self):
        from jobhunt.graph import run_nightly
        from jobhunt.sources import SourceResult
        # Context.load reads only virtual demo data; reuse the already loaded context.
        self.ctx.load = lambda: self.ctx
        first = self.job(title="Python Agent 实习生", remote=True, days_per_week=4,
                         start_month="2030-01", duration_months=6)
        second = self.job(title="Python Agent 售前实习生", url="https://example.invalid/sales",
                          travel="DEMO 每月出差两次，地区待定")
        class Source:
            name = "DEMO"
            def fetch(self, store, limit):
                return SourceResult(self.name, [second, first], scanned=2)
        run_nightly(self.ctx, [Source()])
        report = (self.ctx.reports / "latest.md").read_text(encoding="utf-8")
        self.assertIn("DEMO 到岗和出差需本人决定", report)
        self.assertIn("2030-01", report)
        self.assertIn("每月出差两次", report)
        self.assertLess(report.index("Python Agent 实习生"), report.index("Python Agent 售前实习生"))


if __name__ == "__main__":
    unittest.main()
