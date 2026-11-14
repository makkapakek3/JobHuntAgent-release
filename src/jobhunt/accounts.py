"""Interactive BOSS login and private, persistent browser sessions. No job scraping."""
from contextlib import contextmanager
import base64
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import urlsplit

from .config import UserError
from .storage import run_lock

LOGIN_URL = 'https://www.zhipin.com/web/user/'
CHECK_URL = 'https://www.zhipin.com/'
STATES = {'AUTHENTICATED', 'LOGIN_REQUIRED', 'VERIFICATION_REQUIRED', 'ACCESS_RESTRICTED',
          'UNKNOWN', 'NETWORK_ERROR', 'BROWSER_ERROR', 'CLOSED_BY_USER', 'TIMEOUT', 'PAGE_UNAVAILABLE'}


def browser_environment():
    """Only OS runtime variables; never pass model/platform credentials to Chrome."""
    allowed = {'PATH', 'SYSTEMROOT', 'WINDIR', 'COMSPEC', 'USERPROFILE', 'LOCALAPPDATA',
               'APPDATA', 'TEMP', 'TMP', 'PROGRAMFILES', 'PROGRAMFILES(X86)', 'PROGRAMDATA',
               'HOMEDRIVE', 'HOMEPATH', 'HOME', 'DISPLAY', 'XAUTHORITY', 'LANG', 'LC_ALL'}
    return {key: value for key, value in os.environ.items() if key.upper() in allowed}


def classify_page(url, signals, http_status=None):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname not in {'www.zhipin.com', 'zhipin.com'}
            or parsed.username or parsed.password or parsed.port not in {None, 443}):
        return 'UNKNOWN'
    if http_status in {403, 429} or signals.get('restricted'):
        return 'ACCESS_RESTRICTED'
    if signals.get('verification'):
        return 'VERIFICATION_REQUIRED'
    if parsed.path.startswith('/web/user') or signals.get('login_form'):
        return 'LOGIN_REQUIRED'
    if signals.get('account_menu') and signals.get('member_navigation'):
        return 'AUTHENTICATED'
    return 'UNKNOWN'


def page_signals(page):
    """Return booleans only. Never serialize page text, account names or cookies."""
    def visible(selector):
        return any(item.is_visible() for item in page.locator(selector).all())

    return {
        'account_menu': visible('#header .nav-figure, .user-nav .nav-figure, .header .nav-figure'),
        'member_navigation': visible(
            '#header a[href*="/web/geek/chat"], #header a[href*="/web/geek/resume"], '
            '.user-nav a[href*="/web/geek/chat"], .user-nav a[href*="/web/geek/resume"]'
        ),
        'login_form': visible('input[type="password"], .sign-form input[type="tel"], '
                              '.login-register input[type="tel"]'),
        'verification': visible('.geetest_panel, .geetest_panel_box, #captcha, .captcha-container'),
        'restricted': page.get_by_text(re.compile('访问受限|访问异常|操作过于频繁')).first.is_visible(),
    }


def protect_directory(path):
    path.mkdir(parents=True, exist_ok=True)
    if os.name == 'nt':
        # A new DACL removes existing explicit grants as well as inherited grants.
        # Verify descendants instead of silently overwriting unusual existing ACLs.
        script = r'''
$ErrorActionPreference = 'Stop'
$target = $env:JOBHUNT_PRIVATE_DIR
$userSid = [System.Security.Principal.WindowsIdentity]::GetCurrent().User
$systemSid = New-Object System.Security.Principal.SecurityIdentifier('S-1-5-18')
$acl = New-Object System.Security.AccessControl.DirectorySecurity
$acl.SetAccessRuleProtection($true, $false)
foreach ($sid in @($userSid, $systemSid)) {
    $rule = New-Object System.Security.AccessControl.FileSystemAccessRule($sid, 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')
    $acl.AddAccessRule($rule)
}
Set-Acl -LiteralPath $target -AclObject $acl
foreach ($item in Get-ChildItem -LiteralPath $target -Force -Recurse) {
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'PRIVATE_REPARSE_POINT' }
    $child = Get-Acl -LiteralPath $item.FullName
    foreach ($rule in $child.GetAccessRules($true, $true, [System.Security.Principal.SecurityIdentifier])) {
        # Chrome creates named AppContainer capability ACLs inside its profile.
        # Preserve these, but reject any additional Windows user/group grants.
        $browserPath = Join-Path $target 'browser'
        $isBrowserChild = $item.FullName.StartsWith($browserPath + '\', [StringComparison]::OrdinalIgnoreCase)
        $isCapability = $isBrowserChild -and $rule.IdentityReference.Value -match '^S-1-15-3-1024(?:-\d+){8}$'
        if ($rule.AccessControlType -eq 'Allow' -and -not $isCapability -and $rule.IdentityReference.Value -notin @($userSid.Value, $systemSid.Value)) {
            throw 'PRIVATE_UNEXPECTED_GRANT'
        }
    }
}
'''
        env = browser_environment()
        env['JOBHUNT_PRIVATE_DIR'] = str(path)
        encoded = base64.b64encode(script.encode('utf-16-le')).decode('ascii')
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded],
                                env=env, capture_output=True,
                                creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)
        if result.returncode:
            raise UserError('PRIVATE_DIRECTORY_ACL_FAILED: 私有目录权限异常，请检查额外授权或链接')
    else:
        path.chmod(0o700)


@contextmanager
def launch_browser(profile_path, browser='chrome', headless=False):
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        raise UserError('PLAYWRIGHT_MISSING: 请运行 .venv\\Scripts\\python.exe -m pip install -e ".[browser]"') from None
    with sync_playwright() as runtime:
        context = runtime.chromium.launch_persistent_context(
            str(profile_path), channel=browser, headless=headless, locale='zh-CN',
            no_viewport=True, timeout=30000, env=browser_environment())
        try:
            yield context
        finally:
            context.close()


