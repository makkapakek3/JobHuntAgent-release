"""Real process and checkpoint recovery acceptance; all jobs are synthetic."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from jobhunt.config import UserError
from jobhunt.v2.cli import display_result
from jobhunt.v2.models import Job
from jobhunt.v2.workflow import LocalWorkflow

ROOT = Path(__file__).resolve().parents[1]


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.job_file = self.home / "job.json"
        self.payload = dict(source="demo", external_id="p0", company="DEMO 公司",
                            title="DEMO 工程师", jd="DEMO marker-private-job-body",
                            url="https://example.invalid/p0", is_demo=True)
        self.job_file.write_text(json.dumps(self.payload), encoding="utf-8")
        self.env = {**os.environ, "PYTHONPATH": str(ROOT / "src"), "PYTHONUTF8": "1"}

    def tearDown(self):
        self.tmp.cleanup()

    def cli(self, *args, ok=True, demo=True):
        command = [sys.executable, "-m", "jobhunt", "v2", *args, "--home", str(self.home)]
        if demo:
            command.append("--demo")
        result = subprocess.run(command, env=self.env, capture_output=True, encoding="utf-8", timeout=30)
        if ok:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return json.loads(result.stdout)
        self.assertNotEqual(result.returncode, 0)
        return result.stderr

    def runtime(self):
        return LocalWorkflow(self.home, demo=True)

    def test_create_exit_show_resume_exit_show_and_repeat_is_safe(self):
        created = self.cli("create", "--job", str(self.job_file))
        identifier = created["application_id"]
        self.assertEqual(created["workflow_status"], "PAUSED_LOCAL")
        self.assertEqual(created["mode"], "DEMO")
        self.assertEqual(created["status"], "DISCOVERED")
        self.assertFalse(created["external_submission"])
        self.assertEqual(self.cli("show", identifier)["application_id"], identifier)
        resumed = self.cli("resume", identifier)
        self.assertEqual(resumed["workflow_status"], "COMPLETE")
        self.assertEqual(resumed["status"], "DISCOVERED")
        self.assertEqual(self.cli("show", identifier)["workflow_status"], "COMPLETE")
        self.assertEqual(self.cli("resume", identifier)["workflow_status"], "COMPLETE")
        with self.runtime() as workflow:
            self.assertEqual(len(workflow.store.events(identifier)), 3)
            self.assertEqual(workflow.store.conn.execute("SELECT COUNT(*) FROM approval_records").fetchone()[0], 0)
            checkpoints = list(workflow.checkpointer.list(workflow.config(identifier)))
            self.assertGreater(len(checkpoints), 2)
            encoded = repr(checkpoints)
            self.assertNotIn("marker-private-job-body", encoded)
            self.assertNotIn("example.invalid", encoded)
            self.assertNotIn("DEMO 公司", encoded)
        self.assertFalse((self.home / "data/demo.sqlite3").exists())
        self.assertFalse((self.home / "data/v2/live").exists())

    def test_orphan_business_task_can_start_from_same_id(self):
        with self.runtime() as workflow:
            identifier = workflow.store.create_application(Job(**self.payload))
        result = self.cli("resume", identifier)
        self.assertEqual(result["application_id"], identifier)
        self.assertEqual(result["workflow_status"], "COMPLETE")
        self.assertEqual(len(self.cli("list")["applications"]), 1)

    def test_digit_heavy_generated_ids_survive_output_redaction(self):
        script = '''
import os
from uuid import UUID
from unittest.mock import patch
from jobhunt.cli import main
with patch("jobhunt.v2.storage.uuid4", return_value=UUID("12345678-9012-4abc-8def-0123456789ab")):
    raise SystemExit(main(["v2", "create", "--job", os.environ["V2_TEST_JOB"],
                           "--home", os.environ["V2_TEST_HOME"], "--demo"]))
'''
        self.payload["title"] = "DEMO applicant@example.invalid"
        self.job_file.write_text(json.dumps(self.payload), encoding="utf-8")
        result = subprocess.run([sys.executable, "-c", script],
                                env=self.env | {"V2_TEST_HOME": str(self.home), "V2_TEST_JOB": str(self.job_file)},
                                capture_output=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        created = json.loads(result.stdout)
        identifier = "APP-1234567890124abc8def0123456789ab"
        self.assertEqual(created["application_id"], identifier)
        self.assertEqual(created["job_id"], "JOB-1234567890124abc8def0123456789ab")
        self.assertNotIn("applicant@", result.stdout)
        self.assertEqual(self.cli("list")["applications"][0]["application_id"], identifier)
        self.assertEqual(self.cli("resume", identifier)["workflow_status"], "COMPLETE")

    def test_display_preserves_machine_digest_but_redacts_job_and_invalid_ids(self):
        result = display_result({"application_id": "applicant@example.invalid", "content_digest": "1" * 64,
                                 "job": {"title": "DEMO applicant@example.invalid", "content_digest": "1" * 64}})
        self.assertEqual(result["content_digest"], "1" * 64)
        self.assertNotIn("applicant@", repr(result))
        self.assertNotEqual(result["job"]["content_digest"], "1" * 64)

    def test_crash_after_local_commit_before_checkpoint_is_replay_safe(self):
        # Hard process exit leaves SQLite and LangGraph at a genuine crash boundary.
        script = '''
import os
from pathlib import Path
from jobhunt.v2.models import Job
from jobhunt.v2.storage import BusinessStore
from jobhunt.v2.workflow import LocalWorkflow
original = BusinessStore.initialize_local
def crash_after_commit(self, identifier):
    original(self, identifier)
    os._exit(73)
BusinessStore.initialize_local = crash_after_commit
with LocalWorkflow(Path(os.environ["V2_TEST_HOME"]), demo=True) as workflow:
    workflow.create(Job.model_validate_json(Path(os.environ["V2_TEST_JOB"]).read_text()))
'''
        crashed = subprocess.run([sys.executable, "-c", script],
                                 env=self.env | {"V2_TEST_HOME": str(self.home), "V2_TEST_JOB": str(self.job_file)},
                                 capture_output=True, encoding="utf-8", timeout=30)
        self.assertEqual(crashed.returncode, 73, crashed.stderr)
        tasks = self.cli("list")["applications"]
        self.assertEqual(len(tasks), 1)
        identifier = tasks[0]["application_id"]
        self.assertEqual(self.cli("resume", identifier)["workflow_status"], "COMPLETE")
        with self.runtime() as workflow:
            self.assertEqual([e["event"] for e in workflow.store.events(identifier)],
                             ["TASK_CREATED", "LOCAL_INITIALIZED", "LOCAL_COMPLETED"])

    def test_completion_commit_replay_after_failure_does_not_duplicate(self):
        with self.runtime() as workflow:
            result = workflow.create(Job(**self.payload))
            identifier = result["application_id"]
            original = workflow.store.finish_local

            def fail_after_commit(task_id):
                original(task_id)
                raise RuntimeError("private-error-body")

            with patch.object(workflow.store, "finish_local", side_effect=fail_after_commit):
                with self.assertRaisesRegex(UserError, "LOCAL_EXECUTION_FAILED"):
                    workflow.resume(identifier)
        self.assertEqual(self.cli("resume", identifier)["workflow_status"], "COMPLETE")
        with self.runtime() as workflow:
            events = workflow.store.events(identifier)
            self.assertEqual(sum(e["event"] == "LOCAL_COMPLETED" for e in events), 1)
            self.assertNotIn("private-error-body", repr(events))

    def test_deleted_checkpoint_is_not_reported_as_recovered(self):
        created = self.cli("create", "--job", str(self.job_file))
        checkpoint = self.home / "data/v2/demo/checkpoints.sqlite3"
        checkpoint.unlink()
        self.assertIn("CHECKPOINT_MISSING", self.cli("resume", created["application_id"], ok=False))

    def test_duplicate_unknown_invalid_and_mode_mismatch_inputs_are_clear(self):
        created = self.cli("create", "--job", str(self.job_file))
        self.assertIn("DUPLICATE_APPLICATION", self.cli("create", "--job", str(self.job_file), ok=False))
        self.assertIn("APPLICATION_NOT_FOUND", self.cli("resume", "missing", ok=False))
        self.assertIn("DATABASE_MISSING", self.cli("show", created["application_id"], demo=False, ok=False))
        self.assertIn("MODE_MISMATCH", self.cli("create", "--job", str(self.job_file), demo=False, ok=False))
        self.job_file.write_text('{"password":"do-not-print-this"}', encoding="utf-8")
        self.assertNotIn("do-not-print-this", self.cli("create", "--job", str(self.job_file), ok=False))

    def test_network_is_unavailable_but_local_flow_completes(self):
        with patch("socket.socket.connect", side_effect=AssertionError("network forbidden")):
            with self.runtime() as workflow:
                created = workflow.create(Job(**self.payload))
                self.assertEqual(workflow.resume(created["application_id"])["workflow_status"], "COMPLETE")

    def test_other_process_lock_blocks_all_mutations(self):
        with self.runtime() as workflow:
            with self.assertRaisesRegex(UserError, "RUN_LOCKED"):
                with self.runtime():
                    self.fail("second runtime acquired lock")


if __name__ == "__main__":
    unittest.main()
