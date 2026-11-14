import unittest
from unittest.mock import patch
from jobhunt.config import UserError


class ChannelTests(unittest.TestCase):
    def preflight(self, url):
        from jobhunt.v2.channels import preflight
        return preflight(url)

    def test_known_hosts_are_manual_until_permission_and_adapter_exist(self):
        for url, identifier in [('https://www.zhipin.com/job_detail/123.html','boss'),
                                ('https://jobs.51job.com/shanghai/123.html','51job'),
                                ('https://jobs.zhaopin.com/123.htm','zhaopin'),
                                ('https://job-boards.greenhouse.io/example/jobs/1','greenhouse'),
                                ('https://jobs.lever.co/example/1','lever'),
                                ('https://cloud.italent.cn/PageHome/Index','italent')]:
            with self.subTest(url=url), patch('socket.create_connection', side_effect=AssertionError('network forbidden')):
                result = self.preflight(url)
                self.assertEqual(result['channel']['id'], identifier)
                self.assertEqual(result['status'], 'MANUAL_REQUIRED')
                self.assertFalse(any(result['automation'].values()))
                self.assertFalse(result['network_checked'])
                self.assertTrue(result['per_application_approval_required'])

    def test_spoofed_hosts_and_redirect_wrappers_never_inherit_permissions(self):
        for url in ('https://zhipin.com.example.org/job', 'https://fakezhipin.com/job',
                    'https://careers.example.org/?redirect=https%3A%2F%2Fjobs.lever.co%2Fa',
                    'https://evil.example.org/greenhouse.io/jobs/1'):
            result = self.preflight(url)
            self.assertEqual(result['channel']['id'], 'unknown')
            self.assertFalse(result['automation']['submit'])

    def test_unsafe_or_credential_urls_are_rejected_without_echo(self):
        for url in ('http://www.zhipin.com/job','file:///tmp/job','javascript:alert(1)',
                    'https://user:private-sentinel@www.zhipin.com/', 'https://www.zhipin.com:444/',
                    'https://127.0.0.1/', 'https://[::1]/', 'https://localhost/',
                    'https://127.1/', 'https://0177.0.0.1/', 'https://0x7f.0.0.1/',
                    'https://0x7f.0x0.0x0.0x1/',
                    'https://www.zhipin.com\n.evil.example.org/', 'https://foo.local/',
                    'https://www.zhipin.com/?access_token=private-sentinel',
                    'https://www.zhipin.com/?key=private-sentinel',
                    'https://www.zhipin.com/#private-sentinel'):
            with self.subTest(url=url):
                with self.assertRaises(UserError) as raised:
                    self.preflight(url)
                self.assertNotIn('private-sentinel', str(raised.exception))

    def test_preflight_output_contains_only_origin_not_arbitrary_url_text(self):
        result = self.preflight('https://careers.example.org/private-sentinel?job=private-sentinel')
        self.assertEqual(result['target_origin'], 'https://careers.example.org')
        self.assertNotIn('private-sentinel', str(result))

    def test_catalog_is_offline_and_index_evidence_is_labelled(self):
        from jobhunt.v2.channels import channel_catalog
        result = channel_catalog()
        self.assertEqual(len(result['channels']), 6)
        self.assertFalse(result['network_checked'])
        boss = next(row for row in result['channels'] if row['id'] == 'boss')
        self.assertEqual(boss['evidence'][0]['retrieval'], 'SEARCH_INDEX_ONLY')
        self.assertEqual(boss['reviewed_on'], '2026-11-05')
        result['channels'][0]['id'] = 'changed'
        self.assertNotEqual(channel_catalog()['channels'][0]['id'], 'changed')

    def test_private_invitations_are_not_public_job_links_even_without_fragment(self):
        for key in ('inviteKey', 'INVITEKEY', 'invite_key', '%69nviteKey', 'bc_sign',
                    'bc_nonce', 'quark_s', 'shadow_context', 'sign', 'sig'):
            with self.subTest(key=key):
                with self.assertRaisesRegex(UserError, 'P4_PUBLIC_JOB_URL_REQUIRED') as raised:
                    self.preflight('https://cloud.italent.cn/PageHome/Index?' + key + '=private-sentinel')
                self.assertNotIn('private-sentinel', str(raised.exception))

    def test_provider_website_terms_do_not_claim_target_authorization(self):
        result = self.preflight('https://cloud.italent.cn/PageHome/Index')
        self.assertEqual(result['channel']['permission_status'], 'TARGET_PERMISSION_NOT_VERIFIED')
        self.assertEqual(result['channel']['evidence'][0]['scope'], 'PROVIDER_WEBSITE_ONLY')
        self.assertFalse(any(result['automation'].values()))

    def test_runtime_gate_never_constructs_an_executor_for_unsupported_channels(self):
        from jobhunt.v2.channels import require_automated_channel
        with self.assertRaisesRegex(UserError, 'P4_MANUAL_REQUIRED'):
            require_automated_channel('https://jobs.lever.co/example/1')
