"""P1 read-only adapters. Acquisition never opens a job page or calls a model."""
import csv
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import io
import json
import os
from pathlib import Path
import re
from urllib.error import HTTPError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

from ..config import UserError
from ..models import digest
from .models import Job, SearchCriteria, SourcePolicy

MAX_BYTES = 8_000_000
ENDPOINT = 'https://remotive.com/api/remote-jobs'
TERMS = 'https://github.com/remotive-com/remote-jobs-api'
BROWSER_DETAIL = {
    '51job': ('jobs.51job.com', re.compile(r'/(?!all/)[a-z0-9-]+/([0-9]+)\.html', re.IGNORECASE)),
    'liepin': ('www.liepin.com', re.compile(r'/job/([0-9]+)\.shtml')),
}


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise UserError('V2_SOURCE_REDIRECT_DENIED')


def fetch_remotive():
    request = Request(ENDPOINT, headers={'User-Agent': 'JobHuntAgent/2.0 (personal read-only client)',
                                        'Accept': 'application/json'})
    with build_opener(NoRedirect).open(request, timeout=30) as response:
        content = response.read(MAX_BYTES + 1)
    if len(content) > MAX_BYTES:
        raise UserError('V2_SOURCE_RESPONSE_TOO_LARGE')
    try:
        return json.loads(content)
    except (ValueError, UnicodeError):
        raise UserError('V2_SOURCE_INVALID_PAYLOAD') from None


def job_rows(payload):
    rows = payload.get('jobs') if isinstance(payload, dict) else payload
    if not isinstance(rows, list) or len(rows) > 20000:
        raise UserError('V2_SOURCE_INVALID_PAYLOAD')
    return rows


class DescriptionText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.hidden = [], 0

    def handle_starttag(self, tag, attrs):
        if tag in {'script', 'style', 'noscript'}:
            self.hidden += 1

    def handle_endtag(self, tag):
        if tag in {'script', 'style', 'noscript'}:
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def salary_fields(text):
    number = r'([0-9]+(?:,[0-9]{3})*(?:\.[0-9]+)?)(k?)'
    pattern = r'(USD|EUR|GBP|CNY|AUD|CAD)\s*' + number + r'(?:\s*[-–]\s*' + number + r')?\s*(?:per\s+|/)(year|month|hour)'
    match = re.fullmatch(pattern, text.strip(), re.IGNORECASE)
    if not match:
        return {}
    currency, lower, lower_k, upper, upper_k, period = match.groups()
    low = float(lower.replace(',', '')) * (1000 if lower_k else 1)
    high = float(upper.replace(',', '')) * (1000 if upper_k else 1) if upper else low
    return dict(salary_min=low, salary_max=high, salary_currency=currency.upper(), salary_period=period.lower())


