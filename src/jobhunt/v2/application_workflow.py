"""P3 local-only application graph with durable, content-bound human approval."""
from contextlib import ExitStack, closing
import json
import re
from pathlib import Path
from typing import TypedDict
from urllib.parse import urlsplit
from uuid import uuid4

from .workflow import CHECKPOINT_DATABASE_ID  # Disables tracing before graph imports.
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.errors import GraphInterrupt
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from ..config import Context, UserError
from ..models import digest
from ..storage import run_lock
from .application_store import ApplicationStore
from .application_writer import ApplicationWriter, validate_fields, write_review
from .models import Job


class ApplicationState(TypedDict, total=False):
    application_id: str
    decision: str
    uncertain: bool


def chrome_handle():
    import win32gui
    import win32api
    import win32process
    handles = []
    def collect(handle, _):
        if win32gui.IsWindowVisible(handle) and win32gui.GetClassName(handle) == 'Chrome_WidgetWin_1':
            try:
                _, pid = win32process.GetWindowThreadProcessId(handle)
                process = win32api.OpenProcess(0x410, False, pid)
                try:
                    executable = win32process.GetModuleFileNameEx(process, 0)
                finally:
                    process.Close()
                if Path(executable).name.lower() == 'chrome.exe':
                    handles.append(handle)
            except OSError:
                return
    win32gui.EnumWindows(collect, None)
    if len(handles) != 1:
        raise UserError('P3_CHROME_SELECTION_REQUIRED: 请使用 --handle 指定已打开的普通 Chrome 窗口')
    return handles[0]


