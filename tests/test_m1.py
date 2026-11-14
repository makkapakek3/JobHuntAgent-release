"""M1 acceptance: no network, independent CLI, bounded API and truthful reports."""
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from jobhunt.config import Context, UserError
from jobhunt.models import Settings
from jobhunt.graph import run_nightly
from jobhunt.sources import FileSource
from jobhunt.storage import Store
from test_graph import StaticSource, job

RESULT = {'score': 85, 'reasons': ['TEST 匹配'], 'gaps': [], 'risks': [], 'confidence': 0.7}


def reply(content=None, tools=None, usage=True):
    value = {'choices': [{'message': {'role': 'assistant', 'content': json.dumps(RESULT) if content is None else content}, 'finish_reason': 'stop'}]}
    if tools:
        value['choices'][0]['message']['tool_calls'] = tools
    if usage:
        value['usage'] = {'prompt_tokens': 100, 'completion_tokens': 30, 'prompt_cache_hit_tokens': 0}
    return value


class M1Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.ctx = Context(self.home, True)
        self.env = patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'sk-test-secret', 'DEEPSEEK_MODEL': 'deepseek-flash'})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.tmp.cleanup()

    def configure(self, **settings):
        folder = self.home / 'config'
        folder.mkdir(exist_ok=True)
        (folder / 'settings.yaml').write_text(json.dumps({'matching_backend': 'deepseek', **settings}), encoding='utf-8')

    def test_new_cli_is_independent_and_incremental(self):
        env = {**os.environ, 'PYTHONUTF8': '1', 'PYTHONPATH': str(Path(__file__).resolve().parents[1] / 'src')}
        def cli(*args):
            p = subprocess.run([sys.executable, '-m', 'jobhunt', '--home', str(self.home), '--demo', *args], capture_output=True, text=True, encoding='utf-8', env=env, timeout=30)
            self.assertEqual(p.returncode, 0, p.stdout + p.stderr)
            return p.stdout
        a = json.loads(cli('nightly', '--dry-run'))
        b = json.loads(cli('nightly', '--dry-run'))
        self.assertGreater(a['analyzed'], 0)
        self.assertEqual(b['analyzed'], 0)
        ids = json.loads(cli('jobs', '--status', 'pending'))
        cli('approve', ids[0]['job_id'])
        self.assertIn('APPROVED', cli('jobs', '--status', 'approved'))
        self.assertIn('DEMO', cli('report', '--latest'))
        self.assertIn('daily', cli('status'))

    def test_flash_request_and_daily_usage(self):
        self.configure()
        from jobhunt.usage import UsageLedger
        with patch('jobhunt.deepseek.request_json', return_value=reply()) as request:
            stats = run_nightly(self.ctx, [StaticSource([job()])])
        self.assertFalse(stats['errors'], stats)
        body = request.call_args.args[1]
        self.assertEqual(body['model'], 'deepseek-flash')
        self.assertEqual(body['response_format'], {'type': 'json_object'})
        self.assertEqual(body['thinking'], {'type': 'disabled'})
        text = json.dumps(body)
        self.assertNotIn('sk-test-secret', text)
        totals = UsageLedger(self.home).summary('daily')
        self.assertEqual(totals['input_tokens'], 100)
        self.assertEqual(totals['output_tokens'], 30)
        self.assertIsNone(totals['cost'])
        with patch('jobhunt.deepseek.request_json', side_effect=AssertionError('cache must avoid network')):
            self.assertEqual(run_nightly(self.ctx, [StaticSource([job()])])['analyzed'], 0)

    def test_invalid_json_preserves_usage_and_other_results(self):
        self.configure()
        from jobhunt.usage import UsageLedger
        with patch('jobhunt.deepseek.request_json', side_effect=[reply('invalid'), reply()]):
            stats = run_nightly(self.ctx, [StaticSource([job(1), job(2)])])
        self.assertTrue(stats['errors'])
        self.assertEqual(stats['status'], 'PARTIAL')
        self.assertEqual(UsageLedger(self.home).summary('daily')['input_tokens'], 200)
        db = Store(self.ctx.db_path)
        try:
            self.assertEqual(len(db.listing('PENDING_REVIEW')), 1)
        finally:
            db.close()

    def test_model_may_read_only_whitelisted_profile_tools(self):
        self.configure()
        tool = {'id': 'call_1', 'type': 'function', 'function': {'name': 'get_profile_section', 'arguments': '{"section":"projects"}'}}
        with patch('jobhunt.deepseek.request_json', side_effect=[reply('', [tool]), reply()]) as request:
            stats = run_nightly(self.ctx, [StaticSource([job()])])
        self.assertFalse(stats['errors'])
        self.assertEqual(request.call_count, 2)
        messages = request.call_args.args[1]['messages']
        self.assertTrue(any(m['role'] == 'tool' for m in messages))
        tool['function']['arguments'] = '{"section":"../../.env"}'
        with patch('jobhunt.deepseek.request_json', return_value=reply('', [tool])) as request:
            stats = run_nightly(self.ctx, [StaticSource([job(2)])])
        self.assertTrue(stats['errors'])
        self.assertEqual(request.call_count, 1)

    def test_budget_persists_and_prevents_api_request(self):
        self.configure(daily_token_budget=1)
        with patch('jobhunt.deepseek.request_json') as request:
            a = run_nightly(self.ctx, [StaticSource([job()])])
            b = run_nightly(self.ctx, [StaticSource([job()])])
        request.assert_not_called()
        self.assertTrue(a['errors'])
        self.assertTrue(b['errors'])
        self.assertEqual(a['deferred'], 1)

    def test_timeouts_are_bounded_and_unknown_usage_is_reserved(self):
        self.configure(llm_max_retries=1)
        from jobhunt.usage import UsageLedger
        with patch('jobhunt.deepseek.request_json', side_effect=TimeoutError('sk-test-secret')) as request, patch('jobhunt.deepseek.time.sleep'):
            stats = run_nightly(self.ctx, [StaticSource([job()])])
        self.assertEqual(request.call_count, 2)
        self.assertNotIn('sk-test-secret', json.dumps(stats))
        totals = UsageLedger(self.home).summary('daily')
        self.assertEqual(totals['unknown_usage_calls'], 2)
        self.assertGreater(totals['budget_used'], 0)
        self.assertGreaterEqual(stats['llm_seconds'], 0)

    def test_missing_key_never_falls_back_to_mock(self):
        self.configure()
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': ''}), patch('jobhunt.deepseek.request_json') as request:
            stats = run_nightly(self.ctx, [StaticSource([job()])])
        request.assert_not_called()
        self.assertTrue(any('API_KEY_MISSING' in error for error in stats['errors']))

    def test_same_employer_title_but_different_requisitions_not_collapsed(self):
        db = Store(self.ctx.db_path)
        try:
            a = job().model_copy(update={'is_demo': False})
            b = a.model_copy(update={'external_id': '2', 'url': 'https://example.invalid/jobs/2', 'jd': 'Python different team'})
            first = db.upsert(a)[0]
            second = db.upsert(b)[0]
            self.assertNotEqual(first, second)
        finally:
            db.close()

    def test_manual_import_is_not_claimed_verified_real_source(self):
        path = self.home / 'jobs.json'
        path.write_text(json.dumps([job().model_copy(update={'is_demo': False}).model_dump()]), encoding='utf-8')
        run_nightly(self.ctx, [FileSource(path)])
        report = (self.ctx.reports / 'latest.md').read_text(encoding='utf-8')
        self.assertIn('用户导入，未联网验证', report)
        self.assertNotIn('真实来源数据', report)


if __name__ == '__main__':
    unittest.main()
