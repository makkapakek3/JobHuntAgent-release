import argparse
from importlib.metadata import version
import json
import os
from pathlib import Path
import sqlite3
import sys

# Disable ambient third-party tracing before any LangGraph import.
for tracing_flag in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING", "LANGCHAIN_TRACING_V2"):
    os.environ[tracing_flag] = "false"

from pydantic import ValidationError

from .config import Context, ROOT, UserError, read_env, validated
from .models import display, redact, redact_data
from .storage import Store


def parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--home", type=Path, default=argparse.SUPPRESS)
    common.add_argument("--demo", action="store_true", default=argparse.SUPPRESS)
    common.add_argument('--backend', choices=['mock', 'deepseek', 'ollama'], default=argparse.SUPPRESS)
    p = argparse.ArgumentParser(description="JobHuntAgent；本地检索 / 人工审阅 / V2 任务恢复", parents=[common])
    sub = p.add_subparsers(dest="command", required=True)
    from .v2.cli import add_parser
    add_parser(sub, common)
    doctor_parser = sub.add_parser("doctor", parents=[common])
    doctor_parser.add_argument('--check-api', action='store_true', help='显式只读核验官方模型列表，不发送档案')
    sub.add_parser('status', parents=[common])
    sub.add_parser('sources', parents=[common])
    auth = sub.add_parser('auth', parents=[common])
    auth.add_argument('action', choices=['status', 'login', 'check'])
    auth.add_argument('platform', nargs='?', default='boss', choices=['boss'])
    auth.add_argument('--timeout', type=int)
    auth.add_argument('--browser', default='chrome', choices=['chrome', 'msedge'])
    auth.add_argument('--headless', action='store_true', help='仅供 check 使用；首次登录须使用可见窗口')
    nightly = sub.add_parser("nightly", aliases=["run-nightly", "run-once"], parents=[common])
    nightly.add_argument("--dry-run", action="store_true", help="保留本地结果，禁用外部写操作（始终禁用）")
    nightly.add_argument("--source", action="append", choices=["demo", "remotive"])
    nightly.add_argument("--import", dest="imports", action="append", type=Path)
    nightly.add_argument("--once-per-day", action="store_true")
    report = sub.add_parser("report", parents=[common])
    report.add_argument("which", choices=["latest"], nargs='?', default='latest')
    report.add_argument('--latest', action='store_true')
    jobs = sub.add_parser("jobs", parents=[common])
    jobs.add_argument('--status')
    jobs_sub = jobs.add_subparsers(dest="jobs_command")
    listing = jobs_sub.add_parser("list", parents=[common])
    listing.add_argument("--status")
    show = jobs_sub.add_parser("show", parents=[common])
    show.add_argument("job_id")
    for action in ["approve", "reject", "defer", "favorite"]:
        review = sub.add_parser(action, parents=[common])
        review.add_argument("job_ids", nargs="+")
        review.add_argument("--reason", default="")
    apply = sub.add_parser("apply", parents=[common])
    apply.add_argument("--approved-only", required=True, action="store_true")
    mode = apply.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--prepare-manual", action="store_true")
    mode.add_argument("--execute", action="store_true")
    confirmation = sub.add_parser("confirm", parents=[common])
    confirmation.add_argument("job_id")
    confirmation.add_argument("--result", choices=["applied", "failed"], required=True)
    confirmation.add_argument("--evidence", required=True)
    schedule = sub.add_parser("schedule", parents=[common])
    schedule.add_argument("action", choices=["export"])
    return p


