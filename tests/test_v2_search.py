"""P1 discovery and command-line behavior, exclusively synthetic/offline inputs."""
from contextlib import closing, redirect_stdout, redirect_stderr
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from jobhunt.config import UserError
from jobhunt.v2.models import Job, SearchCriteria, SourcePolicy
from jobhunt.v2.storage import BusinessStore
from jobhunt.v2 import discovery


def raw(identifier='1', **changes):
    return dict(source='DEMO-test', external_id=identifier, title='DEMO Python engineer',
                company='DEMO company', jd='DEMO Python SQLite backend', city='Shanghai',
                url=f'https://example.invalid/jobs/{identifier}', is_demo=True) | changes


class SyntheticSource:
    policy = SourcePolicy(source='DEMO-test')
    report = {'cache_status': 'LOCAL', 'error_code': ''}

    def __init__(self, rows):
        self.rows = rows

    def search_jobs(self, criteria):
        return self.rows

    def normalize_job(self, row):
        return Job.model_validate(row)


class DiscoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.store = BusinessStore(self.home / 'business.sqlite3', demo=True)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_combined_filters_and_limit_after_dedup(self):
        source = SyntheticSource([raw('skip', city='Beijing'), raw(), raw(), raw('2'), raw('3')])
        criteria = SearchCriteria(keywords=['PYTHON', 'Rust'], cities=['Shanghai'],
                                  technical_directions=['SQLite'], limit=2)
        result = discovery.discover(self.store, source, criteria)
        self.assertEqual(result['selected_count'], 2)
        self.assertEqual(result['new_count'], 2)
        self.assertEqual(result['duplicate_rows_count'], 1)
        self.assertEqual(result['filtered_out'], 1)
        self.assertEqual(result['match_count'], 4)
        self.assertEqual(self.store.list_applications(), [])
        second = discovery.discover(self.store, source, criteria)
        self.assertEqual(second['new_count'], 0)
        self.assertEqual({j['job_id'] for j in result['jobs']}, {j['job_id'] for j in second['jobs']})

    def test_location_unknown_or_worldwide_does_not_imply_city_eligibility(self):
        source = SyntheticSource([raw(city=''), raw('2', city='Worldwide'), raw('3', city='Shanghai', active=False)])
        result = discovery.discover(self.store, source, SearchCriteria(cities=['Shanghai']))
        self.assertEqual(result['selected_count'], 0)
        self.assertEqual(self.store.list_jobs(), [])

    def test_invalid_rows_are_counted_without_sensitive_error_text(self):
        source = SyntheticSource([raw(), {'password': 'private-value'}, None])
        result = discovery.discover(self.store, source, SearchCriteria())
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(result['invalid_count'], 2)
        self.assertEqual(result['selected_count'], 1)
        self.assertNotIn('private-value', repr(result))

    def test_mode_mismatch_aborts_before_any_job_write(self):
        source = SyntheticSource([raw(), raw('2', is_demo=False)])
        with self.assertRaisesRegex(UserError, 'MODE_MISMATCH'):
            discovery.discover(self.store, source, SearchCriteria())
        self.assertEqual(self.store.list_jobs(), [])

    def test_stale_acquisition_is_partial_not_fresh_success(self):
        source = SyntheticSource([raw()])
        source.report = {'cache_status': 'STALE', 'error_code': 'V2_SOURCE_NETWORK_FAILED'}
        result = discovery.discover(self.store, source, SearchCriteria())
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(result['acquisition']['cache_status'], 'STALE')

    def test_identity_conflict_reports_partial_with_already_saved_results(self):
        source = SyntheticSource([raw(), raw('2'), raw(url='https://example.invalid/jobs/2')])
        result = discovery.discover(self.store, source, SearchCriteria())
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(result['selected_count'], 2)
        self.assertEqual(result['new_count'], 2)
        self.assertEqual(result['storage_error_count'], 1)
        self.assertEqual(result['errors'], [{'row': 3, 'error_code': 'V2_JOB_IDENTITY_CONFLICT'}])
        self.assertEqual(len(self.store.list_jobs()), 2)

    def test_alias_filters_use_the_primary_facts_that_will_be_returned(self):
        primary = Job(**raw(title='DEMO Java', jd='DEMO Java', city='Beijing'))
        self.store.save_job(primary, SourcePolicy(source=primary.source))
        alias = raw(source='DEMO-alias')
        source = SyntheticSource([alias])
        source.policy = SourcePolicy(source='DEMO-alias')
        result = discovery.discover(self.store, source, SearchCriteria(cities=['Shanghai'], technical_directions=['Python']))
        self.assertEqual(result['selected_count'], 0)
        result = discovery.discover(self.store, source, SearchCriteria(cities=['Beijing'], technical_directions=['Java']))
        self.assertEqual(result['selected_count'], 1)
        self.assertEqual(result['jobs'][0]['city'], 'Beijing')
        self.assertEqual(result['jobs'][0]['title'], 'DEMO Java')


