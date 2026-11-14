"""Local demo application packages, approval gates and durable receipts."""
import hashlib
import json
import re
import secrets
from urllib.parse import urlsplit
from uuid import uuid4

from pydantic import ValidationError

from ..config import UserError
from ..models import digest, now
from .models import Job
from .application_writer import validate_fields
from .storage import BusinessStore

WORKFLOW_VERSION = 2
PACKAGE_KEYS = {"scope", "job", "job_id", "job_hash", "profile_version", "resume_version",
                "entry", "fields", "evidence"}
FIELD_KEYS = {"name", "email", "summary", "resume_text"}
EDITABLE = {"PREPARED", "WAITING_APPROVAL", "FAILED", "APPROVED"}
ERROR_CODES = {"APPLICATION_ERROR", "FORM_INVALID", "FILL_FAILED", "STALE_PACKAGE",
               "SUBMISSION_FAILED", "SUBMISSION_UNCERTAIN", "RECEIPT_MISSING", "RECEIPT_INVALID"}
_UNSET = object()


class ApplicationStore(BusinessStore):
    def _application(self, identifier):
        if not self.demo:
            raise UserError("V2_DEMO_REQUIRED")
        row = self.get_application(identifier)
        if row["workflow_version"] != WORKFLOW_VERSION:
            raise UserError("V2_WORKFLOW_VERSION_UNSUPPORTED")
        return row

    def _validate_package(self, job_id, package, identifier):
        if not self.demo:
            raise UserError("V2_DEMO_REQUIRED")
        if not isinstance(identifier, str) or not re.fullmatch(r"APP-[0-9a-f]{32}", identifier):
            raise UserError("V2_APPLICATION_ID_INVALID")
        if not isinstance(package, dict) or set(package) != PACKAGE_KEYS:
            raise UserError("V2_PACKAGE_INVALID")
        if package["scope"] != "LOCAL_DEMO" or package["job_id"] != job_id:
            raise UserError("V2_PACKAGE_INVALID")
        try:
            job = Job.model_validate(package["job"])
            current = Job.model_validate(self.get_job(job_id)["job"])
        except (ValidationError, TypeError, ValueError) as exc:
            raise UserError("V2_PACKAGE_INVALID") from exc
        if not job.is_demo or not current.is_demo:
            raise UserError("V2_DEMO_REQUIRED")
        if package["job_hash"] != job.jd_hash or job.jd_hash != current.jd_hash:
            raise UserError("V2_JOB_CHANGED_REVIEW_REQUIRED")
        if (job.source, job.external_id) != (current.source, current.external_id):
            raise UserError("V2_PACKAGE_INVALID")
        try:
            entry = urlsplit(package["entry"])
            valid_entry = (entry.scheme == "http" and entry.hostname == "127.0.0.1"
                           and entry.port is not None and 1 <= entry.port <= 65535
                           and package["entry"] == f"http://127.0.0.1:{entry.port}/application/{identifier}")
        except (TypeError, ValueError, AttributeError):
            valid_entry = False
        if not valid_entry:
            raise UserError("V2_LOCAL_ENTRY_REQUIRED")
        fields = package["fields"]
        if (not isinstance(fields, dict) or set(fields) != FIELD_KEYS
                or any(not isinstance(value, str) or not value.strip() or len(value) > 100000
                       for value in fields.values())):
            raise UserError("V2_PACKAGE_FIELDS_INVALID")
        validate_fields(fields)
        if any(not isinstance(package[key], str) or not package[key].strip()
               for key in ("profile_version", "resume_version")):
            raise UserError("V2_PACKAGE_VERSION_REQUIRED")
        if not isinstance(package["evidence"], list):
            raise UserError("V2_PACKAGE_INVALID")
        try:
            json.dumps(package, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise UserError("V2_PACKAGE_INVALID") from exc
        return job

    def create_prepared(self, job_id: str, package: dict, application_id: str) -> str:
        with self.tx():
            job = self._validate_package(job_id, package, application_id)
            if self.conn.execute("SELECT 1 FROM applications WHERE application_id=? OR job_id=?",
                                 (application_id, job_id)).fetchone():
                raise UserError("V2_DUPLICATE_APPLICATION")
            self.conn.execute("""INSERT INTO applications(application_id,job_id,status,workflow_version,
                workflow_stage,content_digest,created_at,updated_at,job_snapshot)
                VALUES(?,?,'PREPARED',?,'CREATED',?,?,?,?)""",
                              (application_id, job_id, WORKFLOW_VERSION, digest(package),
                               now(), now(), job.model_dump_json()))
            self.conn.execute("INSERT INTO application_packages VALUES(?,?)",
                              (application_id, json.dumps(package, ensure_ascii=False)))
            for event in ("DISCOVERED", "MATCHED", "PREPARED"):
                self._event(application_id, event)
        return application_id

    def package(self, identifier: str) -> dict:
        self._application(identifier)
        row = self.conn.execute("SELECT payload FROM application_packages WHERE application_id=?",
                                (identifier,)).fetchone()
        if row is None:
            raise UserError("V2_PACKAGE_NOT_FOUND")
        return json.loads(row["payload"])

    def status(self, identifier: str) -> str:
        return self._application(identifier)["status"]

    def _state(self, row, allowed):
        if row["status"] not in allowed:
            raise UserError("V2_APPLICATION_STATE_INVALID")

    def _current_package(self, row, expected_digest=_UNSET):
        package = self.package(row["application_id"])
        calculated = digest(package)
        if calculated != row["content_digest"] or (expected_digest is not _UNSET and calculated != expected_digest):
            raise UserError("V2_APPROVAL_DIGEST_MISMATCH")
        self._validate_package(row["job_id"], package, row["application_id"])
        return package

    def _transition(self, identifier, status):
        stage = "COMPLETE" if status in {"SUBMITTED", "REJECTED"} else "INITIALIZED"
        self.conn.execute("UPDATE applications SET status=?,workflow_stage=?,updated_at=? WHERE application_id=?",
                          (status, stage, now(), identifier))
        self._event(identifier, status)

    def _invalidate_approvals(self, identifier):
        cursor = self.conn.execute("""UPDATE approval_records SET invalidated_at=?
            WHERE application_id=? AND invalidated_at IS NULL""", (now(), identifier))
        if cursor.rowcount:
            self._event(identifier, "APPROVAL_INVALIDATED")

    def mark_filled(self, identifier: str) -> None:
        with self.tx():
            row = self._application(identifier)
            self._state(row, {"PREPARED", "FORM_FILLED", "WAITING_APPROVAL"})
            self._current_package(row)
            if row["status"] == "WAITING_APPROVAL":
                return
            if row["status"] == "PREPARED":
                self._transition(identifier, "FORM_FILLED")
            self._transition(identifier, "WAITING_APPROVAL")

    def decide(self, identifier: str, decision: str, expected_digest: str) -> None:
        with self.tx():
            row = self._application(identifier)
            self._state(row, {"WAITING_APPROVAL"})
            if decision not in {"approve", "reject", "edit"}:
                raise UserError("V2_APPROVAL_DECISION_INVALID")
            if decision == "approve":
                self._current_package(row, expected_digest)
            elif digest(self.package(identifier)) != row["content_digest"] or expected_digest != row["content_digest"]:
                # Cancellation/edit still identifies the old package when the source JD changed.
                raise UserError("V2_APPROVAL_DIGEST_MISMATCH")
            self._invalidate_approvals(identifier)
            self.conn.execute("""INSERT INTO approval_records(approval_id,application_id,
                content_digest,decision,created_at) VALUES(?,?,?,?,?)""",
                              ("APR-" + uuid4().hex, identifier, row["content_digest"], decision, now()))
            if decision == "edit":
                self._event(identifier, "EDIT_REQUESTED")
            else:
                self._transition(identifier, "APPROVED" if decision == "approve" else "REJECTED")

    def edit_package(self, identifier: str, package: dict) -> None:
        with self.tx():
            row = self._application(identifier)
            self._state(row, EDITABLE)
            job = self._validate_package(row["job_id"], package, identifier)
            self._invalidate_approvals(identifier)
            self.conn.execute("UPDATE application_packages SET payload=? WHERE application_id=?",
                              (json.dumps(package, ensure_ascii=False), identifier))
            self.conn.execute("UPDATE applications SET content_digest=?,job_snapshot=? WHERE application_id=?",
                              (digest(package), job.model_dump_json(), identifier))
            self._transition(identifier, "PREPARED")

    def invalidate(self, identifier: str) -> None:
        with self.tx():
            row = self._application(identifier)
            self._state(row, EDITABLE)
            self._invalidate_approvals(identifier)
            if row["status"] != "PREPARED":
                self._transition(identifier, "PREPARED")

    def begin_submission(self, identifier: str, expected_digest: str) -> str:
        with self.tx():
            row = self._application(identifier)
            self._state(row, {"APPROVED"})
            self._current_package(row, expected_digest)
            approval = self.conn.execute("""SELECT * FROM approval_records WHERE application_id=?
                AND decision='approve' AND content_digest=? AND invalidated_at IS NULL
                AND consumed_at IS NULL AND token_hash IS NULL ORDER BY rowid DESC LIMIT 1""",
                                         (identifier, expected_digest)).fetchone()
            if approval is None:
                raise UserError("V2_VALID_APPROVAL_REQUIRED")
            token = secrets.token_urlsafe(32)
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            self.conn.execute("UPDATE approval_records SET token_hash=?,consumed_at=? WHERE approval_id=?",
                              (token_hash, now(), approval["approval_id"]))
            self._transition(identifier, "SUBMITTING")
        return token

    def accept_submission(self, identifier: str, token: str, content_digest: str, fields: dict) -> dict:
        """The local form server commits receipt evidence without claiming workflow success."""
        with self.tx():
            row = self._application(identifier)
            existing = self.receipt(identifier)
            self._state(row, {"SUBMITTING", "UNKNOWN", "SUBMITTED"} if existing else {"SUBMITTING"})
            package = self._current_package(row, content_digest)
            if not isinstance(fields, dict) or fields != package["fields"]:
                raise UserError("V2_SUBMISSION_FIELDS_MISMATCH")
            if not isinstance(token, str) or not 32 <= len(token) <= 200:
                raise UserError("V2_SUBMISSION_TOKEN_INVALID")
            token_hash = hashlib.sha256(token.encode()).hexdigest()
            approval = self.conn.execute("""SELECT 1 FROM approval_records WHERE application_id=?
                AND decision='approve' AND content_digest=? AND token_hash=?
                AND consumed_at IS NOT NULL AND invalidated_at IS NULL""",
                                         (identifier, content_digest, token_hash)).fetchone()
            if approval is None:
                raise UserError("V2_SUBMISSION_TOKEN_INVALID")
            if existing:
                if existing["content_digest"] != content_digest:
                    raise UserError("V2_RECEIPT_DIGEST_MISMATCH")
                return existing
            receipt = dict(application_id=identifier, content_digest=content_digest,
                           receipt_id="RCP-" + uuid4().hex, received_at=now(), scope="LOCAL_DEMO")
            self.conn.execute("""INSERT INTO submission_receipts(application_id,content_digest,
                receipt_id,received_at,scope) VALUES(?,?,?,?,?)""",
                              (identifier, content_digest, receipt["receipt_id"], receipt["received_at"], receipt["scope"]))
            self._event(identifier, "RECEIPT_RECEIVED")
            return receipt

    def receipt(self, identifier: str) -> dict | None:
        self._application(identifier)
        row = self.conn.execute("SELECT * FROM submission_receipts WHERE application_id=?",
                                (identifier,)).fetchone()
        return dict(row) if row else None

    def finish_submission(self, identifier: str, receipt: dict) -> None:
        with self.tx():
            row = self._application(identifier)
            self._state(row, {"SUBMITTING", "UNKNOWN", "SUBMITTED"})
            stored = self.receipt(identifier)
            # Recovery reads already committed evidence; it must never submit again.
            if not stored or not isinstance(receipt, dict) or stored != receipt:
                raise UserError("V2_RECEIPT_INVALID")
            if stored["content_digest"] != row["content_digest"] or digest(self.package(identifier)) != row["content_digest"]:
                raise UserError("V2_RECEIPT_DIGEST_MISMATCH")
            if row["status"] != "SUBMITTED":
                self._transition(identifier, "SUBMITTED")

    def fail(self, identifier: str, code: str, uncertain: bool = False) -> None:
        with self.tx():
            row = self._application(identifier)
            self._state(row, {"PREPARED", "FORM_FILLED", "WAITING_APPROVAL", "APPROVED",
                              "SUBMITTING", "FAILED", "UNKNOWN"})
            status = "UNKNOWN" if uncertain or row["status"] in {"SUBMITTING", "UNKNOWN"} else "FAILED"
            code = code if isinstance(code, str) and code in ERROR_CODES else "APPLICATION_ERROR"
            self.conn.execute("UPDATE applications SET status=?,updated_at=? WHERE application_id=?",
                              (status, now(), identifier))
            if status == "FAILED":
                self._invalidate_approvals(identifier)
            self._event(identifier, status, code)
