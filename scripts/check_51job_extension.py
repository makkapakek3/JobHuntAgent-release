"""Verify the 51job popup and DEMO import using synthetic pages."""

import json
from pathlib import Path
import subprocess
import sys
import tempfile

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / 'browser-extension'
SEARCH = 'https://we.51job.com/pc/search'
DETAIL = 'https://jobs.51job.com/nanjing-yhtq/123456789.html'
COMPANY = 'https://jobs.51job.com/all/coExample.html'

LIST_HTML = f"""<!doctype html><meta charset="UTF-8">
<div class="joblist-item">
  <span class="jname">Python 开发</span><span class="sal">15-20K</span>
  <div class="area"><div class="shrink-0">南京-雨花台区</div></div>
  <a class="comp" href="{COMPANY}"><span class="cname">示例公司</span></a>
</div>
<div class="joblist-item">
  <a class="jname" href="{DETAIL}?req=sample">Python 开发</a>
  <span class="cname">另一家公司</span>
</div>
"""
DETAIL_HTML = """<!doctype html><meta charset="UTF-8">
<header class="tHeader"><div class="cn">
  <h1>Python 开发</h1><strong>15-20K</strong>
  <p class="cname"><a>另一家公司</a></p>
  <p class="msg ltype">南京-雨花台区 | 本科</p>
</div></header>
<section class="bmsg job_msg inbox">
  <span class="bname">职位信息</span>
  <p>编写 Python 服务。</p><p>维护接口测试。</p>
  <div class="mt10"><span>职能类别：</span>软件工程师</div>
</section>
"""


# Reduced, fictional fixture matching the user-provided rendered detail layout.
# Never include the original saved page or its scripts/account data in the repo.
CURRENT_DETAIL_HTML = """<!doctype html><meta charset="UTF-8">
<aside class="recommendation"><a class="com_name">推荐公司</a></aside>
<div class="tHeader tHjob"><div class="in"><div class="cn">
  <div class="jTitle"><h1>Python 开发</h1><strong>15-20K</strong></div>
  <p class="msg ltype"><span class="type_2">南京-雨花台区</span><span
    class="type_3">无需经验</span><span class="type_4">本科</span></p>
</div></div></div>
<div class="job-detail tBorderTop_box"><h2>职位描述</h2>
  <div class="bmsg job_msg inbox"><p>编写 Python 服务。</p><p>维护接口测试。</p>
    <p>职能类别：软件工程师</p><p>关键字：Python</p>
  </div>
</div>
<div class="job-corp"><div class="tmsg">这里是公司介绍，不是岗位职责。</div></div>
<div class="rgt-sidebar"><div class="sticky-content">
  <div class="corp-card tBorderTop_box"><h2>公司信息</h2><div class="initiator">
    <div class="com_msg"><a class="com_name himg" href="https://jobs.51job.com/all/coExample.html">
      <img alt=""><p>另一家公司</p>
    </a></div>
  </div></div>
</div></div>
"""


def run_detail_layout_checks():
    expected = {
        'external_id': '123456789', 'company': '另一家公司', 'title': 'Python 开发',
        'city': '南京-雨花台区', 'salary_text': '15-20K',
        'jd': '编写 Python 服务。\n维护接口测试。', 'url': DETAIL,
    }
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='chrome', headless=True)
        page = browser.new_page()
        page.route('**/*', lambda route: route.fulfill(body=CURRENT_DETAIL_HTML, content_type='text/html; charset=utf-8'))
        page.goto(DETAIL + '?req=sample')
        collector = (EXTENSION / 'collect-51job.js').read_text(encoding='utf-8')

        def collect():
            return page.evaluate(collector + ';collect51Job()')

        result = collect()
        assert result.get('jobs') == [expected], result
        assert not result['needs_jd'], result
        page.locator('.job_msg').evaluate('(e) => e.remove()')
        result = collect()
        assert not result['jobs'] and result['needs_jd'] == [{
            **expected, 'jd': '', 'detail_status': 'DESCRIPTION_UNAVAILABLE',
        }], result
        page.locator('.corp-card').evaluate('(e) => e.style.display = "none"')
        assert 'error' in collect(), 'Must not use hidden or recommended company'
        page.set_content(DETAIL_HTML)
        assert collect()['jobs'] == [expected], 'Legacy layout must still work'
        browser.close()


