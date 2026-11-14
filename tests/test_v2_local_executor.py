"""P3 executor contract using synthetic UIA controls, without browser access."""
from types import SimpleNamespace
import unittest
from unittest.mock import MagicMock, patch

from jobhunt.config import UserError
from jobhunt.models import digest
from jobhunt.v2.models import Job


APP_ID = "APP-" + "a" * 32
ENTRY = "http://127.0.0.1:56863/application/" + APP_ID


def package():
    job = Job(source="demo", external_id="p3", company="DEMO company",
              title="DEMO Python", jd="DEMO Python", is_demo=True,
              url="https://example.invalid/jobs/p3")
    return {"scope": "LOCAL_DEMO", "job": job.model_dump(mode="json"),
            "job_id": "JOB-" + "b" * 32, "job_hash": job.jd_hash,
            "profile_version": "profile", "resume_version": "resume",
            "entry": ENTRY, "fields": {"name": "DEMO Alice", "email": "demo@example.invalid",
            "summary": "DEMO Python 中文", "resume_text": "DEMO confirmed resume"}, "evidence": []}


class FakeControl:
    def __init__(self, kind, label, value=""):
        self.element_info = SimpleNamespace(control_type=kind, automation_id="")
        self.label, self.value = label, value
        self.set_calls, self.clicks, self.reads = [], 0, 0
        self.delayed = []
        self.on_click = lambda: None

    def window_text(self):
        return self.label

    def is_visible(self):
        return True

    def is_enabled(self):
        return True

    def set_edit_text(self, value):
        self.set_calls.append(value)
        self.delayed = [self.value]
        self.value = value

    def get_value(self):
        self.reads += 1
        return self.delayed.pop(0) if self.delayed else self.value

    def click(self):
        self.clicks += 1
        self.on_click()


class FakeDocument:
    def __init__(self, executor):
        self.title = executor.title
        self.iface_value = SimpleNamespace(CurrentValue=executor.url)
        self.nodes = [FakeControl(kind, label) for kind, label in executor.EXPECTED]
        self.identity = FakeControl("Group", "P3 package " + executor.content_digest)
        self.feedback_nodes = []
        self.feedback_group = FakeControl("Group", "P3 feedback")
        self.feedback_group.descendants = lambda: self.feedback_nodes
        self.nodes.extend([self.identity, self.feedback_group])

    def window_text(self):
        return self.title

    def is_visible(self):
        return True

    def descendants(self, control_type=None):
        return [node for node in self.nodes + self.feedback_nodes if control_type is None or node.element_info.control_type == control_type]


