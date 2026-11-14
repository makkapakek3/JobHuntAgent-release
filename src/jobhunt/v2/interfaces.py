"""Replaceable source/executor contracts, including exact local form readback."""
from typing import Any, Protocol

from .models import Job, SearchCriteria, SourcePolicy, SubmissionResult, ValidationResult


class JobSource(Protocol):
    policy: SourcePolicy

    def search_jobs(self, criteria: SearchCriteria) -> list[dict[str, Any]]: ...

    def get_job(self, job_id: str) -> dict[str, Any]: ...

    def normalize_job(self, raw_data: dict[str, Any]) -> Job: ...


class ApplicationExecutor(Protocol):
    policy: SourcePolicy

    def open_application(self, job: Job) -> None: ...

    def inspect_form(self) -> list[dict[str, Any]]: ...

    def fill_form(self, fields: dict[str, Any]) -> None: ...

    def validate_form(self) -> ValidationResult: ...

    def snapshot_fields(self) -> dict[str, str]:
        """Read submitted business fields for approval comparison; exclude tokens/credentials."""
        ...

    def submit(self, approval_token: str) -> None:
        """Implementations must verify an unconsumed, content-bound approval."""
        ...

    def verify_result(self) -> SubmissionResult:
        """SUBMITTED requires a verifiable receipt, not a successful click."""
        ...
