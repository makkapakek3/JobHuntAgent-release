"""P2 orchestration tests: synthetic jobs and a deterministic fake model only."""
import json
from contextlib import redirect_stdout
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from jobhunt.config import Context, UserError
from jobhunt.v2.models import Job, SourcePolicy
from jobhunt.v2.storage import BusinessStore


class MatchingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)
        self.ctx = Context(self.home, demo=True).load()
        self.ctx.preferences.job_titles = ['Python']
        self.ctx.preferences.locations = {'acceptable': ['上海']}
        self.path = self.home / 'business.sqlite3'
        self.store = BusinessStore(self.path, demo=True)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def save(self, identifier='1', **changes):
        job = Job(source='DEMO-test', external_id=identifier, company='DEMO company',
                  title='Python engineer', jd='Python skills required.', city='Shanghai',
                  url=f'https://example.invalid/job/{identifier}', is_demo=True)
        job = job.model_copy(update=changes)
        return self.store.save_job(job, SourcePolicy(source=job.source))[0]

    def manager(self):
        from jobhunt.v2.resume import ResumeManager
        return ResumeManager(self.ctx)

    def model(self, score=70):
        facts = self.manager().facts
        fact_id = next(k for k, v in facts.items() if 'Python' in v)
        value = {'score': score, 'assessments': [{'job_field': 'jd',
            'requirement_quote': 'Python', 'importance': 'core', 'status': 'MATCHED',
            'profile_evidence': [{'fact_id': fact_id, 'quote': 'Python'}],
            'reason': 'DEMO explicit Python evidence; not a hiring prediction.'}], 'risks': []}
        return Mock(analyze=Mock(return_value=(value, {'input': 20, 'output': 30, 'model': 'demo'})))

    def run_match(self, **kwargs):
        from jobhunt.v2.matching import match_jobs
        return match_jobs(self.ctx, self.store, self.manager(), **kwargs)

    def test_resume_facts_whitelist(self):
        self.ctx.safe_profile['password'] = 'SECRET'
        facts = self.manager().facts
        self.assertTrue(facts)
        self.assertNotIn('SECRET', json.dumps(facts))
        self.assertTrue(all(isinstance(v, str) and v for v in facts.values()))

    def test_rule_reject_has_no_score_or_model_call(self):
        self.save(city='Beijing')
        model = self.model()
        result = self.run_match(matcher=model)
        self.assertEqual(result['filtered_count'], 1)
        self.assertIsNone(result['jobs'][0]['score'])
        self.assertTrue(result['jobs'][0]['rules']['evidence'])
        model.analyze.assert_not_called()
        self.assertEqual(self.store.conn.execute('SELECT COUNT(*) FROM job_matches').fetchone()[0], 0)

    def test_title_fallback_city_alias_and_unknowns(self):
        self.save(title='Software engineer')
        model = self.model()
        result = self.run_match(matcher=model)
        self.assertEqual(result['scored_count'], 1)
        self.assertEqual(result['rankings'][0]['score'], 70)
        self.assertIn('salary', result['rankings'][0]['rules']['unknown'])
        self.assertEqual(self.store.list_applications(), [])
        self.assertEqual(self.store.conn.execute('SELECT COUNT(*) FROM approval_records').fetchone()[0], 0)

    def test_cache_survives_reopen_and_job_or_profile_or_model_change(self):
        self.save()
        model = self.model()
        first = self.run_match(matcher=model)
        self.store.close()
        self.store = BusinessStore(self.path, demo=True)
        self.assertEqual(self.run_match(matcher=model)['cache_hits'], 1)
        self.assertEqual(model.analyze.call_count, 1)
        self.save(jd='Python skills required. SQL too.')
        self.assertEqual(self.run_match(matcher=model)['cache_hits'], 0)
        self.ctx.profile_version = 'changed-profile'
        self.assertEqual(self.run_match(matcher=model)['cache_hits'], 0)
        self.ctx.settings.matching_backend = 'deepseek'
        self.ctx.deepseek_model = 'configured-other-model'
        model.analyze.return_value[1]['model'] = 'configured-other-model'
        self.assertEqual(self.run_match(matcher=model)['cache_hits'], 0)
        self.assertNotEqual(first['rankings'][0]['match_id'], self.run_match(matcher=model)['rankings'][0]['match_id'])

    def test_bad_quote_never_persisted_or_returned_as_score(self):
        self.save()
        model = self.model()
        model.analyze.return_value[0]['assessments'][0]['requirement_quote'] = 'fabricated requirement'
        result = self.run_match(matcher=model)
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual(result['failed_count'], 1)
        self.assertEqual(result['rankings'], [])
        self.assertEqual(result['jobs'][0]['error_code'], 'LLM_EVIDENCE_INVALID')

    def test_safe_parser_failure_code_survives_without_raw_model_body(self):
        self.save()
        model = self.model()
        model.analyze.side_effect = UserError('API_RESPONSE_INVALID_JSONDecodeError')
        result = self.run_match(matcher=model)
        self.assertEqual(result['jobs'][0]['error_code'], 'API_RESPONSE_INVALID_JSONDecodeError')
        self.assertIsNone(result['jobs'][0]['score'])

    def test_rules_only_and_rankings_have_no_model_calls(self):
        from jobhunt.v2.matching import current_rankings
        self.save()
        model = self.model()
        self.run_match(matcher=model, rules_only=True)
        model.analyze.assert_not_called()
        self.assertEqual(current_rankings(self.ctx, self.store, self.manager())['rankings'], [])
        self.run_match(matcher=model)
        self.assertEqual(len(current_rankings(self.ctx, self.store, self.manager())['rankings']), 1)
        self.save(jd='Python changed')
        self.assertEqual(current_rankings(self.ctx, self.store, self.manager())['rankings'], [])

    def test_limit_deferred_and_scores_sorted(self):
        self.save('1')
        self.save('2')
        model = self.model()
        self.ctx.settings.max_new_jobs_for_llm = 1
        result = self.run_match(matcher=model)
        self.assertEqual(result['deferred_count'], 1)
        self.assertEqual(result['status'], 'PARTIAL')
        model.analyze.return_value[0]['score'] = 90
        result = self.run_match(matcher=model)
        self.assertEqual([r['score'] for r in result['rankings']], [90, 70])
        self.assertEqual(result['cache_hits'], 1)

    def test_corrupt_cache_is_reported_without_rescoring(self):
        self.save()
        model = self.model()
        self.run_match(matcher=model)
        with self.store.tx():
            self.store.conn.execute("UPDATE job_matches SET evidence_json='{}'")
        result = self.run_match(matcher=model)
        self.assertEqual(model.analyze.call_count, 1)
        self.assertEqual(result['jobs'][0]['error_code'], 'V2_MATCH_CACHE_INVALID')

    def test_reports_contain_citations_and_no_contacts(self):
        from jobhunt.v2.match_reporting import write_report
        self.save()
        result = self.run_match(matcher=self.model())
        paths = write_report(self.home, result)
        text = Path(paths['markdown']).read_text(encoding='utf-8')
        self.assertIn('Python', text)
        self.assertIn('模型推断', text)
        saved = json.loads(Path(paths['json']).read_text(encoding='utf-8'))
        self.assertEqual(saved['rankings'][0]['score'], 70)

    def test_live_cannot_use_demo_matcher(self):
        self.ctx.demo = False
        with self.assertRaisesRegex(UserError, 'V2_MODE_MISMATCH'):
            self.run_match(matcher=self.model())

    def test_budget_defers_remaining_but_keeps_committed_scores(self):
        from jobhunt.usage import BudgetExceeded
        self.save('1')
        self.save('2')
        self.save('3')
        model = self.model()
        model.analyze.side_effect = [model.analyze.return_value, BudgetExceeded('DAILY_TOKEN_BUDGET')]
        result = self.run_match(matcher=model)
        self.assertEqual((result['scored_count'], result['deferred_count']), (1, 2))
        self.assertEqual(model.analyze.call_count, 2)
        self.assertEqual(self.store.conn.execute('SELECT COUNT(*) FROM job_matches').fetchone()[0], 1)

    def test_profile_permission_is_enforced_before_facts(self):
        self.ctx.permissions.read_profile = False
        with self.assertRaisesRegex(UserError, 'PROFILE_READ_DENIED'):
            self.manager()

    def test_cli_demo_matches_and_reads_rankings_without_network(self):
        from jobhunt.cli import main
        database = self.home / 'data' / 'v2' / 'demo' / 'business.sqlite3'
        self.store.close()
        self.store = BusinessStore(database, demo=True)
        identifier = self.save()
        output = io.StringIO()
        with patch('jobhunt.deepseek.request_json', side_effect=AssertionError('network forbidden')), redirect_stdout(output):
            self.assertEqual(main(['v2', 'match', '--demo', '--home', str(self.home)]), 0)
        matched = json.loads(output.getvalue())
        self.assertEqual(matched['rankings'][0]['job_id'], identifier)
        self.assertEqual(matched['model'], 'demo')
        self.assertTrue(Path(matched['reports']['markdown']).is_file())
        output = io.StringIO()
        with patch('jobhunt.deepseek.request_json', side_effect=AssertionError('network forbidden')), redirect_stdout(output):
            self.assertEqual(main(['v2', 'rankings', '--demo', '--home', str(self.home)]), 0)
        self.assertEqual(json.loads(output.getvalue())['cache_hits'], 1)

    def test_generated_identifiers_survive_display_without_unredacting_job_text(self):
        from jobhunt.v2.cli import display_result
        identifier = 'JOB-' + '1' * 32
        output = display_result({'rankings': [{'job_id': identifier, 'job_hash': '1' * 64,
                                               'title': 'contact test@example.invalid'}]})
        self.assertEqual(output['rankings'][0]['job_id'], identifier)
        self.assertEqual(output['rankings'][0]['job_hash'], '1' * 64)
        self.assertNotIn('test@example.invalid', output['rankings'][0]['title'])

    def test_generated_report_path_is_not_broken_by_digit_redaction(self):
        from jobhunt.v2.cli import display_result
        path = str(self.home / 'reports/v2/live' / ('matches-P2-' + '1' * 32 + '.md'))
        self.assertEqual(display_result({'reports': {'markdown': path}})['reports']['markdown'], path)

    def test_rankings_keep_healthy_results_when_one_cache_is_corrupt(self):
        from jobhunt.v2.matching import current_rankings
        self.save('1')
        corrupt_id = self.save('2')
        self.run_match(matcher=self.model())
        with self.store.tx():
            self.store.conn.execute("UPDATE job_matches SET evidence_json='{}' WHERE job_id=?", (corrupt_id,))
        result = current_rankings(self.ctx, self.store, self.manager())
        self.assertEqual(result['status'], 'PARTIAL')
        self.assertEqual((result['scored_count'], result['failed_count']), (1, 1))

    def test_pagination_reaches_later_jobs_and_rankings_are_global(self):
        from jobhunt.v2.matching import current_rankings
        for i in range(101):
            self.save(str(i))
        self.ctx.settings.max_new_jobs_for_llm = 100
        result = self.run_match(matcher=self.model())
        self.assertEqual(result['next_offset'], 100)
        self.assertEqual(result['status'], 'PARTIAL')
        result = self.run_match(matcher=self.model(90), offset=100)
        self.assertEqual(result['scored_count'], 1)
        self.assertIsNone(result['next_offset'])
        ranked = current_rankings(self.ctx, self.store, self.manager(), limit=1)
        self.assertEqual(ranked['rankings'][0]['score'], 90)
        self.assertEqual(ranked['scored_count'], 101)

    def test_invalid_pagination_fails_before_api(self):
        self.save()
        model = self.model()
        with self.assertRaisesRegex(UserError, 'V2_MATCH_OFFSET_INVALID'):
            self.run_match(matcher=model, offset=-1)
        model.analyze.assert_not_called()

    def test_resume_offset_does_not_skip_default_budget_deferred_jobs(self):
        from jobhunt.v2.matching import current_rankings
        for i in range(101):
            self.save(str(i))
        self.ctx.settings.max_new_jobs_for_llm = 20
        offset = 0
        offsets = []
        for _ in range(6):
            result = self.run_match(matcher=self.model(), offset=offset)
            offset = result['next_offset']
            offsets.append(offset)
            if offset is None:
                break
        self.assertEqual(offsets, [20, 40, 60, 80, 100, None])
        self.assertEqual(current_rankings(self.ctx, self.store, self.manager())['scored_count'], 101)


if __name__ == '__main__':
    unittest.main()
