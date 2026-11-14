"""Explicit local UIA experiment. Never imports a browser automation protocol."""
import argparse
from collections import Counter
import json
from pathlib import Path
import re
import time

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from ..models import digest, now
from ..storage import run_lock
from .uia_review import ProbeError, build_review_graph

FIELDS = {"name": "DEMO name", "email": "DEMO email", "summary": "DEMO summary"}
EXPECTED = {("Edit", label) for label in FIELDS.values()} | {
    ("Button", "Validate DEMO"), ("Button", "Submit DEMO"), ("CheckBox", "Unlock DEMO submission")}


def save(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class ChromeUIAProbe:
    def __init__(self, handle, url):
        # This exact ASCII syntax is also safe as literal pywinauto keyboard text.
        matched = re.fullmatch(r"http://127\.0\.0\.1:([1-9][0-9]{0,4})/uia-demo\.html\?run=([a-f0-9]{32})", url)
        if matched is None or int(matched[1]) > 65535:
            raise ProbeError("LOCAL_DEMO_URL_REQUIRED")
        self.url, self.run = url, matched[2]
        self.title = "JobHuntAgent UIA DEMO " + self.run
        from pywinauto import Application
        import win32api
        import win32process
        self.app = Application(backend="uia").connect(handle=handle, timeout=10)
        self.window = self.app.window(handle=handle).wrapper_object()
        process = win32api.OpenProcess(0x410, False, self.window.process_id())
        try:
            executable = win32process.GetModuleFileNameEx(process, 0)
        finally:
            process.Close()
        if Path(executable).name.lower() != "chrome.exe" or self.window.class_name() != "Chrome_WidgetWin_1":
            raise ProbeError("ORDINARY_CHROME_WINDOW_REQUIRED")

    def open_local_tab(self):
        if self.window.is_minimized():
            self.window.restore()
        self.window.set_focus()
        self.window.type_keys("^t", pause=0.1)
        self.window.type_keys("^l", pause=0.1)
        self.window.type_keys(self.url + "{ENTER}", with_spaces=True, pause=0.01)
        self.wait_until(lambda: self.document(), "LOCAL_DOCUMENT_NOT_EXPOSED", seconds=12)

    @staticmethod
    def wait_until(action, failure, seconds=5):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = action()
            if value:
                return value
            time.sleep(0.2)
        raise ProbeError(failure)

    def document(self):
        # Read a URL only for the named local document; never dump unrelated tabs.
        found = [item for item in self.window.descendants(control_type="Document")
                 if item.window_text() == self.title and item.is_visible()]
        if len(found) > 1:
            raise ProbeError("AMBIGUOUS_LOCAL_DOCUMENT")
        if not found:
            return None
        try:
            actual_url = found[0].iface_value.CurrentValue
        except Exception:
            raise ProbeError("LOCAL_DOCUMENT_URL_UNREADABLE") from None
        if actual_url != self.url:
            raise ProbeError("LOCAL_DOCUMENT_URL_MISMATCH")
        return found[0]

    def inspect(self):
        document = self.document()
        if document is None:
            raise ProbeError("LOCAL_DOCUMENT_NOT_EXPOSED")
        nodes = [{"type": item.element_info.control_type, "name": item.window_text(),
                  "automation_id": item.element_info.automation_id}
                 for item in document.descendants()]
        # This fixed fixture has only these passive types. Unknown types stop it.
        interactive = Counter((node["type"], node["name"]) for node in nodes
                              if node["type"] not in {"Text", "Group", "StatusBar"})
        if set(interactive) - EXPECTED:
            raise ProbeError("UNKNOWN_INTERACTIVE_CONTROL")
        if any(interactive[control] != 1 for control in EXPECTED):
            raise ProbeError("MISSING_OR_AMBIGUOUS_CONTROL")
        return nodes

    def control(self, label, kind):
        document = self.document()
        if document is None:
            raise ProbeError("LOCAL_DOCUMENT_LOST")
        found = [item for item in document.descendants(control_type=kind) if item.window_text() == label]
        if len(found) != 1:
            raise ProbeError("MISSING_OR_AMBIGUOUS_CONTROL")
        return found[0]

    def feedback(self, expected):
        document = self.document()
        if document is None:
            raise ProbeError("LOCAL_DOCUMENT_LOST")
        return any(item.window_text() == expected for item in document.descendants())

    def fill_and_validate(self, index):
        self.inspect()
        values = {"name": "DEMO Alice", "email": "demo@example.invalid", "summary": f"DEMO Python SQLite 中文验证 round {index}"}
        for key, value in values.items():
            control = self.control(FIELDS[key], "Edit")
            control.set_edit_text(value)
            # Chrome publishes UIA Value asynchronously after SetValue returns.
            self.wait_until(lambda: control.get_value() == value, "INPUT_READBACK_MISMATCH")
        self.control("Validate DEMO", "Button").click()
        self.wait_until(lambda: self.feedback(f"DEMO validation OK {index}"), "VALIDATION_FEEDBACK_MISSING")
        return values

    def snapshot(self):
        self.inspect()
        return {"company": "DEMO local company", "job": "DEMO Python engineer", "entry": self.url,
                "resume_version": "DEMO-no-file-upload",
                **{key: self.control(label, "Edit").get_value() for key, label in FIELDS.items()}}

    def submit_demo(self, content_digest, journal):
        if digest(self.snapshot()) != content_digest:
            raise ProbeError("FORM_CHANGED_REVIEW_REQUIRED")
        if journal.exists():
            previous = json.loads(journal.read_text(encoding="utf-8"))
            if previous["status"] == "SUBMITTED_DEMO" and previous["content_digest"] == content_digest:
                return previous["receipt"]
            raise ProbeError("SUBMISSION_UNKNOWN_NO_RETRY")
        save(journal, {"status": "SUBMITTING_DEMO", "content_digest": content_digest, "at": now()})
        try:
            unlock = self.control("Unlock DEMO submission", "CheckBox")
            if unlock.get_toggle_state() != 0:
                raise ProbeError("UNEXPECTED_APPROVAL_CONTROL_STATE")
            unlock.toggle()
            self.wait_until(lambda: self.control("Submit DEMO", "Button").is_enabled(), "SUBMIT_BUTTON_NOT_ENABLED")
            self.control("Submit DEMO", "Button").click()
            receipt = "DEMO receipt " + self.run
            self.wait_until(lambda: self.feedback(receipt), "RECEIPT_MISSING")
            save(journal, {"status": "SUBMITTED_DEMO", "content_digest": content_digest, "receipt": receipt, "at": now()})
            return receipt
        except Exception:
            save(journal, {"status": "UNKNOWN", "content_digest": content_digest, "at": now()})
            raise ProbeError("SUBMISSION_UNKNOWN_NO_RETRY") from None


def run_probe(args):
    directory = args.output.resolve()
    directory.mkdir(parents=True, exist_ok=True)
    report = {"checked_at": now(), "backend": "uia", "scope": "LOCAL_DEMO_ONLY", "rounds": [],
              "window_connected": False, "form_identified": False, "human_review": "NOT_REACHED",
              "real_platform_submission": False}
    with run_lock(directory / "probe.lock"):
        try:
            probe = ChromeUIAProbe(args.handle, args.url)
            report["window_connected"] = True
            save(directory / "session.json", {"handle": args.handle, "url": args.url, "run": probe.run})
            probe.open_local_tab()
            nodes = probe.inspect()
            save(directory / "accessibility-tree.json", nodes)
            report["form_identified"] = True
            report["control_count"] = len(nodes)
            for index in range(1, 6):
                started = time.monotonic()
                probe.fill_and_validate(index)
                report["rounds"].append({"round": index, "exact_input_readback": True, "button_feedback_verified": True,
                                         "seconds": round(time.monotonic() - started, 3)})
            snapshot = probe.snapshot()
            report["snapshot"] = snapshot
            report["content_digest"] = digest(snapshot)
            with SqliteSaver.from_conn_string(str(directory / "review.sqlite3")) as checkpointer:
                graph = build_review_graph(checkpointer, probe.snapshot,
                    lambda approved_digest: probe.submit_demo(approved_digest, directory / "submission.json"))
                config = {"configurable": {"thread_id": probe.run}}
                graph.invoke({"snapshot": snapshot, "content_digest": digest(snapshot)}, config, durability="sync")
                state = graph.get_state(config)
                if not state.interrupts or state.next != ("review",):
                    raise ProbeError("REVIEW_INTERRUPT_MISSING")
            if probe.control("Submit DEMO", "Button").is_enabled() or (directory / "submission.json").exists():
                raise ProbeError("SUBMISSION_GATE_NOT_CLOSED")
            report["human_review"] = "WAITING_APPROVAL"
            report["status"] = "LOCAL_FORM_AND_REVIEW_PAUSE_VERIFIED"
        except Exception as error:
            report["status"] = "STOPPED"
            report["error_code"] = str(error) if isinstance(error, ProbeError) else type(error).__name__
        save(directory / "report.json", report)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["status"] != "STOPPED" else 1


def resume_review(args):
    directory = args.output.resolve()
    with run_lock(directory / "probe.lock"):
        session = json.loads((directory / "session.json").read_text(encoding="utf-8"))
        probe = ChromeUIAProbe(session["handle"], session["url"])
        with SqliteSaver.from_conn_string(str(directory / "review.sqlite3")) as checkpointer:
            graph = build_review_graph(checkpointer, probe.snapshot,
                lambda approved_digest: probe.submit_demo(approved_digest, directory / "submission.json"))
            config = {"configurable": {"thread_id": session["run"]}}
            state = graph.get_state(config)
            if not state.interrupts:
                raise ProbeError("NO_PENDING_REVIEW")
            result = graph.invoke(Command(resume=args.decision), config, durability="sync")
        save(directory / "review-result.json", result)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0


def main():
    parser = argparse.ArgumentParser(description="Local Chrome UIA feasibility experiment")
    commands = parser.add_subparsers(dest="action", required=True)
    probe = commands.add_parser("probe")
    probe.add_argument("--handle", type=int, required=True)
    probe.add_argument("--url", required=True)
    probe.add_argument("--output", type=Path, required=True)
    review = commands.add_parser("review")
    review.add_argument("--output", type=Path, required=True)
    review.add_argument("--decision", choices=["approve", "reject", "edit"], required=True)
    args = parser.parse_args()
    try:
        return run_probe(args) if args.action == "probe" else resume_review(args)
    except Exception as error:
        print(json.dumps({"status": "STOPPED", "error_code": str(error) if isinstance(error, ProbeError) else type(error).__name__}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
