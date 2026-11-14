from contextlib import closing, redirect_stdout, redirect_stderr
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jobhunt.cli import main
from jobhunt.config import Context
from jobhunt.v2.cli import display_result
from jobhunt.v2.models import Job, SourcePolicy
from jobhunt.v2.storage import BusinessStore


class ChannelCLITests(unittest.TestCase):
    def call(self, args):
        output = io.StringIO()
        with redirect_stdout(output), redirect_stderr(output):
            code = main(['v2']+args)
        return code, output.getvalue()

    def test_catalog_and_url_preflight_do_not_read_profile_or_accounts(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(Context,'load',side_effect=AssertionError('private profile')):
            for args in (['channels'],['preflight','--url','https://www.zhipin.com/job_detail/123.html']):
                code, output = self.call(args+['--home',temporary])
                self.assertEqual(code,0)
                self.assertFalse(json.loads(output)['network_checked'])
                self.assertFalse((Path(temporary)/'data').exists())

    def test_saved_job_preflight_and_demo_manual_keep_application_counts_zero(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            path = home/'data/v2/demo/business.sqlite3'
            with closing(BusinessStore(path,demo=True)) as store:
                job = Job(source='DEMO',title='DEMO Python',company='DEMO Company',jd='Python DEMO',
                          url='https://careers.example.org/jobs/1',is_demo=True)
                job_id = store.save_job(job,SourcePolicy(source='DEMO'))[0]
            for command in ('preflight','prepare-manual'):
                code, output = self.call([command,'--job-id',job_id,'--home',temporary,'--demo'])
                self.assertEqual(code,0)
                result = json.loads(output)
                self.assertEqual(result['status'],'MANUAL_REQUIRED')
                self.assertEqual(result['job_id'],job_id)
            with closing(BusinessStore(path,demo=True)) as store:
                self.assertEqual(len(store.list_applications()),0)

    def test_missing_database_and_invalid_url_are_safe_nonzero_errors(self):
        with tempfile.TemporaryDirectory() as temporary:
            code, output = self.call(['preflight','--job-id','JOB-missing','--home',temporary])
            self.assertNotEqual(code,0)
            self.assertIn('V2_DATABASE_MISSING',output)
            code, output = self.call(['preflight','--url','https://user:private-sentinel@zhipin.com/','--home',temporary])
            self.assertNotEqual(code,0)
            self.assertNotIn('private-sentinel',output)

    def test_manual_package_ids_and_paths_survive_redaction(self):
        identifier = 'MANUAL-'+'1'*64
        directory = Path('E:/project/reports/v2/live')/identifier
        original = {'package_id':identifier,'review_path':str(directory/'review.html'),
                    'package_path':str(directory/'package.json')}
        self.assertEqual(display_result(original),original)
