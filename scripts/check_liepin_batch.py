"""Test the batch UI, navigation queue and JSON saving without network access."""

from playwright.sync_api import sync_playwright

from check_liepin_extension import EXTENSION


CHROME_FIXTURE = r"""
const fixture = window.batchFixture = { updated: [], removed: [], requests: [], launch: null };
const platform = new URL(location.href).searchParams.get('platform');
const boss = platform === 'boss';
const fiftyOne = platform === '51job';
const searchUrl = boss ? 'https://www.zhipin.com/web/geek/jobs'
    : fiftyOne ? 'https://we.51job.com/pc/search' : 'https://www.liepin.com/zhaopin/';
const job = (id) => ({
    external_id: id, url: boss
        ? 'https://www.zhipin.com/job_detail/' + id + '.html'
        : fiftyOne ? 'https://jobs.51job.com/shanghai-ypq/' + id + '.html'
        : 'https://www.liepin.com/job/' + id + '.shtml',
    title: '示例职位 ' + id, company: '示例公司', city: '', salary_text: '', jd: '',
    detail_status: 'DESCRIPTION_UNAVAILABLE'
});
const rows = Array.from({length: 12}, (_, index) => job(String(index + 1)));
if (fiftyOne) rows.forEach((row, i) => { if (i % 2 === 0) delete row.url; });
const blocked = new URL(location.href).searchParams.get('blocked') === '1';
const manual = new URL(location.href).searchParams.get('manual') === '1';
let detailUrl;
chrome.runtime = { getURL: (path) => path };
chrome.tabs = {
    query: async () => [{id: 1, url: searchUrl}],
    create: async ({url}) => {
        if (url !== 'about:blank') fixture.launch = url;
        return {id: 2};
    },
    update: async (_id, {url}) => {
        fixture.updated.push(url);
        detailUrl = blocked && !fiftyOne && fixture.updated.length === 2 ? undefined : url;
        return {id: 2};
    },
    get: async (id) => id === 1
        ? {id, url: searchUrl, status: 'complete'}
        : {id, url: detailUrl, status: 'complete'},
    remove: async (id) => fixture.removed.push(id),
};
chrome.permissions = {
    contains: async () => false,
    request: async (permissions) => { fixture.requests.push(permissions); return true; },
};
chrome.scripting = { executeScript: async ({target, func, args, world}) => {
    if (func.name === 'resolve51JobLink') {
        if (!fiftyOne || target.tabId !== 1 || world !== 'MAIN') throw Error('wrong resolver world');
        return [{result: {url: job(args[0].external_id).url}}];
    }
    if (func.name !== (boss ? 'collectBossJobs' : fiftyOne ? 'collect51Job' : 'collectLiepinJobs')) throw Error('wrong platform collector');
    if (target.tabId === 1 && args[0].withDetailLinks) {
        return [{result: {
            list_page: true, jobs: [], needs_jd: rows, skipped: 0,
            ...(boss ? {} : {search: {platform, scope: 'list'}}),
            detail_links: Object.fromEntries(rows.filter(row => row.url).map((row) => [row.external_id, row.url])),
        }}];
    }
    const id = args[0].expectedJobId;
    if (fiftyOne && blocked && id === '2') return [{result: {stopped: '请完成前程无忧验证'}}];
    if (manual && id === '2') document.querySelector('#stop').click();
    const detail = job(id);
    if (detail.url !== detailUrl) throw Error('wrong detail tab');
    delete detail.detail_status;
    detail.jd = '负责自动化测试。';
    detail.city = '广州';
    detail.salary_text = '15-20k';
    return [{result: {jobs: [detail], needs_jd: []}}];
}};
"""


def check_batch(page, platform, blocked=False, manual=False):
    origin = 'https://extension-fixture.invalid'
    page.add_init_script(CHROME_FIXTURE)

    def serve(route):
        name = route.request.url.split('?', 1)[0].rsplit('/', 1)[-1]
        path = EXTENSION / name
        if path.is_file() and path.suffix in {'.js', '.html'}:
            route.fulfill(path=path, content_type=(
                'application/javascript' if path.suffix == '.js' else 'text/html; charset=utf-8'
            ))
        else:
            route.abort()

    page.route('**/*', serve)
    query = f'?platform={platform}' + ('&blocked=1' if blocked else '') + ('&manual=1' if manual else '')
    page.goto(origin + '/popup.html' + query)
    page.get_by_role('button', name='批量补读并导出').click(timeout=3000)
    page.wait_for_function('() => window.batchFixture?.launch != null', timeout=3000)
    launch = page.evaluate('window.batchFixture.launch')
    assert launch.startswith('batch.html#')
    path, tag = launch.split('#', 1)
    page.evaluate("""
        async () => {
            const root = await navigator.storage.getDirectory();
            await exportDirectory(await root.getDirectoryHandle('岗位信息', { create: true }));
        }
    """)
    page.goto(f'{origin}/{path}{query}#{tag}')
    label = {'boss': 'BOSS', '51job': '前程无忧', 'liepin': '猎聘'}[platform]
    page.get_by_role('button', name=f'授权读取{label}详情页').click(timeout=3000)
    page.get_by_text(f'已保存 {platform}-jobs-', exact=False).wait_for(timeout=4000)
    state = page.evaluate('window.batchFixture')
    saved = page.evaluate("""
        async () => {
            const dir = await exportDirectory();
            for await (const file of dir.values()) return JSON.parse(await (await file.getFile()).text());
        }
    """)
    origins = {'boss': ['https://www.zhipin.com/*', 'https://zhipin.com/*'],
               '51job': ['https://we.51job.com/*', 'https://jobs.51job.com/*'],
               'liepin': ['https://www.liepin.com/*']}[platform]
    assert state['requests'] == [{'origins': origins}], state
    assert state['removed'] == [2], state
    assert len(state['updated']) == (2 if blocked or manual else 12), state
    assert saved.get('search') == (None if platform == 'boss' else {'platform': platform, 'scope': 'batch'}), saved
    complete = 1 if blocked else 2 if manual else 12
    assert len(saved['jobs']) == complete, saved
    assert len(saved['needs_jd']) == 12 - complete, saved
    assert bool(saved['detail_read']['stop_reason']) is (blocked or manual), saved
    assert all(job['city'] == ('' if platform == 'boss' else '广州') and job['jd'] and 'detail_status' not in job
               for job in saved['jobs']), saved
    assert 'detail_links' not in saved, saved
    assert all(row.get('url') for row in saved['jobs']), saved


def main():
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(channel='chrome', headless=True)
        for platform, blocked, manual in (('liepin', False, False), ('liepin', True, False), ('boss', False, False),
                                         ('51job', False, False), ('51job', True, False), ('51job', False, True)):
            page = browser.new_page()
            check_batch(page, platform, blocked, manual)
            page.close()
        browser.close()
    print('PASS: three platforms process all 12 jobs; verification and manual stop preserve partial exports')


if __name__ == '__main__':
    main()
