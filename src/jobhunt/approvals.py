from datetime import datetime, timedelta, timezone
import json

from .config import UserError
from .models import Job, digest, display, now, redact
from .matching import cache_key
from .reporting import atomic_text, link
from .storage import encoded


def scope_for(job, ctx):
    return encoded({"action": "manual_package_only", "match_key": cache_key(job, ctx)})


def review(ctx, store, job_ids, action, reason=""):
    ctx.load()
    ids = list(dict.fromkeys(job_ids))
    output = []
    with store.tx():
        for job_id in ids:
            row = store.get(job_id)
            job = Job.model_validate_json(row["payload"])
            if action == "favorite":
                store.conn.execute("UPDATE jobs SET favorite=1 WHERE job_id=?", (job_id,))
                store.audit("FAVORITE", job_id)
                output.append({"job_id": job_id, "favorite": True})
                continue
            if row["status"] == "APPLIED":
                raise UserError("ALREADY_APPLIED: " + job_id)
            if action == "approve":
                if ctx.demo and not job.is_demo:
                    raise UserError("DEMO_PROFILE_REAL_JOB: 请先填写并使用本人已核实的真实资料")
                if not row["active"]:
                    raise UserError("JOB_INACTIVE: " + job_id)
                if row["status"] not in {"PENDING_REVIEW", "DEFERRED", "APPROVED", "MANUAL_ACTION_REQUIRED", "FAILED"}:
                    raise UserError("JOB_NOT_REVIEWABLE: " + job_id)
                # A recommendation for this exact profile/JD must exist; never approve stale analysis.
                match = store.conn.execute("SELECT 1 FROM match_results WHERE cache_key=?", (cache_key(job, ctx),)).fetchone()
                if not match:
                    raise UserError("ANALYSIS_STALE: 请重新运行 nightly")
                request_id = "REQ-" + digest([job_id, row["jd_hash"], ctx.profile_version, ctx.resume_version])[:24]
                existing = store.conn.execute("SELECT * FROM application_requests WHERE request_id=?", (request_id,)).fetchone()
                scope = scope_for(job, ctx)
                if existing and existing["status"] in {"APPROVED", "MANUAL_ACTION_REQUIRED"} and existing["expires_at"] > now() and existing["scope"] == scope:
                    output.append({"job_id": job_id, "status": existing["status"], "idempotent": True})
                    continue
                expires = (datetime.now(timezone.utc) + timedelta(hours=ctx.settings.approval_hours)).isoformat(timespec="seconds")
                store.conn.execute("INSERT INTO application_requests VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(request_id) DO UPDATE SET approved_at=excluded.approved_at,expires_at=excluded.expires_at,scope=excluded.scope,status='APPROVED'",
                                   (request_id, job_id, row["jd_hash"], ctx.profile_version, ctx.resume_version, now(), expires, scope, "APPROVED"))
                store.conn.execute("UPDATE jobs SET status='APPROVED' WHERE job_id=?", (job_id,))
                store.audit("APPROVE", job_id, {"request_id": request_id, "resume_version": ctx.resume_version, "scope": "manual_package_only"})
                output.append({"job_id": job_id, "company": job.company, "title": job.title,
                               "channel": "manual", "resume_version": ctx.resume_version, "status": "APPROVED"})
            else:
                status = "REJECTED" if action == "reject" else "DEFERRED"
                store.conn.execute("UPDATE jobs SET status=? WHERE job_id=?", (status, job_id))
                store.conn.execute("UPDATE application_requests SET status='REVOKED' WHERE job_id=? AND status IN ('APPROVED','MANUAL_ACTION_REQUIRED')", (job_id,))
                store.audit(action.upper(), job_id, {"reason": redact(reason)[:500]})
                output.append({"job_id": job_id, "status": status})
    return output


def approval_error(ctx, row, request):
    if not row["active"]:
        return "JOB_INACTIVE"
    if row["status"] == "APPLIED":
        return "ALREADY_APPLIED"
    if request["expires_at"] <= now():
        return "APPROVAL_EXPIRED"
    if request["resume_version"] != ctx.resume_version or request["profile_version"] != ctx.profile_version:
        return "PROFILE_OR_RESUME_CHANGED"
    if request["jd_hash"] != row["jd_hash"]:
        return "JD_CHANGED"
    if request["scope"] != scope_for(Job.model_validate_json(row["payload"]), ctx):
        return "ANALYSIS_SCOPE_CHANGED"
    if request["status"] not in {"APPROVED", "MANUAL_ACTION_REQUIRED"}:
        return "APPROVAL_INVALID"
    return None


