from contextlib import contextmanager
import json
import os
import sqlite3
from pathlib import Path

from .config import UserError
from .models import Job, digest, now


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


@contextmanager
def run_lock(path):
    """OS lock releases on process death; no PID or stale lock guessing."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+b") as handle:
        if path.stat().st_size == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise UserError("RUN_LOCKED: 已有任务运行") from None
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == "nt":
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class Store:
    def __init__(self, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(path, timeout=10)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.conn.execute("PRAGMA journal_mode=WAL")
        version = self.conn.execute("PRAGMA user_version").fetchone()[0]
        if version > 2:
            raise UserError("DB_SCHEMA_NEWER_THAN_APP")
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS jobs (
            job_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, url TEXT NOT NULL,
            payload TEXT NOT NULL, jd_hash TEXT NOT NULL, status TEXT NOT NULL,
            active INTEGER NOT NULL, favorite INTEGER NOT NULL DEFAULT 0,
            first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, filter_info TEXT NOT NULL DEFAULT '{}');
        CREATE INDEX IF NOT EXISTS jobs_fingerprint ON jobs(fingerprint);
        CREATE TABLE IF NOT EXISTS job_sources (
            source TEXT NOT NULL, external_id TEXT NOT NULL, job_id TEXT NOT NULL REFERENCES jobs,
            url TEXT NOT NULL, fetched_at TEXT NOT NULL, PRIMARY KEY(source,external_id));
        CREATE TABLE IF NOT EXISTS match_results (
            cache_key TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs, jd_hash TEXT NOT NULL,
            profile_version TEXT NOT NULL, backend TEXT NOT NULL, result TEXT NOT NULL, analyzed_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS application_requests (
            request_id TEXT PRIMARY KEY, job_id TEXT NOT NULL REFERENCES jobs,
            jd_hash TEXT NOT NULL, profile_version TEXT NOT NULL, resume_version TEXT NOT NULL,
            approved_at TEXT NOT NULL, expires_at TEXT NOT NULL, scope TEXT NOT NULL,
            status TEXT NOT NULL, UNIQUE(job_id,jd_hash,profile_version,resume_version));
        CREATE TABLE IF NOT EXISTS application_attempts (
            attempt_id TEXT PRIMARY KEY, request_id TEXT NOT NULL UNIQUE REFERENCES application_requests,
            job_id TEXT NOT NULL REFERENCES jobs, channel TEXT NOT NULL, status TEXT NOT NULL,
            created_at TEXT NOT NULL, evidence TEXT, package_path TEXT);
        CREATE TABLE IF NOT EXISTS run_history (
            run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, ended_at TEXT,
            status TEXT NOT NULL, stats TEXT NOT NULL DEFAULT '{}', report_path TEXT);
        CREATE TABLE IF NOT EXISTS llm_usage (
            usage_id INTEGER PRIMARY KEY, run_id TEXT REFERENCES run_history, job_id TEXT,
            model TEXT NOT NULL, task TEXT NOT NULL, input_tokens INTEGER,
            output_tokens INTEGER, cache_hit INTEGER NOT NULL, cost REAL, created_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS audit_events (
            event_id INTEGER PRIMARY KEY, at TEXT NOT NULL, action TEXT NOT NULL,
            job_id TEXT, details TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS source_fetches (
            source TEXT PRIMARY KEY, last_attempt TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS job_snapshots (
            job_id TEXT NOT NULL REFERENCES jobs, jd_hash TEXT NOT NULL,
            payload TEXT NOT NULL, captured_at TEXT NOT NULL, PRIMARY KEY(job_id,jd_hash));
        """)
        columns = {row[1] for row in self.conn.execute('PRAGMA table_info(job_sources)')}
        if 'content_hash' not in columns:
            self.conn.execute("ALTER TABLE job_sources ADD COLUMN content_hash TEXT NOT NULL DEFAULT ''")
            for row in self.conn.execute('SELECT job_id,payload FROM jobs').fetchall():
                self.conn.execute('UPDATE job_sources SET content_hash=? WHERE job_id=?',
                                  (Job.model_validate_json(row['payload']).content_hash, row['job_id']))
        self.conn.execute('PRAGMA user_version=2')
        self.conn.commit()

    def close(self):
        self.conn.close()

    @contextmanager
    def tx(self):
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
            self.conn.commit()
        except BaseException:
            self.conn.rollback()
            raise

    def audit(self, action, job_id=None, details=None):
        self.conn.execute("INSERT INTO audit_events(at,action,job_id,details) VALUES(?,?,?,?)",
                          (now(), action, job_id, encoded(details or {})))

    def get(self, job_id):
        row = self.conn.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise UserError("JOB_NOT_FOUND: " + job_id)
        return row

    def upsert(self, job):
        key = job.external_id or job.url
        with self.tx():
            row = self.conn.execute("SELECT j.* FROM jobs j JOIN job_sources s USING(job_id) WHERE s.source=? AND s.external_id=?",
                                    (job.source, key)).fetchone()
            split_alias = False
            if row is not None:
                primary = Job.model_validate_json(row['payload'])
                previous_hash = self.conn.execute('SELECT content_hash FROM job_sources WHERE source=? AND external_id=?', (job.source, key)).fetchone()[0]
                if ((primary.source, primary.external_id or primary.url) != (job.source, key)
                        and previous_hash != job.content_hash and primary.content_hash != job.content_hash):
                    # Diverging aliases become their own job; never replace an approved primary target.
                    split_alias = True
                    self.audit('SOURCE_ALIAS_DIVERGED', row['job_id'])
                    row = None
            if row is None and not split_alias:
                candidates = self.conn.execute("SELECT * FROM jobs WHERE url=? OR fingerprint=? ORDER BY first_seen",
                                               (job.url, job.fingerprint)).fetchall()
                row = next((r for r in candidates if json.loads(r['payload'])['is_demo'] == job.is_demo and
                            (r['url'] == job.url or Job.model_validate_json(r['payload']).content_hash == job.content_hash)), None)
            new = row is None
            job_id = "JOB-" + digest([job.source, key])[:12].upper() if new else row["job_id"]
            primary = None if new else json.loads(row["payload"])
            if not new:
                self.conn.execute('INSERT OR IGNORE INTO job_snapshots VALUES(?,?,?,?)',
                                  (job_id, row['jd_hash'], row['payload'], row['last_seen']))
            authoritative = new or (primary["source"] == job.source and (primary["external_id"] or primary["url"]) == key)
            changed = not new and authoritative and row["jd_hash"] != job.jd_hash
            if new:
                self.conn.execute("INSERT INTO jobs(job_id,fingerprint,url,payload,jd_hash,status,active,first_seen,last_seen) VALUES(?,?,?,?,?,'DISCOVERED',?,?,?)",
                                  (job_id, job.fingerprint, job.url, encoded(job.model_dump()), job.jd_hash, job.active, now(), now()))
            elif authoritative:
                status = row["status"]
                if changed and status not in {"APPLIED", "REJECTED", "DEFERRED"}:
                    status = "DISCOVERED"
                self.conn.execute("UPDATE jobs SET fingerprint=?,url=?,payload=?,jd_hash=?,active=?,last_seen=?,status=? WHERE job_id=?",
                                  (job.fingerprint, job.url, encoded(job.model_dump()), job.jd_hash, job.active, now(), status, job_id))
                if changed:
                    self.conn.execute("UPDATE application_requests SET status='INVALIDATED' WHERE job_id=? AND status IN ('APPROVED','MANUAL_ACTION_REQUIRED')", (job_id,))
                    self.audit("JD_CHANGED", job_id)
            else:
                # A duplicate source records provenance; it cannot replace the approved target.
                self.conn.execute("UPDATE jobs SET last_seen=? WHERE job_id=?", (now(), job_id))
            self.conn.execute("INSERT INTO job_sources(source,external_id,job_id,url,fetched_at,content_hash) VALUES(?,?,?,?,?,?) ON CONFLICT(source,external_id) DO UPDATE SET job_id=excluded.job_id,url=excluded.url,fetched_at=excluded.fetched_at,content_hash=excluded.content_hash",
                              (job.source, key, job_id, job.url, now(), job.content_hash))
            if authoritative:
                self.conn.execute('INSERT OR IGNORE INTO job_snapshots VALUES(?,?,?,?)', (job_id, job.jd_hash, encoded(job.model_dump()), now()))
        return job_id, new, changed

    def listing(self, status=None):
        sql = "SELECT * FROM jobs"
        args = ()
        if status:
            sql += " WHERE status=?"
            args = (status.upper(),)
        return self.conn.execute(sql + " ORDER BY first_seen,job_id", args).fetchall()
