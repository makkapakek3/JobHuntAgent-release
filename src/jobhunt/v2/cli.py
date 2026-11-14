"""V2 subcommands attached to the existing `python -m jobhunt` entry."""
from pathlib import Path
from contextlib import closing
import re
import json

from ..config import UserError, validated
from ..models import Permissions, redact_data
from ..storage import run_lock
from .models import Job, SearchCriteria


def display_result(output):
    """Redact user text without corrupting generated identifiers needed for recovery."""
    result = redact_data(output)
    patterns = {"application_id": r"APP-[0-9a-f]{32}", "job_id": r"JOB-[0-9a-f]{32}",
                "content_digest": r"[0-9a-f]{64}", "match_id": r"MATCH-[0-9a-f]{64}",
                "run_id": r"P2-[0-9a-f]{32}", "profile_version": r"[0-9a-f]{64}",
                "resume_version": r"[0-9a-f]{64}", "job_hash": r"[0-9a-f]{64}"}
    patterns['receipt_id'] = r'RCP-[0-9a-f]{32}'
    patterns['package_id'] = r'MANUAL-[0-9a-f]{64}'
    for key, pattern in patterns.items():
        value = output.get(key)
        if isinstance(value, str) and re.fullmatch(pattern, value):
            result[key] = value
    for key in ('applications', 'jobs', 'rankings', 'channels'):
        if key in output:
            result[key] = [display_result(row) for row in output[key]]
    if isinstance(output.get('matching'), dict):
        result['matching'] = display_result(output['matching'])
    if isinstance(output.get('receipt'), dict):
        result['receipt'] = display_result(output['receipt'])
    for key, filename in (('review_path', 'review.html'), ('receipt_path', 'receipt.json')):
        value = output.get(key)
        if isinstance(value, str):
            path = Path(value)
            if (path.parent.parent.parts[-3:] == ('reports', 'v2', 'demo')
                    and re.fullmatch(r'APP-[0-9a-f]{32}', path.parent.name) and path.name == filename):
                result[key] = value
    for key, filename in (('review_path', 'review.html'), ('package_path', 'package.json')):
        value = output.get(key)
        if isinstance(value, str):
            path = Path(value)
            if (path.parent.parent.parts[-3:] in {('reports', 'v2', 'demo'), ('reports', 'v2', 'live')}
                    and re.fullmatch(r'MANUAL-[0-9a-f]{64}', path.parent.name) and path.name == filename):
                result[key] = value
    for key, extension in (('json', 'json'), ('markdown', 'md')):
        value = output.get('reports', {}).get(key)
        if isinstance(value, str):
            path = Path(value)
            if (path.parent.parts[-3:] in {('reports', 'v2', 'live'), ('reports', 'v2', 'demo')}
                    and re.fullmatch(r'matches-P2-[0-9a-f]{32}\.' + extension, path.name)):
                result['reports'][key] = value
    return result


