"""Local manual handoff for unsupported real channels; no executor or approval."""
from html import escape
import json

from ..config import UserError
from ..models import digest
from .application_writer import select_personal_evidence
from .channels import preflight, public_url
from .matching import current_rankings, rule_filter
from .models import Job
from .resume import ResumeManager


def prepare_manual(ctx, store, job_id):
    if ctx.demo != store.demo:
        raise UserError('V2_MODE_MISMATCH')
    ctx.load()  # Re-read versions on every invocation; no model or network call.
    if not ctx.permissions.prepare_manual_packages:
        raise UserError('P4_PREPARATION_DENIED')
    manager = ResumeManager(ctx)
    saved = store.get_job(job_id)
    job = Job.model_validate(saved['job'])
    if job.is_demo != ctx.demo:
        raise UserError('V2_MODE_MISMATCH')
    channel = preflight(job.url)
    evidence = select_personal_evidence(ctx, manager, job)
    quoted = '\n'.join(item['quote'] for item in evidence)
    statement = '以下内容直接引用本人提供的资料，个人贡献以原文为准。\n'
    statement += quoted or '资料中未找到可直接对应的技能证据，需本人确认补充。'
    if ctx.demo:
        statement = 'DEMO 虚构资料，仅用于离线测试。\n' + statement
    try:
        matching = current_rankings(ctx, store, manager, job_ids=[job_id], limit=1)['jobs'][0]
    except UserError as error:
        if str(error) != 'V2_MATCH_BACKEND_DENIED':
            raise
        # Manual preparation is model-independent, even with legacy model settings.
        matching = {'state': 'RULES_ONLY', 'score': None, 'rules': rule_filter(job, ctx),
                    'error_code': 'V2_MATCH_BACKEND_DENIED'}
    match = {key: matching[key] for key in ('state', 'score', 'rules', 'error_code')}
    if 'analysis' in matching:
        match['analysis'] = matching['analysis']
    threshold = ctx.preferences.min_match_score_for_review
    package = {'scope': 'P4_MANUAL_PREPARATION', 'mode': 'DEMO' if ctx.demo else 'LIVE',
               'status': 'MANUAL_REQUIRED', 'job_id': job_id, 'job': job.model_dump(mode='json'),
               'job_hash': job.jd_hash, 'source_policy': saved['source_policy'],
               'profile_version': manager.profile_version, 'resume_version': manager.resume_version,
               'entry': public_url(job.url), 'channel': channel, 'match': match,
               'score_threshold': threshold,
               'below_preference_threshold': matching['score'] is not None and matching['score'] < threshold,
               'materials': {'statement': statement, 'resume_text': ctx.safe_profile['summary']},
               'personal_evidence': evidence,
               'review_notes': ['岗位有效性、表单字段与是否已投递需本人在官网核实。',
                                '匹配分数是模型推断，材料中的个人证据是资料原文；准备材料不代表建议投递。',
                                '姓名、联系方式、原始简历文件和涉及承诺的问题由本人确认；本材料不自动补造或上传。',
                                '本地材料未提交，也不是审批令牌或平台成功回执。'],
               'external_submission': False}
    content = digest(package)
    identifier = 'MANUAL-' + content
    directory = ctx.home/'reports'/'v2'/('demo' if ctx.demo else 'live')/identifier
    directory.mkdir(parents=True, exist_ok=True)
    outputs = {'package.json': json.dumps({**package, 'package_id': identifier, 'content_digest': content}, ensure_ascii=False, indent=2),
               'review.html': _review(package, identifier, content)}
    for filename, text in outputs.items():
        path = directory/filename
        temporary = path.with_suffix('.tmp')
        temporary.write_text(text, encoding='utf-8')
        temporary.replace(path)
    return {'mode': package['mode'], 'scope': package['scope'], 'status': 'MANUAL_REQUIRED',
            'package_id': identifier, 'job_id': job_id, 'content_digest': content,
            'profile_version': manager.profile_version, 'resume_version': manager.resume_version,
            'channel_id': channel['channel']['id'], 'score': matching['score'],
            'below_preference_threshold': package['below_preference_threshold'],
            'review_path': str(directory/'review.html'), 'package_path': str(directory/'package.json'),
            'application_status_unchanged': True, 'network_checked': False, 'external_submission': False}


def _review(package, identifier, content):
    job = package['job']
    values = [('公司 / 职位', job['company'] + ' / ' + job['title']),
              ('岗位链接（未访问）', package['entry']), ('岗位来源', job['source']),
              ('原采集时间', job['fetched_at']), ('职位描述（招聘方要求）', job['jd']),
              ('渠道处理方式', package['channel']['channel']['reason']),
              ('匹配结果与待核对事项', json.dumps(package['match'], ensure_ascii=False, indent=2)),
              ('低于本人评分阈值', str(package['below_preference_threshold'])),
              ('申请说明（资料原文）', package['materials']['statement']),
              ('简历文本摘要（未上传原件）', package['materials']['resume_text']),
              ('个人证据引用', json.dumps(package['personal_evidence'], ensure_ascii=False, indent=2)),
              ('资料版本', package['profile_version']), ('简历版本', package['resume_version']),
              ('材料标识', identifier), ('内容摘要', content), ('人工核对', '\n'.join(package['review_notes']))]
    page = ['<!doctype html><html lang="zh-CN"><meta charset="utf-8">',
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; base-uri \'none\'; form-action \'none\'">',
            '<title>人工申请材料 · 未提交</title>',
            '<style>body{font:17px system-ui;max-width:960px;margin:32px auto;padding:0 18px}'
            'pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f6f8;padding:12px}</style>',
            '<h1>人工申请材料 · 未提交</h1>',
            '<p>仅在本机整理已有岗位和资料。本人可核对后在官网手动使用；此页面不会访问目标网站或提交信息。</p>']
    for title, value in values:
        page += ['<h2>' + escape(title) + '</h2><pre>' + escape(value) + '</pre>']
    return '\n'.join(page) + '</html>\n'
