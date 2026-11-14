"""Synthetic P3 workflow with a replaceable executor, real SQLite checkpoints."""
import copy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from jobhunt.config import Context, UserError
from jobhunt.models import digest
from jobhunt.v2.models import Job, SourcePolicy, ValidationResult, SubmissionResult


class FakeExecutor:
    def __init__(self, package, identifier, browser, database):
        self.package, self.identifier, self.browser, self.database = package, identifier, browser, database

    def open_application(self, job):
        self.browser.setdefault(self.identifier, {})

    def inspect_form(self):
        if self.browser.get('unknown_control'):
            raise UserError('P3_UNKNOWN_CONTROL')
        return []

    def fill_form(self, fields):
        self.inspect_form()
        self.browser[self.identifier] = dict(fields)

    def snapshot_fields(self):
        return self.browser[self.identifier]

    def validate_form(self):
        if self.browser.get('on_validate'):
            self.browser['on_validate']()
        return ValidationResult(valid=True)

    def submit(self, token):
        from jobhunt.v2.application_store import ApplicationStore
        self.browser['clicks'] += 1
        if self.browser.get('lose_before_receipt'):
            raise TimeoutError('private marker must not enter checkpoint')
        store = ApplicationStore(self.database, demo=True)
        try:
            store.accept_submission(self.identifier, token, digest(self.package), self.snapshot_fields())
        finally:
            store.close()
        if self.browser.get('lose_after_receipt'):
            raise TimeoutError('private marker must not enter checkpoint')

    def verify_result(self):
        from jobhunt.v2.application_store import ApplicationStore
        store = ApplicationStore(self.database, demo=True)
        try:
            receipt = store.receipt(self.identifier)
        finally:
            store.close()
        return SubmissionResult(status='SUBMITTED' if receipt else 'UNKNOWN',
                                receipt_reference=receipt['receipt_id'] if receipt else '')


class ApplicationWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.ctx = Context(self.home, demo=True).load()
        self.browser = {'clicks': 0}
        self.job = Job(source='DEMO', external_id='p3', title='DEMO Python engineer', company='DEMO company',
                       jd='DEMO Python SQLite', url='https://example.invalid/p3', is_demo=True)

    def tearDown(self):
        self.tmp.cleanup()

    def test_automatic_window_selection_ignores_non_chrome_electron_apps(self):
        from jobhunt.v2.application_workflow import chrome_handle
        from unittest.mock import MagicMock
        def enumerate_windows(callback, value):
            callback(11, value)
            callback(22, value)
        with patch('win32gui.EnumWindows', side_effect=enumerate_windows), \
                patch('win32gui.IsWindowVisible', return_value=True), \
                patch('win32gui.GetClassName', return_value='Chrome_WidgetWin_1'), \
                patch('win32process.GetWindowThreadProcessId', return_value=(1, 2)), \
                patch('win32api.OpenProcess', return_value=MagicMock()), \
                patch('win32process.GetModuleFileNameEx', side_effect=['C:/app/ChatGPT.exe','C:/app/chrome.exe']):
            self.assertEqual(chrome_handle(),22)

    def workflow(self):
        from jobhunt.v2.application_workflow import ApplicationWorkflow
        return ApplicationWorkflow(self.ctx, executor_factory=lambda package, identifier:
            FakeExecutor(package, identifier, self.browser, self.home / 'data/v2/demo/business.sqlite3'))

    def prepare(self, workflow):
        job_id = workflow.store.save_job(self.job, SourcePolicy(source='DEMO'))[0]
        return workflow.prepare(job_id, port=56871)

    def test_fill_pause_close_reopen_approve_receipt_and_idempotent_recovery(self):
        with self.workflow() as workflow:
            created = self.prepare(workflow)
            self.assertEqual(created['status'], 'WAITING_APPROVAL')
            self.assertEqual(self.browser['clicks'], 0)
            self.assertTrue(Path(created['review_path']).is_file())
        with self.workflow() as workflow:
            result = workflow.review(created['application_id'], 'approve', created['content_digest'])
            self.assertEqual(result['status'], 'SUBMITTED')
            self.assertTrue(result['receipt']['receipt_id'])
            self.assertEqual(workflow.recover(created['application_id'])['status'], 'SUBMITTED')
            state = repr(list(workflow.checkpointer.list(workflow.config(created['application_id']))))
            self.assertNotIn('demo@example.invalid', state)
            self.assertNotIn('approval_token', state)
        self.assertEqual(self.browser['clicks'], 1)

    def test_reject_never_calls_submit(self):
        with self.workflow() as workflow:
            created = self.prepare(workflow)
            result = workflow.review(created['application_id'], 'reject', created['content_digest'])
            self.assertEqual(result['status'], 'REJECTED')
            self.assertEqual(workflow.recover(created['application_id'])['status'], 'REJECTED')
        self.assertEqual(self.browser['clicks'], 0)

    def test_edit_refills_reinterrupts_and_old_digest_cannot_approve(self):
        with self.workflow() as workflow:
            created = self.prepare(workflow)
            edited = workflow.review(created['application_id'], 'edit', created['content_digest'],
                                     edits={'summary': 'DEMO 用户确认的修改材料'})
            self.assertEqual(edited['status'], 'WAITING_APPROVAL')
            self.assertNotEqual(created['content_digest'], edited['content_digest'])
            self.assertEqual(self.browser[created['application_id']]['summary'], 'DEMO 用户确认的修改材料')
            with self.assertRaises(UserError):
                workflow.review(created['application_id'], 'approve', created['content_digest'])
            workflow.review(created['application_id'], 'approve', edited['content_digest'])
        self.assertEqual(self.browser['clicks'], 1)

    def test_changed_page_blocks_approval_and_needs_refill(self):
        with self.workflow() as workflow:
            created = self.prepare(workflow)
            self.browser[created['application_id']]['email'] = 'changed@example.invalid'
            with self.assertRaises(UserError):
                workflow.review(created['application_id'], 'approve', created['content_digest'])
            self.assertNotEqual(workflow.store.status(created['application_id']), 'APPROVED')
        self.assertEqual(self.browser['clicks'], 0)

    def test_changed_job_blocks_old_approval(self):
        with self.workflow() as workflow:
            created = self.prepare(workflow)
            workflow.store.save_job(self.job.model_copy(update={'jd':'changed Python JD'}), SourcePolicy(source='DEMO'))
            with self.assertRaises(UserError):
                workflow.review(created['application_id'], 'approve', created['content_digest'])
        self.assertEqual(self.browser['clicks'], 0)

    def test_unknown_without_receipt_never_retries_even_after_restart(self):
        self.browser['lose_before_receipt'] = True
        with self.workflow() as workflow:
            created = self.prepare(workflow)
            result = workflow.review(created['application_id'], 'approve', created['content_digest'])
            self.assertEqual(result['status'], 'UNKNOWN')
        with self.workflow() as workflow:
            self.assertEqual(workflow.recover(created['application_id'])['status'], 'UNKNOWN')
        self.assertEqual(self.browser['clicks'], 1)

    def test_unknown_with_receipt_can_reconcile_readonly(self):
        self.browser['lose_after_receipt'] = True
        with self.workflow() as workflow:
            created = self.prepare(workflow)
            result = workflow.review(created['application_id'], 'approve', created['content_digest'])
            self.assertEqual(result['status'], 'UNKNOWN')
        with self.workflow() as workflow:
            self.assertEqual(workflow.recover(created['application_id'])['status'], 'SUBMITTED')
        self.assertEqual(self.browser['clicks'], 1)

    def test_unknown_controls_fail_without_submitting_or_leaking_errors(self):
        self.browser['unknown_control'] = True
        with self.workflow() as workflow:
            with self.assertRaises(UserError):
                self.prepare(workflow)
            self.assertEqual(workflow.store.list_applications()[0]['status'], 'FAILED')
        self.assertEqual(self.browser['clicks'], 0)

    def test_crash_after_fill_transaction_recovers_to_real_interrupt(self):
        class Crash(BaseException):
            pass
        with self.workflow() as workflow:
            with patch('jobhunt.v2.application_workflow.write_review', side_effect=Crash):
                with self.assertRaises(Crash):
                    self.prepare(workflow)
            identifier = workflow.store.list_applications()[0]['application_id']
            self.assertEqual(workflow.store.status(identifier), 'WAITING_APPROVAL')
        with self.workflow() as workflow:
            recovered = workflow.recover(identifier)
            self.assertTrue(workflow.graph.get_state(workflow.config(identifier)).interrupts)
            workflow.review(identifier, 'reject', recovered['content_digest'])
        self.assertEqual(self.browser['clicks'], 0)

    def test_job_change_allows_edit_and_requires_fresh_approval(self):
        with self.workflow() as workflow:
            created = self.prepare(workflow)
            workflow.store.save_job(self.job.model_copy(update={'jd':'DEMO Python revised'}), SourcePolicy(source='DEMO'))
            edited = workflow.review(created['application_id'], 'edit', created['content_digest'],
                                     edits={'summary': 'DEMO 用户修改'})
            self.assertEqual(edited['status'], 'WAITING_APPROVAL')
            self.assertNotEqual(edited['content_digest'], created['content_digest'])
            with self.assertRaises(UserError):
                workflow.review(created['application_id'], 'approve', created['content_digest'])
        self.assertEqual(self.browser['clicks'], 0)

    def test_job_change_does_not_prevent_reject(self):
        with self.workflow() as workflow:
            created = self.prepare(workflow)
            workflow.store.save_job(self.job.model_copy(update={'jd':'DEMO changed'}), SourcePolicy(source='DEMO'))
            result = workflow.review(created['application_id'], 'reject', created['content_digest'])
            self.assertEqual(result['status'], 'REJECTED')
        self.assertEqual(self.browser['clicks'], 0)

    def test_resume_change_during_final_form_validation_blocks_click(self):
        with self.workflow() as workflow:
            created = self.prepare(workflow)
            calls = []
            def on_validate():
                calls.append(True)
            self.browser['on_validate'] = on_validate
            original = workflow._writer
            def current_writer():
                writer = original()
                if len(calls) >= 2:  # Review validation then final submit validation.
                    writer.manager.resume_version = 'changed-resume-version'
                return writer
            with patch.object(workflow, '_writer', side_effect=current_writer):
                with self.assertRaises(UserError):
                    workflow.review(created['application_id'], 'approve', created['content_digest'])
        self.assertEqual(self.browser['clicks'], 0)


if __name__ == '__main__':
    unittest.main()