class SearchCLITests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.input = self.home / 'synthetic.json'
        self.input.write_text(json.dumps([raw(), raw('2', city='Beijing')]), encoding='utf-8')
        self.env = os.environ | {'PYTHONUTF8': '1'}

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args, expected=0):
        result = subprocess.run([sys.executable, '-m', 'jobhunt', 'v2', *args,
                                 '--home', str(self.home), '--demo'],
                                capture_output=True, encoding='utf-8', timeout=30, env=self.env)
        self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
        return json.loads(result.stdout) if expected in (0, 2) and result.stdout else result.stderr

    def test_browser_export_import_matches_only_this_batch_and_skips_empty_export(self):
        old = self.cli('import', str(self.input), '--city', 'Shanghai')['jobs'][0]['job_id']
        browser_export = self.home / 'boss-jobs.json'
        batch = {'jobs': [raw('new', source='DEMO-browser')], 'needs_jd': [raw('pending', jd='')],
                 'needs_city': [], 'filtered_out': 14, 'skipped': 1,
                 'search': {'platform': 'boss', 'keyword': 'Python', 'city': '上海', 'scope': 'first_page'}}
        browser_export.write_text(json.dumps(batch), encoding='utf-8')
        result = self.cli('import', str(browser_export), '--match', 'rules', expected=2)
        self.assertEqual(result['new_count'], 1)
        self.assertEqual(result['acquisition']['browser_export']['needs_jd_count'], 1)
        matched = result['matching']
        self.assertEqual(matched['new_attempts'], 0)
        self.assertEqual([row['job_id'] for row in matched['jobs']], [result['jobs'][0]['job_id']])
        self.assertNotEqual(matched['jobs'][0]['job_id'], old)
        self.assertTrue(Path(matched['reports']['markdown']).is_file())

        browser_export.write_text(json.dumps(batch | {'jobs': [], 'needs_jd': []}), encoding='utf-8')
        empty = self.cli('import', str(browser_export), '--match', 'semantic')
        self.assertEqual(empty['status'], 'NO_CANDIDATES')
        self.assertEqual(empty['matching']['status'], 'SKIPPED_NO_ELIGIBLE_JOBS')
        self.assertEqual(empty['acquisition']['browser_export']['filtered_out'], 14)
        self.assertEqual(empty['matching']['new_attempts'], 0)

    def test_import_list_detail_create_from_id_and_restore_across_processes(self):
        result = self.cli('import', str(self.input), '--city', 'Shanghai', '--keyword', 'Python')
        self.assertEqual(result['selected_count'], 1)
        identifier = result['jobs'][0]['job_id']
        self.assertEqual(self.cli('jobs')['jobs'][0]['job_id'], identifier)
        detail = self.cli('jobs', identifier)
        self.assertEqual(detail['job']['jd'], raw()['jd'])
        self.assertEqual(len(detail['sources']), 1)
        self.assertFalse((self.home / 'data/v2/demo/checkpoints.sqlite3').exists())
        created = self.cli('create', '--job-id', identifier)
        self.assertEqual(created['workflow_status'], 'PAUSED_LOCAL')
        self.assertEqual(self.cli('resume', created['application_id'])['workflow_status'], 'COMPLETE')
        self.assertFalse((self.home / 'data/demo.sqlite3').exists())

    def test_manual_jd_and_partial_import_exit_status(self):
        result = self.cli('add', '--company', 'DEMO company', '--title', 'DEMO engineer',
                          '--url', 'https://example.invalid/manual', '--jd', 'DEMO Python SQLite')
        self.assertEqual(result['new_count'], 1)
        self.input.write_text(json.dumps([raw(), {'jd': 'secret@example.invalid'}]), encoding='utf-8')
        partial = self.cli('import', str(self.input), expected=2)
        self.assertEqual(partial['invalid_count'], 1)
        self.assertNotIn('secret@', repr(partial))

    def test_search_does_not_load_profile_or_model_and_demo_denies_network(self):
        from jobhunt.cli import main
        output, error = io.StringIO(), io.StringIO()
        with patch('jobhunt.config.Context.load', side_effect=AssertionError('profile forbidden')), \
             patch('socket.socket.connect', side_effect=AssertionError('network forbidden')), \
             redirect_stdout(output), redirect_stderr(error):
            code = main(['v2', 'search', '--home', str(self.home), '--demo'])
        self.assertEqual(code, 1)
        self.assertIn('DEMO', error.getvalue())
        self.assertNotIn('forbidden', error.getvalue())

    def test_source_status_is_offline_and_does_not_create_database(self):
        result = self.cli('source-status')
        self.assertFalse(result['network_checked'])
        self.assertFalse((self.home / 'data/v2/demo/business.sqlite3').exists())

    def test_denied_manual_source_never_opens_jd_file(self):
        from jobhunt.cli import main
        (self.home / 'config').mkdir()
        (self.home / 'config/permissions.yaml').write_text('read_job_sources: false\n', encoding='utf-8')
        jd_file = self.home / 'private-jd.txt'
        jd_file.write_text('private job description', encoding='utf-8')
        opened = []
        original = Path.open
        def tracked(path, *args, **kwargs):
            opened.append(path)
            return original(path, *args, **kwargs)
        with patch.object(Path, 'open', tracked), redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
            code = main(['v2', 'add', '--company', 'DEMO', '--title', 'DEMO',
                         '--url', 'https://example.invalid/job', '--jd-file', str(jd_file),
                         '--home', str(self.home), '--demo'])
        self.assertEqual(code, 1)
        self.assertNotIn(jd_file, opened)

    def test_result_ids_are_preserved_but_user_text_stays_redacted(self):
        from jobhunt.v2.cli import display_result
        identifier = 'JOB-1234567890124abc8def0123456789ab'
        result = display_result({'jobs': [{'job_id': identifier, 'title': 'user@example.invalid'}]})
        self.assertEqual(result['jobs'][0]['job_id'], identifier)
        self.assertNotIn('user@', repr(result))


if __name__ == '__main__':
    unittest.main()
