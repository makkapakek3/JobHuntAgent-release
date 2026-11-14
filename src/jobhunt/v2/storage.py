"""V2 business data; LangGraph owns a separate checkpoint database."""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from uuid import uuid4

from ..config import UserError
from ..models import digest, now
from .models import ApplicationStatus, Job, SourcePolicy

DATABASE_ID = 0x4A485632  # JHV2, distinguishes this database from V1 and checkpoints.
SCHEMA_VERSION = 3
WORKFLOW_VERSION = 1


class BusinessStore:
    def __init__(self, path: Path, *, demo: bool):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        # LangGraph sync nodes may run in its worker thread; the caller holds run_lock.
        self.conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.demo = demo
        try:
            self.conn.execute("PRAGMA foreign_keys=ON")
            self._migrate()
        except BaseException:
            self.conn.close()
            raise

    def close(self):
        self.conn.close()

    @contextmanager
    def tx(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise

    def _migrate(self):
        with self.tx():
            version = self.conn.execute("PRAGMA user_version").fetchone()[0]
            identity = self.conn.execute("PRAGMA application_id").fetchone()[0]
            tables = self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if identity not in (0, DATABASE_ID) or (identity == 0 and (tables or version)):
                raise UserError("V2_FOREIGN_DATABASE")
            if version > SCHEMA_VERSION:
                raise UserError("V2_SCHEMA_TOO_NEW")
            if version == 0:
                if tables:
                    raise UserError("V2_FOREIGN_DATABASE")
                statuses = ",".join(f"'{status.value}'" for status in ApplicationStatus)
                statements = [
                    "CREATE TABLE schema_metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
                    """CREATE TABLE jobs (
                        job_id TEXT PRIMARY KEY, source TEXT NOT NULL, external_id TEXT NOT NULL,
                        url TEXT NOT NULL UNIQUE, payload TEXT NOT NULL, source_policy TEXT NOT NULL,
                        fetched_at TEXT NOT NULL, UNIQUE(source, external_id))""",
                    f"""CREATE TABLE applications (
                        application_id TEXT PRIMARY KEY, job_id TEXT NOT NULL UNIQUE REFERENCES jobs(job_id),
                        status TEXT NOT NULL CHECK(status IN ({statuses})),
                        workflow_version INTEGER NOT NULL,
                        workflow_stage TEXT NOT NULL CHECK(workflow_stage IN ('CREATED','INITIALIZED','COMPLETE')),
                        content_digest TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)""",
                    """CREATE TABLE job_matches (
                        match_id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs(job_id),
                        score INTEGER NOT NULL CHECK(score BETWEEN 0 AND 100),
                        evidence_json TEXT NOT NULL, profile_version TEXT NOT NULL,
                        model TEXT NOT NULL, created_at TEXT NOT NULL)""",
                    """CREATE TABLE approval_records (
                        approval_id TEXT PRIMARY KEY, application_id TEXT NOT NULL REFERENCES applications(application_id),
                        content_digest TEXT NOT NULL, decision TEXT NOT NULL CHECK(decision IN ('approve','reject','edit')),
                        token_hash TEXT UNIQUE, created_at TEXT NOT NULL, invalidated_at TEXT, consumed_at TEXT)""",
                    """CREATE TABLE execution_logs (
                        log_id INTEGER PRIMARY KEY, application_id TEXT NOT NULL REFERENCES applications(application_id),
                        event TEXT NOT NULL CHECK(event IN ('TASK_CREATED','LOCAL_INITIALIZED','LOCAL_COMPLETED','LOCAL_ERROR')),
                        error_code TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL,
                        UNIQUE(application_id,event,error_code))""",
                ]
                for statement in statements:
                    self.conn.execute(statement)
                self.conn.execute("INSERT INTO schema_metadata VALUES('mode',?)", ("DEMO" if self.demo else "LIVE",))
                self.conn.execute(f"PRAGMA application_id={DATABASE_ID}")
                self.conn.execute("PRAGMA user_version=1")
                version = 1
            mode = self.conn.execute("SELECT value FROM schema_metadata WHERE key='mode'").fetchone()
            if mode is None or mode[0] != ("DEMO" if self.demo else "LIVE"):
                raise UserError("V2_MODE_MISMATCH")
            if version == 1:
                self.conn.execute("""CREATE TABLE job_sources (
                    source TEXT NOT NULL, external_id TEXT NOT NULL, job_id TEXT NOT NULL REFERENCES jobs(job_id),
                    url TEXT NOT NULL, payload TEXT NOT NULL, source_policy TEXT NOT NULL, fetched_at TEXT NOT NULL,
                    PRIMARY KEY(source,external_id))""")
                self.conn.execute("""INSERT INTO job_sources
                    SELECT source,external_id,job_id,url,payload,source_policy,fetched_at FROM jobs""")
                self.conn.execute("""CREATE TABLE source_cache (
                    source TEXT PRIMARY KEY, last_attempt TEXT NOT NULL, fetched_at TEXT,
                    payload TEXT, error_code TEXT NOT NULL DEFAULT '')""")
                self.conn.execute("ALTER TABLE applications ADD COLUMN job_snapshot TEXT NOT NULL DEFAULT '{}'")
                self.conn.execute("""UPDATE applications SET job_snapshot=(
                    SELECT payload FROM jobs WHERE jobs.job_id=applications.job_id)""")
                self.conn.execute("PRAGMA user_version=2")
                version = 2
            if version == 2:
                self.conn.execute("""CREATE TABLE application_packages (
                    application_id TEXT PRIMARY KEY REFERENCES applications(application_id),
                    payload TEXT NOT NULL)""")
                self.conn.execute("""CREATE TABLE submission_receipts (
                    application_id TEXT PRIMARY KEY REFERENCES applications(application_id),
                    content_digest TEXT NOT NULL, receipt_id TEXT NOT NULL UNIQUE,
                    received_at TEXT NOT NULL, scope TEXT NOT NULL CHECK(scope='LOCAL_DEMO'))""")
                # P3 edit/refill cycles need repeated events. P0 keeps its idempotence.
                self.conn.execute("""CREATE TABLE execution_logs_v3 (
                    log_id INTEGER PRIMARY KEY, application_id TEXT NOT NULL REFERENCES applications(application_id),
                    event TEXT NOT NULL CHECK(event IN (
                        'TASK_CREATED','LOCAL_INITIALIZED','LOCAL_COMPLETED','LOCAL_ERROR',
                        'DISCOVERED','MATCHED','PREPARED','FORM_FILLED','WAITING_APPROVAL',
                        'APPROVED','REJECTED','EDIT_REQUESTED','APPROVAL_INVALIDATED',
                        'SUBMITTING','RECEIPT_RECEIVED','SUBMITTED','FAILED','UNKNOWN')),
                    error_code TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL)""")
                self.conn.execute("INSERT INTO execution_logs_v3 SELECT * FROM execution_logs")
                self.conn.execute("DROP TABLE execution_logs")
                self.conn.execute("ALTER TABLE execution_logs_v3 RENAME TO execution_logs")
                self.conn.execute("""CREATE UNIQUE INDEX p0_event_idempotence
                    ON execution_logs(application_id,event,error_code)
                    WHERE event IN ('TASK_CREATED','LOCAL_INITIALIZED','LOCAL_COMPLETED','LOCAL_ERROR')""")
                self.conn.execute("PRAGMA user_version=3")

    def _event(self, identifier, event, error_code=""):
        self.conn.execute("""INSERT OR IGNORE INTO execution_logs(application_id,event,error_code,created_at)
                             VALUES(?,?,?,?)""", (identifier, event, error_code, now()))

    def _find_job(self, job):
        by_source = self.conn.execute("""SELECT j.* FROM jobs j JOIN job_sources s USING(job_id)
            WHERE s.source=? AND s.external_id=?""", (job.source, job.external_id or job.url)).fetchone()
        urls = self.conn.execute('''SELECT DISTINCT j.* FROM jobs j LEFT JOIN job_sources s USING(job_id)
            WHERE j.url=? OR s.url=?''', (job.url, job.url)).fetchall()
        if len(urls) > 1:
            raise UserError('V2_JOB_IDENTITY_CONFLICT')
        by_url = urls[0] if urls else None
        if by_source and by_url and by_source["job_id"] != by_url["job_id"]:
            raise UserError("V2_JOB_IDENTITY_CONFLICT")
        return by_source if by_source is not None else by_url

    def _save_job(self, job, policy):
        if job.is_demo != self.demo:
            raise UserError("V2_MODE_MISMATCH")
        if policy.source != job.source:
            raise UserError("V2_SOURCE_POLICY_MISMATCH")
        key = job.external_id or job.url
        row = self._find_job(job)
        new, changed = row is None, False
        job_id = "JOB-" + uuid4().hex if new else row["job_id"]
        primary = new or (row["source"], row["external_id"]) == (job.source, key)
        if new:
            self.conn.execute("INSERT INTO jobs VALUES(?,?,?,?,?,?,?)",
                              (job_id, job.source, key, job.url, job.model_dump_json(),
                               policy.model_dump_json(), job.fetched_at.isoformat()))
        elif primary:
            changed = Job.model_validate_json(row["payload"]).jd_hash != job.jd_hash
            self.conn.execute("UPDATE jobs SET url=?,payload=?,source_policy=?,fetched_at=? WHERE job_id=?",
                              (job.url, job.model_dump_json(), policy.model_dump_json(), job.fetched_at.isoformat(), job_id))
            if changed:
                self.conn.execute("""UPDATE approval_records SET invalidated_at=? WHERE invalidated_at IS NULL
                    AND consumed_at IS NULL AND application_id IN (SELECT application_id FROM applications WHERE job_id=?)""",
                                  (now(), job_id))
        self.conn.execute("""INSERT INTO job_sources VALUES(?,?,?,?,?,?,?)
            ON CONFLICT(source,external_id) DO UPDATE SET url=excluded.url,payload=excluded.payload,
            source_policy=excluded.source_policy,fetched_at=excluded.fetched_at""",
                          (job.source, key, job_id, job.url, job.model_dump_json(), policy.model_dump_json(), job.fetched_at.isoformat()))
        return job_id, new, changed

    def save_job(self, job: Job, policy: SourcePolicy):
        with self.tx():
            return self._save_job(job, policy)

    def preview_job(self, job: Job) -> Job:
        """Facts an upsert would expose: aliases cannot replace the primary job."""
        row = self._find_job(job)
        if row and (row['source'], row['external_id']) != (job.source, job.external_id or job.url):
            return Job.model_validate_json(row['payload'])
        return job

    def get_job(self, job_id):
        row = self.conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise UserError("V2_JOB_NOT_FOUND")
        result = dict(row)
        result["job"] = Job.model_validate_json(result.pop("payload")).model_dump(mode="json")
        result["source_policy"] = json.loads(result["source_policy"])
        result["sources"] = [dict(r) for r in self.conn.execute(
            "SELECT source,external_id,url,fetched_at FROM job_sources WHERE job_id=? ORDER BY rowid", (job_id,))]
        return result

    def list_jobs(self, limit=100):
        return [self.get_job(r[0]) for r in self.conn.execute("SELECT job_id FROM jobs ORDER BY rowid DESC LIMIT ?", (limit,))]

    def create_application(self, job: Job | None = None, *, job_id: str | None = None) -> str:
        if (job is None) == (job_id is None):
            raise UserError("V2_JOB_INPUT_REQUIRED")
        if job is not None and job.is_demo != self.demo:
            raise UserError("V2_MODE_MISMATCH")
        with self.tx():
            row = self._find_job(job) if job is not None else self.get_job(job_id)
            if row and self.conn.execute("SELECT 1 FROM applications WHERE job_id=?", (row["job_id"],)).fetchone():
                raise UserError("V2_DUPLICATE_APPLICATION: 使用 v2 list 查看已有任务")
            if row:
                job_id = row["job_id"]
                saved = Job.model_validate(self.get_job(job_id)["job"])
                if job is not None and saved.jd_hash != job.jd_hash:
                    raise UserError("V2_JOB_CHANGED_USE_JOB_ID")
                job = saved
            else:
                job_id, _, _ = self._save_job(job, SourcePolicy(source=job.source))
            identifier = "APP-" + uuid4().hex
            self.conn.execute("""INSERT INTO applications(application_id,job_id,status,workflow_version,workflow_stage,
                                  content_digest,created_at,updated_at,job_snapshot) VALUES(?,?,?,?,'CREATED',?,?,?,?)""",
                              (identifier, job_id, ApplicationStatus.DISCOVERED.value, WORKFLOW_VERSION,
                               digest(job.model_dump(mode="json", exclude={"fetched_at"})), now(), now(), job.model_dump_json()))
            self._event(identifier, "TASK_CREATED")
        return identifier

    def get_application(self, identifier):
        row = self.conn.execute("""SELECT a.*, j.source_policy FROM applications a
                                   JOIN jobs j USING(job_id) WHERE application_id=?""", (identifier,)).fetchone()
        if row is None:
            raise UserError("V2_APPLICATION_NOT_FOUND")
        result = dict(row)
        result["job"] = Job.model_validate_json(result.pop("job_snapshot")).model_dump(mode="json")
        result["source_policy"] = json.loads(result["source_policy"])
        return result

    def list_applications(self):
        return [dict(row) for row in self.conn.execute(
            "SELECT application_id,job_id,status,workflow_stage,created_at FROM applications ORDER BY rowid")]

    def validate_local(self, identifier):
        row = self.get_application(identifier)
        if row["workflow_version"] != WORKFLOW_VERSION:
            raise UserError("V2_WORKFLOW_VERSION_UNSUPPORTED")
        if row["status"] != ApplicationStatus.DISCOVERED.value:
            raise UserError("V2_LOCAL_ONLY_STATE_REQUIRED")
        if Job.model_validate(row["job"]).jd_hash != Job.model_validate(self.get_job(row["job_id"])["job"]).jd_hash:
            raise UserError("V2_JOB_CHANGED_REVIEW_REQUIRED")
        return row

    def _local_transition(self, identifier, expected, target, event):
        with self.tx():
            row = self.validate_local(identifier)
            if row["workflow_stage"] == target:
                return
            if row["workflow_stage"] != expected:
                raise UserError("V2_INVALID_LOCAL_STAGE")
            self.conn.execute("UPDATE applications SET workflow_stage=?,updated_at=? WHERE application_id=?",
                              (target, now(), identifier))
            self._event(identifier, event)

    def initialize_local(self, identifier):
        self._local_transition(identifier, "CREATED", "INITIALIZED", "LOCAL_INITIALIZED")

    def finish_local(self, identifier):
        self._local_transition(identifier, "INITIALIZED", "COMPLETE", "LOCAL_COMPLETED")

    def events(self, identifier):
        self.get_application(identifier)
        return [dict(row) for row in self.conn.execute(
            "SELECT event,error_code,created_at FROM execution_logs WHERE application_id=? ORDER BY log_id", (identifier,))]

    def record_error(self, identifier, error):
        # No arbitrary exception text or user payloads enter ordinary audit logs.
        error_code = type(error).__name__
        if error_code not in {"UserError", "RuntimeError", "ValueError", "OperationalError",
                              "IntegrityError", "OSError", "TimeoutError"}:
            error_code = "InternalError"
        with self.tx():
            self.get_application(identifier)
            self._event(identifier, "LOCAL_ERROR", error_code)
