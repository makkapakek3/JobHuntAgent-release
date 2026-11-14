"""A local Windows window for the existing import and matching workflow."""

from argparse import Namespace
from contextlib import closing
from pathlib import Path
from queue import Empty, Queue
from threading import Thread
from urllib.parse import urlsplit
import webbrowser
import tkinter as tk
from tkinter import filedialog, ttk

from ..config import UserError
from .cli import run_discovery
from .hr_reply import model_draft, selected_facts
from .storage import BusinessStore


ROW_LABELS = {'SCORED': '已评分', 'CACHED': '已评分（复用）', 'FILTERED': '初筛排除',
              'FAILED': '评分失败', 'DEFERRED': '延期'}
ERROR_LABELS = {'DAILY_TOKEN_BUDGET': '本地日额度不足', 'LLM_EVIDENCE_INVALID': '评分引用校验失败',
                'LLM_SCHEMA_INVALID': '模型结果格式有误'}


def error_label(code):
    if code.startswith('API_NETWORK_'):
        return '无法连接 DeepSeek'
    return ERROR_LABELS.get(code, code)


def data_directory(ctx):
    return ctx.home / 'data' / 'v2' / ('demo' if ctx.demo else 'live')


def import_batch(ctx, path, progress):
    args = Namespace(v2_action='import', path=path, match='semantic', limit=100,
                     keyword=[], city=[], tech=[])
    return run_discovery(args, ctx, data_directory(ctx), progress=progress)


def ordered_jobs(jobs):
    return sorted(jobs, key=lambda row: (row['score'] is None, -(row['score'] or 0)))


def opening_draft(ctx, job_id):
    if ctx.demo:
        return {'status': 'ready', 'draft': '您好，我对这个模拟岗位感兴趣，希望有机会交流。'}
    ctx.load()
    if ctx.settings.matching_backend != 'deepseek':
        raise UserError('MATCH_BACKEND_DENIED')
    with closing(BusinessStore(data_directory(ctx) / 'business.sqlite3', demo=False)) as store:
        job = store.get_job(job_id)['job']
    return model_draft(ctx, job, '', facts=selected_facts(ctx, job, ''), opening=True)


