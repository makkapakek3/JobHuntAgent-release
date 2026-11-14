"""P0 local recovery graph. No model, browser, approval or submission tools."""
from contextlib import ExitStack, closing
import os
from pathlib import Path
from typing import TypedDict

# Also protect direct library callers, not only the shared CLI entry.
for tracing_flag in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING", "LANGCHAIN_TRACING_V2"):
    os.environ[tracing_flag] = "false"

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from ..config import UserError
from ..storage import run_lock
from .models import Job
from .storage import BusinessStore, WORKFLOW_VERSION

CHECKPOINT_DATABASE_ID = 0x4A484350


class LocalState(TypedDict, total=False):
    application_id: str
    job_id: str
    workflow_version: int
    local_complete: bool


class LocalWorkflow:
    def __init__(self, home: Path, *, demo: bool):
        self.directory = Path(home) / "data" / "v2" / ("demo" if demo else "live")
        self.demo = demo

    def __enter__(self):
        self._stack = ExitStack()
        try:
            # ponytail: serialize this local namespace; per-task locks if concurrency is needed.
            self._stack.enter_context(run_lock(self.directory / "workflow.lock"))
            self.store = self._stack.enter_context(closing(BusinessStore(self.directory / "business.sqlite3", demo=self.demo)))
            self.checkpointer = self._stack.enter_context(
                SqliteSaver.from_conn_string(str(self.directory / "checkpoints.sqlite3")))
            conn = self.checkpointer.conn
            identity = conn.execute("PRAGMA application_id").fetchone()[0]
            tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if identity not in (0, CHECKPOINT_DATABASE_ID) or (identity == 0 and tables):
                raise UserError("V2_FOREIGN_CHECKPOINT_DATABASE")
            conn.execute(f"PRAGMA application_id={CHECKPOINT_DATABASE_ID}")
            self.graph = self._build_graph()
            return self
        except BaseException:
            self._stack.close()
            raise

    def __exit__(self, *args):
        return self._stack.__exit__(*args)

    @staticmethod
    def config(identifier):
        return {"configurable": {"thread_id": identifier}}

    def _safe_local_action(self, action, identifier):
        try:
            action(identifier)
        except UserError:
            raise
        except Exception:
            # LangGraph persists node errors too: sanitize before it sees the error.
            raise UserError("V2_LOCAL_EXECUTION_FAILED: 使用同一任务 ID 恢复") from None

    def _build_graph(self):
        def initialize(state):
            self._safe_local_action(self.store.initialize_local, state["application_id"])
            return {}

        def pause_local(state):
            action = interrupt({"kind": "LOCAL_RECOVERY", "application_id": state["application_id"]})
            if action != "continue_local":
                raise UserError("V2_INVALID_LOCAL_RESUME")
            return {}

        def finish(state):
            self._safe_local_action(self.store.finish_local, state["application_id"])
            return {"local_complete": True}

        graph = StateGraph(LocalState)
        graph.add_node("initialize_local", initialize)
        graph.add_node("pause_local", pause_local)
        graph.add_node("finish_local", finish)
        graph.add_edge(START, "initialize_local")
        graph.add_edge("initialize_local", "pause_local")
        graph.add_edge("pause_local", "finish_local")
        graph.add_edge("finish_local", END)
        return graph.compile(checkpointer=self.checkpointer)

    def _snapshot(self, identifier):
        row = self.store.validate_local(identifier)
        snapshot = self.graph.get_state(self.config(identifier))
        if not snapshot.values:
            if row["workflow_stage"] != "CREATED":
                raise UserError("V2_CHECKPOINT_MISSING: 已开始任务的检查点缺失，不能自动重建")
        else:
            if snapshot.values.get("workflow_version") != WORKFLOW_VERSION:
                raise UserError("V2_WORKFLOW_VERSION_UNSUPPORTED")
            if (snapshot.values.get("application_id"), snapshot.values.get("job_id")) != (identifier, row["job_id"]):
                raise UserError("V2_CHECKPOINT_MISMATCH")
        return row, snapshot

    def _invoke(self, identifier, value):
        try:
            self.graph.invoke(value, self.config(identifier), durability="sync")
        except Exception as error:
            self.store.record_error(identifier, error)
            if isinstance(error, UserError):
                raise
            raise UserError("V2_LOCAL_EXECUTION_FAILED: 使用同一任务 ID 恢复") from None

    def _start(self, identifier):
        row = self.store.validate_local(identifier)
        self._invoke(identifier, {"application_id": identifier, "job_id": row["job_id"],
                                  "workflow_version": WORKFLOW_VERSION, "local_complete": False})

    def create(self, job: Job | None = None, *, job_id: str | None = None):
        identifier = self.store.create_application(job, job_id=job_id)
        self._start(identifier)
        return self.show(identifier)

    def resume(self, identifier):
        row, snapshot = self._snapshot(identifier)
        if not snapshot.values:
            # Crash between business transaction and first checkpoint: reuse this task.
            self._start(identifier)
        elif snapshot.next and not snapshot.interrupts:
            # Replay only local idempotent nodes after a crash or local failure.
            self._invoke(identifier, None)
        _, snapshot = self._snapshot(identifier)
        if snapshot.interrupts:
            self._invoke(identifier, Command(resume="continue_local"))
        return self.show(identifier)

    def show(self, identifier):
        row, snapshot = self._snapshot(identifier)
        if not snapshot.values:
            workflow_status = "NOT_STARTED"
        elif snapshot.interrupts:
            workflow_status = "PAUSED_LOCAL"
        elif snapshot.next:
            workflow_status = "RECOVERY_REQUIRED"
        elif snapshot.values.get("local_complete") and row["workflow_stage"] == "COMPLETE":
            workflow_status = "COMPLETE"
        else:
            raise UserError("V2_CHECKPOINT_MISMATCH")
        return {"mode": "DEMO" if self.demo else "LIVE", "scope": "P0_LOCAL_RECOVERY",
                "application_id": identifier, "job_id": row["job_id"], "status": row["status"],
                "workflow_status": workflow_status, "workflow_stage": row["workflow_stage"],
                "workflow_version": row["workflow_version"], "content_digest": row["content_digest"],
                "job": {key: row["job"][key] for key in ("title", "company", "url", "source", "fetched_at")},
                "source_policy": row["source_policy"], "events": self.store.events(identifier),
                "external_submission": False}
