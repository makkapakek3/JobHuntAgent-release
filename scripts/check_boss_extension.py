"""Exercise the collector in Chrome with synthetic pages and an isolated DEMO import."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
CHROME = Path(os.environ.get('ProgramFiles', r'C:\Program Files')) / 'Google/Chrome/Application/chrome.exe'
FIXTURE = '''<!doctype html><meta charset="UTF-8">
<ul class="rec-job-list">
  <li class="job-card-box"><a class="job-name" href="/job_detail/a123.html?securityId=fixture">开发工程师</a><span class="company-name">甲公司</span><span class="job-salary">20–30K</span></li>
  <li class="job-card-box"><a class="job-name" href="/job_detail/b456.html">开发工程师</a><span class="company-name">乙公司</span></li>
  <li class="job-card-box"><a class="job-name" href="/job_detail/b456.html?tracking=1">开发工程师</a><span class="company-name">乙公司</span></li>
  <li class="job-card-box"><a class="job-name" href="https://example.invalid/job_detail/c789.html">外站岗位</a><span class="company-name">丙公司</span></li>
</ul>
<div class="job-detail-box">
  <a class="more" href="/job_detail/b456.html?securityId=fixture">查看详情</a>
  <div class="job-sec-text">负责维护测试服务。</div>
</div>'''

DETAIL_FIXTURE = '''<!doctype html><meta charset="UTF-8">
<header class="position-header">
  <div><h1>基础设施工程师</h1><span class="salary">25-40K·16薪</span></div>
  <span class="text-city">上海</span>
</header>
<div class="job-sec-text">负责维护测试平台。<br>熟悉分布式系统。</div>
<div class="sider-company">
  <h3>公司基本信息</h3>
  <div>丁公司</div>
  <p>天使轮</p>
  <p>20-99人</p>
  <a href="/gongsi/job/example.html">查看全部职位</a>
</div>'''


def main():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(executable_path=CHROME, headless=True)
        page = browser.new_page()
        page.route('**/*', lambda route: route.fulfill(
            body=DETAIL_FIXTURE if '/job_detail/' in route.request.url else FIXTURE,
            content_type='text/html; charset=utf-8',
        ))
        page.goto('https://www.zhipin.com/web/geek/jobs')
        collector = ROOT / 'browser-extension/collect.js'
        assert collector.is_file(), 'Chrome collector is not implemented'
        page.add_script_tag(path=collector)
        export = page.evaluate('collectBossJobs()')
        assert [(row['external_id'], row['company']) for row in export['jobs']] == [('b456', '乙公司')], export
        assert export['jobs'][0]['jd'] == '负责维护测试服务。', export
        assert [(row['external_id'], row['company']) for row in export['needs_jd']] == [('a123', '甲公司')], export
        assert export['needs_jd'][0]['url'] == 'https://www.zhipin.com/job_detail/a123.html', export
        assert export['needs_jd'][0]['salary_text'] == '20–30K', export
        assert export['skipped'] == 1, export

        batch = page.evaluate('collectBossJobs({withDetailLinks: true})')
        assert batch['detail_links']['a123'].endswith('?securityId=fixture'), 'The original navigation link is needed'
        assert 'detail_links' not in export and 'securityId' not in json.dumps(export)
        assert batch['list_page']
        page.goto('https://zhipin.com/job_detail/d012-E1.html?securityId=fixture')
        page.add_script_tag(path=collector)
        detail = page.evaluate('collectBossJobs()')
        assert detail.get('jobs') == [{
            'external_id': 'd012-E1', 'company': '丁公司', 'title': '基础设施工程师',
            'city': '上海', 'salary_text': '25-40K·16薪',
            'jd': '负责维护测试平台。\n熟悉分布式系统。',
            'url': 'https://www.zhipin.com/job_detail/d012-E1.html',
        }], detail
        assert page.evaluate('collectBossJobs({expectedJobId: "different-id"})').get('error')
        page.locator('.sider-company').evaluate('(node) => node.remove()')
        assert '公司名称' in page.evaluate('collectBossJobs()').get('error', '')
        page.goto('https://example.invalid/web/geek/jobs')
        page.add_script_tag(path=collector)
        assert page.evaluate('collectBossJobs()').get('error'), 'Collector should reject a non-BOSS tab'

        popup = browser.new_page()
        popup.expose_function('fixtureSnapshot', lambda: export)
        popup.add_init_script('''
            chrome.tabs = { query: async () => [{id: 1, url: 'https://www.zhipin.com/web/geek/jobs'}] };
            chrome.scripting = { executeScript: async () => [{result: await fixtureSnapshot()}] };
        ''')
        extension = ROOT / 'browser-extension'
        assets = {path.name: path for path in extension.iterdir() if path.suffix in {'.html', '.js'}}

        def serve_popup(route):
            asset = assets.get(route.request.url.rsplit('/', 1)[-1])
            if asset:
                route.fulfill(path=asset, content_type=(
                    'text/html; charset=utf-8' if asset.suffix == '.html' else 'text/javascript; charset=utf-8'))
            else:
                route.abort()

        popup.route('**/*', serve_popup)
        popup.goto('https://extension-fixture.invalid/popup.html')
        assert popup.locator('#search-form').count() == 0, 'The extension must leave the website search to the user'
        popup.evaluate('''
            async () => {
                const root = await navigator.storage.getDirectory();
                const folder = await root.getDirectoryHandle('岗位信息', {create: true});
                await exportDirectory(folder);
            }
        ''')
        button = popup.get_by_role('button', name='导出当前页')
        button.click()
        popup.get_by_text('boss-jobs-', exact=False).wait_for()
        popup.reload()
        button.click()
        popup.get_by_text('boss-jobs-', exact=False).wait_for()
        saved = popup.evaluate('''
            async () => {
                const dir = await exportDirectory();
                const files = [];
                for await (const item of dir.values()) {
                    files.push([item.name, JSON.parse(await (await item.getFile()).text())]);
                }
                return files.sort((a, b) => a[0].localeCompare(b[0]));
            }
        ''')
        assert len(saved) == 2, saved
        assert saved[0][0].endswith('-01.json') and saved[1][0].endswith('-02.json'), saved
        assert all(item[1]['jobs'] == export['jobs'] and item[1]['needs_jd'] == export['needs_jd'] for item in saved)
        export = saved[1][1]
        browser.close()

    with tempfile.TemporaryDirectory() as directory:
        folder = Path(directory)
        path = folder / 'boss-fixture.json'
        export['jobs'][0]['is_demo'] = True
        path.write_text(json.dumps(export, ensure_ascii=False), encoding='utf-8')
        command = [sys.executable, '-X', 'utf8', '-m', 'jobhunt', 'v2', 'import', str(path),
                   '--home', str(folder), '--demo']
        for expected in (1, 0):
            result = subprocess.run(command, cwd=ROOT, check=True, capture_output=True, encoding='utf-8')
            payload = json.loads(result.stdout)
            assert payload['new_count'] == expected, payload
    print('PASS: BOSS page extraction, numbered folder exports, and repeated DEMO import')


if __name__ == '__main__':
    main()