def add_parser(subparsers, common):
    parser = subparsers.add_parser("v2", parents=[common], help="V2：岗位发现与本地任务恢复")
    actions = parser.add_subparsers(dest="v2_action", required=True)
    hr_demo = actions.add_parser('hr-demo', parents=[common], help='仅本地虚构 HR 聊天执行验证')
    hr_demo.add_argument('--handle',type=int)
    hr_demo.add_argument('--port',type=int,default=56875)
    hr_demo.add_argument('--output',type=Path)
    hr_reply = actions.add_parser('hr-reply-serve', parents=[common], help='启动本机 HR 回复草稿助手')
    hr_reply.add_argument('--live', action='store_true', help='允许对手动提供的消息调用 DeepSeek')
    hr_reply.add_argument('--port', type=int, default=56876)
    hr_reply.add_argument('--extension-id', required=True)
    actions.add_parser('desktop', parents=[common], help='打开本机岗位评分窗口')
    actions.add_parser('channels', parents=[common], help='离线查看真实申请渠道的许可与适配情况')
    preflight = actions.add_parser('preflight', parents=[common], help='检查申请渠道并提示人工处理；不访问网站')
    target = preflight.add_mutually_exclusive_group(required=True)
    target.add_argument('--url')
    target.add_argument('--job-id')
    manual_package = actions.add_parser('prepare-manual', parents=[common], help='按已保存岗位和本地资料生成未提交的人工材料')
    manual_package.add_argument('--job-id', required=True)
    serve = actions.add_parser('serve-demo', parents=[common], help='启动本机 P3 模拟申请服务')
    serve.add_argument('--port', type=int, default=56871)
    prepare = actions.add_parser('prepare', parents=[common], help='准备并填写 DEMO 申请，然后暂停等待审批')
    selected = prepare.add_mutually_exclusive_group(required=True)
    selected.add_argument('--job', type=Path)
    selected.add_argument('--job-id')
    prepare.add_argument('--port', type=int, default=56871)
    prepare.add_argument('--contact', type=Path)
    prepare.add_argument('--handle', type=int)
    for name in ('application', 'review', 'recover'):
        action = actions.add_parser(name, parents=[common], help='P3 本地申请：查看、审批或恢复')
        action.add_argument('application_id')
        if name != 'application':
            action.add_argument('--handle', type=int)
        if name == 'review':
            action.add_argument('--decision', required=True, choices=['approve', 'reject', 'edit'])
            action.add_argument('--digest', required=True, help='本次审核文件中的完整内容摘要')
            action.add_argument('--fields', type=Path, help='edit 时提供需要修改的字段 JSON')
    create = actions.add_parser("create", parents=[common], help="从本地岗位 JSON 建立恢复任务")
    job_input = create.add_mutually_exclusive_group(required=True)
    job_input.add_argument("--job", type=Path)
    job_input.add_argument("--job-id")
    for name in ("show", "resume"):
        action = actions.add_parser(name, parents=[common])
        action.add_argument("application_id")
    actions.add_parser("list", parents=[common])
    search = actions.add_parser('search', parents=[common], help='读取获授权公开岗位 API')
    search.add_argument('--source', choices=['remotive'], default='remotive')
    imported = actions.add_parser('import', parents=[common], help='导入本地 CSV/JSON')
    imported.add_argument('path', type=Path)
    imported.add_argument('--match', choices=['rules', 'semantic'], help='仅匹配本次导入的岗位')
    jobs = actions.add_parser('jobs', parents=[common], help='列出岗位或按 ID 查看详情')
    jobs.add_argument('job_id', nargs='?')
    for action in (search, imported, jobs):
        action.add_argument('--keyword', action='append', default=[])
        action.add_argument('--city', action='append', default=[])
        action.add_argument('--tech', action='append', default=[])
        action.add_argument('--limit', type=int, default=10)
    manual = actions.add_parser('add', parents=[common], help='手动提供岗位详情（不访问链接）')
    for field in ('company', 'title', 'url'):
        manual.add_argument('--' + field, required=True)
    manual.add_argument('--city', default='')
    jd = manual.add_mutually_exclusive_group(required=True)
    jd.add_argument('--jd')
    jd.add_argument('--jd-file', type=Path)
    actions.add_parser('source-status', parents=[common], help='离线查看来源权限与缓存状态')
    for name in ('match', 'rankings'):
        action = actions.add_parser(name, parents=[common], help='匹配已保存岗位' if name == 'match' else '离线查看当前有效排名')
        action.add_argument('--job-id', action='append', default=[])
        action.add_argument('--limit', type=int, default=100)
        if name == 'match':
            action.add_argument('--rules-only', action='store_true', help='只做规则筛选，不请求模型')
            action.add_argument('--offset', type=int, default=0, help='从第 N 条已保存岗位继续处理，默认 0')


def match_and_report(ctx, store, manager, *, job_ids, limit, rules_only, offset=0, progress=None):
    from .matching import match_jobs
    from .match_reporting import write_report
    from ..usage import UsageLedger
    result = match_jobs(ctx, store, manager, job_ids=job_ids, limit=limit, rules_only=rules_only,
                        offset=offset, progress=progress)
    if ctx.settings.matching_backend == 'deepseek' and not rules_only:
        result['usage'] = UsageLedger(ctx.home, ctx.settings.schedule_timezone).summary(run_id=result['run_id'])
    result['reports'] = write_report(ctx.home, result)
    return result


