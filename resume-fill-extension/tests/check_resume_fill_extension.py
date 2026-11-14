"""Exercise the real extension UI against temporary loopback fixtures, using demo data only."""
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import shutil
from tempfile import TemporaryDirectory
from threading import Thread

from playwright.sync_api import expect, sync_playwright

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / 'fixtures' / 'profile.demo.json'
CARD = json.loads(DEMO.read_text(encoding='utf-8'))


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_):
        pass


def run():
    with TemporaryDirectory(prefix='resume-extension-test-') as tmp, sync_playwright() as playwright:
        temp = Path(tmp)
        extension = temp / 'extension'
        shutil.copytree(ROOT, extension, ignore=shutil.ignore_patterns('tests', 'fixtures', '__pycache__'))
        manifest_path = extension / 'manifest.json'
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        assert set(manifest['permissions']) == {'activeTab', 'scripting', 'storage'} and not manifest.get('host_permissions')
        # The popup is opened as a test tab, so there is no toolbar gesture. Grant only the test origin in this copy.
        manifest['host_permissions'] = ['http://127.0.0.1/*']
        manifest['background'] = {'service_worker': 'test-worker.js'}
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding='utf-8')
        (extension / 'test-worker.js').write_text('', encoding='utf-8')
        server = ThreadingHTTPServer(('127.0.0.1', 0), partial(QuietHandler, directory=str(ROOT / 'fixtures')))
        Thread(target=server.serve_forever, daemon=True).start()
        base = f'http://127.0.0.1:{server.server_port}'
        try:
            bundled = Path(playwright.chromium.executable_path).is_file()
            browser = playwright.chromium.launch_persistent_context(str(temp / 'browser'), headless=True,
                channel='chromium' if bundled else 'chrome',
                args=[f'--disable-extensions-except={extension}', f'--load-extension={extension}'] if bundled
                else ['--enable-unsafe-extension-debugging'], ignore_default_args=['--disable-extensions'] if not bundled else None)
            try:
                if not bundled:
                    browser.browser.new_browser_cdp_session().send('Extensions.loadUnpacked', {'path': str(extension)})
                worker = browser.service_workers[0] if browser.service_workers else browser.wait_for_event('serviceworker', timeout=12000)
                origin = worker.url.rsplit('/', 1)[0]
                options = browser.new_page()
                options.goto(origin + '/options.html')
                options.locator('#profile-file').set_input_files(DEMO)
                expect(options.locator('#candidate')).to_contain_text('实习经历：4 段')
                options.locator('#save').click()
                options.reload()
                expect(options.locator('#stored')).to_contain_text('实习经历：4 段')
                options.locator('#profile-file').set_input_files(dict(name='bad.json', mimeType='application/json', buffer=b'{'))
                expect(options.locator('#status')).to_contain_text('无法读取 JSON')
                expect(options.locator('#stored')).to_contain_text('实习经历：4 段')
                print('PASS 资料卡导入、重开和拒绝损坏资料')

                popup = browser.new_page()
                page = browser.new_page()
                def scan(path=None, expected='识别完成'):
                    if path is not None:
                        page.goto(f'{base}/{path}')
                    popup.goto(origin + '/popup.html')
                    page.bring_to_front()
                    popup.locator('#scan').evaluate('(e) => e.click()')
                    expect(popup.locator('#status')).to_contain_text(expected)
                def fill():
                    popup.locator('#fill').evaluate('(e) => e.click()')
                    expect(popup.locator('#status')).to_contain_text('填写结束')
                def row(field_id):
                    return popup.locator(f'[data-marker-id="{field_id}"]')

                scan('form-feishu.html')
                expect(popup.locator('#preview')).to_contain_text('实习经历 · 4 个区块')
                expect(row('person')).to_contain_text('已有值保留')
                fill()
                expect(page.locator('#person')).to_have_value('页面已有姓名')
                expect(page.locator('#mail')).to_have_value(CARD['contact']['email'])
                for i, item in enumerate(CARD['internships']):
                    expect(page.locator(f'#co{i}')).to_have_value(item['company'])
                    expect(page.locator(f'#desc{i}')).to_have_value(item['description'])
                assert page.evaluate('window.submits') == 0
                print('PASS 飞书简历组件、四段预填经历与下拉搜索输入共存，未触发保存')

                scan('form-visual.html')
                expect(popup.locator('#preview')).to_contain_text('实习经历 · 4 个区块')
                expect(popup.locator('#preview')).not_to_contain_text('嵌入')
                expect(row('name')).to_contain_text('已有值保留')
                expect(row('email')).to_contain_text('已有值保留')
                fill()
                expect(page.locator('#name')).to_have_value('页面已有姓名')
                expect(page.locator('#email')).to_have_value('existing@example.invalid')
                expect(page.locator('#city')).to_have_value(CARD['contact']['currentCity'])
                expect(page.locator('#emergency')).to_have_value('')
                for i, item in enumerate(CARD['internships']):
                    expect(page.locator(f'#company-{i}')).to_have_value(item['company'])
                    expect(page.locator(f'#description-{i}')).to_have_value(item['description'])
                scan('form-visual.html?no-editor&token=private-query', expected='没有识别到可填写的简历区块')
                expect(popup.locator('#status')).not_to_contain_text('嵌入')
                expect(popup.locator('#fill')).to_be_disabled()
                popup.locator('#diagnose').click()
                report = popup.locator('#diagnostic-text').input_value()
                info = json.loads(report)
                assert info['version'] == manifest['version']
                assert info['main']['controls'] == 1 and info['main']['unscoped'] == 1
                assert info['frames']['visible'] == 1
                assert 'private-' not in report
                expect(page.locator('#private-id')).to_have_value('private-value')
                print('PASS 两列布局的视觉标题及标签、预填经历保留、无关 iframe 不误判')

                scan('form-native.html')
                expect(page.locator('#person')).to_have_value('')
                expect(page.locator('#co0')).to_have_value('')
                fill()
                for i, item in enumerate(CARD['internships']):
                    for selector, key in [('co', 'company'), ('title', 'title'), ('start', 'startMonth'), ('end', 'endMonth'), ('desc', 'description')]:
                        expect(page.locator(f'#{selector}{i}')).to_have_value(item[key])
                for selector, value in {'#person': CARD['contact']['fullName'], '#edu-start': '2021-09', '#edu-end': '', '#city': 'demo', '#co99': '', '#secret': '', '#emergency': '', '#nested-emergency': ''}.items():
                    expect(page.locator(selector)).to_have_value(value)
                assert page.evaluate('window.submits') == 0
                print('PASS 四段逐字段对应、预览零写入、同名日期分区、隐藏模板和敏感字段跳过、零提交')

                scan('form-native.html')
                popup.locator('[data-kind="internships"] .assignment').nth(1).select_option('internships:intern-4')
                fill()
                expect(page.locator('#co1')).to_have_value('示例丁公司')
                expect(page.locator('#co3')).to_have_value('示例乙公司')
                print('PASS 交换第 2、4 段')

                for n in [3, 5]:
                    scan(f'form-native.html?n={n}')
                    expect(popup.locator('#preview')).to_contain_text(f'需要 4 个，识别到 {n} 个')
                    fill()
                    expect(page.locator('#person')).not_to_have_value('')
                    assert page.locator('#internships input, #internships textarea').evaluate_all('(nodes) => nodes.every(n => !n.value)')
                print('PASS 三块和五块均不写入，其他区块仍可填')

                scan('form-native.html?ambiguous')
                expect(popup.locator('#preview')).to_contain_text('无法确认独立区块')
                fill()
                expect(page.locator('#split-company')).to_have_value('')
                expect(page.locator('#split-desc')).to_have_value('')
                expect(page.locator('#co1')).to_have_value('')
                print('PASS 单个已识别公司字段不把两个独立区块合并')

                scan('form-native.html?prefill')
                fill()
                expect(page.locator('#co0')).to_have_value('示例丁公司')
                expect(page.locator('#desc0')).to_have_value(CARD['internships'][3]['description'])
                expect(page.locator('#co2')).to_have_value('不在资料中的公司')
                expect(page.locator('#desc2')).to_have_value('')
                scan('form-native.html?date-conflict')
                fill()
                expect(page.locator('#edu-major')).to_have_value('')
                expect(page.locator('#edu-end')).to_have_value('2026-06-18')
                print('PASS 已有值保留，唯一匹配补空白，冲突区块保持原样')

                for mutation in ["document.querySelector('#internships').prepend(document.querySelector('#co3').closest('fieldset'))",
                                 "document.querySelector('#co2').value = '用户已填写'",
                                 "document.querySelector('#edu-end').value = '2026-06-18'",
                                 "const [a,b]=document.querySelectorAll('#internships legend'); [a.textContent,b.textContent]=[b.textContent,a.textContent]"]:
                    scan('form-native.html')
                    page.evaluate(mutation)
                    fill()
                    expect(popup.locator('#results')).to_contain_text('重新识别')
                    expect(page.locator('#person')).to_have_value('')
                    expect(page.locator('#co0')).to_have_value('')
                scan('form-native.html?rerender')
                fill()
                expect(page.locator('#co0')).to_have_value('示例甲公司')
                expect(page.locator('#co1')).to_have_value('')
                expect(popup.locator('#results')).to_contain_text('未处理')
                print('PASS 预览后更改顺序或值零写入，填写中重绘停止')

                scan('form-native.html?limited&clear')
                fill()
                expect(page.locator('#desc0')).to_have_value('')
                expect(popup.locator('#results')).to_contain_text('写入未通过校验')
                scan('form-native.html?clear')
                fill()
                expect(page.locator('#desc0')).to_have_value('')
                expect(popup.locator('#results')).to_contain_text('写入未通过校验')
                scan('form-native.html?autosave')
                expect(popup.locator('#fill')).to_be_disabled()
                expect(popup.locator('#preview')).to_contain_text('复制')
                print('PASS 长度限制与回读失败报告，已知自动保存页只预览')

                scan('form-variant.html')
                fill()
                expect(page.locator('#co3')).to_have_value('示例丁公司')
                expect(page.locator('#desc1')).to_have_value(CARD['internships'][1]['description'])
                expect(page.locator('#school')).to_have_value('示例大学')
                expect(page.locator('#major')).to_have_value('电子工程')
                expect(page.locator('#folded')).to_have_value('')
                print('PASS 不同结构的重复容器与未展开区块')

                scan('form-native.html')
                row('custom').locator('.correction').select_option('phone')
                row('custom').locator('.remember').evaluate('(e) => e.click()')
                expect(popup.locator('#notice')).to_contain_text('已记住')
                scan('form-native.html')
                fill()
                expect(page.locator('#custom')).to_have_value(CARD['contact']['phone'])
                scan('form-native.html')
                page.evaluate("document.querySelector('#custom').closest('label').prepend('新版本 ')")
                scan()
                fill()
                expect(page.locator('#custom')).to_have_value('')
                print('PASS 映射同结构复用，变更结构后失效')

                scan('form-holdout.html')
                fill()
                expected = {'#candidate': CARD['contact']['fullName'], '#mail': CARD['contact']['email'],
                            '#locality': CARD['contact']['currentCity'], '#training': '示例大学', '#discipline': '电子工程',
                            '[aria-label="毕业月份"]': '2025-06'}
                for i, item in enumerate(CARD['internships']):
                    for suffix, key in [('org', 'company'), ('position', 'title'), ('began', 'startMonth'), ('note', 'description')]:
                        expected[f'#{suffix}-{i}'] = item[key]
                actual = {selector: page.locator(selector).input_value() for selector in expected}
                wrong = [s for s, v in actual.items() if v and v != expected[s]]
                missed = [s for s, v in actual.items() if not v]
                assert not wrong, f'留出表单误填：{wrong}'
                assert len(expected) - len(missed) >= 16, f'留出表单自动填写低于最低门槛：{missed}'
                print(f'PASS 留出模拟表单 {len(expected) - len(missed)}/{len(expected)}，误填 0，人工补项 {missed}')

                options.reload()
                options.locator('#clear').click()
                options.locator('#confirm-clear').click()
                expect(options.locator('#stored')).to_contain_text('尚未保存资料卡')
                assert options.evaluate('async () => Object.keys(await chrome.storage.local.get(null)).length') == 0
                print('PASS 清除本机资料和映射')
            finally:
                browser.close()
        finally:
            server.shutdown()
            server.server_close()


if __name__ == '__main__':
    run()
