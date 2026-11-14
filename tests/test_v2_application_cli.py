import contextlib
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from jobhunt.cli import main, parser
from jobhunt.config import Context, UserError
from jobhunt.v2.cli import display_result, run

APP = 'APP-' + 'a' * 32


class ApplicationCLITests(unittest.TestCase):
    def test_p3_requires_demo_before_loading_or_executing(self):
        for command in (['serve-demo'], ['prepare', '--job-id', 'JOB-demo'], ['application', APP],
                        ['recover', APP], ['review', APP, '--decision', 'approve', '--digest', 'a'*64]):
            args = parser().parse_args(['v2'] + command)
            with self.subTest(command=command), patch.object(Context, 'load') as load:
                with self.assertRaisesRegex(UserError, 'P3_DEMO_REQUIRED'):
                    run(args, Context(Path('unused')))
                load.assert_not_called()

    def test_prepare_and_review_forward_explicit_inputs(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            contact = home / 'contact.json'
            contact.write_text(json.dumps({'name':'DEMO', 'email':'demo@example.invalid'}), encoding='utf-8')
            edits = home / 'edits.json'
            edits.write_text(json.dumps({'summary':'DEMO revised'}), encoding='utf-8')
            with patch('jobhunt.v2.application_workflow.ApplicationWorkflow') as factory:
                workflow = factory.return_value.__enter__.return_value
                args = parser().parse_args(['v2','prepare','--job-id','JOB-test','--contact',str(contact),'--demo'])
                run(args, Context(home, demo=True))
                workflow.prepare.assert_called_once_with('JOB-test', port=56871,
                    contact={'name':'DEMO','email':'demo@example.invalid'})
                args = parser().parse_args(['v2','review',APP,'--decision','edit','--digest','a'*64,'--fields',str(edits),'--demo'])
                run(args, Context(home, demo=True))
                workflow.review.assert_called_once_with(APP,'edit','a'*64,edits={'summary':'DEMO revised'})

    def test_display_preserves_generated_review_paths_and_receipt_ids(self):
        path = str(Path('E:/project/reports/v2/demo') / APP / 'review.html')
        value = display_result({'review_path':path,'receipt':{'receipt_id':'RCP-'+'1'*32}})
        self.assertEqual(value['review_path'],path)
        self.assertEqual(value['receipt']['receipt_id'],'RCP-'+'1'*32)

    def test_unknown_status_is_nonzero_without_private_output(self):
        with patch('jobhunt.v2.cli.run', return_value={'status':'UNKNOWN','external_submission':False}):
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(['v2','application',APP,'--demo']),2)

    def test_invalid_or_large_fields_stop_before_workflow(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            path = home / 'edits.json'
            for body in ('[1]', '{"password":"private-sentinel"}', '{broken-private-sentinel', 'x'*131073):
                path.write_text(body, encoding='utf-8')
                args = parser().parse_args(['v2','review',APP,'--decision','edit','--digest','a'*64,'--fields',str(path),'--demo'])
                with patch('jobhunt.v2.application_workflow.ApplicationWorkflow') as factory:
                    with self.assertRaises(UserError) as error:
                        run(args, Context(home, demo=True))
                    self.assertNotIn('private-sentinel',str(error.exception))
                    factory.assert_not_called()

    def test_prepare_file_honors_existing_source_read_permission(self):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            (home/'config').mkdir()
            (home/'config/permissions.yaml').write_text('read_job_sources: false\n', encoding='utf-8')
            args = parser().parse_args(['v2','prepare','--job',str(home/'unread.json'),'--demo'])
            with patch('jobhunt.v2.application_workflow.ApplicationWorkflow') as factory:
                with self.assertRaisesRegex(UserError, 'V2_SOURCE_PERMISSION_DENIED'):
                    run(args, Context(home, demo=True))
                factory.assert_not_called()
