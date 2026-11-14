"""Offline approval-gate tests, not evidence of real Chrome control."""
from copy import deepcopy
import unittest

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command
from jobhunt.models import digest
from jobhunt.v2.uia_review import build_review_graph, ProbeError


class ReviewGateTests(unittest.TestCase):
    def setUp(self):
        self.snapshot = {"company": "DEMO local", "job": "DEMO Python", "name": "DEMO Alice",
                         "email": "demo@example.invalid", "summary": "DEMO SQLite", "resume_version": "DEMO-none"}
        self.calls = []
        self.graph = build_review_graph(InMemorySaver(), lambda: deepcopy(self.snapshot),
                                        lambda value: self.calls.append(value) or "DEMO-receipt")
        self.config = {"configurable": {"thread_id": "DEMO-offline"}}

    def start(self):
        return self.graph.invoke({"snapshot": deepcopy(self.snapshot), "content_digest": digest(self.snapshot)}, self.config)

    def test_fill_snapshot_pauses_without_submission(self):
        self.assertIn("__interrupt__", self.start())
        self.assertEqual(self.calls, [])
        self.assertEqual(self.graph.get_state(self.config).next, ("review",))

    def test_reject_never_submits(self):
        self.start()
        result = self.graph.invoke(Command(resume="reject"), self.config)
        self.assertEqual(result["status"], "REJECTED")
        self.assertEqual(self.calls, [])

    def test_edit_requires_new_review_without_submission(self):
        self.start()
        result = self.graph.invoke(Command(resume="edit"), self.config)
        self.assertEqual(result["status"], "EDIT_REQUIRED")
        self.assertEqual(self.calls, [])

    def test_explicit_approve_executes_separate_submit_node(self):
        self.start()
        result = self.graph.invoke(Command(resume="approve"), self.config)
        self.assertEqual(result["status"], "SUBMITTED_DEMO")
        self.assertEqual(result["receipt"], "DEMO-receipt")
        self.assertEqual(len(self.calls), 1)

    def test_changed_form_invalidates_approval(self):
        self.start()
        self.snapshot["summary"] = "DEMO edited"
        with self.assertRaisesRegex(ProbeError, "FORM_CHANGED"):
            self.graph.invoke(Command(resume="approve"), self.config)
        self.assertEqual(self.calls, [])

    def test_unknown_decision_stops(self):
        self.start()
        with self.assertRaisesRegex(ProbeError, "UNKNOWN_DECISION"):
            self.graph.invoke(Command(resume="unexpected"), self.config)
        self.assertEqual(self.calls, [])


if __name__ == "__main__":
    unittest.main()