class RemotiveSource:
    policy = SourcePolicy(source='Remotive', allowed_operations=('search_jobs', 'get_job'),
                          authorization_reference=TERMS)

    def __init__(self, store, permissions, *, demo, fetch=None, clock=None):
        self.store, self.permissions, self.demo = store, permissions, demo
        self.fetch = fetch or fetch_remotive
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.acquired_at = self.clock()
        self.report = {'attribution': 'Remotive; retain source and original links; public feed delayed 24 hours',
                       'terms': TERMS, 'endpoint': ENDPOINT, 'error_code': ''}

    def _permissions(self):
        if self.demo or self.store.demo:
            raise UserError('V2_DEMO_REAL_SOURCE_DENIED')
        if not self.permissions.read_job_sources or 'Remotive' not in self.permissions.allowed_job_sources:
            raise UserError('V2_SOURCE_PERMISSION_DENIED')

    def _cached(self, previous, status):
        self.acquired_at = datetime.fromisoformat(previous['fetched_at'])
        self.report.update(cache_status=status, fetched_at=self.acquired_at.isoformat(),
                           last_attempt=previous['last_attempt'], error_code=previous['error_code'])
        return job_rows(json.loads(previous['payload']))

    def search_jobs(self, criteria):
        # Criteria are applied after normalization by discovery, so cache serves all searches.
        self._permissions()
        instant = self.clock()
        with self.store.tx():
            previous = self.store.conn.execute('SELECT * FROM source_cache WHERE source=?', ('Remotive',)).fetchone()
            if previous and instant - datetime.fromisoformat(previous['last_attempt']) < timedelta(hours=6):
                if previous['payload'] is not None:
                    return self._cached(previous, 'STALE' if previous['error_code'] else 'CACHE')
                self.report.update(cache_status='COOLDOWN', last_attempt=previous['last_attempt'],
                                   error_code=previous['error_code'])
                raise UserError('V2_SOURCE_COOLDOWN')
            self.store.conn.execute('''INSERT INTO source_cache(source,last_attempt,error_code) VALUES(?,?,?)
                ON CONFLICT(source) DO UPDATE SET last_attempt=excluded.last_attempt,error_code=excluded.error_code''',
                ('Remotive', instant.isoformat(), 'V2_SOURCE_ATTEMPT_INCOMPLETE'))
        try:
            payload = self.fetch()
            if not isinstance(payload, dict):
                raise UserError('V2_SOURCE_INVALID_PAYLOAD')
            rows = job_rows(payload)
            encoded = json.dumps({'jobs': rows}, ensure_ascii=False, allow_nan=False)
            if len(encoded.encode('utf-8')) > MAX_BYTES:
                raise UserError('V2_SOURCE_RESPONSE_TOO_LARGE')
        except Exception as error:
            if isinstance(error, HTTPError):
                code = 'V2_SOURCE_HTTP_' + str(error.code)
            elif isinstance(error, UserError) and str(error) in {
                    'V2_SOURCE_INVALID_PAYLOAD', 'V2_SOURCE_REDIRECT_DENIED', 'V2_SOURCE_RESPONSE_TOO_LARGE'}:
                code = str(error)
            else:
                code = 'V2_SOURCE_NETWORK_ERROR'
            with self.store.tx():
                self.store.conn.execute('UPDATE source_cache SET error_code=? WHERE source=?', (code, 'Remotive'))
            self.report.update(cache_status='ERROR', last_attempt=instant.isoformat(), error_code=code)
            if previous and previous['payload'] is not None:
                previous = dict(previous) | {'last_attempt': instant.isoformat(), 'error_code': code}
                return self._cached(previous, 'STALE')
            raise UserError(code) from None
        self.acquired_at = instant
        with self.store.tx():
            self.store.conn.execute('UPDATE source_cache SET fetched_at=?,payload=?,error_code=? WHERE source=?',
                                    (instant.isoformat(), encoded, '', 'Remotive'))
        self.report.update(cache_status='NETWORK', fetched_at=instant.isoformat(), last_attempt=instant.isoformat(), error_code='')
        return rows

    def get_job(self, job_id):
        for row in self.search_jobs(SearchCriteria()):
            if isinstance(row, dict) and str(job_id) in (str(row.get('id')), row.get('url')):
                return row
        raise UserError('V2_SOURCE_JOB_NOT_FOUND')

    def normalize_job(self, raw_data):
        try:
            row = dict(raw_data)
            url = urlsplit(row['url'])
            if (url.scheme != 'https' or url.hostname != 'remotive.com' or url.port not in (None, 443)
                    or url.username or url.password or not url.path.startswith('/remote-jobs/')):
                raise ValueError()
            identity = row['id']
            if isinstance(identity, bool) or not isinstance(identity, (int, str)) or not str(identity).strip():
                raise ValueError()
            parser = DescriptionText()
            parser.feed(row['description'])
            text = ' '.join(' '.join(parser.parts).split())
            salary = row.get('salary') or ''
            return Job(source='Remotive', external_id=str(identity), company=row['company_name'], title=row['title'],
                       jd=text, url=row['url'], city=row.get('candidate_required_location') or '',
                       category=row.get('category') or '', employment_type=row.get('job_type') or '',
                       required_skills=row.get('tags') or [], published_at=row.get('publication_date') or '',
                       salary_text=salary, remote=True, fetched_at=self.acquired_at, is_demo=False,
                       **salary_fields(salary))
        except (ValueError, TypeError, KeyError, AttributeError):
            raise UserError('V2_SOURCE_ROW_INVALID') from None


