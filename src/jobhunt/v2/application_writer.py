"""Evidence-preserving local application material preparation; no model invention."""
from html import escape
import json
import re

from ..config import UserError
from ..models import digest
from .resume import ResumeManager

FIELD_LIMITS = {'name': 120, 'email': 254, 'summary': 8000, 'resume_text': 50000}
MAX_SUBMISSION_BYTES = 128 * 1024


def validate_fields(fields):
    if not isinstance(fields, dict) or set(fields) != set(FIELD_LIMITS):
        raise UserError('P3_FIELDS_INVALID')
    for key, maximum in FIELD_LIMITS.items():
        value = fields[key]
        if not isinstance(value, str) or not value.strip() or len(value) > maximum or '\x00' in value:
            raise UserError('P3_FIELDS_INVALID')
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', fields['email']):
        raise UserError('P3_FIELDS_INVALID')
    # Reserve the longest permitted token, including UTF-8 and JSON escaping overhead.
    payload = {'approval_token': 'x' * 512, 'content_digest': '0' * 64, 'fields': fields}
    if len(json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) > MAX_SUBMISSION_BYTES:
        raise UserError('P3_FIELDS_TOO_LARGE')
    return dict(fields)


def select_personal_evidence(ctx, manager, job):
    """Select exact confirmed facts; never turn a JD statement into a personal claim."""
    facts = manager.facts
    wanted = [value for key, value in facts.items() if key.startswith('skills.')
              and value.casefold() in (job.title + ' ' + job.jd).casefold()]
    evidence = [{'fact_id': key, 'quote': value} for key, value in facts.items()
                if key.startswith('skills.') and value in wanted]
    for i, project in enumerate(ctx.safe_profile.get('projects', [])):
        if any(skill in project.get('stack', []) for skill in wanted):
            for key, value in facts.items():
                if key.startswith(f'projects.{i}.') and any(part in key for part in ('name', 'role', 'description', 'responsibilities')):
                    evidence.append({'fact_id': key, 'quote': value})
    return evidence


class ApplicationWriter:
    def __init__(self, ctx):
        if not ctx.demo:
            raise UserError('P3_DEMO_REQUIRED')
        self.manager = ResumeManager(ctx)
        if not ctx.permissions.prepare_manual_packages:
            raise UserError('P3_PREPARATION_DENIED')
        self.ctx = ctx

    def build(self, job, job_id, application_id, port, contact=None):
        if not job.is_demo:
            raise UserError('P3_DEMO_REQUIRED')
        if not re.fullmatch(r'APP-[0-9a-f]{32}', application_id) or not 1024 <= port <= 65535:
            raise UserError('P3_LOCAL_ENTRY_INVALID')
        contact = contact if contact is not None else {'name': 'DEMO Alice', 'email': 'demo@example.invalid'}
        if not isinstance(contact, dict) or set(contact) != {'name', 'email'}:
            raise UserError('P3_CONTACT_INVALID')
        evidence = select_personal_evidence(self.ctx, self.manager, job)
        summary = 'DEMO 模拟申请。以下内容直接引用资料，个人贡献以原文为准。\n'
        summary += '\n'.join(item['quote'] for item in evidence) if evidence else '现有资料中没有可直接对应的技能证据，需本人补充确认。'
        resume_text = self.ctx.safe_profile['summary']
        fields = validate_fields({**contact, 'summary': summary, 'resume_text': resume_text})
        return {'scope': 'LOCAL_DEMO', 'job_id': job_id, 'job': job.model_dump(mode='json'),
                'job_hash': job.jd_hash, 'profile_version': self.manager.profile_version,
                'resume_version': self.manager.resume_version,
                'entry': f'http://127.0.0.1:{port}/application/{application_id}',
                'fields': fields, 'evidence': evidence}


def write_review(home, application_id, package):
    directory = home / 'reports' / 'v2' / 'demo' / application_id
    directory.mkdir(parents=True, exist_ok=True)
    content = digest(package)
    job = package['job']
    parts = ['<!doctype html><meta charset="utf-8"><title>P3 本地模拟申请审核</title>',
             '<style>body{font:17px system-ui;max-width:900px;margin:30px auto;padding:0 18px}'
             'pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f4f6f8;padding:12px}</style>',
             '<h1>P3 本地模拟申请审核</h1><p>以下是本次准备提交的全部内容。仅提交至本机模拟服务。</p>']
    values = {'申请 ID': application_id, '公司': job['company'], '职位': job['title'],
              '岗位原链接（未访问）': job['url'], '实际模拟申请入口': package['entry'],
              '原简历摘要版本': package['resume_version'], '个人资料版本': package['profile_version'],
              '本次全部内容摘要': content, **package['fields'],
              '材料引用来源': json.dumps(package['evidence'], ensure_ascii=False, indent=2)}
    for key, value in values.items():
        parts += ['<h2>' + escape(key) + '</h2><pre>' + escape(value) + '</pre>']
    path = directory / 'review.html'
    temporary = path.with_suffix('.tmp')
    temporary.write_text('\n'.join(parts), encoding='utf-8')
    temporary.replace(path)
    return str(path)