class LocalExecutorTests(unittest.TestCase):
    def make_executor(self):
        from jobhunt.v2.local_executor import LocalUIAExecutor
        with patch.object(LocalUIAExecutor, "_connect", return_value=MagicMock()):
            executor = LocalUIAExecutor(12, package(), APP_ID)
        document = FakeDocument(executor)
        executor.window.descendants.return_value = [document]
        executor.wait_until = self.no_delay_wait
        return executor, document

    @staticmethod
    def no_delay_wait(action, failure, seconds=5):
        for _ in range(5):
            value = action()
            if value:
                return value
        raise UserError(failure)

    def test_module_provides_executor(self):
        from jobhunt.v2 import local_executor
        self.assertTrue(hasattr(local_executor, "LocalUIAExecutor"))

    def test_invalid_entry_is_rejected_before_desktop_connection(self):
        from jobhunt.v2.local_executor import LocalUIAExecutor
        for entry in (ENTRY + "?x=1", ENTRY + "#fragment", ENTRY + "\n",
                      ENTRY.replace("127.0.0.1", "localhost"), ENTRY.replace(":56863", ":99999"),
                      ENTRY + "{ENTER}", ENTRY.replace("http:", "https:"), ENTRY.replace(APP_ID, "APP-" + "c" * 32)):
            with self.subTest(entry=entry), patch.object(LocalUIAExecutor, "_connect") as connect:
                candidate = package() | {"entry": entry}
                with self.assertRaisesRegex(UserError, "P3_LOCAL_DEMO_URL_REQUIRED"):
                    LocalUIAExecutor(12, candidate, APP_ID)
                connect.assert_not_called()

    def test_live_or_malformed_package_never_connects(self):
        from jobhunt.v2.local_executor import LocalUIAExecutor
        for change in ({"scope": "LIVE"}, {"fields": {"name": "only one"}},
                       {"job": package()["job"] | {"is_demo": False}}):
            with self.subTest(change=change), patch.object(LocalUIAExecutor, "_connect") as connect:
                with self.assertRaisesRegex(UserError, "P3_DEMO_PACKAGE_REQUIRED"):
                    LocalUIAExecutor(12, package() | change, APP_ID)
                connect.assert_not_called()

    def test_document_checks_actual_url_and_title(self):
        executor, document = self.make_executor()
        document.iface_value.CurrentValue = "https://example.invalid/copied-form"
        with self.assertRaisesRegex(UserError, "P3_LOCAL_DOCUMENT_URL_MISMATCH"):
            executor.inspect_form()
        document.iface_value.CurrentValue = ENTRY
        document.title = "different title"
        with self.assertRaisesRegex(UserError, "P3_LOCAL_DOCUMENT_NOT_EXPOSED"):
            executor.inspect_form()

    def test_unknown_and_duplicate_controls_stop_before_fill(self):
        for kind, label in (("Slider", "unexpected"), ("Edit", "DEMO name")):
            executor, document = self.make_executor()
            document.nodes.append(FakeControl(kind, label))
            with self.subTest(kind=kind), self.assertRaisesRegex(UserError, "P3_(UNKNOWN_INTERACTIVE|MISSING_OR_AMBIGUOUS)_CONTROL"):
                executor.fill_form(package()["fields"])
            self.assertFalse(any(node.set_calls for node in document.nodes))

    def test_fill_waits_for_exact_async_readback_and_metadata_excludes_values(self):
        executor, document = self.make_executor()
        executor.fill_form(package()["fields"])
        self.assertEqual(executor.snapshot_fields(), package()["fields"])
        self.assertTrue(all(node.reads >= 2 for node in document.nodes if node.label in executor.FIELDS.values()))
        token = executor.control(executor.TOKEN_LABEL, "Edit")
        token.value = "secret-approval-token"
        metadata = executor.inspect_form()
        self.assertNotIn("secret-approval-token", str(metadata))
        self.assertNotIn("demo@example.invalid", str(metadata))
        self.assertEqual(len(metadata), 7)

    def test_fill_rejects_stale_approval_control(self):
        executor, document = self.make_executor()
        executor.control(executor.TOKEN_LABEL, "Edit").value = "stale-token"
        with self.assertRaisesRegex(UserError, "P3_UNEXPECTED_APPROVAL_TOKEN"):
            executor.fill_form(package()["fields"])
        self.assertFalse(any(node.set_calls for node in document.nodes))

    def test_validate_requires_exact_fields_and_visible_feedback(self):
        executor, document = self.make_executor()
        executor.fill_form(package()["fields"])
        button = executor.control("Validate DEMO", "Button")
        button.on_click = lambda: document.feedback_nodes.append(FakeControl("Text", "P3 validation OK"))
        self.assertTrue(executor.validate_form().valid)
        self.assertEqual(button.clicks, 1)
        executor.control("DEMO summary", "Edit").value = "changed"
        with self.assertRaisesRegex(UserError, "P3_FORM_CHANGED_REVIEW_REQUIRED"):
            executor.validate_form()
        self.assertEqual(button.clicks, 1)

    def test_submit_rechecks_fields_after_token_write_and_never_retries_click(self):
        executor, document = self.make_executor()
        executor.fill_form(package()["fields"])
        button = executor.control("Submit DEMO", "Button")
        button.on_click = lambda: (_ for _ in ()).throw(RuntimeError("private secret"))
        for _ in range(2):
            with self.assertRaisesRegex(UserError, "P3_SUBMISSION_UNKNOWN_NO_RETRY"):
                executor.submit("secret-token")
        self.assertEqual(button.clicks, 1)

    def test_changed_field_during_token_write_blocks_click(self):
        executor, document = self.make_executor()
        executor.fill_form(package()["fields"])
        token = executor.control(executor.TOKEN_LABEL, "Edit")
        original = token.set_edit_text
        def mutate(value):
            original(value)
            executor.control("DEMO summary", "Edit").value = "unreviewed"
        token.set_edit_text = mutate
        with self.assertRaisesRegex(UserError, "P3_FORM_CHANGED_REVIEW_REQUIRED"):
            executor.submit("secret-token")
        self.assertEqual(executor.control("Submit DEMO", "Button").clicks, 0)

    def test_submit_success_requires_matching_durable_and_visible_receipt(self):
        executor, document = self.make_executor()
        executor.fill_form(package()["fields"])
        receipt = {"receipt_id": "RCP-" + "d" * 32, "application_id": APP_ID,
                   "content_digest": digest(package()), "scope": "LOCAL_DEMO", "received_at": "2026-11-04"}
        # Bind the exact package instance because job fetched_at is time-sensitive.
        receipt["content_digest"] = digest(executor.package)
        document.feedback_nodes.append(FakeControl("Text", "P3 receipt " + receipt["receipt_id"]))
        with patch("jobhunt.v2.local_executor.fetch_receipt", return_value=receipt):
            executor.submit("secret-token")
            result = executor.verify_result()
            self.assertEqual(result.status, "SUBMITTED")
            self.assertEqual(result.receipt_reference, receipt["receipt_id"])
        self.assertEqual(executor.control("Submit DEMO", "Button").clicks, 1)

    def test_receipt_without_visible_feedback_or_with_wrong_digest_fails(self):
        executor, document = self.make_executor()
        receipt = {"receipt_id": "RCP-" + "d" * 32, "application_id": APP_ID,
                   "content_digest": digest(executor.package), "scope": "LOCAL_DEMO", "received_at": "2026-11-04"}
        with patch("jobhunt.v2.local_executor.fetch_receipt", return_value=receipt):
            with self.assertRaisesRegex(UserError, "P3_RECEIPT_FEEDBACK_MISSING"):
                executor.verify_result()
        document.feedback_nodes.append(FakeControl("Text", "P3 receipt " + receipt["receipt_id"]))
        receipt["content_digest"] = "wrong"
        with patch("jobhunt.v2.local_executor.fetch_receipt", return_value=receipt):
            with self.assertRaisesRegex(UserError, "P3_RECEIPT_MISMATCH"):
                executor.verify_result()

    def test_open_uses_exact_current_package_without_navigation(self):
        executor, document = self.make_executor()
        executor.open_application(Job.model_validate(executor.package["job"]))
        executor.window.type_keys.assert_not_called()

    def test_open_refreshes_stale_package_and_waits_for_current_identity(self):
        executor, document = self.make_executor()
        document.identity.label = "P3 package stale"
        executor.window.type_keys.side_effect = lambda *args, **kwargs: setattr(document.identity, "label", "P3 package " + executor.content_digest)
        executor.open_application(Job.model_validate(executor.package["job"]))
        executor.window.type_keys.assert_called_once_with("^r", pause=0.1)

    def test_passive_jd_text_cannot_spoof_validation_feedback(self):
        executor, document = self.make_executor()
        executor.fill_form(package()["fields"])
        document.nodes.append(FakeControl("Text", "P3 validation OK"))
        with self.assertRaisesRegex(UserError, "P3_VALIDATION_FEEDBACK_MISSING"):
            executor.validate_form()

    def test_verification_waits_for_visible_receipt_before_http_read(self):
        executor, document = self.make_executor()
        receipt = {"receipt_id": "RCP-" + "d" * 32, "application_id": APP_ID,
                   "content_digest": executor.content_digest, "scope": "LOCAL_DEMO", "received_at": "2026-11-04"}
        def delayed_feedback(action, failure, seconds=5):
            self.assertFalse(action())
            document.feedback_nodes.append(FakeControl("Text", "P3 receipt " + receipt["receipt_id"]))
            return action()
        executor.wait_until = delayed_feedback
        def stored_receipt(*args):
            self.assertTrue(document.feedback_nodes, "receipt read raced ahead of page submission")
            return receipt
        with patch("jobhunt.v2.local_executor.fetch_receipt", side_effect=stored_receipt):
            self.assertEqual(executor.verify_result().status, "SUBMITTED")

    def test_connection_checks_chrome_executable_and_window_class(self):
        from jobhunt.v2.local_executor import LocalUIAExecutor
        for executable, kind in (("C:/msedge.exe", "Chrome_WidgetWin_1"), ("C:/chrome.exe", "Other")):
            window = MagicMock()
            window.class_name.return_value = kind
            backend, winapi, winprocess = MagicMock(), MagicMock(), MagicMock()
            backend.Application.return_value.connect.return_value.window.return_value.wrapper_object.return_value = window
            winprocess.GetModuleFileNameEx.return_value = executable
            with self.subTest(executable=executable), patch.dict("sys.modules", {
                "pywinauto": backend, "win32api": winapi, "win32process": winprocess}):
                with self.assertRaisesRegex(UserError, "P3_ORDINARY_CHROME_WINDOW_REQUIRED"):
                    LocalUIAExecutor(12, package(), APP_ID)
                winapi.OpenProcess.return_value.Close.assert_called_once()


if __name__ == "__main__":
    unittest.main()
