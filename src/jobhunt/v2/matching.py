"""P2 rules and evidence-bearing scores over already saved V2 jobs."""
import json
import re
from uuid import uuid4

from ..config import UserError
from ..filters import evaluate, role_priority
from ..models import digest, now, redact_data
from ..usage import BudgetExceeded
from .match_evidence import MATCH_VERSION, JobMatcher, job_facts, validate_match
from .models import Job

RULE_VERSION = 'v2-rules-1'
CITY_ALIASES = {'shanghai': '上海', 'nanjing': '南京', 'wuxi': '无锡', 'suzhou': '苏州',
                'hefei': '合肥', 'yancheng': '盐城', 'changzhou': '常州'}
RULE_FIELDS = {
    '岗位已失效': ('active',), '公司黑名单': ('company',), '岗位类别不符': ('title', 'jd'),
    '排除词': ('title', 'jd'), '必要关键词缺失': ('title', 'jd'), '不接受远程': ('remote',),
    '排除地点': ('city',), '地点不符': ('city',), '学历门槛': ('minimum_degree',),
    '用工类型不符': ('job_type', 'campus', 'title'),
    '薪资低于下限': ('salary_max', 'salary_currency', 'salary_period'),
    '薪资超出偏好边界': ('salary_min', 'salary_currency', 'salary_period'),
    '非应届岗位': ('campus',), '毕业年份不符': ('graduation_year',),
    '每周出勤超过上限': ('days_per_week',), '经验门槛': ('experience_years',)}


def rule_filter(job, ctx):
    # Normalize only known place names for comparison; retain original fields in evidence.
    city = job.city
    for english, chinese in CITY_ALIASES.items():
        city = re.sub(r'\b' + english + r'\b', chinese, city, flags=re.I)
    result = evaluate(job.model_copy(update={'city': city}), ctx)
    if '岗位类别不符' in result['reasons'] and any(
            term.casefold() in job.jd.casefold() for term in ctx.preferences.job_titles):
        result['reasons'].remove('岗位类别不符')
    if ctx.preferences.review_notes:
        result['unknown'].append('working_hours_and_review_notes')
    result['decision'] = 'REJECT' if result['reasons'] else ('UNKNOWN' if result['unknown'] else 'PASS')
    result['evidence'] = [{'reason': reason, 'job_fields': redact_data({
        field: getattr(job, field) for field in RULE_FIELDS[reason]})} for reason in result['reasons']]
    return result


def model_name(ctx):
    backend = ctx.settings.matching_backend
    if backend == 'mock' and ctx.demo:
        return 'demo'
    if backend != 'deepseek':
        raise UserError('V2_MATCH_BACKEND_DENIED')
    return ctx.deepseek_model


def cache_key(job_id, job, manager, ctx):
    return 'MATCH-' + digest([job_id, job.jd_hash, manager.profile_version, manager.resume_version,
                              ctx.settings.matching_backend, model_name(ctx), MATCH_VERSION, RULE_VERSION])


def selected_jobs(store, job_ids=None, limit=100, offset=0):
    if limit is not None and not 1 <= limit <= 100:
        raise UserError('V2_MATCH_LIMIT_INVALID')
    if offset < 0:
        raise UserError('V2_MATCH_OFFSET_INVALID')
    identifiers = list(dict.fromkeys(job_ids)) if job_ids else [
        r[0] for r in store.conn.execute('SELECT job_id FROM jobs ORDER BY rowid')]
    # Validate all explicitly requested identifiers before any paid model request.
    rows = [store.get_job(identifier) for identifier in identifiers]
    return rows[offset:offset + limit] if limit is not None else rows[offset:], len(rows)


