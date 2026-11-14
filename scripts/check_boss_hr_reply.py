"""Drive the unpacked extension and a local mock reply service with a temporary browser profile."""

import json
import os
import re
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread

from playwright.sync_api import expect, sync_playwright

from jobhunt.v2.hr_reply import DEMO_JOB, ReplyStore, demo_draft, make_server

ROOT = Path(__file__).resolve().parents[1]
CHROME = Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'Google/Chrome/Application/chrome.exe'


def main():
    with TemporaryDirectory() as scratch, sync_playwright() as pw:
        extension = ROOT / 'browser-extension'
        browser = pw.chromium.launch_persistent_context(
            scratch + '/browser', executable_path=CHROME, headless=True,
            ignore_default_args=['--disable-extensions'], args=['--enable-unsafe-extension-debugging'])
        try:
            cdp = browser.browser.new_browser_cdp_session()
            installed = cdp.send('Extensions.loadUnpacked', {'path': str(extension)})
            origin = 'chrome-extension://' + installed['id']
            calls = []
            release = Event()

            def generate(job, message):
                calls.append((job, message))
                if message == '可以介绍项目吗？':
                    release.wait(timeout=5)
                return demo_draft(job, message)

            with make_server(ReplyStore(Path(scratch) / 'replies.sqlite3'), origin,
                             'demo-pairing-token', 0,
                             lambda: [DEMO_JOB, DEMO_JOB | {'job_id': 'demo-second', 'title': '另一模拟岗位'}],
                             generate, demo=True) as server:
                thread = Thread(target=server.serve_forever, daemon=True)
                thread.start()
                try:
                    base = f'http://127.0.0.1:{server.server_port}'
                    page = browser.new_page()
                    page.goto(base + '/demo/chat')
                    ui = browser.new_page()
                    ui.goto(origin + '/popup.html')
                    ui.locator('#hr-settings summary').click()
                    ui.locator('#hr-port').fill(str(server.server_port))
                    ui.locator('#hr-token').fill('demo-pairing-token')
                    ui.locator('#hr-pair').click()
                    ui.wait_for_selector('#hr-job option[value="demo-job"]', state='attached')
                    ui.locator('#hr-job').select_option('demo-job')
                    tab_id = ui.evaluate('''async (url) => (await chrome.tabs.query({})).find(tab => tab.url === url).id''',
                                         page.url)
                    ui.evaluate('''(id) => {
                        const query = chrome.tabs.query.bind(chrome.tabs);
                        chrome.tabs.query = async (options) => options.active ? [await chrome.tabs.get(id)] : query(options);
                    }''', tab_id)
                    ui.locator('#hr-start').click()
                    expect(ui.locator('#hr-status')).to_contain_text('正在监听')
                    page.locator('#incoming').click()
                    page.locator('#incoming').click()
                    page.locator('#history').click()
                    ui.locator('#hr-refresh').click()
                    expect(ui.locator('#hr-draft')).to_have_value(re.compile('1月'))
                    assert calls == [('demo-job', '最快什么时候能到岗？')], calls
                    ui.locator('#hr-draft').fill('自定义草稿')
                    browser.grant_permissions(['clipboard-read', 'clipboard-write'])
                    ui.bring_to_front()
                    ui.locator('#hr-copy').click()
                    expect(ui.locator('#hr-status')).to_contain_text('已复制')
                    assert ui.evaluate('navigator.clipboard.readText()') == '自定义草稿'
                    page.reload()
                    ui.locator('#hr-refresh').click()
                    expect(ui.locator('#hr-draft')).to_have_value(re.compile('1月'))
                    assert len(calls) == 1
                    page.locator('#switch').click()
                    ui.locator('#hr-refresh').click()
                    expect(ui.locator('#hr-draft')).to_have_value('')
                    ui.locator('#hr-start').click()
                    expect(ui.locator('#hr-status')).to_contain_text('正在监听')
                    ui.locator('#hr-stop').click()
                    expect(ui.locator('#hr-status')).to_contain_text('已停止')
                    page.locator('#incoming').click()
                    assert len(calls) == 1
                    ui.locator('#hr-manual').fill('能提前到岗吗？')
                    ui.locator('#hr-submit').click()
                    expect(ui.locator('#hr-draft')).to_have_value(re.compile('核对'))
                    ui.locator('#hr-submit').click()
                    expect(ui.locator('#hr-status')).to_contain_text('需要核对')
                    assert len(calls) == 2
                    ui.locator('#hr-manual').fill('可以介绍项目吗？')
                    ui.locator('#hr-submit').click()
                    expect(ui.locator('#hr-status')).to_contain_text('正在生成')
                    ui.locator('#hr-job').select_option('demo-second')
                    release.set()
                    expect(ui.locator('#hr-submit')).to_be_enabled()
                    expect(ui.locator('#hr-draft')).to_have_value('')
                    assert len(calls) == 3
                    print(json.dumps({'mode': 'DEMO', 'browser': 'Chrome', 'calls': len(calls),
                                      'refresh_reused': True, 'switch_isolated': True,
                                      'copy_verified': True, 'stale_draft_hidden': True}))
                finally:
                    release.set()
                    server.shutdown()
                    thread.join()
        finally:
            browser.close()


if __name__ == '__main__':
    main()