def doctor(ctx, check_api=False):
    checks = {"python": sys.version.split()[0], "python_ok": sys.version_info >= (3, 12),
              "platform": sys.platform, "demo": ctx.demo, "cloud_upload": False,
              "real_submission": False, "hr_send": False,
              "native_automation": "本项目只导出系统任务示例；没有安装新计划任务",
              "network": "未探测；仅显式真实来源读取时访问网络",
              "key_configured": bool(read_env(ctx.home).get('DEEPSEEK_API_KEY')),
              'independent_runtime': True, 'browser_plugin_required': False}
    for module in ["langgraph", "pydantic", "PyYAML"]:
        checks[module] = version(module)
    try:
        ctx.load()
        checks["profile"] = "DEMO 虚构资料" if ctx.demo else "本地资料已校验"
        checks["matching"] = ctx.settings.matching_backend
        checks['cloud_upload'] = ctx.settings.matching_backend == 'deepseek'
        checks['model'] = ctx.deepseek_model
        checks['api'] = '未请求；--check-api 可核验官方模型列表'
        if check_api:
            from .deepseek import probe_models
            checks['api'] = probe_models(ctx)
            checks['network'] = '已显式核验 DeepSeek 官方模型列表；没有生成请求'
        checks["resume_version"] = ctx.resume_version
        ctx.db_path.parent.mkdir(parents=True, exist_ok=True)
        probe = ctx.db_path.parent / ".write-check"
        probe.write_text("", encoding="utf-8")
        probe.unlink()
        store = Store(ctx.db_path)
        checks["database_integrity"] = store.conn.execute("PRAGMA quick_check").fetchone()[0]
        store.close()
        checks["database_permissions"] = "本地可写；未更改系统 ACL；请将 data/profile/reports 保存在本人私有目录"
        print(json.dumps(checks, ensure_ascii=False, indent=2))
        return 0 if checks["python_ok"] else 1
    except (UserError, ValidationError, OSError) as exc:
        checks["profile_error"] = str(exc) if isinstance(exc, UserError) else type(exc).__name__
        print(json.dumps(checks, ensure_ascii=False, indent=2))
        return 1


def main(argv=None):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    args = parser().parse_args(argv)
    demo = getattr(args, 'demo', False)
    if args.command in {'nightly', 'run-nightly', 'run-once'} and args.dry_run and not args.source and not args.imports:
        demo = True
    ctx = Context(getattr(args, "home", ROOT).resolve(), demo, getattr(args, 'backend', 'mock' if demo else None))
    store = None
    try:
        if args.command == "v2":
            from .v2.cli import display_result, run
            output = run(args, ctx)
            print(json.dumps(display_result(output), ensure_ascii=False, indent=2))
            return 2 if output.get('status') in {'PARTIAL', 'FAILED', 'UNKNOWN'} else 0
        if args.command == "doctor":
            return doctor(ctx, args.check_api)
        if args.command == 'auth':
            from .accounts import AccountManager
            if ctx.demo:
                raise UserError('AUTH_DEMO_DENIED: 模拟命名空间禁止读取或打开真实账号')
            manager = AccountManager(ctx.home)
            if args.action == 'status':
                output = manager.status()
            else:
                if args.action == 'login':
                    print('请在专用浏览器中本人扫码或验证登录；程序检测成功后自动保存并关闭。', file=sys.stderr)
                output = manager.open(args.action, timeout=args.timeout if args.timeout is not None else (600 if args.action == 'login' else 15),
                                      browser=args.browser, headless=args.headless)
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return 0 if args.action == 'status' or output['status'] == 'AUTHENTICATED' else 3
        if args.command == 'sources':
            from .sources import source_status
            output = source_status(ctx)
        elif args.command == 'status':
            output = runtime_status(ctx)
        elif args.command in {"nightly", "run-nightly", "run-once"}:
            from .sources import FileSource, RemotiveSource, demo_source
            from .graph import run_nightly
            names = args.source or ([] if args.imports else (["demo"] if ctx.demo else ['remotive']))
            if "demo" in names and not ctx.demo:
                raise UserError("DEMO_REQUIRES_DEMO_NAMESPACE")
            sources = [demo_source() if x == "demo" else RemotiveSource() for x in dict.fromkeys(names)]
            sources += [FileSource(p) for p in (args.imports or [])]
            if not sources:
                raise UserError("SOURCE_REQUIRED: 使用 --source remotive 或 --import 本地文件")
            output = run_nightly(ctx, sources, args.once_per_day)
        elif args.command == "report":
            path = ctx.reports / "latest.md" if ctx.demo else ctx.home / 'reports' / 'latest.md'
            if not path.is_file():
                raise UserError("REPORT_MISSING: 先运行 run-nightly")
            print(path.read_text(encoding="utf-8"))
            return 0
        elif args.command == "schedule":
            from .scheduling import export_task
            from .models import Settings
            cfg_path = ctx.home / 'config/settings.yaml'
            cfg = validated(Settings, cfg_path) if cfg_path.exists() else Settings()
            output = {"file": export_task(ctx), "registered": False, "timezone": cfg.schedule_timezone, "daily": cfg.nightly_time}
        else:
            if not ctx.db_path.is_file():
                raise UserError("DATABASE_MISSING: 先运行 run-nightly")
            store = Store(ctx.db_path)
            if args.command == "jobs":
                if args.jobs_command in {None, "list"}:
                    output = []
                    for row in store.listing('pending_review' if args.status == 'pending' else args.status):
                        job = json.loads(row["payload"])
                        output.append({"job_id": row["job_id"], "company": job["company"], "title": job["title"],
                                       "status": row["status"], "favorite": bool(row["favorite"]), "source": job["source"], "url": job["url"]})
                else:
                    row = dict(store.get(args.job_id))
                    row["payload"] = json.loads(row["payload"])
                    row["filter_info"] = json.loads(row["filter_info"])
                    row["matches"] = [dict(x) for x in store.conn.execute("SELECT * FROM match_results WHERE job_id=?", (args.job_id,))]
                    output = row
            elif args.command in {"approve", "reject", "defer", "favorite"}:
                from .approvals import review
                output = review(ctx, store, args.job_ids, args.command, args.reason)
            elif args.command == "apply":
                if args.execute:
                    raise UserError("EXTERNAL_EXECUTION_DISABLED: V1 仅支持 --dry-run 和 --prepare-manual")
                from .approvals import apply_approved
                output = apply_approved(ctx, store, args.prepare_manual)
            else:
                from .approvals import confirm
                output = confirm(ctx, store, args.job_id, args.result, args.evidence)
        print(json.dumps(redact_data(output), ensure_ascii=False, indent=2))
        return 2 if isinstance(output, dict) and output.get('status') == 'PARTIAL' else 0
    except (UserError, ValidationError, OSError, ValueError, sqlite3.Error) as exc:
        message = str(exc) if isinstance(exc, UserError) else type(exc).__name__ + ": 输入或本地状态无效，请运行 doctor"
        print("ERROR " + redact(message), file=sys.stderr)
        return 1
    finally:
        if store:
            store.close()


