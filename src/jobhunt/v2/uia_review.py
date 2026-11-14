"""DEMO-only approval experiment; UIA probing and production applications stay separate."""
import os
from typing import TypedDict

for flag in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING", "LANGCHAIN_TRACING_V2"):
    os.environ[flag] = "false"

from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from ..models import digest


class ProbeError(Exception):
    """Fixed diagnostic codes only; no arbitrary browser or exception content."""


class ReviewState(TypedDict, total=False):
    snapshot: dict
    content_digest: str
    decision: str
    status: str
    receipt: str


def build_review_graph(checkpointer, read_snapshot, submit_demo, *, review_kind="LOCAL_DEMO_APPLICATION_REVIEW"):
    def check(state):
        if digest(read_snapshot()) != state["content_digest"]:
            raise ProbeError("FORM_CHANGED_REVIEW_REQUIRED")

    def review(state):
        check(state)
        decision = interrupt({"kind": review_kind, "snapshot": state["snapshot"],
                              "content_digest": state["content_digest"], "choices": ["approve", "reject", "edit"]})
        if decision not in {"approve", "reject", "edit"}:
            raise ProbeError("UNKNOWN_DECISION")
        check(state)
        return {"decision": decision, "status": {"approve": "APPROVED_DEMO", "reject": "REJECTED", "edit": "EDIT_REQUIRED"}[decision]}

    def submit(state):
        if state.get("decision") != "approve":
            raise ProbeError("APPROVAL_REQUIRED")
        check(state)
        receipt = submit_demo(state["content_digest"])
        if not receipt:
            raise ProbeError("RECEIPT_MISSING")
        return {"status": "SUBMITTED_DEMO", "receipt": receipt}

    graph = StateGraph(ReviewState)
    graph.add_node("review", review)
    graph.add_node("submit_demo", submit)
    graph.add_edge(START, "review")
    graph.add_conditional_edges("review", lambda state: "submit_demo" if state["decision"] == "approve" else END)
    graph.add_edge("submit_demo", END)
    return graph.compile(checkpointer=checkpointer)