def run_popup_checks():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='chrome', headless=True)
        job_page = browser.new_page()
        job_page.route('**/*', lambda route: route.fulfill(
            body=LIST_HTML if route.request.url.startswith(SEARCH) else CURRENT_DETAIL_HTML,
            content_type='text/html; charset=utf-8',
        ))
        job_page.goto(SEARCH)
        collector = EXTENSION / 'collect-51job.js'
        job_page.add_script_tag(path=collector)
        tab = {'url': job_page.url, 'snapshot': job_page.evaluate('collect51Job()')}

        popup = browser.new_page()
        popup.expose_function('activePageUrl', lambda: tab['url'])
        popup.expose_function('executeCollector', lambda name: tab['snapshot'] if name == 'collect51Job' else None)
        popup.add_init_script("""
            chrome.tabs = { query: async () => [{ id: 1, url: await activePageUrl() }] };
            chrome.scripting = { executeScript: async ({ func }) =>
                [{ result: await executeCollector(func.name) }] };
        """)

        def serve_popup(route):
            asset = EXTENSION / route.request.url.rsplit('/', 1)[-1]
            if asset.is_file() and asset.suffix in {'.js', '.html'}:
                route.fulfill(path=asset, content_type=(
                    'text/html; charset=utf-8' if asset.suffix == '.html' else 'text/javascript; charset=utf-8'
                ))
            else:
                route.abort()

        popup.route('https://extension-fixture.invalid/**', serve_popup)
        popup.goto('https://extension-fixture.invalid/popup.html')
        popup.evaluate("""
            async () => {
                const root = await navigator.storage.getDirectory();
                await exportDirectory(await root.getDirectoryHandle('岗位信息', { create: true }));
            }
        """)
        button = popup.get_by_role('button', name='导出当前页')
        button.click()
        popup.get_by_text('51job-jobs-', exact=False).wait_for(timeout=3000)
        assert popup.get_by_role('button', name='批量补读并导出').is_visible()

        job_page.goto(DETAIL + '?req=sample')
        job_page.add_script_tag(path=collector)
        tab.update(url=job_page.url, snapshot=job_page.evaluate('collect51Job()'))
        popup.reload()
        button.click()
        popup.get_by_text('51job-jobs-', exact=False).wait_for(timeout=3000)
        job_page.set_content('<h1>滑动验证页面</h1><p>为了更好的访问体验，请进行验证</p>')
        job_page.add_script_tag(path=collector)
        tab.update(snapshot=job_page.evaluate('collect51Job()'))
        button.click()
        popup.get_by_text('请在前程无忧页面完成登录或验证', exact=False).wait_for(timeout=3000)

        saved = popup.evaluate("""
            async () => {
                const dir = await exportDirectory();
                const files = [];
                for await (const item of dir.values()) {
                    files.push([item.name, JSON.parse(await (await item.getFile()).text())]);
                }
                return files.sort((a, b) => a[0].localeCompare(b[0]));
            }
        """)
        browser.close()

    assert len(saved) == 2, saved
    assert saved[0][0].endswith('-01.json') and saved[1][0].endswith('-02.json'), saved
    listing, detail = [payload for _, payload in saved]
    assert listing['search']['platform'] == detail['search']['platform'] == '51job', saved
    assert not listing['jobs'] and len(listing['needs_jd']) == 2, listing
    first, second = listing['needs_jd']
    assert first['company'] == '示例公司' and first['city'] == '南京-雨花台区' and not first.get('url'), first
    assert second['company'] == '另一家公司' and second['url'] == DETAIL, second
    assert detail['jobs'] == [{
        'external_id': '123456789', 'company': '另一家公司', 'title': 'Python 开发',
        'city': '南京-雨花台区', 'salary_text': '15-20K', 'jd': '编写 Python 服务。\n维护接口测试。',
        'url': DETAIL,
    }], detail
    assert 'req=sample' not in json.dumps(saved) and COMPANY not in json.dumps(saved), saved
    return detail


def main():
    run_detail_layout_checks()
    export = run_popup_checks()
    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory)
        export['jobs'][0]['is_demo'] = True
        original_id = None
        for name, expected_new, expected_updated in (
            ('51job-first.json', 1, 0), ('51job-repeat.json', 0, 0), ('51job-edited.json', 0, 1),
        ):
            if expected_updated:
                export['jobs'][0]['jd'] += '\n补充持续集成职责。'
            path = folder / name
            path.write_text(json.dumps(export, ensure_ascii=False), encoding='utf-8')
            command = [
                sys.executable, '-X', 'utf8', '-m', 'jobhunt', 'v2', 'import', str(path),
                '--home', str(folder), '--demo', '--match', 'rules',
            ]
            result = subprocess.run(command, cwd=ROOT, check=True, capture_output=True, encoding='utf-8')
            payload = json.loads(result.stdout)
            assert payload['new_count'] == expected_new and payload['updated_count'] == expected_updated, payload
            assert payload['matching']['selected_count'] == 1, payload
            job_id = payload['jobs'][0]['job_id']
            original_id = original_id or job_id
            assert job_id == original_id, payload
    print('PASS: 51job list, detail, verification stop, numbered exports, and repeated DEMO matching')


if __name__ == '__main__':
    main()
