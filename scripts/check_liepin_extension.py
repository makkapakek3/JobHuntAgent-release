"""Exercise Liepin exports and imports with invented jobs and no network."""

import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
EXTENSION = ROOT / 'browser-extension'
SEARCH = 'https://www.liepin.com/zhaopin/'
JOB = 'https://www.liepin.com/job/1234567890.shtml'
FIXTURE_CARD = f"""
<div class="jobCardPcContainer">
  <div class="jobDetailBox"><a href="{JOB}?skId=synthetic"><div>
    <div class="jobTitleBox"><div class="ellipsis-1">Python 开发</div>
      <div>[<span class="ellipsis-1">广州·天河区</span>]</div>
    </div><span>15-25k</span>
  </div></a></div>
  <div class="jobCompanyInfoBox"><span class="companyName">示例科技</span></div>
  <span class="recruiter-name">模拟招聘方</span>
</div>
"""
DETAIL = """
<section class="job-apply-container"><div class="job-apply-content">
  <div class="name-box"><span class="name"><span class="job-title">Python 开发</span></span>
    <span class="salary">15-25k</span>
  </div>
  <div class="job-properties"><span>广州·天河区</span><span class="split"></span><span>本科</span></div>
</div></section>
<aside>
  <div class="company-info-container"><div class="company-card">
    <div class="content"><div class="name ellipsis-1">示例科技</div></div>
    <div class="follow-box"><a class="follow">关注</a></div>
  </div></div>
  <div class="recommend-company-container"><div class="company-name">另一家公司</div></div>
</aside>
<section class="job-intro-container"><dl class="paragraph">
  <dt>职位描述</dt><dd>负责开发 Python 服务。<br>编写自动化测试。</dd>
</dl></section>
"""


def chrome_export():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='chrome', headless=True)
        page = browser.new_page()
        page.route('**/*', lambda route: route.fulfill(
            content_type='text/html; charset=utf-8',
            body='<!doctype html><meta charset="UTF-8">' + (
                FIXTURE_CARD * 2 if route.request.url.startswith(SEARCH) else DETAIL + FIXTURE_CARD
            ),
        ))
        collector = EXTENSION / 'collect-liepin.js'
        assert collector.is_file(), 'Liepin collector is missing'
        page.goto(SEARCH + '?skId=synthetic')
        page.add_script_tag(path=collector)
        listing = page.evaluate('collectLiepinJobs()')
        assert not listing['jobs'] and len(listing['needs_jd']) == 1, listing
        assert listing['needs_jd'][0]['url'] == JOB, listing
        assert listing['needs_jd'][0]['city'] == '广州·天河区', listing
        assert listing['needs_jd'][0]['salary_text'] == '15-25k', listing
        assert listing['search'] == {'platform': 'liepin', 'scope': 'list'}, listing
        batch = page.evaluate('collectLiepinJobs({withDetailLinks: true})')
        assert batch['list_page'] and batch['detail_links'] == {'1234567890': JOB}, batch

        page.goto(JOB + '?skId=synthetic')
        page.add_script_tag(path=collector)
        detail = page.evaluate('collectLiepinJobs()')
        assert not detail.get('error'), detail
        assert detail['jobs'] == [{
            'external_id': '1234567890', 'url': JOB, 'title': 'Python 开发', 'company': '示例科技',
            'city': '广州·天河区', 'salary_text': '15-25k', 'jd': '负责开发 Python 服务。\n编写自动化测试。',
        }], detail
        assert page.evaluate('collectLiepinJobs({expectedJobId: "999"})').get('error')
        page.goto('https://safe.liepin.com/page/liepin/captchaPage_ip_PC')
        page.add_script_tag(path=collector)
        assert '验证' in page.evaluate('collectLiepinJobs()')['error']

        popup = browser.new_page()
        popup.expose_function('liepinDetail', lambda: detail)
        popup.add_init_script(f"""
            chrome.tabs = {{query: async () => [{{id: 1, url: {json.dumps(JOB)}}}]}};
            chrome.scripting = {{executeScript: async ({{func}}) =>
                [{{result: func.name === 'collectLiepinJobs' ? await liepinDetail() : null}}]}};
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
        popup.evaluate('async () => await exportDirectory(await navigator.storage.getDirectory())')
        popup.get_by_role('heading', name='猎聘岗位导出').wait_for(timeout=3000)
        popup.get_by_role('button', name='导出当前页').click()
        popup.get_by_text('liepin-jobs-', exact=False).wait_for(timeout=3000)
        saved = popup.evaluate("""
            async () => {
                const dir = await exportDirectory();
                for await (const file of dir.values()) return JSON.parse(await (await file.getFile()).text());
            }
        """)
        browser.close()
    assert saved == detail
    assert all(value not in json.dumps((listing, detail), ensure_ascii=False)
               for value in ('skId', '模拟招聘方'))
    return saved


def main():
    export = chrome_export()
    export['jobs'][0]['is_demo'] = True
    with TemporaryDirectory() as directory:
        folder = Path(directory)

        def import_jobs(name, payload):
            path = folder / name
            path.write_text(json.dumps(payload, ensure_ascii=False), encoding='utf-8')
            result = subprocess.run([
                sys.executable, '-X', 'utf8', '-m', 'jobhunt', 'v2', 'import', str(path),
                '--home', str(folder), '--demo', '--match', 'rules',
            ], cwd=ROOT, check=True, capture_output=True, encoding='utf-8')
            return json.loads(result.stdout)

        first = import_jobs('liepin-first.json', export)
        export['jobs'][0]['jd'] += '\n补充测试职责。'
        updated = import_jobs('liepin-updated.json', export)
        other = import_jobs('51job.json', {
            'search': {'platform': '51job', 'scope': 'detail'}, 'needs_jd': [],
            'jobs': [export['jobs'][0] | {'url': 'https://jobs.51job.com/guangzhou/1234567890.html'}],
        })
        assert first['new_count'] == 1 and first['matching']['selected_count'] == 1, first
        assert updated['new_count'] == 0 and updated['updated_count'] == 1, updated
        assert first['jobs'][0]['job_id'] == updated['jobs'][0]['job_id']
        assert other['new_count'] == 1 and other['jobs'][0]['job_id'] != first['jobs'][0]['job_id'], other
    print('PASS: Liepin listing, detail, verification, JSON export, DEMO matching and platform identity')


if __name__ == '__main__':
    main()