def run_matching(args, ctx, directory):
    from .matching import current_rankings
    from .resume import ResumeManager
    from .storage import BusinessStore
    database = directory / 'business.sqlite3'
    if not database.is_file():
        raise UserError('V2_DATABASE_MISSING: 先运行 v2 search 或 v2 import')
    manager = ResumeManager(ctx)
    with run_lock(directory / 'workflow.lock'), closing(BusinessStore(database, demo=ctx.demo)) as store:
        options = {'job_ids': args.job_id, 'limit': args.limit}
        if args.v2_action == 'rankings':
            return current_rankings(ctx, store, manager, **options)
        return match_and_report(ctx, store, manager, rules_only=args.rules_only, offset=args.offset, **options)


def match_import(ctx, store, result, mode, *, progress=None):
    from .resume import ResumeManager
    result['scope'] = 'IMPORT_AND_MATCH'
    job_ids = [job['job_id'] for job in result['jobs']]
    batch = result['acquisition'].get('browser_export', {})
    if batch.get('needs_jd_count') or batch.get('needs_city_count'):
        result['status'] = 'PARTIAL'
    if not job_ids:
        if result['status'] == 'COMPLETE':
            result['status'] = 'NO_CANDIDATES'
        result['matching'] = {'status': 'SKIPPED_NO_ELIGIBLE_JOBS', 'selected_count': 0, 'new_attempts': 0}
        return result

    try:
        manager = ResumeManager(ctx)
    except UserError as error:
        code = str(error).split(':', 1)[0]
        result['matching'] = {'status': 'FAILED', 'new_attempts': 0,
                              'error_code': code if re.fullmatch(r'[A-Z][A-Z0-9_]+', code) else 'V2_PROFILE_UNAVAILABLE'}
        result['status'] = 'PARTIAL'
        return result
    matching = match_and_report(ctx, store, manager, job_ids=job_ids, limit=len(job_ids),
                                rules_only=mode == 'rules', progress=progress)
    result['matching'] = matching
    result.pop('model_called')  # Discovery alone cannot attest to the matching stage's model activity.
    if matching['status'] == 'PARTIAL':
        result['status'] = 'PARTIAL'
    return result


def run_discovery(args, ctx, directory, *, progress=None):
    from .discovery import discover, matches, summary
    from .sources import LocalFileSource, ManualSource, RemotiveSource
    from .storage import BusinessStore
    permission_file = ctx.home / 'config' / 'permissions.yaml'
    permissions = validated(Permissions, permission_file) if permission_file.exists() else Permissions()
    if args.v2_action in {'import', 'add'} and not permissions.read_job_sources:
        raise UserError('V2_SOURCE_PERMISSION_DENIED')
    database = directory / 'business.sqlite3'
    if args.v2_action == 'source-status':
        cached = None
        if database.exists():
            with run_lock(directory / 'workflow.lock'), closing(BusinessStore(database, demo=ctx.demo)) as store:
                row = store.conn.execute('SELECT source,last_attempt,fetched_at,error_code FROM source_cache WHERE source=?',
                                         ('Remotive',)).fetchone()
                cached = dict(row) if row else None
        return {'mode': 'DEMO' if ctx.demo else 'LIVE', 'network_checked': False, 'external_submission': False,
                'sources': [{'source': 'Remotive', 'enabled': not ctx.demo and permissions.read_job_sources
                             and 'Remotive' in permissions.allowed_job_sources,
                             'policy': RemotiveSource.policy.model_dump(mode='json'), 'cache': cached},
                            {'source': 'CSV/JSON / manual', 'enabled': permissions.read_job_sources,
                             'allowed_operations': ['import'], 'verified_online': False}]}
    if args.v2_action == 'jobs' and not database.exists():
        raise UserError('V2_DATABASE_MISSING: 先运行 v2 search 或 v2 import')
    criteria = SearchCriteria(keywords=getattr(args, 'keyword', []),
                              cities=args.city if isinstance(getattr(args, 'city', None), list) else [],
                              technical_directions=getattr(args, 'tech', []), limit=getattr(args, 'limit', 10))
    with run_lock(directory / 'workflow.lock'), closing(BusinessStore(database, demo=ctx.demo)) as store:
        if args.v2_action == 'jobs':
            if args.job_id:
                return store.get_job(args.job_id)
            rows = []
            for item in store.conn.execute('SELECT job_id FROM jobs ORDER BY rowid DESC'):
                row = store.get_job(item[0])
                if matches(Job.model_validate(row['job']), criteria):
                    rows.append(summary(row))
                if len(rows) >= criteria.limit:
                    break
            return {'mode': 'DEMO' if ctx.demo else 'LIVE', 'jobs': rows, 'selected_count': len(rows),
                    'criteria': criteria.model_dump(), 'external_submission': False}
        if args.v2_action == 'search':
            source = RemotiveSource(store, permissions, demo=ctx.demo)
        elif args.v2_action == 'import':
            source = LocalFileSource(args.path, permissions, demo=ctx.demo)
        else:
            jd = args.jd
            if args.jd_file:
                with args.jd_file.open('rb') as handle:
                    data = handle.read(240001)
                if len(data) > 240000:
                    raise UserError('V2_JD_TOO_LARGE')
                jd = data.decode('utf-8-sig')
            source = ManualSource({'company': args.company, 'title': args.title, 'url': args.url,
                                   'city': args.city, 'jd': jd, 'is_demo': ctx.demo}, permissions, demo=ctx.demo)
        result = discover(store, source, criteria)
        if args.v2_action == 'import' and args.match:
            return match_import(ctx, store, result, args.match, progress=progress)
        return result