class AccountManager:
    def __init__(self, home):
        self.home = Path(home).resolve()
        self.private = self.home / 'private' / 'accounts' / 'boss'
        self.profile_path = self.private / 'browser'
        self.state_path = self.private / 'status.json'
        self.lock_path = self.private / 'session.lock'
        for path in (self.private, self.profile_path, self.state_path, self.lock_path):
            if not path.resolve().is_relative_to(self.home):
                raise UserError('ACCOUNT_PATH_OUTSIDE_HOME')

    def _history(self):
        if not self.state_path.exists():
            return {}
        try:
            if self.state_path.stat().st_size > 4096:
                return {}
            data = json.loads(self.state_path.read_text(encoding='utf-8'))
            if not isinstance(data, dict) or data.get('last_result') not in STATES:
                return {}
            # Only known fields and ISO timestamps are allowed back into output.
            result = {'last_result': data['last_result']}
            for name in ('checked_at', 'last_verified_at'):
                if isinstance(data.get(name), str):
                    result[name] = datetime.fromisoformat(data[name]).isoformat()
            return result
        except (ValueError, OSError):
            return {}

    def status(self):
        history = self._history()
        configured = bool(history) or self.profile_path.exists()
        return {'platform': 'boss', 'status': 'SAVED_STATE_UNVERIFIED' if configured else 'NOT_CONFIGURED',
                'live_checked': False, **history}

    def record(self, state, mode, browser):
        if state not in STATES or mode not in {'check', 'login'} or browser not in {'chrome', 'msedge'}:
            raise UserError('INVALID_AUTH_RESULT')
        history = self._history()
        now = datetime.now(timezone.utc).isoformat()
        value = {'platform': 'boss', 'last_result': state, 'mode': mode,
                 'browser': browser, 'checked_at': now}
        if state == 'AUTHENTICATED':
            value['last_verified_at'] = now
        elif history.get('last_verified_at'):
            value['last_verified_at'] = history['last_verified_at']
        self.private.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix('.tmp')
        temporary.write_text(json.dumps(value, indent=2), encoding='utf-8')
        temporary.replace(self.state_path)

    def open(self, mode, timeout=300, browser='chrome', headless=False):
        if mode not in {'login', 'check'} or browser not in {'chrome', 'msedge'} or not 1 <= timeout <= 900:
            raise UserError('INVALID_AUTH_OPTIONS: timeout 范围 1..900 秒')
        if mode == 'login' and headless:
            raise UserError('LOGIN_REQUIRES_VISIBLE_BROWSER')
        with run_lock(self.lock_path):
            protect_directory(self.private)
            state, live_checked = 'BROWSER_ERROR', False
            try:
                with launch_browser(self.profile_path, browser, headless) as context:
                    page = context.pages[0] if context.pages else context.new_page()
                    page.set_default_timeout(1500)
                    try:
                        response = page.goto(CHECK_URL, wait_until='domcontentloaded', timeout=min(timeout, 30) * 1000)
                    except Exception:
                        state = 'CLOSED_BY_USER' if page.is_closed() else 'NETWORK_ERROR'
                    else:
                        live_checked = True
                        http_status = response.status if response else None
                        deadline = time.monotonic() + timeout
                        success_count = 0
                        login_opened = False
                        menu_revealed = False
                        login_after = time.monotonic() + min(3, timeout / 2)
                        state = 'UNKNOWN'
                        while time.monotonic() < deadline:
                            if page.is_closed():
                                state = 'CLOSED_BY_USER'
                                break
                            if urlsplit(page.url).scheme in {'about', 'chrome-error', 'edge-error'}:
                                state = 'PAGE_UNAVAILABLE'
                                break
                            signals = page_signals(page)
                            if signals.get('account_menu') and not signals.get('member_navigation') and not menu_revealed:
                                # The account dropdown is normally hidden until hovered.
                                # Reveal it without clicking or sending any account action.
                                for item in page.locator('#header .nav-figure, .user-nav .nav-figure, .header .nav-figure').all():
                                    if item.is_visible():
                                        item.hover(timeout=1500)
                                        menu_revealed = True
                                        break
                                signals = page_signals(page)
                            state = classify_page(page.url, signals, http_status)
                            success_count = success_count + 1 if state == 'AUTHENTICATED' else 0
                            if success_count >= 2 or state == 'ACCESS_RESTRICTED':
                                break
                            if mode == 'check' and state in {'LOGIN_REQUIRED', 'VERIFICATION_REQUIRED'}:
                                break
                            if (mode == 'login' and not login_opened and state in {'UNKNOWN', 'LOGIN_REQUIRED'}
                                    and time.monotonic() >= login_after):
                                response = page.goto(LOGIN_URL, wait_until='domcontentloaded', timeout=min(timeout, 30) * 1000)
                                http_status = response.status if response else None
                                login_opened = True
                            page.wait_for_timeout(750)
                        else:
                            # A single late success is insufficient evidence.
                            if state in {'UNKNOWN', 'AUTHENTICATED'}:
                                state = 'TIMEOUT'
            except UserError:
                raise
            except Exception as exc:
                # Playwright exception text may contain page contents or launch arguments.
                state = 'CLOSED_BY_USER' if type(exc).__name__ == 'TargetClosedError' else 'BROWSER_ERROR'
            self.record(state, mode, browser)
            return {'platform': 'boss', 'status': state, 'live_checked': live_checked,
                    'browser': browser, 'persistent_profile': True, 'automatic_submission': False}
