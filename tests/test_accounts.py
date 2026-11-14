import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from jobhunt.accounts import AccountManager, classify_page, browser_environment, page_signals, protect_directory
from jobhunt.config import UserError
from jobhunt.storage import run_lock


class AccountTests(unittest.TestCase):
    def test_no_cookie_or_url_only_authentication_claim(self):
        self.assertEqual(classify_page('https://www.zhipin.com/web/geek/job', {}), 'UNKNOWN')
        self.assertEqual(classify_page('https://www.zhipin.com/', {'account_menu': True}), 'UNKNOWN')

    def test_hidden_member_navigation_is_not_login_evidence(self):
        page = MagicMock()
        visible_item, hidden_item = MagicMock(), MagicMock()
        visible_item.is_visible.return_value = True
        hidden_item.is_visible.return_value = False
        def locator(selector):
            result = MagicMock()
            result.all.return_value = [visible_item] if 'nav-figure' in selector else [hidden_item]
            return result
        page.locator.side_effect = locator
        page.get_by_text.return_value.first.is_visible.return_value = False
        signals = page_signals(page)
        self.assertTrue(signals['account_menu'])
        self.assertFalse(signals['member_navigation'])
        self.assertEqual(classify_page('https://www.zhipin.com/', signals), 'UNKNOWN')

    def test_authentication_requires_official_origin_and_multiple_signals(self):
        signals = {'account_menu': True, 'member_navigation': True}
        self.assertEqual(classify_page('https://www.zhipin.com/', signals), 'AUTHENTICATED')
        self.assertEqual(classify_page('https://www.zhipin.com.evil.invalid/', signals), 'UNKNOWN')
        self.assertEqual(classify_page('http://www.zhipin.com/', signals), 'UNKNOWN')

    def test_login_verification_and_restriction_override_old_account_elements(self):
        common = {'account_menu': True, 'member_navigation': True}
        for signal, state in [('login_form', 'LOGIN_REQUIRED'), ('verification', 'VERIFICATION_REQUIRED'), ('restricted', 'ACCESS_RESTRICTED')]:
            self.assertEqual(classify_page('https://www.zhipin.com/', {**common, signal: True}), state)
        self.assertEqual(classify_page('https://www.zhipin.com/web/user/', common), 'LOGIN_REQUIRED')
        self.assertEqual(classify_page('https://www.zhipin.com/', common, 403), 'ACCESS_RESTRICTED')

    def test_status_never_implies_last_success_is_current_login(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = AccountManager(Path(directory))
            self.assertEqual(manager.status()['status'], 'NOT_CONFIGURED')
            manager.record('AUTHENTICATED', 'check', 'chrome')
            result = manager.status()
            self.assertEqual(result['status'], 'SAVED_STATE_UNVERIFIED')
            self.assertEqual(result['last_result'], 'AUTHENTICATED')
            self.assertFalse(result['live_checked'])

    def test_account_storage_is_separate_and_contains_no_credentials(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            manager = AccountManager(home)
            self.assertTrue(manager.profile_path.is_relative_to(home.resolve() / 'private'))
            manager.record('LOGIN_REQUIRED', 'check', 'chrome')
            value = manager.state_path.read_text(encoding='utf-8')
            self.assertNotIn('cookie', value.lower())
            self.assertNotIn('password', value.lower())
            self.assertNotIn('DEEPSEEK', value)

    def test_demo_mode_cannot_open_real_account(self):
        with tempfile.TemporaryDirectory() as directory:
            from jobhunt.cli import main
            with patch('jobhunt.accounts.AccountManager.open') as opened:
                self.assertEqual(main(['--home', directory, '--demo', 'auth', 'login', 'boss']), 1)
                opened.assert_not_called()

    def test_boss_login_parser_and_readonly_status(self):
        from jobhunt.cli import parser, main
        args = parser().parse_args(['auth', 'login', 'boss', '--timeout', '120'])
        self.assertEqual(args.timeout, 120)
        with tempfile.TemporaryDirectory() as directory, patch('jobhunt.accounts.AccountManager.open') as opened:
            self.assertEqual(main(['--home', directory, 'auth', 'status', 'boss']), 0)
            opened.assert_not_called()

    def test_browser_child_does_not_inherit_model_or_platform_credentials(self):
        with patch.dict(os.environ, {'DEEPSEEK_API_KEY': 'sk-never-copy', 'BOSS_PASSWORD': 'never-copy', 'PATH': 'test-path'}):
            env = browser_environment()
        self.assertNotIn('DEEPSEEK_API_KEY', env)
        self.assertNotIn('BOSS_PASSWORD', env)
        self.assertEqual(env['PATH'], 'test-path')

    def test_profile_lock_prevents_duplicate_launch(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = AccountManager(Path(directory))
            with run_lock(manager.lock_path), patch('jobhunt.accounts.launch_browser') as launch:
                with self.assertRaisesRegex(UserError, 'RUN_LOCKED'):
                    manager.open('check', timeout=1)
                launch.assert_not_called()

    def test_browser_errors_do_not_expose_exception_body(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = AccountManager(Path(directory))
            with patch('jobhunt.accounts.launch_browser', side_effect=RuntimeError('password=secret-cookie')):
                result = manager.open('check', timeout=1)
            self.assertEqual(result['status'], 'BROWSER_ERROR')
            self.assertNotIn('secret-cookie', json.dumps(result))
            self.assertNotIn('secret-cookie', manager.state_path.read_text(encoding='utf-8'))

    def test_reused_login_does_not_navigate_to_login_form(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = AccountManager(Path(directory))
            page = MagicMock()
            page.url = 'https://www.zhipin.com/'
            page.is_closed.return_value = False
            page.goto.return_value.status = 200
            context = MagicMock()
            context.pages = [page]
            with patch('jobhunt.accounts.launch_browser') as launch, patch('jobhunt.accounts.page_signals',
                    return_value={'account_menu': True, 'member_navigation': True}):
                launch.return_value.__enter__.return_value = context
                result = manager.open('login', timeout=2)
            self.assertEqual(result['status'], 'AUTHENTICATED')
            self.assertEqual(page.goto.call_count, 1)

    def test_expired_session_check_stops_without_login_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = AccountManager(Path(directory))
            page = MagicMock()
            page.url = 'https://www.zhipin.com/web/user/'
            page.is_closed.return_value = False
            page.goto.return_value.status = 200
            context = MagicMock()
            context.pages = [page]
            with patch('jobhunt.accounts.launch_browser') as launch, patch('jobhunt.accounts.page_signals', return_value={}):
                launch.return_value.__enter__.return_value = context
                result = manager.open('check', timeout=2)
            self.assertEqual(result['status'], 'LOGIN_REQUIRED')
            self.assertEqual(page.goto.call_count, 1)

    def test_hidden_dropdown_is_revealed_without_clicking(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = AccountManager(Path(directory))
            page = MagicMock()
            page.url = 'https://www.zhipin.com/'
            page.is_closed.return_value = False
            page.goto.return_value.status = 200
            avatar = MagicMock()
            avatar.is_visible.return_value = True
            page.locator.return_value.all.return_value = [avatar]
            context = MagicMock()
            context.pages = [page]
            hidden = {'account_menu': True, 'member_navigation': False}
            visible = {'account_menu': True, 'member_navigation': True}
            with patch('jobhunt.accounts.launch_browser') as launch, patch('jobhunt.accounts.page_signals', side_effect=[hidden, visible, visible]):
                launch.return_value.__enter__.return_value = context
                result = manager.open('check', timeout=2)
            self.assertEqual(result['status'], 'AUTHENTICATED')
            avatar.hover.assert_called_once()
            avatar.click.assert_not_called()
            page.click.assert_not_called()

    def test_navigation_failure_is_not_login_failure_or_leaked_error(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = AccountManager(Path(directory))
            page = MagicMock()
            page.is_closed.return_value = False
            page.goto.side_effect = RuntimeError('sensitive-page-content')
            context = MagicMock()
            context.pages = [page]
            with patch('jobhunt.accounts.launch_browser') as launch:
                launch.return_value.__enter__.return_value = context
                result = manager.open('check', timeout=2)
            self.assertEqual(result['status'], 'NETWORK_ERROR')
            self.assertFalse(result['live_checked'])
            self.assertNotIn('sensitive-page-content', json.dumps(result))

    def test_blank_page_after_navigation_stops_without_claiming_login(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = AccountManager(Path(directory))
            page = MagicMock()
            page.url = 'about:blank'
            page.is_closed.return_value = False
            page.goto.return_value.status = 200
            context = MagicMock()
            context.pages = [page]
            with patch('jobhunt.accounts.launch_browser') as launch:
                launch.return_value.__enter__.return_value = context
                result = manager.open('check', timeout=2)
            self.assertEqual(result['status'], 'PAGE_UNAVAILABLE')
            self.assertEqual(page.goto.call_count, 1)
            page.wait_for_timeout.assert_not_called()

    def test_corrupt_history_is_not_returned_to_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = AccountManager(Path(directory))
            manager.private.mkdir(parents=True)
            manager.state_path.write_text('{"last_result":"secret-value"}', encoding='utf-8')
            self.assertNotIn('secret-value', json.dumps(manager.status()))

    @unittest.skipUnless(os.name == 'nt', 'Windows ACL integration')
    def test_existing_explicit_public_grant_is_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            private = Path(directory) / 'private'
            private.mkdir()
            subprocess.run(['icacls', str(private), '/grant', '*S-1-1-0:(OI)(CI)R'], check=True, capture_output=True)
            protect_directory(private)
            # Reintroduce a broad explicit child grant. Validation must refuse it.
            child = private / 'unexpected.txt'
            child.write_text('synthetic', encoding='utf-8')
            subprocess.run(['icacls', str(child), '/grant', '*S-1-1-0:R'], check=True, capture_output=True)
            with self.assertRaisesRegex(UserError, 'PRIVATE_DIRECTORY_ACL_FAILED'):
                protect_directory(private)


if __name__ == '__main__':
    unittest.main()
