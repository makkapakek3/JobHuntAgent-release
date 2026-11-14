"""V2 contracts extend the existing normalized job without changing V1."""
from datetime import datetime, timezone
from enum import StrEnum
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..models import Job as V1Job, digest


class ApplicationStatus(StrEnum):
    DISCOVERED = "DISCOVERED"
    MATCHED = "MATCHED"
    PREPARED = "PREPARED"
    FORM_FILLED = "FORM_FILLED"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    APPROVED = "APPROVED"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    REJECTED = "REJECTED"


class Job(V1Job):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    # Missing salary units are unknown; an adapter must supply actual source units.
    salary_currency: str = ""
    salary_period: str = ""
    salary_text: str = Field(default="", max_length=2000)
    category: str = Field(default="", max_length=300)
    employment_type: str = Field(default="", max_length=100)
    fetched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))

    @field_validator("fetched_at")
    @classmethod
    def aware_timestamp(cls, value):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("fetched_at must include a timezone")
        return value.astimezone(timezone.utc)

    @property
    def jd_hash(self):
        return digest(self.model_dump(mode="json", exclude={"source", "external_id", "published_at", "fetched_at"}))

    @property
    def content_hash(self):
        facts = self.model_dump(mode="json", exclude={"source", "external_id", "url", "published_at", "fetched_at"})
        facts["jd"] = re.sub(r"\s+", " ", self.jd).strip()
        return digest(facts)


class SearchCriteria(BaseModel):
    model_config = ConfigDict(extra="forbid")
    keywords: list[str] = Field(default_factory=list)
    cities: list[str] = Field(default_factory=list)
    technical_directions: list[str] = Field(default_factory=list)
    limit: int = Field(default=10, ge=1, le=100)

    @field_validator("keywords", "cities", "technical_directions")
    @classmethod
    def clean_terms(cls, value):
        if any(not term.strip() or len(term) > 100 for term in value) or len(value) > 20:
            raise ValueError("search terms must be nonblank and bounded")
        return list(dict.fromkeys(term.strip() for term in value))


class SourcePolicy(BaseModel):
    """Adapter-owned permission metadata, never authority taken from a job's JD."""
    model_config = ConfigDict(extra="forbid", frozen=True)
    source: str = Field(min_length=1)
    allowed_operations: tuple[Literal["import", "search_jobs", "get_job", "open_application",
                                      "inspect_form", "fill_form", "validate_form",
                                      "submit", "verify_result"], ...] = ("import",)
    authorization_reference: str = "user_provided_local_file"


class ValidationResult(BaseModel):
    valid: bool
    errors: list[str] = Field(default_factory=list)


class SubmissionResult(BaseModel):
    status: Literal["SUBMITTED", "FAILED", "UNKNOWN"]
    receipt_reference: str = ""
    evidence_path: str = ""