class ApplicationWorkflow:
    def __init__(self, ctx, *, handle=None, executor_factory=None):
        if not ctx.demo:
            raise UserError('P3_DEMO_REQUIRED')
        self.ctx, self.handle, self.executor_factory = ctx, handle, executor_factory
        self.directory = ctx.home / 'data' / 'v2' / 'demo'

    def __enter__(self):
        self._stack = ExitStack()
        try:
            self._stack.enter_context(run_lock(self.directory / 'workflow.lock'))
            self.store = self._stack.enter_context(closing(ApplicationStore(self.directory / 'business.sqlite3', demo=True)))
            self.checkpointer = self._stack.enter_context(SqliteSaver.from_conn_string(str(self.directory / 'checkpoints.sqlite3')))
            conn = self.checkpointer.conn
            identity = conn.execute('PRAGMA application_id').fetchone()[0]
            tables = conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            if identity not in (0, CHECKPOINT_DATABASE_ID) or (identity == 0 and tables):
                raise UserError('V2_FOREIGN_CHECKPOINT_DATABASE')
            conn.execute(f'PRAGMA application_id={CHECKPOINT_DATABASE_ID}')
            self.graph = self._build_graph()
            return self
        except BaseException:
            self._stack.close()
            raise

    def __exit__(self, *args):
        return self._stack.__exit__(*args)

    @staticmethod
    def config(identifier):
        return {'configurable': {'thread_id': 'p3:' + identifier}}

    def _writer(self):
        # DEMO always uses the fixture and local mock matching, regardless of LIVE settings.
        return ApplicationWriter(Context(self.ctx.home, demo=True, backend_override='mock').load())

    def _fresh(self, identifier):
        package = self.store.package(identifier)
        current = Job.model_validate(self.store.get_job(package['job_id'])['job'])
        manager = self._writer().manager
        if (current.jd_hash != package['job_hash'] or manager.profile_version != package['profile_version']
                or manager.resume_version != package['resume_version']
                or digest(package) != self.store.get_application(identifier)['content_digest']):
            raise UserError('P3_CONTENT_CHANGED_REVIEW_REQUIRED')
        return package

    def _executor(self, package, identifier):
        if self.executor_factory:
            executor = self.executor_factory(package, identifier)
        else:
            from .local_executor import LocalUIAExecutor
            executor = LocalUIAExecutor(self.handle if self.handle is not None else chrome_handle(), package, identifier)
        executor.open_application(Job.model_validate(package['job']))
        return executor

    def _checked_executor(self, identifier):
        package = self._fresh(identifier)
        executor = self._executor(package, identifier)
        executor.inspect_form()
        if executor.snapshot_fields() != package['fields']:
            raise UserError('P3_FORM_CHANGED_REVIEW_REQUIRED')
        if not executor.validate_form().valid:
            raise UserError('P3_FORM_INVALID')
        return package, executor

    def _guard(self, action):
        def call(state):
            try:
                return action(state)
            except GraphInterrupt:
                raise
            except Exception as error:
                self.store.fail(state['application_id'], 'APPLICATION_ERROR')
                code = str(error).split(':', 1)[0] if isinstance(error, UserError) else ''
                if not re.fullmatch(r'(P3|V2)_[A-Z_]{2,80}', code):
                    code = 'P3_EXECUTION_STOPPED'
                # Sanitize before LangGraph persists the exception; never include UIA values.
                raise UserError(code) from None
        return call

    def _build_graph(self):
        def prepare(state):
            self._fresh(state['application_id'])
            return {'decision': '', 'uncertain': False}

        def fill(state):
            identifier = state['application_id']
            package = self._fresh(identifier)
            executor = self._executor(package, identifier)
            executor.inspect_form()
            executor.fill_form(package['fields'])
            if not executor.validate_form().valid or executor.snapshot_fields() != package['fields']:
                raise UserError('P3_FORM_INVALID')
            self.store.mark_filled(identifier)
            write_review(self.ctx.home, identifier, package)
            return {}

        def review(state):
            identifier = state['application_id']
            content = self.store.get_application(identifier)['content_digest']
            choice = interrupt({'kind': 'P3_LOCAL_APPLICATION_REVIEW', 'application_id': identifier,
                                'content_digest': content, 'choices': ['approve', 'reject', 'edit']})
            if (not isinstance(choice, dict) or choice.get('digest') != content
                    or choice.get('decision') not in {'approve', 'reject'}):
                raise UserError('P3_REVIEW_INVALID')
            if choice['decision'] == 'approve':
                self._checked_executor(identifier)
            # A crash after the business transaction may replay this node. Never issue a new approval.
            status = self.store.status(identifier)
            expected = 'APPROVED' if choice['decision'] == 'approve' else 'REJECTED'
            if status != expected:
                self.store.decide(identifier, choice['decision'], content)
            return {'decision': choice['decision']}

        def submit(state):
            identifier = state['application_id']
            status = self.store.status(identifier)
            if status == 'SUBMITTED':
                return {}
            if status in {'SUBMITTING', 'UNKNOWN'}:
                self.store.fail(identifier, 'SUBMISSION_UNCERTAIN', uncertain=True)
                return {'uncertain': True}
            package, executor = self._checked_executor(identifier)
            # UIA validation may take seconds; re-read source versions after it completes.
            self._fresh(identifier)
            token = self.store.begin_submission(identifier, digest(package))
            try:
                executor.submit(token)
            except Exception:
                self.store.fail(identifier, 'SUBMISSION_UNCERTAIN', uncertain=True)
                return {'uncertain': True}
            return {}

        def verify(state):
            identifier = state['application_id']
            if state.get('uncertain'):
                return {}
            if self.store.status(identifier) == 'SUBMITTED':
                return {}
            try:
                package = self.store.package(identifier)
                result = self._executor(package, identifier).verify_result()
                receipt = self.store.receipt(identifier)
                if result.status != 'SUBMITTED' or not receipt or result.receipt_reference != receipt['receipt_id']:
                    raise UserError('P3_RECEIPT_MISSING')
                self.store.finish_submission(identifier, receipt)
                self._write_receipt(identifier)
            except Exception:
                if self.store.status(identifier) != 'SUBMITTED':
                    self.store.fail(identifier, 'SUBMISSION_UNCERTAIN', uncertain=True)
            return {}

        graph = StateGraph(ApplicationState)
        for name, node in [('prepare_application', prepare), ('fill_application', fill),
                           ('review_application', review), ('submit_application', submit), ('verify_submission', verify)]:
            graph.add_node(name, self._guard(node))
        graph.add_edge(START, 'prepare_application')
        graph.add_edge('prepare_application', 'fill_application')
        graph.add_edge('fill_application', 'review_application')
        graph.add_conditional_edges('review_application', lambda state: state['decision'],
                                    {'approve': 'submit_application', 'reject': END})
        graph.add_edge('submit_application', 'verify_submission')
        graph.add_edge('verify_submission', END)
        return graph.compile(checkpointer=self.checkpointer)

    def _invoke(self, identifier, value):
        self.graph.invoke(value, self.config(identifier), durability='sync')

    def prepare(self, job_id, *, port=56871, contact=None):
        from .matching import match_jobs
        writer = self._writer()
        job = Job.model_validate(self.store.get_job(job_id)['job'])
        matched = match_jobs(writer.ctx, self.store, writer.manager, job_ids=[job_id])
        if not matched['rankings']:
            raise UserError('P3_JOB_NOT_MATCHED')
        identifier = 'APP-' + uuid4().hex
        package = writer.build(job, job_id, identifier, port, contact)
        self.store.create_prepared(job_id, package, identifier)
        self._invoke(identifier, {'application_id': identifier})
        return self.show(identifier)

    def review(self, identifier, decision, expected_digest, *, edits=None):
        row = self.store.get_application(identifier)
        self.store.package(identifier)  # Check P3 workflow version before any mutation.
        if expected_digest != row['content_digest']:
            raise UserError('P3_APPROVAL_DIGEST_MISMATCH')
        if decision == 'edit':
            if not isinstance(edits, dict) or not edits or set(edits) - {'name', 'email', 'summary', 'resume_text'}:
                raise UserError('P3_EDIT_FIELDS_REQUIRED')
            old = self.store.package(identifier)
            fields = validate_fields({**old['fields'], **edits})
            writer = self._writer()
            job = Job.model_validate(self.store.get_job(old['job_id'])['job'])
            package = writer.build(job, old['job_id'], identifier, urlsplit(old['entry']).port,
                                   {key: fields[key] for key in ('name', 'email')})
            package['fields'] = fields
            if row['status'] == 'WAITING_APPROVAL':
                self.store.decide(identifier, 'edit', expected_digest)
            self.store.edit_package(identifier, package)
            self._restart_fill(identifier)
        else:
            if decision not in {'approve', 'reject'} or row['status'] != 'WAITING_APPROVAL':
                raise UserError('P3_APPROVAL_STATE_INVALID')
            snapshot = self.graph.get_state(self.config(identifier))
            if not snapshot.interrupts:
                raise UserError('P3_CHECKPOINT_MISSING')
            self._invoke(identifier, Command(resume={'decision': decision, 'digest': expected_digest}))
        return self.show(identifier)

    def _restart_fill(self, identifier):
        self.graph.update_state(self.config(identifier),
                                {'application_id': identifier, 'decision': '', 'uncertain': False},
                                as_node='prepare_application')
        self._invoke(identifier, None)

    def recover(self, identifier):
        status = self.store.status(identifier)
        if status in {'SUBMITTING', 'UNKNOWN'}:
            receipt = self.store.receipt(identifier)
            if receipt:
                self.store.finish_submission(identifier, receipt)
            else:
                self.store.fail(identifier, 'SUBMISSION_UNCERTAIN', uncertain=True)
        elif status in {'PREPARED', 'FORM_FILLED'}:
            self._restart_fill(identifier)
        elif status == 'WAITING_APPROVAL':
            snapshot = self.graph.get_state(self.config(identifier))
            if not snapshot.values:
                raise UserError('P3_CHECKPOINT_MISSING')
            if not snapshot.interrupts and snapshot.next:
                if not set(snapshot.next) <= {'prepare_application', 'fill_application', 'review_application'}:
                    raise UserError('P3_CHECKPOINT_MISMATCH')
                # Crash after mark_filled but before the interrupt: finish only safe preparation.
                self._invoke(identifier, None)
        elif status == 'APPROVED':
            snapshot = self.graph.get_state(self.config(identifier))
            if not snapshot.values or not snapshot.next:
                raise UserError('P3_CHECKPOINT_MISSING')
            self._invoke(identifier, None)
        # An existing interrupt, FAILED and terminal states are inspection-only on recovery.
        return self.show(identifier)

    def _write_receipt(self, identifier):
        receipt = self.store.receipt(identifier)
        if not receipt:
            return None
        path = self.ctx.home / 'reports' / 'v2' / 'demo' / identifier / 'receipt.json'
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix('.tmp')
        temporary.write_text(json.dumps(receipt, ensure_ascii=False, indent=2), encoding='utf-8')
        temporary.replace(path)
        return str(path)

    def show(self, identifier):
        package = self.store.package(identifier)
        row = self.store.get_application(identifier)
        return {'mode': 'DEMO', 'scope': 'P3_LOCAL_DEMO', 'application_id': identifier,
                'job_id': row['job_id'], 'status': row['status'], 'content_digest': row['content_digest'],
                'review_path': write_review(self.ctx.home, identifier, package),
                'receipt': self.store.receipt(identifier), 'receipt_path': self._write_receipt(identifier),
                'external_submission': False}
