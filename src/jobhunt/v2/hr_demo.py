"""Closed-loop local HR chat experiment. Never connects to a real HR service."""
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import threading
import traceback
from uuid import uuid4

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from ..config import UserError
from ..models import digest, now
from ..storage import run_lock
from .uia_probe import ChromeUIAProbe
from .uia_review import ProbeError, build_review_graph

TARGET = 'HR DEMO target synthetic-company / synthetic-agent-role / synthetic-HR'
EXPECTED = {('Edit','DEMO recipient'), ('Edit','DEMO message'),
            ('Button','Validate DEMO message'), ('Button','Send DEMO message'),
            ('CheckBox','Unlock DEMO message')}
MESSAGE = 'DEMO 您好，我是虚构候选人，有 Python Agent 模拟项目经验。期待交流。'


def save(path, value):
    temporary = path.with_suffix('.tmp')
    with temporary.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    temporary.replace(path)


class HRDemoProbe:
    def __init__(self, handle, url):
        matched = re.fullmatch(r'http://127\.0\.0\.1:([1-9][0-9]{0,4})/uia-demo\.html\?run=([a-f0-9]{32})', url)
        if matched is None or int(matched[1]) > 65535:
            raise ProbeError('LOCAL_DEMO_URL_REQUIRED')
        self.chrome = ChromeUIAProbe(handle, url)

    def open(self):
        import win32gui
        window = self.chrome.window
        if win32gui.IsIconic(window.handle):
            win32gui.ShowWindow(window.handle, 9)
        window.set_focus()
        window.type_keys('^t', pause=0.1)
        window.type_keys('^l', pause=0.1)
        window.type_keys(self.chrome.url+'{ENTER}', with_spaces=True, pause=0.005)
        self.chrome.wait_until(self.chrome.document, 'HR_DEMO_DOCUMENT_MISSING', seconds=12)
        self.inspect()

    def document(self):
        doc = self.chrome.document()
        if doc is None:
            # Chrome can temporarily remove the old UIA document while loading.
            # Wait only for this exact local URL/title; never repeat an action.
            doc = self.chrome.wait_until(self.chrome.document,'HR_DEMO_DOCUMENT_MISSING')
        return doc

    def inspect(self):
        doc = self.document()
        if doc is None:
            raise ProbeError('HR_DEMO_DOCUMENT_MISSING')
        nodes = doc.descendants()
        for node in nodes:
            if node.element_info.control_type == 'Group':
                try:
                    invokable = getattr(node,'iface_invoke',None) is not None
                except Exception as error:
                    if type(error).__name__ not in {'NoPatternInterfaceError','AttributeError'}:
                        raise ProbeError('HR_GROUP_PATTERN_UNREADABLE') from None
                    invokable = False
                if invokable:
                    raise ProbeError('UNKNOWN_HR_CONTROL')
        interactive = Counter((n.element_info.control_type,n.window_text()) for n in nodes
                              if n.element_info.control_type not in {'Text','Group','StatusBar'})
        if set(interactive) - EXPECTED:
            raise ProbeError('UNKNOWN_HR_CONTROL')
        if any(interactive[item] != 1 for item in EXPECTED):
            raise ProbeError('MISSING_OR_AMBIGUOUS_HR_CONTROL')
        groups = [n.window_text() for n in nodes if n.element_info.control_type == 'Group']
        if groups.count(TARGET) != 1:
            raise ProbeError('HR_DEMO_TARGET_MISMATCH')
        return nodes

    def control(self, label, kind):
        doc = self.document()
        if doc is None:
            raise ProbeError('HR_DEMO_DOCUMENT_MISSING')
        found = [n for n in doc.descendants(control_type=kind) if n.window_text() == label]
        if len(found) != 1:
            raise ProbeError('MISSING_OR_AMBIGUOUS_HR_CONTROL')
        return found[0]

    def feedback(self, text):
        return any(n.window_text() == text for n in self.inspect())

    def fill(self):
        self.inspect()
        if (self.control('DEMO recipient','Edit').get_value() != 'DEMO local HR'
                or self.control('Unlock DEMO message','CheckBox').get_toggle_state() != 0
                or self.control('Send DEMO message','Button').is_enabled()):
            raise ProbeError('UNEXPECTED_HR_DEMO_STATE')
        self.control('DEMO message','Edit').set_edit_text(MESSAGE)
        self.chrome.wait_until(lambda:self.control('DEMO message','Edit').get_value()==MESSAGE,'HR_MESSAGE_READBACK_MISMATCH')
        self.control('Validate DEMO message','Button').click()
        self.chrome.wait_until(lambda:self.feedback('DEMO message validation OK'),'HR_VALIDATION_MISSING')
        if not self.feedback('DEMO sent count 0'):
            raise ProbeError('HR_UNEXPECTED_SEND')

    def snapshot(self):
        self.inspect()
        return {'scope':'LOCAL_HR_DEMO','approval_mode':'SYNTHETIC_TEST',
                'company':'DEMO company','job':'DEMO Agent role','entry':self.chrome.url,
                'recipient':self.control('DEMO recipient','Edit').get_value(),
                'message':self.control('DEMO message','Edit').get_value(),
                'resume_version':'DEMO-no-attachment'}

    def verify(self, receipt):
        self.inspect()
        doc = self.document()
        groups = [n for n in doc.descendants(control_type='Group') if n.window_text() == 'DEMO conversation']
        if len(groups) != 1:
            raise ProbeError('HR_CONVERSATION_MISSING')
        text = {n.window_text() for n in groups[0].descendants()}
        if (receipt not in text or 'DEMO sent count 1' not in text
                or self.control('DEMO message','Edit').get_value() not in text
                or self.control('Send DEMO message','Button').is_enabled()):
            raise ProbeError('HR_RECEIPT_UNVERIFIED')
        return receipt

    def load_marker(self):
        doc = self.chrome.document()
        if doc is None:
            return None
        found = [n.window_text() for n in doc.descendants(control_type='Text')
                 if re.fullmatch(r'DEMO page load [0-9]+-[0-9]+',n.window_text())]
        if len(found) > 1:
            raise ProbeError('HR_LOAD_MARKER_AMBIGUOUS')
        return found[0] if found else None

    def reload_and_verify(self, receipt):
        old = self.load_marker()
        if old is None:
            raise ProbeError('HR_LOAD_MARKER_MISSING')
        self.chrome.window.set_focus()
        if self.chrome.document() is None:
            raise ProbeError('HR_DEMO_DOCUMENT_MISSING')
        self.chrome.window.type_keys('{F5}')
        def loaded():
            marker = self.load_marker()
            return bool(marker and marker != old and self.feedback(receipt))
        self.chrome.wait_until(loaded,'HR_RECEIPT_RELOAD_FAILED',seconds=10)
        self.verify(receipt)
        return {'old':old,'new':self.load_marker()}

    def send_demo(self, content_digest, journal):
        if digest(self.snapshot()) != content_digest:
            raise ProbeError('MESSAGE_CHANGED_REVIEW_REQUIRED')
        if journal.exists():
            previous = json.loads(journal.read_text(encoding='utf-8'))
            if previous['status']=='SENT_DEMO' and previous['content_digest']==content_digest:
                return self.verify(previous['receipt'])
            raise ProbeError('MESSAGE_UNKNOWN_NO_RETRY')
        approval_path = journal.parent/'synthetic-decision.json'
        if not approval_path.is_file():
            raise ProbeError('HR_DEMO_APPROVAL_REQUIRED')
        approval = json.loads(approval_path.read_text(encoding='utf-8'))
        if approval != {'scope':'LOCAL_HR_DEMO','source':'SYNTHETIC_TEST',
                         'decision':'approve','content_digest':content_digest}:
            raise ProbeError('HR_DEMO_APPROVAL_REQUIRED')
        save(journal,{'status':'SENDING_DEMO','content_digest':content_digest,'at':now()})
        try:
            unlock = self.control('Unlock DEMO message','CheckBox')
            if unlock.get_toggle_state() != 0:
                raise ProbeError('UNEXPECTED_HR_DEMO_STATE')
            unlock.toggle()
            self.chrome.wait_until(lambda:self.control('Send DEMO message','Button').is_enabled(),'HR_SEND_NOT_ENABLED')
            self.chrome.wait_until(lambda:self.control('DEMO message','Edit').iface_value.CurrentIsReadOnly,
                                   'HR_MESSAGE_NOT_LOCKED')
            if not self.control('DEMO message','Edit').iface_value.CurrentIsReadOnly:
                raise ProbeError('HR_MESSAGE_NOT_LOCKED')
            if digest(self.snapshot()) != content_digest:
                raise ProbeError('MESSAGE_CHANGED_REVIEW_REQUIRED')
            self.control('Send DEMO message','Button').click()
            receipt = 'DEMO message receipt '+self.chrome.run+' 1'
            self.chrome.wait_until(lambda:self.feedback(receipt),'HR_RECEIPT_MISSING')
            self.verify(receipt)
            if digest(self.snapshot()) != content_digest:
                raise ProbeError('MESSAGE_CHANGED_REVIEW_REQUIRED')
            save(journal,{'status':'SENT_DEMO','content_digest':content_digest,'receipt':receipt,'at':now()})
            return receipt
        except Exception:
            save(journal,{'status':'UNKNOWN','content_digest':content_digest,'at':now()})
            raise ProbeError('MESSAGE_UNKNOWN_NO_RETRY') from None


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        expected = '127.0.0.1:'+str(self.server.server_address[1])
        if (self.headers.get_all('Host',[]) != [expected]
                or not re.fullmatch(r'/uia-demo\.html\?run=[a-f0-9]{32}',self.path)):
            self.send_response(404); self.end_headers(); return
        body = self.server.fixture.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type','text/html; charset=utf-8')
        self.send_header('Content-Length',str(len(body)))
        self.send_header('Cache-Control','no-store')
        self.send_header('Referrer-Policy','no-referrer')
        self.send_header('Content-Security-Policy',"default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'none'; form-action 'none'; frame-ancestors 'none'; base-uri 'none'")
        self.end_headers(); self.wfile.write(body)


