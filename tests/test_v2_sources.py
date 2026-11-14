"""Offline source tests. Every job and transport response here is synthetic."""
from contextlib import closing
from datetime import datetime, timedelta, timezone
import importlib
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, Mock, patch
from urllib.error import HTTPError

from jobhunt.config import UserError
from jobhunt.models import Permissions
from jobhunt.v2.models import SearchCriteria
from jobhunt.v2.storage import BusinessStore


def api_job(**changes):
    return dict(id=42, company_name="Synthetic Company", title="Synthetic Python role",
                description="<p>Build <b>Python</b> APIs &amp; SQLite.</p>",
                url="https://remotive.com/remote-jobs/software-dev/synthetic-42",
                candidate_required_location="Worldwide", publication_date="2026-10-01T10:00:00",
                category="Software Development", job_type="full_time", tags=["Python", "SQLite"],
                salary="", **changes)


def local_job(**changes):
    result = dict(company="Synthetic Company", title="Synthetic Python role", jd="Synthetic Python JD",
                  url="https://example.invalid/jobs/42", external_id="42", is_demo=True)
    result.update(changes)
    return result


class SourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = Path(self.tmp.name)
        self.instant = datetime(2026, 10, 30, 8, tzinfo=timezone.utc)
        self.permissions = Permissions()

    def tearDown(self):
        self.tmp.cleanup()

    def adapter(self, name, *args, **kwargs):
        self.assertIsNotNone(importlib.util.find_spec("jobhunt.v2.sources"), "V2 source adapters are missing")
        module = importlib.import_module("jobhunt.v2.sources")
        self.assertTrue(hasattr(module, name), name + " adapter is missing")
        return getattr(module, name)(*args, **kwargs)

    def remotive(self, store, fetch, permissions=None, demo=False):
        return self.adapter("RemotiveSource", store, permissions or self.permissions,
                            demo=demo, fetch=fetch, clock=lambda: self.instant)

    def file(self, rows, *, extension="json", permissions=None, demo=True):
        path = self.directory / ("synthetic." + extension)
        path.write_text(json.dumps(rows), encoding="utf-8-sig")
        return self.adapter("LocalFileSource", path, permissions or self.permissions, demo=demo)

    def test_network_attempt_is_committed_before_transport_and_cache_survives_reopen(self):
        path = self.directory / "live.sqlite3"
        def fetch():
            with closing(sqlite3.connect(path)) as other:
                row = other.execute("SELECT last_attempt FROM source_cache WHERE source='Remotive'").fetchone()
            self.assertEqual(datetime.fromisoformat(row[0]), self.instant)
            return {"jobs": [api_job()]}
        with closing(BusinessStore(path, demo=False)) as store:
            source = self.remotive(store, fetch)
            rows = source.search_jobs(SearchCriteria(limit=1))
            self.assertEqual(len(rows), 1)
            self.assertEqual(source.report["cache_status"], "NETWORK")
            job = source.normalize_job(rows[0])
            self.assertEqual(job.fetched_at, self.instant)
            self.assertIn("Remotive", source.report["attribution"])
        self.instant += timedelta(hours=1)
        with closing(BusinessStore(path, demo=False)) as store:
            no_network = Mock(side_effect=AssertionError("cache must prevent network"))
            source = self.remotive(store, no_network)
            rows = source.search_jobs(SearchCriteria())
            self.assertEqual(source.report["cache_status"], "CACHE")
            self.assertEqual(source.normalize_job(rows[0]).fetched_at, self.instant - timedelta(hours=1))
            no_network.assert_not_called()

    def test_acquisition_does_not_filter_or_apply_limit(self):
        with closing(BusinessStore(self.directory / "db", demo=False)) as store:
            source = self.remotive(store, lambda: {"jobs": [api_job(), {"invalid": True}, None]})
            self.assertEqual(len(source.search_jobs(SearchCriteria(keywords=["no-match"], limit=1))), 3)

    def test_permissions_are_checked_before_cache_or_transport(self):
        with closing(BusinessStore(self.directory / "db", demo=False)) as store:
            self.remotive(store, lambda: {"jobs": [api_job()]}).search_jobs(SearchCriteria())
            for permissions in (Permissions(read_job_sources=False), Permissions(allowed_job_sources=[])):
                with self.subTest(permissions=permissions):
                    fetch = Mock()
                    source = self.remotive(store, fetch, permissions=permissions)
                    with self.assertRaisesRegex(UserError, "PERMISSION_DENIED"):
                        source.search_jobs(SearchCriteria())
                    fetch.assert_not_called()

    def test_demo_denies_real_source_and_mismatched_store_before_transport(self):
        with closing(BusinessStore(self.directory / "demo", demo=True)) as store:
            for demo in (True, False):
                fetch = Mock()
                source = self.remotive(store, fetch, demo=demo)
                with self.assertRaisesRegex(UserError, "DEMO_REAL_SOURCE_DENIED|MODE_MISMATCH"):
                    source.search_jobs(SearchCriteria())
                fetch.assert_not_called()
                self.assertEqual(store.conn.execute("SELECT COUNT(*) FROM source_cache").fetchone()[0], 0)

    def test_failure_uses_safe_code_and_consumes_persistent_cooldown(self):
        path = self.directory / "db"
        with closing(BusinessStore(path, demo=False)) as store:
            source = self.remotive(store, Mock(side_effect=RuntimeError("password=secret-body")))
            with self.assertRaisesRegex(UserError, "SOURCE_NETWORK_ERROR"):
                source.search_jobs(SearchCriteria())
            self.assertNotIn("secret-body", json.dumps(source.report))
        self.instant += timedelta(minutes=10)
        with closing(BusinessStore(path, demo=False)) as store:
            fetch = Mock()
            source = self.remotive(store, fetch)
            with self.assertRaisesRegex(UserError, "SOURCE_COOLDOWN"):
                source.search_jobs(SearchCriteria())
            self.assertEqual(source.report["error_code"], "V2_SOURCE_NETWORK_ERROR")
            fetch.assert_not_called()

    def test_failed_refresh_and_following_cooldown_keep_stale_timestamp_and_error(self):
        with closing(BusinessStore(self.directory / "db", demo=False)) as store:
            self.remotive(store, lambda: {"jobs": [api_job()]}).search_jobs(SearchCriteria())
            original_time = self.instant
            self.instant += timedelta(hours=6)
            failure = Mock(side_effect=HTTPError("https://remotive.com", 429, "private body", {}, None))
            source = self.remotive(store, failure)
            rows = source.search_jobs(SearchCriteria())
            self.assertEqual(source.report["cache_status"], "STALE")
            self.assertEqual(source.report["error_code"], "V2_SOURCE_HTTP_429")
            self.assertEqual(source.normalize_job(rows[0]).fetched_at, original_time)
            failure.assert_called_once()
            self.instant += timedelta(minutes=2)
            fetch = Mock()
            again = self.remotive(store, fetch)
            self.assertEqual(again.search_jobs(SearchCriteria()), rows)
            self.assertEqual(again.report["cache_status"], "STALE")
            self.assertEqual(again.report["error_code"], "V2_SOURCE_HTTP_429")
            fetch.assert_not_called()

    def test_success_after_cooldown_refreshes_timestamp_and_clears_failure(self):
        with closing(BusinessStore(self.directory / "db", demo=False)) as store:
            failing = self.remotive(store, Mock(side_effect=TimeoutError()))
            with self.assertRaises(UserError):
                failing.search_jobs(SearchCriteria())
            self.instant += timedelta(hours=6)
            source = self.remotive(store, lambda: {"jobs": [api_job()]})
            source.search_jobs(SearchCriteria())
            self.assertEqual(source.report["cache_status"], "NETWORK")
            self.assertEqual(source.report["error_code"], "")
            self.assertEqual(store.conn.execute("SELECT error_code FROM source_cache").fetchone()[0], "")

    def test_access_restrictions_and_redirects_are_never_retried(self):
        for status in (403, 429):
            with self.subTest(status=status), closing(BusinessStore(self.directory / str(status), demo=False)) as store:
                fetch = Mock(side_effect=HTTPError('https://remotive.com', status, 'private-body', {}, None))
                source = self.remotive(store, fetch)
                with self.assertRaisesRegex(UserError, f'V2_SOURCE_HTTP_{status}'):
                    source.search_jobs(SearchCriteria())
                with self.assertRaisesRegex(UserError, 'COOLDOWN'):
                    source.search_jobs(SearchCriteria())
                fetch.assert_called_once()
        from jobhunt.v2.sources import NoRedirect
        with self.assertRaisesRegex(UserError, 'REDIRECT_DENIED'):
            NoRedirect().redirect_request(None, None, 302, 'private', {}, 'https://example.invalid')

    def test_http_transport_is_bounded_and_uses_only_fixed_endpoint(self):
        from jobhunt.v2.sources import ENDPOINT, MAX_BYTES, fetch_remotive
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b'{"jobs":[]}'
        opener = MagicMock()
        opener.open.return_value = response
        with patch('jobhunt.v2.sources.build_opener', return_value=opener):
            self.assertEqual(fetch_remotive(), {'jobs': []})
            request = opener.open.call_args.args[0]
            self.assertEqual(request.full_url, ENDPOINT)
            self.assertEqual(request.get_method(), 'GET')
            self.assertIsNone(request.data)
            response.read.assert_called_once_with(MAX_BYTES + 1)
            response.read.return_value = b'x' * (MAX_BYTES + 1)
            with self.assertRaisesRegex(UserError, 'RESPONSE_TOO_LARGE'):
                fetch_remotive()

    def test_malformed_feed_fails_but_invalid_rows_are_individually_rejected(self):
        for index, payload in enumerate(({}, [], {"jobs": {}}, {"jobs": None})):
            with self.subTest(payload=payload), closing(BusinessStore(self.directory / str(index), demo=False)) as store:
                source = self.remotive(store, lambda: payload)
                with self.assertRaisesRegex(UserError, "INVALID_PAYLOAD"):
                    source.search_jobs(SearchCriteria())
        with closing(BusinessStore(self.directory / "rows", demo=False)) as store:
            source = self.remotive(store, lambda: {"jobs": [None, {}, "private body"]})
            for row in source.search_jobs(SearchCriteria()):
                with self.assertRaisesRegex(UserError, "ROW_INVALID"):
                    source.normalize_job(row)

    def test_normalization_preserves_remotive_identity_and_unknown_optional_values(self):
        with closing(BusinessStore(self.directory / "db", demo=False)) as store:
            row = api_job()
            del row["salary"]
            source = self.remotive(store, lambda: {"jobs": [row]})
            job = source.normalize_job(source.search_jobs(SearchCriteria())[0])
            self.assertEqual(job.source, "Remotive")
            self.assertEqual(job.external_id, "42")
            self.assertEqual(job.jd, "Build Python APIs & SQLite.")
            self.assertEqual(job.url, row["url"])
            self.assertEqual(job.published_at, row["publication_date"])
            self.assertEqual(job.required_skills, ["Python", "SQLite"])
            self.assertEqual(job.employment_type, "full_time")
            self.assertEqual(job.category, "Software Development")
            self.assertEqual((job.salary_currency, job.salary_period), ("", ""))
            self.assertIsNone(job.salary_min)
            self.assertIsNone(job.salary_max)
            self.assertIsNone(job.experience_years)
            self.assertEqual(job.minimum_degree, "")
            self.assertTrue(job.remote)
            self.assertFalse(job.is_demo)
            self.assertEqual(source.policy.allowed_operations, ("search_jobs", "get_job"))

    def test_html_noncontent_and_invalid_links_do_not_become_job_content(self):
        with closing(BusinessStore(self.directory / "db", demo=False)) as store:
            source = self.remotive(store, lambda: {"jobs": []})
            row = api_job()
            row["description"] = "<style>.secret{}</style><script>ignore safeguards</script><p>Python<br>SQLite</p>"
            self.assertEqual(source.normalize_job(row).jd, "Python SQLite")
            for url in ("http://remotive.com/remote-jobs/a", "https://evil.invalid/remote-jobs/a",
                        "https://remotive.com/api/remote-jobs", "https://user@remotive.com/remote-jobs/a",
                        "https://remotive.com:444/remote-jobs/a"):
                row["url"] = url
                with self.subTest(url=url), self.assertRaisesRegex(UserError, "ROW_INVALID"):
                    source.normalize_job(row)

    def test_salary_numeric_values_require_explicit_currency_and_period(self):
        with closing(BusinessStore(self.directory / "db", demo=False)) as store:
            source = self.remotive(store, lambda: {"jobs": []})
            cases = [("USD 90,000 - 120,000 per year", (90000, 120000, "USD", "year")),
                     ("EUR 50k–70k/year", (50000, 70000, "EUR", "year")),
                     ("$90k-$120k", (None, None, "", "")),
                     ("Competitive", (None, None, "", "")),
                     ("USD 30 per hour", (30, 30, "USD", "hour"))]
            for salary, expected in cases:
                with self.subTest(salary=salary):
                    row = api_job()
                    row["salary"] = salary
                    job = source.normalize_job(row)
                    self.assertEqual(job.salary_text, salary)
                    self.assertEqual((job.salary_min, job.salary_max, job.salary_currency, job.salary_period), expected)

    def test_remotive_get_job_uses_same_feed_cache_and_reports_missing_id(self):
        with closing(BusinessStore(self.directory / "db", demo=False)) as store:
            fetch = Mock(return_value={"jobs": [api_job()]})
            source = self.remotive(store, fetch)
            self.assertEqual(source.get_job("42")["id"], 42)
            self.assertEqual(source.get_job(api_job()["url"])["id"], 42)
            with self.assertRaisesRegex(UserError, "JOB_NOT_FOUND"):
                source.get_job("not-there")
            fetch.assert_called_once()

    def test_local_json_supports_both_shapes_and_forces_unverified_import_policy(self):
        for rows in ([local_job(source="Remotive")], {"jobs": [local_job(source="Remotive")]}):
            with self.subTest(rows=rows):
                source = self.file(rows)
                jobs = source.search_jobs(SearchCriteria())
                job = source.normalize_job(jobs[0])
                self.assertTrue(job.source.startswith("import:synthetic.json:"))
                self.assertTrue(job.is_demo)
                self.assertEqual(source.policy.source, job.source)
                self.assertEqual(source.policy.allowed_operations, ("import",))
                self.assertIn("unverified", source.policy.authorization_reference)
                self.assertEqual(source.report["cache_status"], "LOCAL")
                self.assertTrue(source.report["warnings"])

    def test_local_csv_parses_bom_booleans_numbers_and_semicolon_skills(self):
        path = self.directory / "synthetic.csv"
        path.write_text("company,title,jd,url,is_demo,remote,active,salary_min,salary_max,required_skills,experience_years\n"
                        "Synthetic,Synthetic Python,Synthetic JD,https://example.invalid/csv,true,false,true,10,20,Python;SQL,0\n",
                        encoding="utf-8-sig")
        source = self.adapter("LocalFileSource", path, self.permissions, demo=True)
        job = source.normalize_job(source.search_jobs(SearchCriteria())[0])
        self.assertEqual(job.required_skills, ["Python", "SQL"])
        self.assertEqual((job.salary_min, job.salary_max, job.experience_years), (10, 20, 0))
        self.assertFalse(job.remote)
        self.assertTrue(job.active)
        self.assertEqual((job.salary_currency, job.salary_period), ("", ""))

    def test_different_files_with_same_name_and_id_cannot_overwrite_each_other(self):
        paths = [self.directory / parent / 'jobs.json' for parent in ('origin_a', 'origin_b')]
        with closing(BusinessStore(self.directory / 'demo-db', demo=True)) as store:
            identities = []
            for index, path in enumerate(paths):
                path.parent.mkdir()
                path.write_text(json.dumps([local_job(url=f'https://example.invalid/{index}',
                                                     company=f'Synthetic {index}')]), encoding='utf-8')
                source = self.adapter('LocalFileSource', path, self.permissions, demo=True)
                job = source.normalize_job(source.search_jobs(SearchCriteria())[0])
                identities.append(job.source)
                store.save_job(job, source.policy)
            self.assertNotEqual(*identities)
            self.assertEqual(len(store.list_jobs()), 2)
            again = self.adapter('LocalFileSource', paths[0], self.permissions, demo=True)
            self.assertEqual(again.policy.source, identities[0])
            job = again.normalize_job(again.search_jobs(SearchCriteria())[0])
            self.assertFalse(store.save_job(job, again.policy)[1])

    def test_local_no_silent_mode_conversion(self):
        for row, demo in ((local_job(is_demo=False), True), (local_job(is_demo=True), False)):
            with self.subTest(demo=demo):
                source = self.file([row], demo=demo)
                with self.assertRaisesRegex(UserError, "MODE_MISMATCH"):
                    source.normalize_job(source.search_jobs(SearchCriteria())[0])
        row = local_job()
        row.pop("is_demo")
        source = self.file([row])
        with self.assertRaisesRegex(UserError, "MODE_MISMATCH"):
            source.normalize_job(source.search_jobs(SearchCriteria())[0])

    def test_local_invalid_rows_survive_acquisition_and_raise_safe_errors(self):
        invalid = [None, {"company": "password=secret"}, local_job(remote="maybe"), local_job(extra="unknown")]
        source = self.file(invalid)
        rows = source.search_jobs(SearchCriteria(limit=1))
        self.assertEqual(rows, invalid)
        for row in rows:
            with self.assertRaisesRegex(UserError, "ROW_INVALID") as caught:
                source.normalize_job(row)
            self.assertNotIn("secret", str(caught.exception))

    def test_local_malformed_structure_encoding_and_format_fail_at_acquisition(self):
        for value in ({}, {"jobs": {}}, 42):
            source = self.file(value)
            with self.subTest(value=value), self.assertRaisesRegex(UserError, "INVALID_PAYLOAD"):
                source.search_jobs(SearchCriteria())
        source = self.file([local_job()], extension="txt")
        with self.assertRaisesRegex(UserError, "UNSUPPORTED_FORMAT"):
            source.search_jobs(SearchCriteria())

    def test_local_permission_denial_precedes_missing_file(self):
        source = self.adapter("LocalFileSource", self.directory / "absent.json",
                              Permissions(read_job_sources=False), demo=True)
        with self.assertRaisesRegex(UserError, "PERMISSION_DENIED"):
            source.search_jobs(SearchCriteria())

    def test_local_get_job_preserves_valid_supplied_fetched_time(self):
        stamp = "2025-01-02T03:04:05+08:00"
        source = self.file([local_job(fetched_at=stamp)])
        job = source.normalize_job(source.get_job("42"))
        self.assertEqual(job.fetched_at, datetime.fromisoformat(stamp))
        with self.assertRaisesRegex(UserError, "JOB_NOT_FOUND"):
            source.get_job("absent")
        row = local_job(fetched_at="2025-01-02T03:04:05")
        with self.assertRaisesRegex(UserError, "ROW_INVALID"):
            source.normalize_job(row)

    def test_manual_accepts_complete_explicit_demo_data_and_never_requests_url(self):
        data = local_job(source="Remotive")
        source = self.adapter("ManualSource", data, self.permissions, demo=True)
        self.assertEqual(len(source.search_jobs(SearchCriteria(keywords=["no-match"]))), 1)
        job = source.normalize_job(source.get_job("42"))
        self.assertEqual(job.source, "manual")
        self.assertEqual(source.policy.allowed_operations, ("import",))
        self.assertEqual(source.report["cache_status"], "LOCAL")
        self.assertEqual(data["source"], "Remotive")

    def test_manual_link_only_missing_fields_denied_without_exposing_input(self):
        source = self.adapter("ManualSource", {"url": "https://example.invalid/private", "is_demo": True},
                              self.permissions, demo=True)
        with self.assertRaisesRegex(UserError, "ROW_INVALID|MANUAL_DETAILS_REQUIRED") as caught:
            source.normalize_job(source.search_jobs(SearchCriteria())[0])
        self.assertNotIn("private", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