def base_row(saved, ctx):
    job = Job.model_validate(saved['job'])
    return job, {'job_id': saved['job_id'], 'title': job.title, 'company': job.company,
                 'url': job.url, 'source': job.source, 'source_external_id': job.external_id,
                 'fetched_at': job.fetched_at.isoformat(), 'job_hash': job.jd_hash,
                 'role_priority': role_priority(job.title, ctx.preferences), 'score': None,
                 'rules': rule_filter(job, ctx), 'state': 'PENDING', 'error_code': ''}


def read_cached(store, match_id, job, manager, expected_model):
    row = store.conn.execute('SELECT * FROM job_matches WHERE match_id=?', (match_id,)).fetchone()
    if row is None:
        return None
    try:
        evidence = json.loads(row['evidence_json'])
        analysis = validate_match(evidence['analysis'], job_facts(job), manager.facts)
        if (row['score'] != analysis.score or row['profile_version'] != manager.profile_version
                or row['model'] != expected_model or evidence['job_hash'] != job.jd_hash
                or evidence['resume_version'] != manager.resume_version
                or evidence['matcher_version'] != MATCH_VERSION or evidence['rule_version'] != RULE_VERSION):
            raise ValueError()
    except (UserError, ValueError, KeyError, TypeError):
        raise UserError('V2_MATCH_CACHE_INVALID') from None
    return {'match_id': match_id, 'score': analysis.score, 'analysis': analysis.model_dump(),
            'model': row['model'], 'scored_at': row['created_at']}


def save_match(store, row, job, manager, analysis, model):
    evidence = {'analysis': analysis.model_dump(), 'job_hash': job.jd_hash,
                'resume_version': manager.resume_version, 'matcher_version': MATCH_VERSION,
                'rule_version': RULE_VERSION,
                'profile_facts': {citation.fact_id: manager.facts[citation.fact_id]
                                  for assessment in analysis.assessments for citation in assessment.profile_evidence}}
    timestamp = now()
    with store.tx():
        store.conn.execute('INSERT INTO job_matches VALUES(?,?,?,?,?,?,?)',
                           (row['match_id'], row['job_id'], analysis.score,
                            json.dumps(evidence, ensure_ascii=False), manager.profile_version, model, timestamp))
    return {'score': analysis.score, 'analysis': analysis.model_dump(), 'model': model, 'scored_at': timestamp}


def result_for(ctx, manager, rows, run_id, total, *, rules_only=False):
    rankings = sorted([row for row in rows if row['state'] in {'SCORED', 'CACHED'}],
                      key=lambda r: (-r['score'], r['role_priority'], r['job_id']))
    failed = sum(row['state'] == 'FAILED' for row in rows)
    deferred = sum(row['state'] == 'DEFERRED' for row in rows)
    return {'mode': 'DEMO' if ctx.demo else 'LIVE', 'status': 'PARTIAL' if failed or deferred else 'COMPLETE',
            'run_id': run_id, 'created_at': now(), 'model': model_name(ctx), 'rules_only': rules_only,
            'score_meaning': '模型推断的匹配排序分，不代表录用概率；引用校验不等于语义结论已被证实。',
            'profile_version': manager.profile_version, 'resume_version': manager.resume_version,
            'matcher_version': MATCH_VERSION, 'rule_version': RULE_VERSION,
            'preferences': redact_data(ctx.preferences.model_dump()), 'selected_count': len(rows),
            'available_count': total, 'unselected_count': max(total - len(rows), 0),
            'scored_count': len(rankings), 'cache_hits': sum(row['state'] == 'CACHED' for row in rows),
            'filtered_count': sum(row['state'] == 'FILTERED' for row in rows),
            'failed_count': failed, 'deferred_count': deferred, 'rankings': rankings, 'jobs': rows,
            'external_submission': False}


