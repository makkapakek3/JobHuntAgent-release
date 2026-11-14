from datetime import datetime
import os
import json
from typing import TypedDict
import uuid
import time

for tracing_flag in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING", "LANGCHAIN_TRACING_V2"):
    os.environ[tracing_flag] = "false"

from langgraph.graph import END, START, StateGraph

from .config import UserError
from .approvals import scope_for
from .filters import evaluate, role_priority
from .matching import analyze, backend_id, cache_key
from .models import Job, MatchResult, now
from .reporting import SHANGHAI, generate
from .storage import Store, encoded, run_lock
from .timeutil import local_zone
from .usage import BudgetExceeded, UsageLedger


class AgentState(TypedDict, total=False):
    stats: dict
    queue: list[str]
    profile_ready: bool
    jd_ready: bool
    action: str
    current: str


def build_graph(ctx, store, sources, run_id):
    """One bounded agent chooses safe local tools; approval tools are absent."""
    def collect(state):
        started = time.perf_counter()
        stats = state["stats"]
        for source in sources:
            try:
                if source.name == "Remotive" and source.name not in ctx.permissions.allowed_job_sources:
                    stats["errors"].append("Remotive:SOURCE_NOT_ALLOWED")
                    stats['sources'].append({'name': source.name, 'status': 'UNAUTHORIZED', 'scanned': 0, 'valid': 0, 'attribution': '未访问网络'})
                    continue
                source_started = time.perf_counter()
                result = source.fetch(store, ctx.settings.max_jobs_per_source)
                stats["sources"].append({"name": result.name, "scanned": result.scanned,
                                         "valid": len(result.jobs), "attribution": result.attribution,
                                         'status': 'PARTIAL' if result.errors else 'SUCCESS',
                                         'seconds': round(time.perf_counter()-source_started, 4)})
                stats["scanned"] += result.scanned
                stats["errors"].extend(f"{result.name}:{e}" for e in result.errors)
                for job in result.jobs:
                    _, new, changed = store.upsert(job)
                    stats["new"] += int(new)
                    stats["changed"] += int(changed)
                    stats["duplicates"] += int(not new and not changed)
            except Exception as exc:
                stats["errors"].append(f"{source.name}:SOURCE_FAILED:{type(exc).__name__}")
                stats['sources'].append({'name': source.name, 'status': 'FAILED', 'scanned': 0, 'valid': 0, 'attribution': type(exc).__name__})
        stats['collection_seconds'] = round(time.perf_counter()-started, 4)
        return {"stats": stats}

    def prepare(state):
        queue = []
        stats = state["stats"]
        with store.tx():
            for row in sorted(store.listing(), key=lambda r: role_priority(json.loads(r["payload"])["title"], ctx.preferences)):
                job = Job.model_validate_json(row["payload"])
                outcome = evaluate(job, ctx)
                store.conn.execute("UPDATE jobs SET filter_info=? WHERE job_id=?", (encoded(outcome), row["job_id"]))
                status = row["status"]
                approval = store.conn.execute("SELECT * FROM application_requests WHERE job_id=? AND status IN ('APPROVED','MANUAL_ACTION_REQUIRED')", (row["job_id"],)).fetchone()
                if approval and (approval["profile_version"] != ctx.profile_version or approval["resume_version"] != ctx.resume_version or approval["scope"] != scope_for(job, ctx)):
                    store.conn.execute("UPDATE application_requests SET status='INVALIDATED' WHERE request_id=?", (approval["request_id"],))
                    status = "DISCOVERED"
                    store.conn.execute("UPDATE jobs SET status=? WHERE job_id=?", (status, row["job_id"]))
                    store.audit("ANALYSIS_OR_PROFILE_CHANGED", row["job_id"])
                if outcome["decision"] == "REJECT":
                    stats["filtered"] += 1
                    if status not in {"APPLIED", "REJECTED", "DEFERRED"}:
                        store.conn.execute("UPDATE jobs SET status='FILTERED' WHERE job_id=?", (row["job_id"],))
                        store.conn.execute("UPDATE application_requests SET status='INVALIDATED' WHERE job_id=? AND status IN ('APPROVED','MANUAL_ACTION_REQUIRED')", (row["job_id"],))
                    continue
                if status in {'DEFERRED', 'FAILED'}:
                    if not store.conn.execute('SELECT 1 FROM match_results WHERE cache_key=?', (cache_key(job, ctx),)).fetchone():
                        queue.append(row['job_id'])
                    continue
                if status not in {"APPLIED", "REJECTED", "APPROVED", "MANUAL_ACTION_REQUIRED"}:
                    if not store.conn.execute("SELECT 1 FROM match_results WHERE cache_key=?", (cache_key(job, ctx),)).fetchone():
                        store.conn.execute("UPDATE jobs SET status='DISCOVERED' WHERE job_id=?", (row["job_id"],))
                    queue.append(row["job_id"])
        return {"queue": queue, "stats": stats, "profile_ready": False, "jd_ready": False}

    def agent(state):
        if not state["queue"]:
            return {"action": "report"}
        job_id = state["queue"][0]
        job = Job.model_validate_json(store.get(job_id)["payload"])
        cached = store.conn.execute("SELECT result FROM match_results WHERE cache_key=?", (cache_key(job, ctx),)).fetchone()
        if cached:
            return {"action": "match", "current": job_id}
        if state["stats"]["analyzed"] >= ctx.settings.max_new_jobs_for_llm:
            state["stats"]["deferred"] += len(state["queue"])
            state['stats']['errors'].append('CANDIDATE_LIMIT: 剩余岗位留待下次分析')
            return {"action": "report", "stats": state["stats"]}
        if not state["profile_ready"]:
            return {"action": "profile", "current": job_id}
        if not state["jd_ready"]:
            return {"action": "jd", "current": job_id}
        return {"action": "match", "current": job_id}

    def read_profile(state):
        # Only the already validated, redacted summary is available to the agent.
        return {"profile_ready": True}

    def read_jd(state):
        # Full imported/API JD is local; no arbitrary webpage fetch tool.
        store.get(state["current"])
        return {"jd_ready": True}

    def match(state):
        started = time.perf_counter()
        row = store.get(state["current"])
        job = Job.model_validate_json(row["payload"])
        key = cache_key(job, ctx)
        cached = store.conn.execute("SELECT result FROM match_results WHERE cache_key=?", (key,)).fetchone()
        stats = state["stats"]
        try:
            if cached:
                result = MatchResult.model_validate_json(cached["result"])
                usage = {"input": 0, "output": 0, "model": backend_id(ctx)}
                stats["cached"] += 1
            else:
                stats["analyzed"] += 1
                ctx.job_id = row['job_id']
                result, usage = analyze(job, ctx, json.loads(row["filter_info"])["unknown"])
                result = MatchResult.model_validate(result.model_dump())
            with store.tx():
                store.conn.execute("INSERT OR IGNORE INTO match_results VALUES(?,?,?,?,?,?,?)",
                                   (key, row["job_id"], job.jd_hash, ctx.profile_version, backend_id(ctx), encoded(result.model_dump()), now()))
                next_status = "PENDING_REVIEW" if result.score >= ctx.preferences.min_match_score_for_review else "NOT_RECOMMENDED"
                # Match tools cannot override a human decision made while the model ran.
                store.conn.execute("UPDATE jobs SET status=? WHERE job_id=? AND jd_hash=? AND status IN ('DISCOVERED','PENDING_REVIEW','NOT_RECOMMENDED','FILTERED')",
                                   (next_status, row["job_id"], row["jd_hash"]))
                cost = None
                cfg = ctx.settings
                if cfg.input_price_per_million is not None and cfg.output_price_per_million is not None and usage["input"] is not None and usage["output"] is not None:
                    cost = (usage["input"] * cfg.input_price_per_million + usage["output"] * cfg.output_price_per_million) / 1_000_000
                store.conn.execute("INSERT INTO llm_usage(run_id,job_id,model,task,input_tokens,output_tokens,cache_hit,cost,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                                   (run_id, row["job_id"], usage["model"], "match", usage["input"], usage["output"], bool(cached), cost, now()))
        except BudgetExceeded as exc:
            stats['deferred'] += len(state['queue'])
            stats['errors'].append(str(exc))
            stats['llm_seconds'] += time.perf_counter()-started
            return {'queue': [], 'stats': stats}
        except Exception as exc:
            code = str(exc) if isinstance(exc, UserError) else type(exc).__name__
            stats["errors"].append(f"{row['job_id']}:MATCH_FAILED:{code}")
            with store.tx():
                store.conn.execute("INSERT INTO llm_usage(run_id,job_id,model,task,cache_hit,created_at) VALUES(?,?,?,?,0,?)", (run_id, row["job_id"], backend_id(ctx), "match_failed", now()))
        if not cached:
            stats['llm_seconds'] += time.perf_counter()-started
        return {"queue": state["queue"][1:], "jd_ready": False, "stats": stats}

    def report(state):
        status = "PARTIAL" if state["stats"]["errors"] else "SUCCESS"
        state['stats']['status'] = status
        state['stats']['total_seconds'] = round(time.perf_counter()-ctx.started_clock, 4)
        state['stats']['llm_seconds'] = round(state['stats']['llm_seconds'], 4)
        state['stats']['api_usage'] = UsageLedger(ctx.home, ctx.settings.schedule_timezone).summary(run_id=run_id)
        path = generate(ctx, store, run_id, state["stats"], status)
        with store.tx():
            store.conn.execute("UPDATE run_history SET ended_at=?,status=?,stats=?,report_path=? WHERE run_id=?", (now(), status, encoded(state["stats"]), path, run_id))
        return {}

    graph = StateGraph(AgentState)
    for name, func in [("collect", collect), ("prepare", prepare), ("agent", agent),
                       ("profile", read_profile), ("jd", read_jd), ("match", match), ("report", report)]:
        graph.add_node(name, func)
    graph.add_edge(START, "collect")
    graph.add_edge("collect", "prepare")
    graph.add_edge("prepare", "agent")
    graph.add_conditional_edges("agent", lambda s: s["action"], {x: x for x in ["profile", "jd", "match", "report"]})
    for tool in ["profile", "jd", "match"]:
        graph.add_edge(tool, "agent")
    graph.add_edge("report", END)
    return graph.compile()


def run_nightly(ctx, sources, once_per_day=False):
    ctx.started_clock = time.perf_counter()
    ctx.load()
    if not ctx.permissions.read_job_sources:
        raise UserError("SOURCE_READ_DENIED")
    with run_lock(ctx.db_path.with_suffix(".run.lock")):
        store = Store(ctx.db_path)
        run_id = uuid.uuid4().hex[:12]
        ctx.run_id = run_id
        stats = {"scanned": 0, "new": 0, "changed": 0, "duplicates": 0, "filtered": 0,
                 "analyzed": 0, "cached": 0, "deferred": 0, "sources": [], "errors": [],
                 'collection_seconds': 0, 'llm_seconds': 0, 'total_seconds': 0}
        try:
            if once_per_day:
                last = store.conn.execute("SELECT started_at FROM run_history WHERE status='SUCCESS' ORDER BY started_at DESC LIMIT 1").fetchone()
                zone = local_zone(ctx.settings.schedule_timezone)
                if last and datetime.fromisoformat(last[0]).astimezone(zone).date() == datetime.now(zone).date():
                    return {"status": "SKIPPED_ALREADY_RAN_TODAY"}
            with store.tx():
                store.conn.execute("UPDATE run_history SET status='INTERRUPTED',ended_at=? WHERE status='RUNNING'", (now(),))
                store.conn.execute("INSERT INTO run_history(run_id,started_at,status) VALUES(?,?,'RUNNING')", (run_id, now()))
            graph = build_graph(ctx, store, sources, run_id)
            final = graph.invoke({"stats": stats}, config={"recursion_limit": 10000, "callbacks": []})
            return {"run_id": run_id, **final["stats"]}
        except Exception as exc:
            stats["errors"].append("RUN_FAILED:" + type(exc).__name__)
            path = generate(ctx, store, run_id, stats, "FAILED")
            with store.tx():
                store.conn.execute("UPDATE run_history SET status='FAILED',ended_at=?,stats=?,report_path=? WHERE run_id=?", (now(), encoded(stats), path, run_id))
            raise UserError("RUN_FAILED: 请查看日报与 run_history") from None
        finally:
            store.close()