def _p3_json(path, keys):
    if path is None:
        return None
    try:
        with path.open('rb') as stream:
            raw = stream.read(131073)
        if len(raw) > 131072:
            raise ValueError()
        from .local_form import _unique_object
        value = json.loads(raw.decode('utf-8-sig'), object_pairs_hook=_unique_object)
        if not isinstance(value, dict) or not value or set(value) - keys or any(not isinstance(v, str) for v in value.values()):
            raise ValueError()
        return value
    except (ValueError, UnicodeError, OSError):
        raise UserError('P3_INPUT_JSON_INVALID') from None


def run_application(args, ctx):
    if not ctx.demo:
        raise UserError('P3_DEMO_REQUIRED: P3 仅支持 --demo 本地模拟申请')
    if args.v2_action == 'serve-demo':
        from .local_form import serve
        if not 1024 <= args.port <= 65535:
            raise UserError('P3_LOCAL_PORT_INVALID')
        print(f'P3 DEMO service: http://127.0.0.1:{args.port} (Ctrl+C to stop)', flush=True)
        try:
            serve(ctx.home, args.port)
        except KeyboardInterrupt:
            pass
        return {'mode':'DEMO', 'scope':'P3_LOCAL_DEMO', 'status':'STOPPED', 'external_submission':False}
    contact = _p3_json(getattr(args, 'contact', None), {'name', 'email'})
    edits = _p3_json(getattr(args, 'fields', None), {'name', 'email', 'summary', 'resume_text'})
    if args.v2_action == 'review' and ((args.decision == 'edit') != (edits is not None)):
        raise UserError('P3_EDIT_FIELDS_REQUIRED')
    from .application_workflow import ApplicationWorkflow
    from .models import SourcePolicy
    job = None
    if args.v2_action == 'prepare' and args.job:
        permission_file = ctx.home / 'config' / 'permissions.yaml'
        permissions = validated(Permissions, permission_file) if permission_file.exists() else Permissions()
        if not permissions.read_job_sources:
            raise UserError('V2_SOURCE_PERMISSION_DENIED')
        if args.job.stat().st_size > 1_000_000:
            raise UserError('V2_JOB_FILE_TOO_LARGE')
        job = Job.model_validate_json(args.job.read_text(encoding='utf-8-sig'))
        if not job.is_demo:
            raise UserError('P3_DEMO_REQUIRED')
    with ApplicationWorkflow(ctx, handle=getattr(args, 'handle', None)) as workflow:
        if args.v2_action == 'prepare':
            job_id = workflow.store.save_job(job, SourcePolicy(source=job.source))[0] if job else args.job_id
            return workflow.prepare(job_id, port=args.port, contact=contact)
        if args.v2_action == 'review':
            return workflow.review(args.application_id, args.decision, args.digest, edits=edits)
        if args.v2_action == 'recover':
            return workflow.recover(args.application_id)
        return workflow.show(args.application_id)


