import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Thread
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from jobhunt.config import Context, UserError
from jobhunt.v2.hr_reply import DEMO_JOB, ReplyStore, demo_draft, live_draft, make_server, model_draft
from jobhunt.v2.models import Job


class ReplyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'drafts.sqlite3'
        self.store = ReplyStore(self.path)
        self.store.bind('chat-a', 'demo-job')
        self.message = dict(job_id='demo-job', conversation_id='chat-a',
                            message_id='one', message_text='最快什么时候能到岗？')

    def test_reply_and_recovery_do_not_generate_twice(self):
        generate = Mock(side_effect=demo_draft)
        draft = self.store.submit(self.message, generate)
        self.assertEqual(draft['status'], 'ready')
        self.assertIn('1月', draft['draft'])
        self.assertEqual(ReplyStore(self.path).get('chat-a', 'one'), draft)
        self.assertEqual(self.store.submit(self.message, generate), draft)
        generate.assert_called_once()

    def test_binding_conflict_and_message_content_conflict(self):
        self.store.submit(self.message, demo_draft)
        for candidate in (dict(job_id='other-job'), dict(message_text='不同内容')):
            with self.assertRaises(UserError):
                self.store.submit(self.message | candidate, demo_draft)
        self.store.bind('chat-b', 'demo-job')
        result = self.store.submit(self.message | {'conversation_id': 'chat-b',
                                    'message_text': '能提前到岗吗？'}, demo_draft)
        self.assertEqual(result['status'], 'needs_review')
        self.assertTrue(result['review_reasons'])

    def test_concurrent_requests_share_one_attempt(self):
        entered, release = Event(), Event()

        def paused(job, message):
            entered.set()
            if not release.wait(3):
                raise RuntimeError('test deadline')
            return demo_draft(job, message)

        with ThreadPoolExecutor() as pool:
            result = pool.submit(self.store.submit, self.message, paused)
            try:
                self.assertTrue(entered.wait(3))
                generate = Mock(side_effect=demo_draft)
                self.assertEqual(self.store.submit(self.message, generate)['status'], 'processing')
                generate.assert_not_called()
            finally:
                release.set()
            self.assertEqual(result.result()['status'], 'ready')

    def test_budget_failure_requires_explicit_retry(self):
        generate = Mock(side_effect=UserError('DAILY_TOKEN_BUDGET'))
        result = self.store.submit(self.message, generate)
        self.assertEqual(result, {'status': 'failed', 'error_code': 'DAILY_TOKEN_BUDGET'})
        self.assertEqual(self.store.submit(self.message, demo_draft), result)
        self.assertEqual(self.store.submit(self.message, demo_draft, retry=True)['status'], 'ready')

    def test_interrupted_request_is_recovered_without_replay(self):
        with self.assertRaises(KeyboardInterrupt):
            self.store.submit(self.message, Mock(side_effect=KeyboardInterrupt))
        self.store.recover_interrupted()
        generate = Mock(side_effect=demo_draft)
        result = self.store.submit(self.message, generate)
        self.assertEqual(result, {'status': 'failed', 'error_code': 'HR_REPLY_INTERRUPTED'})
        generate.assert_not_called()

    def test_http_rejects_unpaired_and_other_origin_requests(self):
        origin = 'chrome-extension://' + 'a' * 32
        with make_server(self.store, origin, 'sample-token', 0, lambda: [DEMO_JOB], demo_draft, demo=False) as server:
            thread = Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                url = f'http://127.0.0.1:{server.server_port}/v1/hr/jobs'
                for headers in ({}, {'X-JobHunt-Extension': 'a' * 32, 'Authorization': 'Bearer sample-token',
                                     'Origin': 'https://example.invalid'}):
                    with self.assertRaises(HTTPError) as captured:
                        urlopen(Request(url, headers=headers), timeout=2)
                    self.assertEqual(captured.exception.code, 403)
                with urlopen(Request(url, headers={'X-JobHunt-Extension': 'a' * 32,
                                                   'Authorization': 'Bearer sample-token'}), timeout=2) as response:
                    self.assertEqual(response.status, 200)
            finally:
                server.shutdown()
                thread.join()

    @patch('jobhunt.v2.hr_reply.model_draft')
    def test_full_time_start_and_contact_request_stay_for_manual_review(self, generate):
        ctx = Context(Path(self.tmp.name), demo=True).load()
        ctx.preferences.internship_start_month = '2027-01'
        job = Job(source='fixture', company='模拟公司', title='全职开发工程师',
                  url='https://example.org/job', jd='维护服务。')
        for message in ('全职最快什么时候能入职？', '请加我的微信 recruit_bob，联系人王女士。'):
            result = live_draft(ctx, job.model_dump(mode='json'), message)
            self.assertEqual(result['status'], 'needs_review')
            self.assertNotIn('1月', result['draft'])
        internship = job.model_dump(mode='json') | {'title': '开发实习生'}
        self.assertIn('2027年1月', live_draft(ctx, internship, '什么时候可以到岗？')['draft'])
        generate.assert_not_called()

    @patch('jobhunt.v2.hr_reply.deepseek.read_env', return_value={'DEEPSEEK_API_KEY': 'fixture-key'})
    @patch('jobhunt.v2.hr_reply.deepseek.request_json')
    def test_model_uses_only_redacted_selected_data(self, request, read_env):
        ctx = Context(Path(self.tmp.name), demo=True).load()
        request.return_value = {'choices': [{'finish_reason': 'stop', 'message': {
            'role': 'assistant', 'content': '{"draft":"您好，我可以介绍相关项目。","review_reasons":[]}'}}],
            'usage': {'prompt_tokens': 36, 'completion_tokens': 12}}
        result = model_draft(ctx, {'title': 'Agent 实习', 'company': '示例公司', 'jd': '维护 Python 服务。'},
                             '能聊聊 Python 吗？我的邮箱是 recruiter@example.org。', facts={'skill': 'Python'})
        self.assertEqual(result['status'], 'needs_review')
        prompt = request.call_args.args[1]['messages'][1]['content']
        self.assertIn('维护 Python 服务', prompt)
        self.assertIn('[EMAIL]', prompt)
        self.assertNotIn('recruiter@example.org', prompt)
        self.assertNotIn(ctx.safe_profile['summary'], prompt)


if __name__ == '__main__':
    unittest.main()
