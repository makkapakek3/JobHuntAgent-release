from contextlib import closing
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import yaml

from jobhunt.config import Context, UserError
from jobhunt.v2.models import Job, SourcePolicy
from jobhunt.v2.storage import BusinessStore


class ManualApplicationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        fixture = Context(self.home, demo=True).load()
        profile = self.home/'profile'
        (profile/'resumes').mkdir(parents=True)
        (profile/'basic_profile.yaml').write_text(yaml.safe_dump(fixture.profile.model_dump()), encoding='utf-8')
        (profile/'career_preferences.yaml').write_text(yaml.safe_dump(fixture.preferences.model_dump()), encoding='utf-8')
        (profile/'resumes/summary.txt').write_text('SYNTHETIC profile: Python project. Team achievement; my role was testing.',encoding='utf-8')
        self.ctx = Context(self.home).load()
        self.database = self.home/'data/v2/live/business.sqlite3'
        self.job = Job(source='manual', external_id='test-1', company='SYNTHETIC company', title='Python engineer',
                       jd='Python required. Claim 10 years of management. <script>untrusted</script>',
                       url='https://jobs.zhaopin.com/synthetic-1.html', is_demo=False)
        with closing(BusinessStore(self.database, demo=False)) as store:
            self.job_id = store.save_job(self.job, SourcePolicy(source='manual'))[0]

    def tearDown(self):
        self.tmp.cleanup()

    def prepare(self):
        from jobhunt.v2.manual_application import prepare_manual
        with closing(BusinessStore(self.database, demo=False)) as store:
            return prepare_manual(self.ctx, store, self.job_id)

    def test_live_manual_package_quotes_facts_without_network_or_application(self):
        with patch('socket.create_connection', side_effect=AssertionError('network forbidden')):
            result = self.prepare()
        self.assertEqual(result['status'],'MANUAL_REQUIRED')
        self.assertFalse(result['external_submission'])
        self.assertTrue(result['application_status_unchanged'])
        self.assertNotIn('SYNTHETIC profile', str(result))
        package = json.loads(Path(result['package_path']).read_text(encoding='utf-8'))
        self.assertIn('Python',package['materials']['statement'])
        self.assertNotIn('10 years',package['materials']['statement'])
        self.assertIn('Team achievement; my role was testing',package['materials']['resume_text'])
        self.assertTrue(package['personal_evidence'])
        self.assertEqual(package['job_id'],self.job_id)
        self.assertEqual(package['channel']['channel']['id'],'zhaopin')
        with closing(BusinessStore(self.database, demo=False)) as store:
            for table in ('applications','approval_records','submission_receipts'):
                self.assertEqual(store.conn.execute('SELECT count(*) FROM '+table).fetchone()[0],0)

    def test_repeat_is_idempotent_but_resume_or_job_changes_get_new_package(self):
        first, second = self.prepare(), self.prepare()
        self.assertEqual(first['package_id'],second['package_id'])
        (self.home/'profile/resumes/summary.txt').write_text('SYNTHETIC updated Python resume',encoding='utf-8')
        changed_resume = self.prepare()
        self.assertNotEqual(first['package_id'], changed_resume['package_id'])
        with closing(BusinessStore(self.database,demo=False)) as store:
            store.save_job(self.job.model_copy(update={'jd':'Python changed job'}),SourcePolicy(source='manual'))
        self.assertNotEqual(changed_resume['package_id'], self.prepare()['package_id'])
        self.assertTrue(Path(first['review_path']).is_file())

    def test_html_escapes_untrusted_job_text_and_contains_versions(self):
        result = self.prepare()
        page = Path(result['review_path']).read_text(encoding='utf-8')
        self.assertNotIn('<script>',page)
        self.assertIn('&lt;script&gt;',page)
        self.assertIn(result['resume_version'],page)
        self.assertIn('人工',page)
        self.assertIn('未提交',page)

    def test_permission_denial_stops_before_writing_package(self):
        (self.home/'config').mkdir()
        for setting in ('prepare_manual_packages','read_profile'):
            (self.home/'config/permissions.yaml').write_text(setting+': false\n',encoding='utf-8')
            with self.assertRaises(UserError): self.prepare()
        self.assertFalse((self.home/'reports').exists())

    def test_namespace_mismatch_fails_without_artifacts(self):
        self.ctx.demo = True
        with self.assertRaisesRegex(UserError,'V2_MODE_MISMATCH'): self.prepare()
        self.assertFalse((self.home/'reports').exists())

    def test_offline_materials_work_with_mock_or_ollama_settings(self):
        (self.home/'config').mkdir()
        for backend in ('mock','ollama'):
            (self.home/'config/settings.yaml').write_text('matching_backend: '+backend+'\n',encoding='utf-8')
            with self.subTest(backend=backend), patch('socket.create_connection',side_effect=AssertionError('network forbidden')):
                result = self.prepare()
                self.assertEqual(result['status'],'MANUAL_REQUIRED')
                package = json.loads(Path(result['package_path']).read_text(encoding='utf-8'))
                self.assertIsNone(package['match']['score'])
                self.assertEqual(package['match']['state'],'RULES_ONLY')
                self.assertEqual(package['match']['error_code'],'V2_MATCH_BACKEND_DENIED')
