"""Reproducible subprocess acceptance and wall-clock baseline; all data are DEMO."""
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import uuid

from jobhunt.config import ROOT
from jobhunt.models import now
from jobhunt.reporting import atomic_text

home = ROOT / 'artifacts' / ('m1-' + uuid.uuid4().hex[:8])
home.mkdir(parents=True)
commands = []


def cli(*args, parse=True):
    started = time.perf_counter()
    command = [sys.executable, '-X', 'utf8', '-m', 'jobhunt', '--home', str(home), '--demo', *args]
    # Intentionally no PYTHONPATH: the standalone installation must work on its own.
    env = {k: v for k, v in os.environ.items() if k not in {'PYTHONPATH', 'CODEX_HOME'}}
    process = subprocess.run(command, cwd=home, env=env, capture_output=True, encoding='utf-8', timeout=30)
    commands.append({'args': list(args), 'exit_code': process.returncode, 'wall_seconds': round(time.perf_counter()-started, 4)})
    if process.returncode:
        raise RuntimeError(process.stderr)
    return json.loads(process.stdout) if parse else process.stdout


doctor = cli('doctor')
first = cli('nightly', '--dry-run')
second = cli('nightly', '--dry-run')
assert first['analyzed'] > 0 and second['analyzed'] == 0 and second['new'] == 0
pending = cli('jobs', '--status', 'pending')
assert len(pending) >= 2
cli('approve', pending[0]['job_id'])
cli('reject', pending[1]['job_id'], '--reason', 'DEMO acceptance only')
third = cli('nightly', '--dry-run')
assert cli('jobs', '--status', 'approved')[0]['job_id'] == pending[0]['job_id']
assert cli('jobs', '--status', 'rejected')[0]['job_id'] == pending[1]['job_id']
report = cli('report', '--latest', parse=False)
assert 'DEMO' in report
status = cli('status')
db = sqlite3.connect(home / 'data/demo.sqlite3')
try:
    assert db.execute('SELECT COUNT(*) FROM application_attempts').fetchone()[0] == 0
    assert db.execute('SELECT COALESCE(SUM(input_tokens+output_tokens),0) FROM llm_usage').fetchone()[0] == 0
finally:
    db.close()
for args in [[], ['doctor'], ['sources'], ['auth'], ['nightly'], ['report'], ['jobs'], ['approve'], ['reject'], ['status']]:
    cli(*args, '--help', parse=False)
result = {'checked_at': now(), 'data_mode': 'DEMO_SYNTHETIC', 'home': str(home),
          'browser_or_codex_used': False, 'first': first, 'second': second, 'after_review': third,
          'api_calls': 0, 'tokens': 0, 'approval_persisted': True, 'external_submissions': 0,
          'commands': commands, 'status': 'PASS'}
output = json.dumps(result, ensure_ascii=False, indent=2)
atomic_text(home / 'acceptance.json', output)
atomic_text(ROOT / 'reports/m1-acceptance.json', output)
print(output)
