from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
import csv
import json
from pathlib import Path
from typing import Protocol
from urllib.request import Request, build_opener, HTTPRedirectHandler

from .config import ROOT, UserError
from .models import Job, now


@dataclass
class SourceResult:
    name: str
    jobs: list[Job] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    scanned: int = 0
    attribution: str = "用户本地文件；调用者负责确认原始数据的使用权限"


class JobSource(Protocol):
    name: str

    def fetch(self, store, limit: int) -> SourceResult: ...


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise UserError("SOURCE_REDIRECT_DENIED")


def fetch_json(url):
    req = Request(url, headers={"User-Agent": "JobHuntAgent/1.0 (personal read-only client)",
                                "Accept": "application/json"})
    with build_opener(NoRedirect).open(req, timeout=20) as response:
        data = response.read(8_000_001)
        if len(data) > 8_000_000:
            raise UserError("SOURCE_RESPONSE_TOO_LARGE")
        return json.loads(data)


class FileSource:
    def __init__(self, path, demo=False):
        self.path = Path(path)
        self.demo = demo
        self.name = "DEMO" if demo else "import:" + self.path.name

    def fetch(self, store, limit):
        result = SourceResult(self.name)
        try:
            if self.path.stat().st_size > 8_000_000:
                raise UserError("FILE_TOO_LARGE")
            if self.path.suffix.lower() == ".csv":
                with self.path.open(encoding="utf-8-sig", newline="") as handle:
                    rows = list(csv.DictReader(handle))
            else:
                rows = json.loads(self.path.read_text(encoding="utf-8-sig"))
                if isinstance(rows, dict):
                    rows = rows["jobs"]
            if not isinstance(rows, list):
                raise ValueError()
            for index, row in enumerate(rows[:limit], 1):
                result.scanned += 1
                try:
                    row = dict(row)
                    row["source"] = self.name
                    if self.demo:
                        row["is_demo"] = True
                    for key in list(row):
                        if row[key] == "":
                            del row[key]
                    if isinstance(row.get("required_skills"), str):
                        row["required_skills"] = row["required_skills"].split(";")
                    result.jobs.append(Job.model_validate(row))
                except (ValueError, TypeError):
                    result.errors.append(f"ROW_{index}_INVALID")
        except (OSError, ValueError, TypeError, KeyError, UserError) as exc:
            result.errors.append("SOURCE_FAILED:" + type(exc).__name__)
        return result


def demo_source():
    return FileSource(ROOT / "examples" / "jobs.demo.json", demo=True)


def source_status(ctx):
    from .config import validated
    from .models import Permissions
    path = ctx.home / 'config' / 'permissions.yaml'
    permissions = validated(Permissions, path) if path.exists() else Permissions()
    return [
        {'source': 'DEMO', 'status': 'OFFLINE_SYNTHETIC', 'automatic_login': False},
        {'source': 'CSV/JSON', 'status': 'LOCAL_IMPORT_UNVERIFIED', 'automatic_login': False},
        {'source': 'Remotive', 'status': 'CONFIGURED_NOT_PROBED' if permissions.read_job_sources and 'Remotive' in permissions.allowed_job_sources else 'UNAUTHORIZED',
         'automatic_login': False, 'terms': RemotiveSource.terms},
        *[{'source': name, 'status': 'MANUAL_IMPORT_ONLY', 'session': 'NOT_CHECKED',
           'permission': '机器访问许可尚未核实，未自动访问', 'automatic_login': False}
          for name in ['智联招聘', '前程无忧', 'BOSS直聘']]]


class TextHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


class RemotiveSource:
    name = "Remotive"
    terms = "https://github.com/remotive-com/remote-jobs-api"
    url = "https://remotive.com/api/remote-jobs?search=python&limit=100"

    def fetch(self, store, limit):
        result = SourceResult(self.name, attribution="Remotive；原站链接和来源必须保留；公开 API 数据延迟 24 小时；" + self.terms)
        with store.tx():
            previous = store.conn.execute("SELECT last_attempt FROM source_fetches WHERE source=?", (self.name,)).fetchone()
            if previous and datetime.fromisoformat(previous[0]) > datetime.now(timezone.utc) - timedelta(hours=6):
                result.errors.append("RATE_LIMIT: 六小时内已请求，本轮未请求网络")
                return result
            store.conn.execute("INSERT INTO source_fetches VALUES(?,?) ON CONFLICT(source) DO UPDATE SET last_attempt=excluded.last_attempt", (self.name, now()))
        try:
            payload = fetch_json(self.url)
            rows = payload["jobs"]
            if not isinstance(rows, list):
                raise ValueError()
            for index, row in enumerate(rows[:limit], 1):
                result.scanned += 1
                try:
                    parser = TextHTML()
                    parser.feed(row["description"])
                    result.jobs.append(Job(source=self.name, external_id=str(row["id"]),
                        company=row["company_name"], title=row["title"],
                        city=row.get("candidate_required_location", ""), jd=" ".join(parser.parts).strip(),
                        url=row["url"], published_at=row.get("publication_date", ""),
                        remote=True,
                        salary_currency="UNKNOWN", salary_period="unknown"))
                except (ValueError, TypeError, KeyError):
                    result.errors.append(f"ROW_{index}_INVALID")
        except Exception as exc:
            # No blind retries: access restrictions and 429 are never bypassed.
            result.errors.append("SOURCE_FAILED:" + type(exc).__name__)
        return result
