"""Ordinary Chrome UIA adapter for the explicit local P3 DEMO fixture only."""
from collections import Counter
from copy import deepcopy
from functools import wraps
import http.client
import json
from pathlib import Path
import re
import time

from ..config import UserError
from ..models import digest
from .local_form import EXPECTED, FIELDS, TOKEN_LABEL, validate_entry, validate_package, _unique_object
from .models import Job, SourcePolicy, SubmissionResult, ValidationResult


def _safe_uia(method):
    @wraps(method)
    def call(*args, **kwargs):
        try:
            return method(*args, **kwargs)
        except UserError:
            raise
        except Exception:
            raise UserError("P3_UIA_OPERATION_FAILED") from None
    return call


def fetch_receipt(entry: str, application_id: str):
    """Read just this local receipt, with no proxy, redirect or browser protocol."""
    port = validate_entry(entry, application_id)
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        connection.request("GET", "/receipt/" + application_id)
        response = connection.getresponse()
        if response.status != 200 or response.getheader("Content-Type", "").split(";")[0] != "application/json":
            raise UserError("P3_RECEIPT_UNAVAILABLE")
        body = response.read(16385)
        if len(body) > 16384:
            raise UserError("P3_RECEIPT_INVALID")
        receipt = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object)
        if not isinstance(receipt, dict):
            raise UserError("P3_RECEIPT_INVALID")
        return receipt
    except UserError:
        raise
    except Exception:
        raise UserError("P3_RECEIPT_UNAVAILABLE") from None
    finally:
        connection.close()


