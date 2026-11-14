from datetime import datetime, timezone
import hashlib
import html
import json
import re
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def redact(value):
    text = str(value)
    text = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[EMAIL]", text)
    text = re.sub(r"(?<!\d)\+?\d[\d -]{9,}\d[Xx]?(?!\d)", "[PRIVATE_NUMBER]", text)
    text = re.sub(r"(?i)(bearer\s+|sk-)[A-Za-z0-9._-]+", "[SECRET]", text)
    text = re.sub(r'''(?i)\b(api[_-]?key|password|cookie|authorization|access_token)(\s*[=:]\s*)(?:"[^"]*"|'[^']*'|[^\s,;]+)''', r'\1\2[SECRET]', text)
    return text


def redact_data(value):
    if isinstance(value, dict):
        return {k: ('[SECRET]' if re.fullmatch(r'(?i)api[_-]?key|password|cookie|authorization|access_token', k) else redact_data(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_data(v) for v in value]
    return redact(value) if isinstance(value, str) else value


def display(value):
    text = html.escape(redact(value)).replace("|", "&#124;")
    return re.sub(r"[\r\n\t]+", " ", text).replace("[", "&#91;").replace("]", "&#93;").replace("`", "&#96;")


def canonical_url(value):
    parts = urlsplit(value.strip())
    if parts.scheme not in {"http", "https"} or not parts.hostname or parts.username or parts.password:
        raise ValueError("URL must be public HTTP(S) without credentials")
    query = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
             if not k.lower().startswith("utm_") and k.lower() not in {"ref", "source", "fbclid", "gclid"}]
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/") or "/",
                       urlencode(sorted(query)), ""))


class Job(BaseModel):
    model_config = ConfigDict(extra="ignore", str_strip_whitespace=True)
    source: str = Field(min_length=1, max_length=100)
    external_id: str = ""
    company: str = Field(min_length=1, max_length=300)
    title: str = Field(min_length=1, max_length=300)
    city: str = ""
    jd: str = Field(min_length=1, max_length=60000)
    url: str
    required_skills: list[str] = Field(default_factory=list)
    minimum_degree: str = ""
    salary_min: float | None = Field(default=None, ge=0)
    salary_max: float | None = Field(default=None, ge=0)
    salary_currency: str = "CNY"
    salary_period: str = "month"
    campus: bool | None = None
    job_type: Literal["", "campus_recruiting", "internship", "experienced"] = ""
    remote: bool | None = None
    start_month: str = Field(default="", pattern=r"^(?:\d{4}-(?:0[1-9]|1[0-2]))?$")
    days_per_week: int | None = Field(default=None, ge=1, le=7)
    duration_months: int | None = Field(default=None, ge=1)
    travel: str = Field(default="", max_length=1000)
    graduation_year: int | None = None
    experience_years: float | None = Field(default=None, ge=0)
    published_at: str = ""
    active: bool = True
    is_demo: bool = False

    @field_validator("url")
    @classmethod
    def valid_url(cls, value):
        return canonical_url(value)

    @model_validator(mode="after")
    def salary_order(self):
        if self.salary_min is not None and self.salary_max is not None and self.salary_min > self.salary_max:
            raise ValueError("salary_min exceeds salary_max")
        return self

    @property
    def fingerprint(self):
        return digest([re.sub(r"\s+", "", x).casefold() for x in [self.company, self.title, self.city]])

    @property
    def jd_hash(self):
        return digest(self.model_dump(exclude={"source", "external_id", "published_at"}))

    @property
    def content_hash(self):
        facts = self.model_dump(exclude={"source", "external_id", "url", "published_at"})
        facts["jd"] = re.sub(r"\s+", " ", self.jd).strip()
        return digest(facts)


class MatchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    score: int = Field(ge=0, le=100)
    reasons: list[str] = Field(min_length=1, max_length=10)
    gaps: list[str] = Field(max_length=20)
    risks: list[str] = Field(max_length=20)
    confidence: float = Field(ge=0, le=1)


