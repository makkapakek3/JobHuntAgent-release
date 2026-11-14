import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from test_graph import StaticSource, job
from test_m1 import reply
from jobhunt.config import Context, UserError, read_env
from jobhunt.deepseek import endpoint
from jobhunt.graph import run_nightly
from jobhunt.matching import cache_key
from jobhunt.usage import UsageLedger


class SafetyTests(unittest.TestCase):
    def test_deferred_job_can_be_reanalyzed_then_explicitly_approved(self):
        from jobhunt.storage import Store
        from jobhunt.approvals import review
        with tempfile.TemporaryDirectory() as directory:
            ctx = Context(Path(directory), True)
            run_nightly(ctx, [StaticSource([job()])])
            db = Store(ctx.db_path)
            try:
                identifier = db.listing()[0]['job_id']
                review(ctx, db, [identifier], 'defer')
                result = run_nightly(ctx, [StaticSource([job().model_copy(update={'jd': 'Python LangGraph SQLite updated'})])])
                self.assertEqual(result['analyzed'], 1)
                self.assertEqual(db.get(identifier)['status'], 'DEFERRED')
                self.assertEqual(review(ctx, db, [identifier], 'approve')[0]['status'], 'APPROVED')
            finally:
                db.close()

    def test_shared_content_cache_allows_both_stable_ids_review(self):
        from jobhunt.storage import Store
        from jobhunt.approvals import review
        with tempfile.TemporaryDirectory() as directory:
            ctx = Context(Path(directory), True)
            first = job()
            second = first.model_copy(update={'external_id': '2', 'url': 'https://example.invalid/2', 'jd': 'Python LangGraph SQLite other'})
            run_nightly(ctx, [StaticSource([first, second])])
            db = Store(ctx.db_path)
            try:
                db.upsert(first.model_copy(update={'external_id': '2'}))
                run_nightly(ctx, [StaticSource()])
                ids = [r['job_id'] for r in db.listing()]
                report = (ctx.reports / 'latest.md').read_text(encoding='utf-8')
                for identifier in ids:
                    self.assertIn(identifier, report)
                    self.assertEqual(review(ctx, db, [identifier], 'approve')[0]['status'], 'APPROVED')
            finally:
                db.close()

    def test_config_only_budget_keeps_real_backend_deepseek(self):
        from jobhunt.config import ROOT
        import shutil
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / 'profile/resumes').mkdir(parents=True)
            (home / 'config').mkdir()
            for name in ['basic_profile.yaml', 'career_preferences.yaml']:
                shutil.copy2(ROOT / 'examples' / name, home / 'profile' / name)
            shutil.copy2(ROOT / 'examples/resume_summary.txt', home / 'profile/resumes/summary.txt')
            (home / 'config/settings.yaml').write_text('daily_token_budget: 1', encoding='utf-8')
            self.assertEqual(Context(home).load().settings.matching_backend, 'deepseek')

    def test_redaction_of_quoted_credentials_keeps_json_valid(self):
        from jobhunt.models import redact_data
        value = {'summary': 'API_KEY="private_value" password=\'hidden\' phone 13800138000', 'score': 85}
        redacted = redact_data(value)
        text = json.dumps(redacted)
        self.assertEqual(json.loads(text)['score'], 85)
        self.assertNotIn('private_value', text)
        self.assertNotIn('hidden', text)
        self.assertNotIn('13800138000', text)

    def test_duplicate_alias_update_is_preserved_without_replacing_primary(self):
        from jobhunt.storage import Store
        with tempfile.TemporaryDirectory() as directory:
            db = Store(Path(directory) / 'test.sqlite3')
            try:
                first = job()
                second = first.model_copy(update={'source': 'second', 'external_id': '2', 'url': 'https://example.invalid/other/2'})
                primary_id = db.upsert(first)[0]
                self.assertEqual(db.upsert(second)[0], primary_id)
                revised = second.model_copy(update={'active': False, 'jd': 'Python changed requirements'})
                second_id = db.upsert(revised)[0]
                self.assertNotEqual(primary_id, second_id)
                self.assertFalse(db.get(second_id)['active'])
                self.assertEqual(db.upsert(revised)[0], second_id)
                self.assertEqual(json.loads(db.get(primary_id)['payload'])['jd'], first.jd)
            finally:
                db.close()

    def test_stale_secondary_snapshot_does_not_split_or_overwrite_primary(self):
        from jobhunt.storage import Store
        with tempfile.TemporaryDirectory() as directory:
            db = Store(Path(directory) / 'test.sqlite3')
            try:
                first = job()
                second = first.model_copy(update={'source': 'second', 'external_id': '2', 'url': 'https://example.invalid/other/2'})
                identifier = db.upsert(first)[0]
                db.upsert(second)
                db.upsert(first.model_copy(update={'jd': 'Python updated primary'}))
                self.assertEqual(db.upsert(second)[0], identifier)
                self.assertEqual(len(db.listing()), 1)
            finally:
                db.close()

    def test_profile_errors_identify_fields_without_echoing_private_input(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / 'profile').mkdir()
            (home / 'profile/basic_profile.yaml').write_text('education: []\nskills: []\nprojects: []\ncontact: SECRET\n', encoding='utf-8')
            with self.assertRaises(UserError) as caught:
                Context(home).load()
            text = str(caught.exception)
            self.assertIn('education', text)
            self.assertIn('skills', text)
            self.assertNotIn('SECRET', text)

    def test_unknown_usage_reservation_is_shared_and_crash_durable(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            ledger = UsageLedger(home)
            ledger.reserve('deepseek-flash', 90, 100)
            from jobhunt.usage import BudgetExceeded
            with self.assertRaises(BudgetExceeded):
                UsageLedger(home).reserve('deepseek-flash', 11, 100)
            self.assertEqual(UsageLedger(home).summary()['unfinished_calls'], 1)

    def test_model_prices_and_cached_tokens_are_configurable(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger = UsageLedger(Path(directory))
            ident = ledger.reserve('test', 1000, 2000)
            ledger.finish(ident, {'prompt_tokens': 100, 'completion_tokens': 20, 'prompt_cache_hit_tokens': 50}, .1,
                          prices={'input': 2, 'output': 4, 'cached_input': .2})
            self.assertAlmostEqual(ledger.summary()['cost'], .00019)

    def test_endpoint_cannot_exfiltrate_key(self):
        for url in ['http://api.deepseek.com', 'https://api.deepseek.com.evil.invalid',
                    'https://secret@api.deepseek.com', 'https://api.deepseek.com/v1?key=secret']:
            with self.subTest(url=url), self.assertRaises(UserError):
                endpoint({'DEEPSEEK_BASE_URL': url})

    def test_env_does_not_execute_and_process_environment_has_priority(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / '.env').write_text('DEEPSEEK_API_KEY=local\nDEEPSEEK_MODEL=deepseek-flash\nPASSWORD=secret\n', encoding='utf-8')
            with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'inherited'}):
                values = read_env(home)
            self.assertEqual(values['DEEPSEEK_API_KEY'], 'inherited')
            self.assertNotIn('PASSWORD', values)

    def test_permission_denied_source_never_fetches(self):
        from jobhunt.sources import RemotiveSource
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / 'config').mkdir()
            (home / 'config/permissions.yaml').write_text('allowed_job_sources: []', encoding='utf-8')
            with patch.object(RemotiveSource, 'fetch') as fetch:
                stats = run_nightly(Context(home, True), [RemotiveSource()])
            fetch.assert_not_called()
            self.assertEqual(stats['sources'][0]['status'], 'UNAUTHORIZED')

    def test_model_changes_invalidate_cache(self):
        with tempfile.TemporaryDirectory() as directory:
            ctx = Context(Path(directory), True, 'deepseek').load()
            before = cache_key(job(), ctx)
            ctx.deepseek_model = 'explicit-other-model'
            self.assertNotEqual(before, cache_key(job(), ctx))

    def test_429_is_bounded_and_auth_failure_is_not_retried(self):
        for status, calls in [(429, 2), (401, 1)]:
            with self.subTest(status=status), tempfile.TemporaryDirectory() as directory:
                ctx = Context(Path(directory), True, 'deepseek')
                with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'sk-test'}), patch('jobhunt.deepseek.time.sleep'), \
                     patch('jobhunt.deepseek.request_json', side_effect=HTTPError('https://api.deepseek.com', status, 'SECRET', {}, None)) as request:
                    result = run_nightly(ctx, [StaticSource([job()])])
                self.assertEqual(request.call_count, calls)
                self.assertNotIn('SECRET', json.dumps(result))

    def test_git_ignore_protects_private_runtime_files(self):
        root = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / '.gitignore').write_bytes((root / '.gitignore').read_bytes())
            subprocess.run(['git', 'init', '-q', str(home)], check=True, capture_output=True)
            paths = ['.env', 'data/demo.sqlite3-wal', 'profile/basic_profile.yaml', 'profile/resumes/latest.pdf',
                     'playwright/.auth/boss.json', 'private/session.json', 'reports/latest.md']
            result = subprocess.run(['git', '-C', str(home), 'check-ignore', '--stdin', '-z'],
                                    input='\0'.join(paths).encode(), capture_output=True, check=True)
            self.assertEqual(set(result.stdout.decode().strip('\0').split('\0')), set(paths))

    def test_schedule_uses_python_directly_and_respects_time(self):
        from jobhunt.scheduling import export_task
        import xml.etree.ElementTree as ET
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            (home / 'config').mkdir()
            (home / 'config/settings.yaml').write_text('nightly_time: "02:30"\nschedule_timezone: UTC\n', encoding='utf-8')
            path = export_task(Context(home, True))
            tree = ET.parse(path)
            ns = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}
            self.assertNotIn('powershell', tree.find('.//t:Command', ns).text.lower())
            self.assertIn('02:30:00+00:00', tree.find('.//t:StartBoundary', ns).text)


if __name__ == '__main__':
    unittest.main()