class LocalUIAExecutor:
    FIELDS, TOKEN_LABEL, EXPECTED = FIELDS, TOKEN_LABEL, EXPECTED
    policy = SourcePolicy(source="local-demo", authorization_reference="explicit_local_demo",
                          allowed_operations=("open_application", "inspect_form", "fill_form", "validate_form", "submit", "verify_result"))

    @_safe_uia
    def __init__(self, handle: int, package: dict, application_id: str):
        validate_package(package, application_id)
        self.package = deepcopy(package)
        self.application_id = application_id
        self.url = self.package["entry"]
        self.title = "JobHuntAgent P3 DEMO " + application_id
        self.content_digest = digest(self.package)
        self._submit_attempted = False
        self.window = self._connect(handle)

    @staticmethod
    def _connect(handle):
        from pywinauto import Application
        import win32api
        import win32process
        app = Application(backend="uia").connect(handle=handle, timeout=10)
        window = app.window(handle=handle).wrapper_object()
        process = win32api.OpenProcess(0x410, False, window.process_id())
        try:
            executable = win32process.GetModuleFileNameEx(process, 0)
        finally:
            process.Close()
        if Path(executable).name.lower() != "chrome.exe" or window.class_name() != "Chrome_WidgetWin_1":
            raise UserError("P3_ORDINARY_CHROME_WINDOW_REQUIRED")
        return window

    @staticmethod
    def wait_until(action, failure, seconds=5):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = action()
            if value:
                return value
            time.sleep(0.1)
        raise UserError(failure)

    @_safe_uia
    def document(self):
        found = [item for item in self.window.descendants(control_type="Document")
                 if item.window_text() == self.title and item.is_visible()]
        if len(found) > 1:
            raise UserError("P3_AMBIGUOUS_LOCAL_DOCUMENT")
        if not found:
            return None
        try:
            actual_url = found[0].iface_value.CurrentValue
        except Exception:
            raise UserError("P3_LOCAL_DOCUMENT_URL_UNREADABLE") from None
        if actual_url != self.url:
            raise UserError("P3_LOCAL_DOCUMENT_URL_MISMATCH")
        return found[0]

    @_safe_uia
    def open_application(self, job: Job):
        if not isinstance(job, Job) or job.model_dump(mode="json") != self.package["job"] or not job.is_demo:
            raise UserError("P3_JOB_MISMATCH")
        existing = self.document()
        if self.window.is_minimized():
            self.window.restore()
        self.window.set_focus()
        if existing is not None and not self._current_identity():
            # Refresh the exact target so an edited package gets the new digest.
            self.window.type_keys("^r", pause=0.1)
        elif existing is None:
            self.window.type_keys("^t", pause=0.1)
            self.window.type_keys("^l", pause=0.1)
            self.window.type_keys(self.url + "{ENTER}", with_spaces=True, pause=0.01)
        self.wait_until(self._current_identity, "P3_LOCAL_DOCUMENT_NOT_EXPOSED", seconds=12)
        self.inspect_form()

    def _current_identity(self):
        document = self.document()
        return document is not None and any(item.is_visible() and item.window_text() == "P3 package " + self.content_digest
                                           for item in document.descendants(control_type="Group"))

    @_safe_uia
    def inspect_form(self):
        document = self.document()
        if document is None:
            raise UserError("P3_LOCAL_DOCUMENT_NOT_EXPOSED")
        interactive = []
        for item in document.descendants():
            kind = item.element_info.control_type
            if kind in {"Text", "Group", "StatusBar"}:
                continue
            label = item.window_text()
            if (kind, label) not in EXPECTED:
                raise UserError("P3_UNKNOWN_INTERACTIVE_CONTROL")
            # Never read Value or return arbitrary text, including the approval token.
            interactive.append({"type": kind, "name": label})
        counts = Counter((node["type"], node["name"]) for node in interactive)
        if any(counts[control] != 1 for control in EXPECTED):
            raise UserError("P3_MISSING_OR_AMBIGUOUS_CONTROL")
        return interactive

    @_safe_uia
    def control(self, label, kind):
        if (kind, label) not in EXPECTED:
            raise UserError("P3_UNKNOWN_INTERACTIVE_CONTROL")
        document = self.document()
        if document is None:
            raise UserError("P3_LOCAL_DOCUMENT_NOT_EXPOSED")
        found = [item for item in document.descendants(control_type=kind) if item.window_text() == label]
        if len(found) != 1 or not found[0].is_visible():
            raise UserError("P3_MISSING_OR_AMBIGUOUS_CONTROL")
        return found[0]

    @_safe_uia
    def feedback(self, expected):
        return expected in self._feedback_values()

    def _feedback_values(self):
        document = self.document()
        if document is None:
            raise UserError("P3_LOCAL_DOCUMENT_NOT_EXPOSED")
        groups = [item for item in document.descendants(control_type="Group")
                  if item.window_text() == "P3 feedback" and item.is_visible()]
        if len(groups) != 1:
            raise UserError("P3_FEEDBACK_CONTAINER_MISSING")
        return {item.window_text() for item in groups[0].descendants() if item.is_visible()}

    def _visible_receipt(self):
        found = {value.removeprefix("P3 receipt ") for value in self._feedback_values()
                 if re.fullmatch(r"P3 receipt RCP-[a-f0-9]{32}", value)}
        if len(found) > 1:
            raise UserError("P3_AMBIGUOUS_RECEIPT")
        return next(iter(found), None)

    @_safe_uia
    def snapshot_fields(self):
        self.inspect_form()
        return {key: self.control(label, "Edit").get_value() for key, label in FIELDS.items()}

    def _unchanged(self):
        if digest(self.package) != self.content_digest or self.snapshot_fields() != self.package["fields"]:
            raise UserError("P3_FORM_CHANGED_REVIEW_REQUIRED")

    @_safe_uia
    def fill_form(self, fields):
        self.inspect_form()
        if not isinstance(fields, dict) or fields != self.package["fields"]:
            raise UserError("P3_FORM_CHANGED_REVIEW_REQUIRED")
        if self.control(TOKEN_LABEL, "Edit").get_value() != "":
            raise UserError("P3_UNEXPECTED_APPROVAL_TOKEN")
        for key, label in FIELDS.items():
            self.inspect_form()
            self.control(label, "Edit").set_edit_text(fields[key])
            # Chromium publishes UIA Value asynchronously after SetValue returns.
            self.wait_until(lambda: self.control(label, "Edit").get_value() == fields[key],
                            "P3_INPUT_READBACK_MISMATCH")
        self._unchanged()

    @_safe_uia
    def validate_form(self):
        self._unchanged()
        if self.control(TOKEN_LABEL, "Edit").get_value() != "":
            raise UserError("P3_UNEXPECTED_APPROVAL_TOKEN")
        self.control("Validate DEMO", "Button").click()
        self.wait_until(lambda: self.feedback("P3 validation OK"), "P3_VALIDATION_FEEDBACK_MISSING")
        self._unchanged()
        return ValidationResult(valid=True)

    @_safe_uia
    def submit(self, approval_token):
        if self._submit_attempted:
            raise UserError("P3_SUBMISSION_UNKNOWN_NO_RETRY")
        if not isinstance(approval_token, str) or not 1 <= len(approval_token) <= 512:
            raise UserError("P3_APPROVAL_TOKEN_INVALID")
        self._unchanged()
        if self.control(TOKEN_LABEL, "Edit").get_value() != "":
            raise UserError("P3_UNEXPECTED_APPROVAL_TOKEN")
        self.control(TOKEN_LABEL, "Edit").set_edit_text(approval_token)
        self.wait_until(lambda: self.control(TOKEN_LABEL, "Edit").get_value() == approval_token,
                        "P3_APPROVAL_READBACK_MISMATCH")
        self._unchanged()
        button = self.control("Submit DEMO", "Button")
        if not button.is_enabled():
            raise UserError("P3_SUBMIT_BUTTON_NOT_ENABLED")
        # The workflow already consumed the approval and committed SUBMITTING.
        # Mark uncertainty before click: exceptions may occur after the OS acted.
        self._submit_attempted = True
        try:
            button.click()
        except Exception:
            raise UserError("P3_SUBMISSION_UNKNOWN_NO_RETRY") from None

    @_safe_uia
    def verify_result(self):
        self.inspect_form()
        # The page displays a receipt only after its asynchronous POST is committed.
        # Waiting here avoids falsely declaring UNKNOWN while that POST is in flight.
        visible_receipt = self.wait_until(self._visible_receipt, "P3_RECEIPT_FEEDBACK_MISSING")
        receipt = fetch_receipt(self.url, self.application_id)
        if (receipt.get("application_id") != self.application_id
                or receipt.get("content_digest") != self.content_digest
                or receipt.get("scope") != "LOCAL_DEMO"
                or receipt.get("receipt_id") != visible_receipt
                or not isinstance(receipt.get("receipt_id"), str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", receipt["receipt_id"])):
            raise UserError("P3_RECEIPT_MISMATCH")
        if not self.feedback("P3 receipt " + receipt["receipt_id"]):
            raise UserError("P3_RECEIPT_FEEDBACK_MISSING")
        return SubmissionResult(status="SUBMITTED", receipt_reference=receipt["receipt_id"])
