"""Durable per-request ledger shared by demo/live runs in the same home."""
from contextlib import contextmanager
import sqlite3
from .config import UserError
from .models import now
from .timeutil import period_start


class BudgetExceeded(UserError):
    pass


class UsageLedger:
    def __init__(self, home, zone='Asia/Shanghai'):
        self.path = home / 'data' / 'usage.sqlite3'
        self.zone = zone
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.execute('''CREATE TABLE IF NOT EXISTS calls (
                id INTEGER PRIMARY KEY, run_id TEXT, job_id TEXT, model TEXT NOT NULL,
                started_at TEXT NOT NULL, ended_at TEXT, state TEXT NOT NULL,
                reserved INTEGER NOT NULL, input_tokens INTEGER, output_tokens INTEGER,
                cached_input_tokens INTEGER, cost REAL, duration REAL, error TEXT)''')

    @contextmanager
    def db(self):
        conn = sqlite3.connect(self.path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    def reserve(self, model, tokens, budget, run_id='', job_id=''):
        with self.db() as db:
            db.execute('BEGIN IMMEDIATE')
            used = db.execute('''SELECT COALESCE(SUM(CASE WHEN input_tokens IS NULL OR output_tokens IS NULL
                THEN reserved ELSE input_tokens+output_tokens END),0) FROM calls WHERE started_at>=?''',
                (period_start('daily', self.zone),)).fetchone()[0]
            if used + tokens > budget:
                raise BudgetExceeded('DAILY_TOKEN_BUDGET: 预算不足；未发送请求，待下次运行')
            return db.execute('INSERT INTO calls(run_id,job_id,model,started_at,state,reserved) VALUES(?,?,?,?,?,?)',
                              (run_id, job_id, model, now(), 'RESERVED', tokens)).lastrowid

    def finish(self, call_id, usage, duration, error=None, prices=None):
        usage = usage if isinstance(usage, dict) else {}
        def count(key):
            value = usage.get(key)
            return value if type(value) is int and value >= 0 else None
        inp, out, cached = count('prompt_tokens'), count('completion_tokens'), count('prompt_cache_hit_tokens')
        cost = None
        if prices and inp is not None and out is not None and 'input' in prices and 'output' in prices:
            hit = min(cached or 0, inp)
            cost = ((inp-hit)*prices['input'] + hit*prices.get('cached_input', prices['input']) + out*prices['output'])/1_000_000
        with self.db() as db:
            db.execute('''UPDATE calls SET ended_at=?,state=?,input_tokens=?,output_tokens=?,cached_input_tokens=?,
                       cost=?,duration=?,error=? WHERE id=?''',
                       (now(), 'FAILED' if error else 'SUCCESS', inp, out, cached, cost, duration, error, call_id))

    def summary(self, period='daily', run_id=None):
        where, values = ('run_id=?', (run_id,)) if run_id else ('started_at>=?', (period_start(period, self.zone),))
        with self.db() as db:
            rows = db.execute('SELECT * FROM calls WHERE '+where+' ORDER BY id', values).fetchall()
        unknown = [r for r in rows if r['input_tokens'] is None or r['output_tokens'] is None]
        cost_known = bool(rows) and all(r['cost'] is not None for r in rows)
        return {'period': period, 'calls': len(rows), 'input_tokens': sum(r['input_tokens'] or 0 for r in rows),
                'output_tokens': sum(r['output_tokens'] or 0 for r in rows), 'unknown_usage_calls': len(unknown),
                'failed_calls': sum(r['state'] == 'FAILED' for r in rows),
                'unfinished_calls': sum(r['state'] == 'RESERVED' for r in rows),
                'budget_used': sum(r['reserved'] if r in unknown else r['input_tokens']+r['output_tokens'] for r in rows),
                'cost': sum(r['cost'] for r in rows) if cost_known else None,
                'seconds': round(sum(r['duration'] or 0 for r in rows), 4)}