def runtime_status(ctx):
    from .usage import UsageLedger
    from .models import Settings
    from .timeutil import local_zone
    from datetime import datetime
    path = ctx.home / 'config' / 'settings.yaml'
    cfg = validated(Settings, path) if path.exists() else Settings()
    ledger = UsageLedger(ctx.home, cfg.schedule_timezone)
    runs = []
    counts = {}
    if ctx.db_path.exists():
        store = Store(ctx.db_path)
        try:
            for row in store.conn.execute('SELECT * FROM run_history ORDER BY rowid DESC LIMIT 5'):
                item = dict(row)
                item['stats'] = json.loads(item['stats'])
                runs.append(item)
            counts = {r[0]: r[1] for r in store.conn.execute('SELECT status,COUNT(*) FROM jobs GROUP BY status')}
        finally:
            store.close()
    current = datetime.now(local_zone(cfg.schedule_timezone))
    ran_today = any(datetime.fromisoformat(r['started_at']).astimezone(current.tzinfo).date() == current.date() for r in runs)
    due = current.strftime('%H:%M') >= cfg.nightly_time
    return {'mode': 'DEMO' if ctx.demo else 'LIVE', 'recent_runs': runs, 'jobs': counts,
            'daily': ledger.summary(), 'monthly': ledger.summary('monthly'),
            'daily_token_budget': cfg.daily_token_budget, 'schedule_timezone': cfg.schedule_timezone,
            'nightly_time': cfg.nightly_time, 'schedule_installed_by_this_project': False,
            'schedule_status': 'missed_run / 今日尚无运行记录（尚未启用调度也会出现）' if due and not ran_today else '已有记录或尚未到点',
            'external_submission': False, 'hr_send': False}


if __name__ == "__main__":
    raise SystemExit(main())
