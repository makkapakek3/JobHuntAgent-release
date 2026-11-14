"""Synthetic 51job DOM checks; no user page, credentials or external requests."""

import json

from playwright.sync_api import sync_playwright
from check_51job_extension import EXTENSION, SEARCH, DETAIL, CURRENT_DETAIL_HTML


def listing():
    cards = []
    for i in range(1, 21):
        cards.append(f'''<div class="joblist-item"><div class="joblist-item-job"
          sensorsdata='{{"jobId":"{i}","requestId":"PRIVATE_TRACKING"}}'>
          <span class="jname">示例职位 {i}</span><span class="cname">示例公司</span>
          <span class="sal">8千-1.2万</span><div class="area"><span class="shrink-0">上海</span></div>
          <a href="https://jobs.51job.com/all/coExample.html">公司页</a>
          <button class="apply">投递</button></div></div>''')
    return '<!doctype html><meta charset="UTF-8">' + ''.join(cards) + '''<script>
      window.applied = 0; window.originalOpen = window.open;
      document.querySelectorAll('.apply').forEach(e => e.onclick = () => window.applied++);
      document.querySelectorAll('.jname').forEach((e, i) => e.onclick = () => {
        const tab = window.open('_blank');
        tab.location.href = 'https://jobs.51job.com/shanghai-ypq/' + (i+1) + '.html?req=PRIVATE_TRACKING';
      });
    </script>'''


def main():
    with sync_playwright() as p:
        browser = p.chromium.launch(channel='chrome', headless=True)
        page = browser.new_page()
        page.route('**/*', lambda r: r.fulfill(body=listing(), content_type='text/html; charset=utf-8'))
        page.goto(SEARCH)
        script = (EXTENSION / 'collect-51job.js').read_text(encoding='utf-8')
        page.evaluate(script)
        batch = page.evaluate('collect51Job({withDetailLinks:true})')
        assert batch.get('list_page'), batch
        assert len(batch['needs_jd']) == 20, batch
        assert [j['external_id'] for j in batch['needs_jd']] == [str(i) for i in range(1, 21)]
        assert 'PRIVATE_TRACKING' not in json.dumps(batch)
        for row in batch['needs_jd']:
            resolved = page.evaluate('row => resolve51JobLink(row)', row)
            assert resolved == {'url': f"https://jobs.51job.com/shanghai-ypq/{row['external_id']}.html"}, resolved
        assert page.evaluate('window.open === window.originalOpen && window.applied === 0')
        assert len(page.context.pages) == 1, 'Resolver must not open extra tabs'
        target = batch['needs_jd'][0]
        assert page.evaluate('t => resolve51JobLink({...t, company:"已变更"})', target).get('stopped')
        for url in ['https://example.com/1.html', 'https://jobs.51job.com/all/coExample.html',
                    'https://jobs.51job.com/shanghai-ypq/999.html']:
            page.evaluate('url => document.querySelector(".jname").onclick = () => window.open(url)', url)
            assert not page.evaluate('t => resolve51JobLink(t)', target).get('url')
            assert page.evaluate('window.open === window.originalOpen')
        page.goto(DETAIL)
        page.set_content(CURRENT_DETAIL_HTML)
        page.evaluate(script)
        assert page.evaluate('collect51Job({expectedJobId:"wrong"})').get('error')
        assert len(page.evaluate('collect51Job({expectedJobId:"123456789"})')['jobs']) == 1
        page.set_content('<h1>访问验证</h1>')
        assert page.evaluate(script + ';collect51Job({expectedJobId:"123456789"})').get('stopped')
        browser.close()
    print('PASS: all 20 cards, synchronous detail URL resolution, restoration, no apply, wrong URL/ID, verification')


if __name__ == '__main__':
    main()
