"""Probe failure semantics with synthetic controls; not real-browser acceptance."""
from types import SimpleNamespace
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from jobhunt.v2.uia_probe import ChromeUIAProbe, EXPECTED, FIELDS
from jobhunt.v2.uia_review import ProbeError
from jobhunt.models import digest


class ProbeTests(unittest.TestCase):
    def test_unknown_submission_is_not_retried(self):
        probe = ChromeUIAProbe.__new__(ChromeUIAProbe)
        probe.snapshot = lambda: {'entry': 'local demo'}
        probe.control = MagicMock()
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / 'submission.json'
            for status in ('SUBMITTING_DEMO', 'UNKNOWN'):
                journal.write_text(json.dumps({'status': status}), encoding='utf-8')
                with self.subTest(status=status), self.assertRaisesRegex(ProbeError, 'SUBMISSION_UNKNOWN_NO_RETRY'):
                    probe.submit_demo(digest(probe.snapshot()), journal)
        probe.control.assert_not_called()

    def test_click_without_receipt_is_unknown_and_cannot_be_retried(self):
        probe = ChromeUIAProbe.__new__(ChromeUIAProbe)
        probe.run = 'a' * 32
        probe.snapshot = lambda: {'entry': 'local demo'}
        unlock, button = MagicMock(), MagicMock()
        unlock.get_toggle_state.return_value = 0
        probe.control = lambda label, kind: unlock if kind == 'CheckBox' else button
        probe.feedback = lambda expected: False
        def no_wait(action, failure):
            if not action():
                raise ProbeError(failure)
        probe.wait_until = no_wait
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / 'submission.json'
            for _ in range(2):
                with self.assertRaisesRegex(ProbeError, 'SUBMISSION_UNKNOWN_NO_RETRY'):
                    probe.submit_demo(digest(probe.snapshot()), journal)
            self.assertEqual(json.loads(journal.read_text())['status'], 'UNKNOWN')
        button.click.assert_called_once()

    def test_url_rejects_extra_parameters_and_keyboard_syntax_before_connect(self):
        base = 'http://127.0.0.1:56863/uia-demo.html?run=' + 'a' * 32
        invalid = [base + '&unexpected=', base + '&{ESC}^lhttps://example.invalid{ENTER}',
                   base.replace('run=a', 'run=%61', 1), base + '#fragment',
                   base.replace(':56863', ':99999'), base + '\n',
                   base.replace('127.0.0.1', 'localhost')]
        module = MagicMock()
        module.Application.side_effect = AssertionError('UNVALIDATED_URL_REACHED_DESKTOP')
        with patch.dict('sys.modules', {'pywinauto': module}):
            for url in invalid:
                with self.subTest(url=url), self.assertRaisesRegex(ProbeError, 'LOCAL_DEMO_URL_REQUIRED'):
                    ChromeUIAProbe(0, url)

    def test_matching_title_does_not_accept_another_url_or_unreadable_url(self):
        probe = ChromeUIAProbe.__new__(ChromeUIAProbe)
        probe.title = 'JobHuntAgent UIA DEMO ' + 'a' * 32
        probe.url = 'http://127.0.0.1:56863/uia-demo.html?run=' + 'a' * 32
        document = MagicMock()
        document.window_text.return_value = probe.title
        document.is_visible.return_value = True
        probe.window = MagicMock()
        probe.window.descendants.return_value = [document]
        for value in ('https://example.invalid/copied-form', ''):
            document.iface_value.CurrentValue = value
            with self.subTest(value=value), self.assertRaisesRegex(ProbeError, 'LOCAL_DOCUMENT_URL_MISMATCH'):
                probe.document()
        document.iface_value.CurrentValue = probe.url
        self.assertIs(probe.document(), document)

    def test_unknown_control_types_are_rejected(self):
        probe = ChromeUIAProbe.__new__(ChromeUIAProbe)
        def node(kind, name):
            return SimpleNamespace(element_info=SimpleNamespace(control_type=kind, automation_id=''),
                                   window_text=lambda: name)
        expected = [node(kind, name) for kind, name in EXPECTED]
        for kind in ('Slider', 'Spinner', 'List', 'Custom', 'ComboBox'):
            probe.document = lambda: SimpleNamespace(descendants=lambda: expected + [node(kind, 'Unknown')])
            with self.subTest(kind=kind), self.assertRaisesRegex(ProbeError, 'UNKNOWN_INTERACTIVE_CONTROL'):
                probe.inspect()

    def test_asynchronous_accessibility_value_is_waited_for(self):
        probe = ChromeUIAProbe.__new__(ChromeUIAProbe)
        probe.inspect = MagicMock()
        probe.feedback = MagicMock(return_value=True)
        controls = {}
        for label in FIELDS.values():
            control = MagicMock()
            control.set_edit_text.side_effect = lambda value, c=control: setattr(c.get_value, "side_effect", ["", value])
            controls[label] = control
        button = MagicMock()
        probe.control = lambda label, kind: button if kind == "Button" else controls[label]
        values = probe.fill_and_validate(1)
        self.assertEqual(values["name"], "DEMO Alice")
        self.assertTrue(all(control.get_value.call_count == 2 for control in controls.values()))
        button.click.assert_called_once()

    def test_unknown_interactive_control_stops_before_actions(self):
        probe = ChromeUIAProbe.__new__(ChromeUIAProbe)
        control = SimpleNamespace(element_info=SimpleNamespace(control_type="Button", automation_id="unexpected"),
                                  window_text=lambda: "Unexpected action")
        probe.document = lambda: SimpleNamespace(descendants=lambda: [control])
        with self.assertRaisesRegex(ProbeError, "UNKNOWN_INTERACTIVE_CONTROL"):
            probe.inspect()

    def test_local_document_missing_stops(self):
        probe = ChromeUIAProbe.__new__(ChromeUIAProbe)
        probe.document = lambda: None
        with self.assertRaisesRegex(ProbeError, "LOCAL_DOCUMENT_NOT_EXPOSED"):
            probe.inspect()


if __name__ == "__main__":
    unittest.main()