def run_selftest(handle, output, port):
    output = output.resolve()
    output.mkdir(parents=True,exist_ok=True)
    if any(output.iterdir()):
        raise UserError('HR_DEMO_FRESH_OUTPUT_REQUIRED')
    fixture = Path(__file__).resolve().parents[3]/'examples/hr-demo/uia-demo.html'
    if not fixture.is_file() or not 1024 <= port <= 65535:
        raise UserError('HR_DEMO_CONFIGURATION_INVALID')
    report = {'scope':'LOCAL_HR_DEMO','approval_mode':'SYNTHETIC_TEST','real_messages_sent':0,'cases':[],'status':'RUNNING'}
    with run_lock(output/'selftest.lock'):
        server = ThreadingHTTPServer(('127.0.0.1',port),_Handler)
        server.fixture = fixture
        thread = threading.Thread(target=server.serve_forever,daemon=True)
        thread.start()
        try:
            for decision in ('reject','changed','approve'):
                report.update(active_case=decision,phase='OPEN_PAGE')
                run = uuid4().hex
                case = output/decision
                case.mkdir()
                probe = HRDemoProbe(handle,f'http://127.0.0.1:{port}/uia-demo.html?run={run}')
                probe.open()
                report['phase'] = 'FILL'
                probe.fill()
                report['phase'] = 'PREPARE_REVIEW'
                snapshot = probe.snapshot(); content = digest(snapshot)
                journal = case/'attempt.json'
                save(case/'review.json',{'snapshot':snapshot,'content_digest':content})
                config = {'configurable':{'thread_id':run}}
                def graph(checkpointer):
                    return build_review_graph(checkpointer,probe.snapshot,lambda value:probe.send_demo(value,journal),
                                              review_kind='LOCAL_DEMO_HR_MESSAGE_REVIEW')
                with SqliteSaver.from_conn_string(str(case/'checkpoints.sqlite3')) as saver:
                    workflow = graph(saver)
                    workflow.invoke({'snapshot':snapshot,'content_digest':content},config,durability='sync')
                    if not workflow.get_state(config).interrupts or journal.exists():
                        raise ProbeError('HR_REVIEW_PAUSE_NOT_VERIFIED')
                case_report = {'case':decision,'run':run,'pause_verified':True,'checkpoint_reopened':True,'local_messages_sent':0}
                if decision != 'reject':
                    save(case/'synthetic-decision.json',{'scope':'LOCAL_HR_DEMO','source':'SYNTHETIC_TEST','decision':'approve','content_digest':content})
                if decision == 'changed':
                    probe.control('DEMO message','Edit').set_edit_text('DEMO changed after review')
                    probe.chrome.wait_until(lambda:probe.control('DEMO message','Edit').get_value()=='DEMO changed after review','HR_MESSAGE_READBACK_MISMATCH')
                report['phase'] = 'RESUME_SYNTHETIC_REVIEW'
                with SqliteSaver.from_conn_string(str(case/'checkpoints.sqlite3')) as saver:
                    workflow = graph(saver)
                    if not workflow.get_state(config).interrupts:
                        raise ProbeError('HR_REVIEW_RESUME_NOT_VERIFIED')
                    try:
                        result = workflow.invoke(Command(resume='reject' if decision=='reject' else 'approve'),config,durability='sync')
                        if decision == 'changed':
                            raise ProbeError('HR_CHANGED_MESSAGE_WAS_ACCEPTED')
                    except ProbeError as error:
                        if decision != 'changed' or str(error) != 'FORM_CHANGED_REVIEW_REQUIRED':
                            raise
                        case_report['changed_content_blocked'] = True
                if decision != 'approve':
                    if journal.exists() or not probe.feedback('DEMO sent count 0') or probe.control('Send DEMO message','Button').is_enabled():
                        raise ProbeError('HR_UNAPPROVED_SEND_DETECTED')
                    case_report['status'] = 'ZERO_SEND_VERIFIED'
                else:
                    report['phase'] = 'VERIFY_AND_RECOVER'
                    receipt = result['receipt']
                    probe.verify(receipt)
                    with SqliteSaver.from_conn_string(str(case/'checkpoints.sqlite3')) as saver:
                        resumed = graph(saver).invoke(None,config,durability='sync')
                    if resumed['receipt'] != receipt or probe.send_demo(content,journal) != receipt:
                        raise ProbeError('HR_DUPLICATE_SUPPRESSION_FAILED')
                    report['phase'] = 'VERIFY_RELOAD'
                    markers = probe.reload_and_verify(receipt)
                    case_report.update(status='SINGLE_LOCAL_SEND_VERIFIED',local_messages_sent=1,receipt=receipt,
                                       repeat_send_blocked=True,conversation_survived_reload=True,reload_markers=markers)
                save(case/'result.json',case_report)
                report['cases'].append(case_report)
            report.update(status='LOCAL_HR_DEMO_VERIFIED',phase='COMPLETE')
        except Exception as error:
            report.update(status='STOPPED',error_code=str(error) if isinstance(error,ProbeError) else type(error).__name__)
            report['error_locations'] = [{'file':Path(frame.filename).name,'function':frame.name,'line':frame.lineno}
                                         for frame in traceback.extract_tb(error.__traceback__)[-5:]]
        finally:
            server.shutdown();server.server_close();thread.join(timeout=5)
            report['local_server_stopped'] = True
            save(output/'report.json',report)
    return {**report,'report_path':str(output/'report.json')}
