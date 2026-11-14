"""Synthetic local HR execution guards, not real-platform acceptance."""
import json
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from langgraph.checkpoint.memory import InMemorySaver
from jobhunt.models import digest
from jobhunt.v2.uia_review import ProbeError, build_review_graph


class HRDemoTests(unittest.TestCase):
    def probe(self):
        from jobhunt.v2.hr_demo import HRDemoProbe
        probe = HRDemoProbe.__new__(HRDemoProbe)
        probe.chrome = MagicMock()
        probe.chrome.run = 'a' * 32
        probe.snapshot = lambda: {'scope':'LOCAL_HR_DEMO','recipient':'DEMO HR','message':'DEMO greeting'}
        probe.verify = MagicMock(return_value='DEMO receipt')
        probe.control = MagicMock()
        return probe

    def test_real_and_modified_urls_rejected_before_desktop_connection(self):
        from jobhunt.v2.hr_demo import HRDemoProbe
        for url in ('https://www.zhipin.com/chat', 'http://localhost:56875/uia-demo.html?run='+'a'*32,
                    'http://127.0.0.1:56875/uia-demo.html?run='+'a'*32+'&redirect=https://example.org'):
            with self.subTest(url=url), patch('jobhunt.v2.hr_demo.ChromeUIAProbe') as chrome:
                with self.assertRaisesRegex(ProbeError,'LOCAL_DEMO_URL_REQUIRED'):
                    HRDemoProbe(0,url)
                chrome.assert_not_called()

    def test_pending_or_unknown_attempt_cannot_click_again(self):
        probe=self.probe()
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'attempt.json'
            for status in ('SENDING_DEMO','UNKNOWN'):
                path.write_text(json.dumps({'status':status,'content_digest':digest(probe.snapshot())}),encoding='utf-8')
                with self.subTest(status=status), self.assertRaisesRegex(ProbeError,'MESSAGE_UNKNOWN_NO_RETRY'):
                    probe.send_demo(digest(probe.snapshot()),path)
        probe.control.assert_not_called()
        probe.verify.assert_not_called()

    def test_completed_attempt_is_reverified_without_click(self):
        probe=self.probe()
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'attempt.json'
            path.write_text(json.dumps({'status':'SENT_DEMO','content_digest':digest(probe.snapshot()),'receipt':'DEMO receipt'}),encoding='utf-8')
            self.assertEqual(probe.send_demo(digest(probe.snapshot()),path),'DEMO receipt')
        probe.verify.assert_called_once_with('DEMO receipt')
        probe.control.assert_not_called()

    def test_changed_content_stops_before_attempt_or_click(self):
        probe=self.probe()
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'attempt.json'
            with self.assertRaisesRegex(ProbeError,'MESSAGE_CHANGED_REVIEW_REQUIRED'):
                probe.send_demo(digest({'message':'old'}),path)
            self.assertFalse(path.exists())
        probe.control.assert_not_called()

    def test_unknown_control_stops_before_actions(self):
        probe=self.probe()
        node=SimpleNamespace(element_info=SimpleNamespace(control_type='Hyperlink'),window_text=lambda:'Unexpected')
        probe.chrome.document.return_value=SimpleNamespace(descendants=lambda:[node])
        with self.assertRaisesRegex(ProbeError,'UNKNOWN_HR_CONTROL'):
            probe.inspect()
        probe.control.assert_not_called()

    def test_unknown_clickable_group_stops_before_actions(self):
        from jobhunt.v2.hr_demo import EXPECTED,TARGET
        probe=self.probe()
        node=SimpleNamespace(element_info=SimpleNamespace(control_type='Group'),
                             iface_invoke=object(),window_text=lambda:'Unexpected action')
        known=[SimpleNamespace(element_info=SimpleNamespace(control_type=kind),
                               window_text=lambda name=name:name) for kind,name in EXPECTED]
        known.append(SimpleNamespace(element_info=SimpleNamespace(control_type='Group'),window_text=lambda:TARGET))
        probe.chrome.document.return_value=SimpleNamespace(descendants=lambda:known+[node])
        with self.assertRaisesRegex(ProbeError,'UNKNOWN_HR_CONTROL'):
            probe.inspect()
        probe.control.assert_not_called()

    def test_new_send_requires_approval_record_before_attempt(self):
        probe=self.probe()
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'attempt.json'
            with self.assertRaisesRegex(ProbeError,'HR_DEMO_APPROVAL_REQUIRED'):
                probe.send_demo(digest(probe.snapshot()),path)
            self.assertFalse(path.exists())
        probe.control.assert_not_called()

    def approved_attempt(self, probe, directory):
        content=digest(probe.snapshot())
        (Path(directory)/'synthetic-decision.json').write_text(json.dumps({
            'scope':'LOCAL_HR_DEMO','source':'SYNTHETIC_TEST','decision':'approve',
            'content_digest':content}),encoding='utf-8')
        return content,Path(directory)/'attempt.json'

    def test_click_failure_is_unknown_and_blocks_second_click(self):
        probe=self.probe()
        button=MagicMock()
        button.get_toggle_state.return_value=0
        button.iface_value.CurrentIsReadOnly=True
        button.click.side_effect=RuntimeError('synthetic click failure')
        probe.control=MagicMock(return_value=button)
        with tempfile.TemporaryDirectory() as directory:
            content,path=self.approved_attempt(probe,directory)
            for _ in range(2):
                with self.assertRaisesRegex(ProbeError,'MESSAGE_UNKNOWN_NO_RETRY'):
                    probe.send_demo(content,path)
                self.assertEqual(json.loads(path.read_text())['status'],'UNKNOWN')
        button.click.assert_called_once()

    def test_editable_message_after_unlock_stops_before_click(self):
        probe=self.probe()
        button=MagicMock()
        button.get_toggle_state.return_value=0
        button.iface_value.CurrentIsReadOnly=False
        probe.control=MagicMock(return_value=button)
        with tempfile.TemporaryDirectory() as directory:
            content,path=self.approved_attempt(probe,directory)
            with self.assertRaisesRegex(ProbeError,'MESSAGE_UNKNOWN_NO_RETRY'):
                probe.send_demo(content,path)
        button.click.assert_not_called()

    def test_reload_does_not_accept_receipt_on_old_dom(self):
        probe=self.probe()
        probe.load_marker=MagicMock(side_effect=['old','old','new','new'])
        probe.feedback=MagicMock(return_value=True)
        checks=[]
        def wait(action,*args,**kwargs):
            checks.append(action())
            checks.append(action())
            self.assertEqual(checks,[False,True])
        probe.chrome.wait_until=wait
        markers=probe.reload_and_verify('DEMO receipt')
        self.assertEqual(markers,{'old':'old','new':'new'})
        probe.verify.assert_called_once_with('DEMO receipt')

    def test_changed_body_during_click_cannot_be_marked_sent(self):
        probe=self.probe()
        button=MagicMock()
        button.get_toggle_state.return_value=0
        button.iface_value.CurrentIsReadOnly=True
        def change():
            probe.snapshot=lambda:{'scope':'LOCAL_HR_DEMO','recipient':'DEMO HR','message':'DEMO changed'}
        button.click.side_effect=change
        probe.control=MagicMock(return_value=button)
        with tempfile.TemporaryDirectory() as directory:
            content,path=self.approved_attempt(probe,directory)
            with self.assertRaisesRegex(ProbeError,'MESSAGE_UNKNOWN_NO_RETRY'):
                probe.send_demo(content,path)
            self.assertEqual(json.loads(path.read_text())['status'],'UNKNOWN')

    def test_hr_review_interrupt_has_explicit_local_message_scope(self):
        snapshot={'scope':'LOCAL_HR_DEMO','message':'DEMO greeting'}
        graph=build_review_graph(InMemorySaver(),lambda:snapshot,lambda _:self.fail('unapproved send'),
                                 review_kind='LOCAL_DEMO_HR_MESSAGE_REVIEW')
        result=graph.invoke({'snapshot':snapshot,'content_digest':digest(snapshot)},
                            {'configurable':{'thread_id':'local-hr-test'}})
        self.assertEqual(result['__interrupt__'][0].value['kind'],'LOCAL_DEMO_HR_MESSAGE_REVIEW')

    def test_cli_requires_demo_before_profile_or_executor(self):
        from jobhunt.cli import parser
        from jobhunt.config import Context, UserError
        from jobhunt.v2.cli import run
        args=parser().parse_args(['v2','hr-demo'])
        with patch.object(Context,'load') as load:
            with self.assertRaisesRegex(UserError,'HR_DEMO_MODE_REQUIRED'):
                run(args,Context(Path('unused')))
        load.assert_not_called()

    def test_transient_missing_document_only_retries_passive_lookup(self):
        probe=self.probe()
        doc=SimpleNamespace()
        probe.chrome.document.side_effect=[None,doc]
        probe.chrome.wait_until.side_effect=lambda action,*args,**kwargs:action()
        self.assertIs(probe.document(),doc)
        probe.control.assert_not_called()
        probe.chrome.window.type_keys.assert_not_called()

    def test_stopped_cli_run_has_nonzero_exit(self):
        from contextlib import redirect_stdout,redirect_stderr
        import io
        from jobhunt.cli import main
        with patch('jobhunt.v2.hr_demo.run_selftest',return_value={'status':'STOPPED','error_code':'HR_DEMO_DOCUMENT_MISSING'}), patch('jobhunt.v2.application_workflow.chrome_handle',return_value=123), redirect_stdout(io.StringIO()),redirect_stderr(io.StringIO()):
            self.assertNotEqual(main(['v2','hr-demo','--demo']),0)