class LocalFileSource:
    def __init__(self, path, permissions, *, demo):
        self.path, self.permissions, self.demo = Path(path), permissions, demo
        identity = os.path.normcase(str(self.path.resolve()))
        self.file_policy = SourcePolicy(source='import:' + self.path.name[:60] + ':' + digest(identity)[:16],
                                        authorization_reference='user_provided_local_file_unverified')
        self.policy = self.file_policy
        self.acquired_at = datetime.now(timezone.utc)
        self.report = {'cache_status': 'LOCAL', 'fetched_at': self.acquired_at.isoformat(), 'error_code': '',
                       'warnings': ['User-provided data; not verified online'], 'network_requested': False}

    def _permissions(self):
        if not self.permissions.read_job_sources:
            raise UserError('V2_SOURCE_PERMISSION_DENIED')

    def search_jobs(self, criteria):
        self._permissions()
        self.policy = self.file_policy
        if self.path.suffix.lower() not in {'.csv', '.json'}:
            raise UserError('V2_SOURCE_UNSUPPORTED_FORMAT')
        try:
            with self.path.open('rb') as handle:
                content = handle.read(MAX_BYTES + 1)
            if len(content) > MAX_BYTES:
                raise UserError('V2_SOURCE_RESPONSE_TOO_LARGE')
            text = content.decode('utf-8-sig')
            if self.path.suffix.lower() == '.csv':
                rows = list(csv.DictReader(io.StringIO(text, newline='')))
            else:
                rows = json.loads(text)
            if isinstance(rows, dict) and 'needs_jd' in rows:
                pending = {key: rows.get(key, []) for key in ('needs_jd', 'needs_city')}
                counts = {key: rows.get(key, 0) for key in ('filtered_out', 'skipped')}
                if (any(not isinstance(items, list) for items in pending.values())
                        or any(type(count) is not int or count < 0 for count in counts.values())):
                    raise ValueError('invalid browser export counts')
                search = rows.get('search')
                platform = search.get('platform') if isinstance(search, dict) else None
                if isinstance(platform, str) and platform in BROWSER_DETAIL:
                    self.policy = self.file_policy.model_copy(update={'source': 'import:' + platform})
                self.report['browser_export'] = {**counts, **{
                    key + '_count': len(items) for key, items in pending.items()
                }, 'search': {key: search[key][:100] for key in ('platform', 'keyword', 'city', 'scope')
                              if isinstance(search, dict) and isinstance(search.get(key), str)}}
            return job_rows(rows)
        except (ValueError, UnicodeError, csv.Error):
            raise UserError('V2_SOURCE_INVALID_PAYLOAD') from None

    def normalize_job(self, raw_data):
        try:
            row = dict(raw_data)
            row['source'] = self.policy.source
            browser_detail = BROWSER_DETAIL.get(row['source'].removeprefix('import:'))
            if browser_detail:
                host, path_pattern = browser_detail
                raw_url = row.get('url')
                if not isinstance(raw_url, str):
                    raise ValueError('invalid browser detail URL')
                url = urlsplit(raw_url)
                detail = path_pattern.fullmatch(url.path)
                if (url.scheme != 'https' or url.netloc != host or url.query or url.fragment
                        or detail is None or row.get('external_id') != detail[1]):
                    raise ValueError('invalid browser detail identity')
            row = {key: value for key, value in row.items() if value != ''}
            if isinstance(row.get('required_skills'), str):
                row['required_skills'] = [part.strip() for part in row['required_skills'].split(';') if part.strip()]
            row.setdefault('fetched_at', self.acquired_at)
            job = Job.model_validate(row)
        except (ValueError, TypeError):
            raise UserError('V2_SOURCE_ROW_INVALID') from None
        if job.is_demo != self.demo:
            raise UserError('V2_MODE_MISMATCH')
        return job

    def get_job(self, job_id):
        for row in self.search_jobs(SearchCriteria()):
            if isinstance(row, dict) and str(job_id) in (str(row.get('external_id')), row.get('url')):
                return row
        raise UserError('V2_SOURCE_JOB_NOT_FOUND')


class ManualSource(LocalFileSource):
    def __init__(self, data, permissions, *, demo):
        super().__init__(Path('manual'), permissions, demo=demo)
        self.data = data
        self.policy = SourcePolicy(source='manual', authorization_reference='user_provided_description_unverified')

    def search_jobs(self, criteria):
        self._permissions()
        return [self.data]