def run_channel(args, ctx, directory):
    from .channels import channel_catalog, preflight
    if args.v2_action == 'channels':
        return channel_catalog()
    if args.v2_action == 'preflight' and args.url is not None:
        return preflight(args.url)
    from .storage import BusinessStore
    database = directory / 'business.sqlite3'
    if not database.is_file():
        raise UserError('V2_DATABASE_MISSING: 先导入岗位或使用 preflight --url')
    with run_lock(directory/'workflow.lock'), closing(BusinessStore(database, demo=ctx.demo)) as store:
        if args.v2_action == 'prepare-manual':
            from .manual_application import prepare_manual
            return prepare_manual(ctx, store, args.job_id)
        job = Job.model_validate(store.get_job(args.job_id)['job'])
        if job.is_demo != ctx.demo:
            raise UserError('V2_MODE_MISMATCH')
        return {**preflight(job.url), 'job_id': args.job_id, 'mode': 'DEMO' if ctx.demo else 'LIVE'}


def run(args, ctx):
    if args.v2_action == 'hr-demo':
        if not ctx.demo:
            raise UserError('HR_DEMO_MODE_REQUIRED')
        from .hr_demo import run_selftest
        from .application_workflow import chrome_handle
        from uuid import uuid4
        output = args.output or ctx.home/'artifacts'/('hr-demo-'+uuid4().hex)
        result = run_selftest(args.handle or chrome_handle(),output,args.port)
        if result['status'] != 'LOCAL_HR_DEMO_VERIFIED':
            raise UserError('HR_DEMO_TEST_FAILED: '+result.get('error_code','UNKNOWN'))
        return result
    directory = ctx.home / 'data' / 'v2' / ('demo' if ctx.demo else 'live')
    if args.v2_action == 'hr-reply-serve':
        from .hr_reply import serve
        return serve(args, ctx, directory)
    if args.v2_action == 'desktop':
        from .desktop import run_desktop
        return run_desktop(ctx)
    if args.v2_action in {'channels', 'preflight', 'prepare-manual'}:
        return run_channel(args, ctx, directory)
    if args.v2_action in {'serve-demo', 'prepare', 'application', 'review', 'recover'}:
        return run_application(args, ctx)
    if args.v2_action in {'search', 'import', 'add', 'jobs', 'source-status'}:
        return run_discovery(args, ctx, directory)
    if args.v2_action in {'match', 'rankings'}:
        return run_matching(args, ctx, directory)
    # Lazy import keeps the old CLI usable when an existing V1 environment needs updating.
    try:
        from .workflow import LocalWorkflow
    except ModuleNotFoundError as error:
        if error.name and error.name.startswith("langgraph.checkpoint.sqlite"):
            raise UserError("V2_DEPENDENCY_MISSING: 请按 docs/V2_P0.md 安装检查点依赖") from None
        raise
    directory = ctx.home / "data" / "v2" / ("demo" if ctx.demo else "live")
    if args.v2_action != "create" and not (directory / "business.sqlite3").is_file():
        raise UserError("V2_DATABASE_MISSING: 先运行 v2 create")
    job = None
    if args.v2_action == "create" and args.job is not None:
        if args.job.stat().st_size > 1_000_000:
            raise UserError("V2_JOB_FILE_TOO_LARGE")
        job = Job.model_validate_json(args.job.read_text(encoding="utf-8-sig"))
        if job.is_demo != ctx.demo:
            raise UserError("V2_MODE_MISMATCH: DEMO 岗位必须使用 --demo")
    with LocalWorkflow(ctx.home, demo=ctx.demo) as workflow:
        if args.v2_action == "create":
            return workflow.create(job, job_id=args.job_id)
        if args.v2_action == "show":
            return workflow.show(args.application_id)
        if args.v2_action == "resume":
            return workflow.resume(args.application_id)
        return {"mode": "DEMO" if ctx.demo else "LIVE", "scope": "P0_LOCAL_RECOVERY",
                "applications": workflow.store.list_applications(), "external_submission": False}