def apply_approved(ctx, store, prepare_manual=False):
    ctx.load()
    if prepare_manual and not ctx.permissions.prepare_manual_packages:
        raise UserError("MANUAL_PACKAGE_PERMISSION_DENIED")
    plans = []
    # Serialize validation and package creation with approvals, updates, and confirmation.
    with store.tx():
        requests = store.conn.execute("SELECT * FROM application_requests WHERE status IN ('APPROVED','MANUAL_ACTION_REQUIRED') ORDER BY approved_at,request_id").fetchall()
        for request in requests:
            row = store.get(request["job_id"])
            problem = approval_error(ctx, row, request)
            if problem:
                plans.append({"job_id": row["job_id"], "blocked": problem})
                continue
            job = Job.model_validate_json(row["payload"])
            path = ctx.reports / "manual" / f"{request['request_id']}.md"
            plan = {"job_id": row["job_id"], "channel": "manual", "status": "MANUAL_ACTION_REQUIRED",
                    "dry_run": not prepare_manual, "resume_version": ctx.resume_version,
                    "url": job.url, "package": str(path)}
            if prepare_manual:
                body = ["# 人工投递包" + (" — DEMO 虚构岗位与档案" if ctx.demo else ""), "",
                        f"岗位：`{row['job_id']}` · {display(job.company)} / {display(job.title)}", "",
                        link(job.url), "", f"来源：{display(job.source)}；采集：{display(row['last_seen'])}",
                        f"审批：`{request['request_id']}`；到期：{request['expires_at']}",
                        f"简历文本版本：`{ctx.resume_version}`", "",
                        "请自行选择本地真实简历，核对薪资、地点、资格、原站条款后手动操作。",
                        "程序没有访问网页、填表、提交或上传任何文件。此包只保存资料版本，不复制联系方式、简历原件和身份证明。",
                        "预填参考仅来自已校验资料：技能 " + display("、".join(ctx.profile.skills)) + "。其他未知字段保持空白。", "",
                        "实际完成后在 Codex 明确确认结果，并记录证据说明；证据是用户自述，程序不验证外部回执。", ""]
                atomic_text(path, "\n".join(body))
                attempt_id = "ATT-" + digest(request["request_id"])[:24]
                # Latest manual-plan snapshot; immutable audit_events preserve prior user results.
                store.conn.execute("INSERT INTO application_attempts(attempt_id,request_id,job_id,channel,status,created_at,package_path) VALUES(?,?,?,'manual','MANUAL_ACTION_REQUIRED',?,?) ON CONFLICT(request_id) DO UPDATE SET status='MANUAL_ACTION_REQUIRED',evidence=NULL,package_path=excluded.package_path",
                                   (attempt_id, request["request_id"], row["job_id"], now(), str(path)))
                store.conn.execute("UPDATE application_requests SET status='MANUAL_ACTION_REQUIRED' WHERE request_id=?", (request["request_id"],))
                store.conn.execute("UPDATE jobs SET status='MANUAL_ACTION_REQUIRED' WHERE job_id=?", (row["job_id"],))
                store.audit("PREPARE_MANUAL", row["job_id"], {"request_id": request["request_id"]})
            plans.append(plan)
    return plans


def confirm(ctx, store, job_id, result, evidence):
    ctx.load()
    evidence = redact(evidence.strip())[:500]
    if not evidence:
        raise UserError("EVIDENCE_REQUIRED")
    target = "APPLIED" if result == "applied" else "FAILED"
    with store.tx():
        row = store.get(job_id)
        if row["status"] == target:
            return {"job_id": job_id, "status": target, "idempotent": True}
        request = store.conn.execute("SELECT * FROM application_requests WHERE job_id=? AND status='MANUAL_ACTION_REQUIRED' ORDER BY approved_at DESC LIMIT 1", (job_id,)).fetchone()
        if request is None or row["status"] != "MANUAL_ACTION_REQUIRED":
            raise UserError("MANUAL_ACTION_NOT_PREPARED")
        if request["jd_hash"] != row["jd_hash"] or request["profile_version"] != ctx.profile_version or request["resume_version"] != ctx.resume_version:
            raise UserError("APPROVAL_VERSION_MISMATCH")
        store.conn.execute("UPDATE jobs SET status=? WHERE job_id=?", (target, job_id))
        store.conn.execute("UPDATE application_requests SET status=? WHERE request_id=?", (target, request["request_id"]))
        store.conn.execute("UPDATE application_attempts SET status=?,evidence=? WHERE request_id=?", (target, encoded({"user_reported": True, "note": evidence, "confirmed_at": now()}), request["request_id"]))
        store.audit("USER_CONFIRMED_" + target, job_id, {"user_reported": True, "note": evidence})
    return {"job_id": job_id, "status": target, "user_reported": True}