class Education(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    degree: str = Field(min_length=1)
    major: str = Field(min_length=1)
    graduation_date: str = Field(pattern=r"^\d{4}-\d{2}$")


class Project(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)
    name: str = Field(min_length=1)
    description: str = ""
    stack: list[str] = Field(default_factory=list)
    role: str = ""
    responsibilities: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def details(self):
        if not self.description and not self.responsibilities:
            raise ValueError("project description or responsibilities required")
        return self


class Profile(BaseModel):
    education: list[Education] = Field(min_length=1)
    skills: list[str] = Field(min_length=1)
    projects: list[Project] = Field(min_length=1)
    resume_summary_file: str = "profile/resumes/summary.txt"


class Preferences(BaseModel):
    job_titles: list[str] = Field(min_length=1)
    locations: dict[str, list[str]] = Field(default_factory=dict)
    compensation: dict[str, float | str | None] = Field(default_factory=dict)
    job_type: str = "campus_recruiting"
    job_types: list[Literal["campus_recruiting", "internship", "experienced"]] = Field(default_factory=list)
    secondary_job_titles: list[str] = Field(default_factory=list)
    allow_remote: bool | None = None
    internship_start_month: str = Field(default="", pattern=r"^(?:\d{4}-(?:0[1-9]|1[0-2]))?$")
    internship_max_days_per_week: int | None = Field(default=None, ge=1, le=7)
    internship_min_months: int | None = Field(default=None, ge=1)
    review_notes: list[str] = Field(default_factory=list)
    expected_graduation_year: int | None = None
    company_blacklist: list[str] = Field(default_factory=list)
    keywords_must_have: list[str] = Field(default_factory=list)
    keywords_exclude: list[str] = Field(default_factory=list)
    min_match_score_for_review: int = Field(default=75, ge=0, le=100)
    max_experience_years: float | None = None


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    max_new_jobs_for_llm: int = Field(default=20, ge=1, le=100)
    max_jobs_per_source: int = Field(default=100, ge=1, le=1000)
    approval_hours: int = Field(default=72, ge=1, le=168)
    matching_backend: str = "mock"
    deepseek_model: str = "deepseek-flash"
    daily_token_budget: int = Field(default=100000, ge=0)
    llm_max_output_tokens: int = Field(default=1200, ge=64, le=4096)
    llm_max_input_bytes: int = Field(default=48000, ge=1000, le=200000)
    llm_timeout_seconds: float = Field(default=30, gt=0, le=120)
    llm_max_retries: int = Field(default=1, ge=0, le=2)
    llm_tool_rounds: int = Field(default=2, ge=0, le=3)
    schedule_timezone: str = "Asia/Shanghai"
    nightly_time: str = Field(default="01:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    model_prices: dict[str, dict[str, float]] = Field(default_factory=dict)
    ollama_model: str = ""
    allow_local_model: bool = False
    input_price_per_million: float | None = Field(default=None, ge=0)
    output_price_per_million: float | None = Field(default=None, ge=0)

    @field_validator("matching_backend")
    @classmethod
    def backend(cls, value):
        if value not in {"mock", "ollama", "deepseek"}:
            raise ValueError("Supported backends: mock, deepseek, ollama (legacy)")
        return value

    @field_validator("schedule_timezone")
    @classmethod
    def timezone_valid(cls, value):
        from .timeutil import local_zone
        local_zone(value)
        return value

    @field_validator("model_prices")
    @classmethod
    def price_valid(cls, value):
        import math
        for rates in value.values():
            if any(k not in {"input", "output", "cached_input"} or not math.isfinite(v) or v < 0 for k, v in rates.items()):
                raise ValueError("Invalid model pricing")
        return value


JobPosting = Job


class Permissions(BaseModel):
    read_profile: bool = True
    read_job_sources: bool = True
    prepare_manual_packages: bool = True
    allowed_job_sources: list[str] = Field(default_factory=lambda: ["Remotive"])
    submit_applications: Literal[False] = False
    send_hr_messages: Literal[False] = False
    read_sensitive_private_documents: Literal[False] = False
    upload_sensitive_documents: Literal[False] = False
