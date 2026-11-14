from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from jobhunt.config import Context, ROOT
from jobhunt.v2.desktop import import_batch, ordered_jobs


class DesktopTests(unittest.TestCase):
    def test_batch_uses_existing_demo_import_and_reports_progress(self):
        with TemporaryDirectory() as directory:
            ctx = Context(Path(directory), demo=True)
            progress = []
            result = import_batch(ctx, ROOT / 'examples' / 'v2-jobs.demo.csv',
                                  lambda done, total: progress.append((done, total)))
            matching = result['matching']
            self.assertTrue(matching['jobs'])
            self.assertEqual(progress[0], (0, len(matching['jobs'])))
            self.assertEqual(progress[-1], (len(matching['jobs']), len(matching['jobs'])))
            self.assertTrue(Path(matching['reports']['markdown']).is_file())

    def test_visible_rows_put_scored_jobs_first_and_keep_failed_jobs(self):
        jobs = [{'score': None, 'state': 'FAILED'}, {'score': 56}, {'score': 0}, {'score': 93}]
        self.assertEqual([row['score'] for row in ordered_jobs(jobs)], [93, 56, 0, None])