def match_jobs(ctx, store, manager, *, job_ids=None, limit=100, offset=0, rules_only=False, matcher=None, progress=None):
    if store.demo != ctx.demo:
        raise UserError('V2_MODE_MISMATCH')
    expected_model = model_name(ctx)
    selected, total = selected_jobs(store, job_ids, limit, offset)
    run_id = 'P2-' + uuid4().hex
    rows, attempts, budget_exhausted = [], 0, False
    matcher = matcher or JobMatcher(ctx, manager.facts)
    for saved in selected:
        if progress:
            progress(len(rows), len(selected))
        job, row = base_row(saved, ctx)
        if job.is_demo != ctx.demo:
            raise UserError('V2_MODE_MISMATCH')
        rows.append(row)
        if row['rules']['decision'] == 'REJECT':
            row['state'] = 'FILTERED'
            continue
        if rules_only:
            row['state'] = 'RULES_ONLY'
            continue
        row['match_id'] = cache_key(row['job_id'], job, manager, ctx)
        try:
            cached = read_cached(store, row['match_id'], job, manager, expected_model)
            if cached:
                row.update(cached, state='CACHED')
                continue
            if budget_exhausted or attempts >= ctx.settings.max_new_jobs_for_llm:
                row.update(state='DEFERRED', error_code='DAILY_TOKEN_BUDGET' if budget_exhausted else 'V2_MATCH_RUN_LIMIT')
                continue
            attempts += 1
            value, usage = matcher.analyze(job, row['rules']['unknown'], job_id=row['job_id'], run_id=run_id)
            analysis = validate_match(value, job_facts(job), manager.facts)
            if usage.get('model') != expected_model:
                raise UserError('V2_MATCH_MODEL_MISMATCH')
            row.update(save_match(store, row, job, manager, analysis, expected_model), state='SCORED')
        except BudgetExceeded:
            budget_exhausted = True
            row.update(state='DEFERRED', error_code='DAILY_TOKEN_BUDGET')
        except UserError as error:
            code = str(error).split(':', 1)[0]
            row.update(state='FAILED', error_code=code if re.fullmatch(r'[A-Z][A-Za-z0-9_]{1,80}', code) else 'V2_MATCH_FAILED')
    if progress:
        progress(len(rows), len(selected))
    result = result_for(ctx, manager, rows, run_id, total, rules_only=rules_only)
    result['new_attempts'] = attempts
    result['offset'] = offset
    result['scope'] = 'SELECTED_BATCH'
    incomplete = next((i for i, row in enumerate(rows) if row['state'] in {'FAILED', 'DEFERRED'}), None)
    result['next_offset'] = (offset + incomplete if incomplete is not None else
                             offset + len(rows) if offset + len(rows) < total else None)
    if result['next_offset'] is not None:
        result['status'] = 'PARTIAL'
    return result


def current_rankings(ctx, store, manager, *, job_ids=None, limit=100):
    if store.demo != ctx.demo:
        raise UserError('V2_MODE_MISMATCH')
    if not 1 <= limit <= 100:
        raise UserError('V2_MATCH_LIMIT_INVALID')
    # Rank across all current records before limiting the display, not by insertion order.
    selected, total = selected_jobs(store, job_ids, limit=None)
    rows = []
    for saved in selected:
        job, row = base_row(saved, ctx)
        row['state'] = 'FILTERED' if row['rules']['decision'] == 'REJECT' else 'UNSCORED'
        if row['state'] != 'FILTERED':
            try:
                cached = read_cached(store, cache_key(row['job_id'], job, manager, ctx), job, manager, model_name(ctx))
                if cached:
                    row.update(cached, state='CACHED')
            except UserError:
                row.update(state='FAILED', error_code='V2_MATCH_CACHE_INVALID')
        rows.append(row)
    result = result_for(ctx, manager, rows, 'P2-' + uuid4().hex, total)
    result['rankings'] = result['rankings'][:limit]
    result['shown_rankings_count'] = len(result['rankings'])
    result['unscored_count'] = sum(row['state'] == 'UNSCORED' for row in rows)
    if result['unscored_count']:
        result['status'] = 'PARTIAL'
    return result