class Desktop:
    def __init__(self, root, ctx):
        self.root, self.ctx = root, ctx
        self.path = None
        self.rows = {}
        self.running = False
        self.events = Queue()
        root.title('JobHuntAgent · 岗位评分' + (' · 模拟' if ctx.demo else ''))
        root.geometry('1040x690')
        root.minsize(780, 560)
        main = ttk.Frame(root, padding=16)
        main.pack(fill='both', expand=True)
        ttk.Label(main, text='岗位评分', font=('Microsoft YaHei UI', 17, 'bold')).pack(anchor='w')
        controls = ttk.Frame(main, padding=(0, 8))
        controls.pack(fill='x')
        ttk.Button(controls, text='选择导出文件', command=self.choose).pack(side='left')
        self.run_button = ttk.Button(controls, text='开始评分' if ctx.demo else '交给 DeepSeek 评分',
                                     command=self.start, state='disabled')
        self.run_button.pack(side='left', padx=8)
        self.filename = tk.StringVar(value='请选择 Chrome 扩展导出的 JSON。')
        ttk.Label(controls, textvariable=self.filename).pack(side='left')
        self.progress = ttk.Progressbar(main)
        self.progress.pack(fill='x')
        self.status = tk.StringVar(value='等待选择文件。')
        ttk.Label(main, textvariable=self.status).pack(anchor='w', pady=6)

        listing = ttk.Frame(main)
        listing.pack(fill='both', expand=True)
        columns = ('company', 'title', 'score', 'status')
        self.table = ttk.Treeview(listing, columns=columns, show='headings', height=8, selectmode='browse')
        for name, label, width in zip(columns, ('公司', '岗位', '分数', '状态'), (210, 350, 70, 180)):
            self.table.heading(name, text=label)
            self.table.column(name, width=width, anchor='w')
        scroll = ttk.Scrollbar(listing, command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        scroll.pack(side='right', fill='y')
        self.table.pack(fill='both', expand=True)
        self.table.bind('<<TreeviewSelect>>', self.show_details)
        self.details = tk.Text(main, height=7, wrap='word', state='disabled', font=('Microsoft YaHei UI', 10))
        self.details.pack(fill='x', pady=(8, 4))
        actions = ttk.Frame(main)
        actions.pack(fill='x')
        ttk.Button(actions, text='打开原岗位', command=self.open_job).pack(side='left')
        ttk.Button(actions, text='生成开场白' if ctx.demo else '用 DeepSeek 生成开场白',
                   command=self.greeting).pack(side='left', padx=8)
        ttk.Button(actions, text='复制开场白', command=self.copy).pack(side='left')
        self.draft = tk.Text(main, height=3, wrap='word', font=('Microsoft YaHei UI', 10))
        self.draft.pack(fill='x', pady=(4, 0))
        root.protocol('WM_DELETE_WINDOW', self.close)
        root.after(80, self.poll)

    def choose(self):
        if self.running:
            return
        files = [('岗位 JSON', '*.json')]
        if self.ctx.demo:
            files.append(('模拟岗位 CSV', '*.csv'))
        selected = filedialog.askopenfilename(filetypes=files)
        if selected:
            self.path = Path(selected)
            self.filename.set(self.path.name)
            self.run_button.configure(state='normal')

    def start(self):
        if self.path and not self.running:
            self.progress.configure(value=0)
            self.begin('正在导入并评分…', 'batch', import_batch, self.ctx, self.path,
                       lambda done, total: self.events.put(('progress', (done, total))))

    def begin(self, label, event, operation, *args):
        self.running = True
        self.status.set(label)
        self.run_button.configure(state='disabled')

        def work():
            try:
                self.events.put((event, operation(*args)))
            except UserError as error:
                self.events.put(('error', str(error)))
            except Exception as error:
                self.events.put(('error', f'操作失败（{type(error).__name__}）。'))

        Thread(target=work, daemon=True).start()

    def poll(self):
        try:
            while True:
                event, value = self.events.get_nowait()
                if event == 'progress':
                    done, total = value
                    self.progress.configure(maximum=max(total, 1), value=done)
                    self.status.set(f'已处理 {done} / {total} 条岗位。')
                    continue
                self.running = False
                if self.path:
                    self.run_button.configure(state='normal')
                if event == 'batch':
                    self.show_batch(value)
                elif event == 'draft':
                    job, result = value
                    if self.selected().get('job_id') == job:
                        self.draft.delete('1.0', 'end')
                        self.draft.insert('1.0', result['draft'])
                        if result['status'] == 'needs_review':
                            self.status.set('开场白需要核对。' + ' '.join(result.get('review_reasons', [])))
                        else:
                            self.status.set('开场白已生成，可编辑和复制。')
                    else:
                        self.status.set('岗位已切换，请在当前岗位重新生成开场白。')
                elif event == 'error':
                    self.status.set(value)
        except Empty:
            pass
        self.root.after(80, self.poll)

    def show_batch(self, result):
        matching = result.get('matching', {})
        self.table.delete(*self.table.get_children())
        self.rows.clear()
        for row in ordered_jobs(matching.get('jobs', [])):
            self.rows[row['job_id']] = row
            self.table.insert('', 'end', iid=row['job_id'], values=(
                row['company'], row['title'], row['score'] if row['score'] is not None else '—',
                error_label(row.get('error_code', '')) or ROW_LABELS.get(row['state'], row['state'])))
        self.show_details()
        acquisition = result.get('acquisition', {})
        exported = acquisition.get('browser_export', {})
        issues = [f"第 {item['row']} 条：{item['error_code']}" for item in result.get('errors', [])]
        if acquisition.get('error_code'):
            issues.append(acquisition['error_code'])
        for field, label in (('needs_jd_count', '缺少职位描述'), ('needs_city_count', '缺少城市')):
            if exported.get(field):
                issues.append(f'{exported[field]} 条{label}')
        if issues:
            self.details.configure(state='normal')
            self.details.insert('end', '\n'.join(issues))
            self.details.configure(state='disabled')
        if not self.rows:
            self.status.set(matching.get('error_code') or (
                '没有可评分的岗位，请查看导入问题。' if issues else '文件里没有可评分的岗位。'))
        else:
            self.status.set(f"已评分 {matching['scored_count']} 条，失败 {matching['failed_count']} 条，"
                            f"延期 {matching['deferred_count']} 条。" + (' 另有导入问题。' if issues else ''))

    def selected(self):
        row = self.table.selection()
        return self.rows.get(row[0], {}) if row else {}

    def show_details(self, _event=None):
        row = self.selected()
        analysis = row.get('analysis', {})
        lines = [*(f"待核实：{risk}" for risk in analysis.get('risks', [])),
                 *row.get('rules', {}).get('reasons', [])]
        for item in analysis.get('assessments', []):
            evidence = '；'.join(quote['quote'] for quote in item['profile_evidence'])
            lines.append(f"{item['requirement_quote']}\n{item['reason']}" + (f'\n个人资料引用：{evidence}' if evidence else ''))
        if row.get('error_code'):
            lines.append(f"{error_label(row['error_code'])}（{row['error_code']}）")
        self.details.configure(state='normal')
        self.details.delete('1.0', 'end')
        self.details.insert('1.0', '\n\n'.join(filter(None, lines)))
        self.details.configure(state='disabled')
        self.draft.delete('1.0', 'end')

    def open_job(self):
        url = self.selected().get('url', '')
        if urlsplit(url).scheme in {'http', 'https'}:
            webbrowser.open(url)

    def greeting(self):
        row = self.selected()
        if row and not self.running:
            job = row['job_id']
            self.begin('正在生成开场白…', 'draft', lambda: (job, opening_draft(self.ctx, job)))

    def copy(self):
        text = self.draft.get('1.0', 'end-1c').strip()
        if text:
            self.root.clipboard_clear()
            self.root.clipboard_append(text)
            self.status.set('已复制开场白。')

    def close(self):
        if self.running:
            self.status.set('当前任务还在运行，请等它完成。')
        else:
            self.root.destroy()


def run_desktop(ctx):
    root = tk.Tk()
    Desktop(root, ctx)
    root.mainloop()
    return {'status': 'STOPPED', 'mode': 'DEMO' if ctx.demo else 'LIVE'}
