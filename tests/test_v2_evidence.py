"""P2 evidence contracts; synthetic facts and isolated local ledgers only."""
import copy
import importlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import HTTPError

from jobhunt.config import Context, UserError
from jobhunt.models import Preferences, Settings
from jobhunt.usage import UsageLedger
from jobhunt.v2.models import Job


def candidate(**changes):
    return Job(**(dict(source="DEMO-source", external_id="DEMO-id", company="DEMO 公司",
                       title="DEMO 工程师", jd="熟悉 Python，了解 SQL 优先。",
                       url="https://example.invalid/jobs/1", required_skills=["Python"],
                       is_demo=True) | changes))


PROFILE = {"skills.0": "Python", "education.0.degree": "本科",
           "projects.0.description": "团队采用 SQL；我负责 Python 数据处理。"}


def analysis(**changes):
    result = {"score": 75, "assessments": [{"job_field": "jd", "requirement_quote": "熟悉 Python",
              "importance": "core", "status": "MATCHED", "profile_evidence": [
                  {"fact_id": "skills.0", "quote": "Python"}], "reason": "技能表中有 Python。"}], "risks": []}
    return result | changes


def reply(result=None, *, content=None, finish="stop", usage=True):
    payload = {"choices": [{"finish_reason": finish, "message": {"role": "assistant", "content":
               json.dumps(analysis() if result is None else result, ensure_ascii=False) if content is None else content}}]}
    if usage:
        payload["usage"] = {"prompt_tokens": 100, "completion_tokens": 25, "prompt_cache_hit_tokens": 10}
    return payload


class EvidenceTests(unittest.TestCase):
    def test_model_gap_is_not_accepted_as_a_confirmed_personal_deficit(self):
        value = analysis()
        value['assessments'][0].update(status='GAP', reason='Candidate lacks years of experience')
        result = self.module.validate_match(value, self.module.job_facts(candidate()), PROFILE)
        self.assertEqual(result.assessments[0].status, 'EVIDENCE_INSUFFICIENT')
        self.assertNotIn('Candidate lacks', result.assessments[0].reason)

    def setUp(self):
        self.assertIsNotNone(importlib.util.find_spec("jobhunt.v2.match_evidence"),
                             "P2 evidence module must be implemented")
        self.module = importlib.import_module("jobhunt.v2.match_evidence")

    def test_allowlisted_job_facts_drop_unknowns_and_redact(self):
        job = candidate(jd="熟悉 Python；联系 demo@example.invalid，password=demo-secret",
                        city="上海", minimum_degree="本科", salary_min=0, remote=False,
                        experience_years=0, employment_type="全职", campus=None,
                        start_month="2027-01", days_per_week=3, duration_months=4, travel="无需出差")
        facts = self.module.job_facts(job)
        self.assertEqual(set(facts), {"title", "jd", "required_skills", "city", "minimum_degree", "salary_min",
                                     "remote", "experience_years", "employment_type", "start_month",
                                     "days_per_week", "duration_months", "travel"})
        self.assertTrue(all(type(value) is str for value in facts.values()))
        self.assertIn("Python", facts["required_skills"])
        self.assertNotIn("demo@example.invalid", facts["jd"])
        self.assertNotIn("demo-secret", facts["jd"])
        self.assertEqual(facts["remote"], "false")
        self.assertEqual(facts["title"], "DEMO 工程师")
        self.assertEqual(facts["days_per_week"], "3")
        self.assertEqual(facts["start_month"], "2027-01")

    def test_valid_match_has_original_job_and_profile_quotes(self):
        value = self.module.validate_match(analysis(), self.module.job_facts(candidate()), PROFILE)
        self.assertIsInstance(value, self.module.MatchAnalysis)
        self.assertEqual(value.assessments[0].profile_evidence[0].fact_id, "skills.0")
        self.assertEqual(value.model_dump(), analysis())
        self.assertTrue(self.module.MATCH_VERSION)

    def test_fake_wrong_field_or_blank_citations_are_rejected_without_echo(self):
        for changes in [{"job_field": "url"}, {"requirement_quote": "FABRICATED"},
                        {"requirement_quote": "  "}, {"profile_evidence": [{"fact_id": "missing.0", "quote": "Python"}]},
                        {"profile_evidence": [{"fact_id": "education.0.degree", "quote": "Python"}]},
                        {"profile_evidence": [{"fact_id": "skills.0", "quote": "FABRICATED"}]},
                        {"profile_evidence": [{"fact_id": "skills.0", "quote": "  "}]}]:
            with self.subTest(changes=changes):
                value = analysis()
                value["assessments"][0].update(changes)
                with self.assertRaises(UserError) as caught:
                    self.module.validate_match(value, self.module.job_facts(candidate()), PROFILE)
                self.assertRegex(str(caught.exception), "^LLM_(EVIDENCE|SCHEMA)_INVALID$")
                self.assertNotIn("FABRICATED", str(caught.exception))

    def test_match_expression_gap_and_gap_require_personal_citations(self):
        for status in ["MATCHED", "EXPRESSION_GAP", "GAP"]:
            with self.subTest(status=status):
                value = analysis()
                value["assessments"][0].update(status=status, profile_evidence=[])
                with self.assertRaisesRegex(UserError, "LLM_EVIDENCE_INVALID"):
                    self.module.validate_match(value, self.module.job_facts(candidate()), PROFILE)

    def test_missing_personal_evidence_can_be_unknown_or_insufficient(self):
        for status in ["UNKNOWN", "EVIDENCE_INSUFFICIENT"]:
            value = analysis()
            value["assessments"][0].update(status=status, profile_evidence=[])
            self.assertEqual(self.module.validate_match(value, self.module.job_facts(candidate()), {}).assessments[0].status,
                             status)

    def test_schema_rejects_coercion_extra_fields_and_unbounded_output(self):
        bad = [analysis(score="75"), analysis(score=True), analysis(score=101), analysis(score=-1),
               analysis(assessments=[]), analysis(assessments=analysis()["assessments"] * 7),
               analysis(risks=["r"] * 21), analysis(unexpected="PRIVATE-VALUE")]
        extra_citation = analysis()
        extra_citation["assessments"][0]["profile_evidence"][0]["filename"] = "PRIVATE-VALUE"
        bad.append(extra_citation)
        for value in bad:
            with self.subTest(value=value), self.assertRaisesRegex(UserError, "^LLM_SCHEMA_INVALID$"):
                self.module.validate_match(value, self.module.job_facts(candidate()), PROFILE)


class MatcherTests(unittest.TestCase):
    def setUp(self):
        self.module = importlib.import_module("jobhunt.v2.match_evidence")
        self.assertTrue(hasattr(self.module, "JobMatcher"), "P2 matcher must be implemented")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ctx = Context(Path(self.tmp.name))
        self.ctx.settings = Settings(matching_backend="deepseek", deepseek_model="DEMO-config-model",
                                     llm_timeout_seconds=4, llm_max_output_tokens=512)
        self.ctx.deepseek_model = "DEMO-env-model"
        self.ctx.preferences = Preferences(job_titles=["Python 工程师"], job_types=["internship"],
                                           locations={"preferred": ["上海"]},
                                           compensation={"minimum": 5000}, internship_max_days_per_week=3,
                                           review_notes=["不接受单休；联系 demo@example.invalid"])
        self.ctx.safe_profile = {"summary": "PRIVATE-EXTRA-PROFILE", "contact": "secret@example.invalid"}
        self.ctx.private_secret = "PRIVATE-EXTRA-CONTEXT"
        self.env = self.enterContext(patch("jobhunt.deepseek.read_env", return_value={
            "DEEPSEEK_API_KEY": "sk-DEMO-test-key", "DEEPSEEK_BASE_URL": "https://api.deepseek.com/v1"}))
        self.request = self.enterContext(patch("jobhunt.deepseek.request_json", return_value=reply()))

    def matcher(self, profile=None):
        return self.module.JobMatcher(self.ctx, PROFILE if profile is None else profile)

    def ledger(self):
        return UsageLedger(self.ctx.home, self.ctx.settings.schedule_timezone)

    def test_configured_request_has_exact_facts_preferences_and_no_tools(self):
        result, usage = self.matcher().analyze(candidate(), ["working_hours"], job_id="DEMO-job", run_id="DEMO-run")
        self.assertEqual(result.score, 75)
        self.assertEqual(usage, {"input": 100, "output": 25, "model": "DEMO-env-model"})
        url, body, key, timeout = self.request.call_args.args
        self.assertEqual(url, "https://api.deepseek.com/v1/chat/completions")
        self.assertEqual(key, "sk-DEMO-test-key")
        self.assertEqual(timeout, 4)
        self.assertEqual(body["model"], "DEMO-env-model")
        self.assertEqual(body["max_tokens"], 512)
        self.assertEqual(body["thinking"], {"type": "disabled"})
        self.assertEqual(body["response_format"], {"type": "json_object"})
        self.assertFalse(body["stream"])
        self.assertEqual(set(body), {"model", "max_tokens", "thinking", "response_format", "stream", "messages"})
        self.assertEqual([item["role"] for item in body["messages"]], ["system", "user"])
        facts = json.loads(body["messages"][1]["content"])
        self.assertEqual(facts["job"], self.module.job_facts(candidate()))
        self.assertEqual(facts["profile"], PROFILE)
        self.assertEqual(facts["unknown"], ["working_hours"])
        self.assertEqual(facts["preferences"]["job_types"], ["internship"])
        self.assertEqual(facts["preferences"]["internship_max_days_per_week"], 3)
        self.assertEqual(facts["preferences"]["review_notes"], ["不接受单休；联系 [EMAIL]"])
        serialized = json.dumps(body, ensure_ascii=False)
        for secret in ["sk-DEMO-test-key", "PRIVATE-EXTRA-PROFILE", "PRIVATE-EXTRA-CONTEXT",
                       "demo@example.invalid", "secret@example.invalid", "example.invalid/jobs"]:
            self.assertNotIn(secret, serialized)
        self.assertNotIn("get_profile_section", serialized)
        with self.ledger().db() as db:
            call = db.execute("SELECT * FROM calls").fetchone()
        self.assertEqual((call["state"], call["run_id"], call["job_id"]), ("SUCCESS", "DEMO-run", "DEMO-job"))
        self.assertEqual((call["input_tokens"], call["output_tokens"]), (100, 25))
        self.assertGreater(call["reserved"], 512)

    def test_key_and_endpoint_fail_without_request_or_budget_reservation(self):
        for env, code in [({}, "API_KEY_MISSING"),
                          ({"DEEPSEEK_API_KEY": "sk-DEMO", "DEEPSEEK_BASE_URL": "https://evil.invalid"},
                           "DEEPSEEK_ENDPOINT_DENIED")]:
            self.env.return_value = env
            with self.subTest(code=code), self.assertRaisesRegex(UserError, code):
                self.matcher().analyze(candidate(), [])
        self.request.assert_not_called()
        self.assertEqual(self.ledger().summary()["calls"], 0)

    def test_input_size_and_daily_budget_prevent_network(self):
        self.ctx.settings.llm_max_input_bytes = 1000
        with self.assertRaisesRegex(UserError, "LLM_INPUT_TOO_LARGE"):
            self.matcher().analyze(candidate(jd="DEMO " * 500), [])
        self.ctx.settings.llm_max_input_bytes = 48000
        self.ctx.settings.daily_token_budget = 1
        with self.assertRaisesRegex(UserError, "DAILY_TOKEN_BUDGET"):
            self.matcher().analyze(candidate(), [])
        self.request.assert_not_called()
        self.assertEqual(self.ledger().summary()["calls"], 0)

    def test_no_internal_cache_or_profile_mutation(self):
        supplied = copy.deepcopy(PROFILE)
        matcher = self.matcher(supplied)
        matcher.analyze(candidate(), [])
        matcher.analyze(candidate(), [])
        self.assertEqual(self.request.call_count, 2)
        self.assertEqual(supplied, PROFILE)
        self.assertEqual(self.ledger().summary()["calls"], 2)

    def test_invalid_json_and_invalid_evidence_are_charged_without_retry(self):
        bad_evidence = analysis()
        bad_evidence["assessments"][0]["profile_evidence"][0]["quote"] = "SECRET-FABRICATED"
        self.ctx.settings.llm_max_retries = 2
        for response in [reply(content="SECRET-BROKEN-JSON"), reply(bad_evidence)]:
            self.request.return_value = response
            with self.assertRaises(UserError) as caught:
                self.matcher().analyze(candidate(), [])
            self.assertNotIn("SECRET", str(caught.exception))
        self.assertEqual(self.request.call_count, 2)
        total = self.ledger().summary()
        self.assertEqual((total["failed_calls"], total["input_tokens"], total["output_tokens"]), (2, 200, 50))
        self.assertEqual(total["unfinished_calls"], 0)

    def test_tool_calls_and_truncation_are_rejected_and_charged(self):
        tool_response = reply()
        tool_response["choices"][0]["message"]["tool_calls"] = [{"function": {"name": "arbitrary_fetch"}}]
        for response, code in [(tool_response, "LLM_TOOL_DENIED"),
                               (reply(finish="length"), "LLM_INCOMPLETE_RESPONSE"),
                               (reply(finish=None), "LLM_INCOMPLETE_RESPONSE")]:
            self.request.return_value = response
            with self.subTest(code=code), self.assertRaisesRegex(UserError, code):
                self.matcher().analyze(candidate(), [])
        self.assertEqual(self.request.call_count, 3)
        self.assertEqual(self.ledger().summary()["failed_calls"], 3)

    def test_network_errors_are_safe_and_unknown_usage_keeps_reservation(self):
        self.ctx.settings.llm_max_retries = 2
        self.request.side_effect = TimeoutError("SECRET-RESPONSE")
        with self.assertRaisesRegex(UserError, "^API_NETWORK_TimeoutError$"):
            self.matcher().analyze(candidate(), [])
        self.assertEqual(self.request.call_count, 1)
        total = self.ledger().summary()
        self.assertEqual(total["unknown_usage_calls"], 1)
        self.assertGreater(total["budget_used"], 512)
        self.assertEqual(total["unfinished_calls"], 0)

    def test_missing_usage_remains_unknown_on_success(self):
        self.request.return_value = reply(usage=False)
        result, usage = self.matcher().analyze(candidate(), [])
        self.assertEqual(result.score, 75)
        self.assertEqual(usage, {"input": None, "output": None, "model": "DEMO-env-model"})
        self.assertEqual(self.ledger().summary()["unknown_usage_calls"], 1)

    def test_reservation_exists_before_sending_and_configured_prices_are_used(self):
        self.ctx.settings.model_prices = {"DEMO-env-model": {"input": 1, "output": 2, "cached_input": 0.1}}
        def server(*args):
            with self.ledger().db() as db:
                reserved = db.execute("SELECT state, reserved FROM calls").fetchall()
            self.assertEqual(len(reserved), 1)
            self.assertEqual(reserved[0]["state"], "RESERVED")
            self.assertGreater(reserved[0]["reserved"], 512)
            return reply()
        self.request.side_effect = server
        self.matcher().analyze(candidate(), [])
        self.assertAlmostEqual(self.ledger().summary()["cost"], 0.000141)

    def test_http_errors_are_safe_and_not_automatically_retried(self):
        self.request.side_effect = HTTPError("https://example.invalid/SECRET-URL", 429, "SECRET-BODY", {}, None)
        with self.assertRaisesRegex(UserError, "^API_HTTP_429$"):
            self.matcher().analyze(candidate(), [])
        self.assertEqual(self.request.call_count, 1)
        self.assertEqual(self.ledger().summary()["failed_calls"], 1)

    def test_malformed_usage_values_cannot_be_reported_as_measured_counts(self):
        payload = reply()
        payload["usage"] = {"prompt_tokens": True, "completion_tokens": -5, "prompt_cache_hit_tokens": "10"}
        self.request.return_value = payload
        _, usage = self.matcher().analyze(candidate(), [])
        self.assertIsNone(usage["input"])
        self.assertIsNone(usage["output"])
        self.assertGreater(self.ledger().summary()["budget_used"], 512)

    def test_non_assistant_or_refusal_response_is_rejected_and_charged(self):
        for change in [{"role": "tool"}, {"refusal": "SECRET-REFUSAL"}]:
            payload = reply()
            payload["choices"][0]["message"].update(change)
            self.request.return_value = payload
            with self.subTest(change=change), self.assertRaisesRegex(UserError, "^LLM_SCHEMA_INVALID$"):
                self.matcher().analyze(candidate(), [])
        self.assertEqual(self.ledger().summary()["failed_calls"], 2)

    def test_malformed_endpoint_uses_safe_code_without_config_echo(self):
        self.env.return_value["DEEPSEEK_BASE_URL"] = "https://api.deepseek.com:SECRET-PORT"
        with self.assertRaisesRegex(UserError, "^DEEPSEEK_ENDPOINT_DENIED$"):
            self.matcher().analyze(candidate(), [])
        self.request.assert_not_called()

    def test_live_mock_and_ollama_are_not_supported(self):
        for backend in ["mock", "ollama"]:
            self.ctx.settings.matching_backend = backend
            with self.subTest(backend=backend), self.assertRaisesRegex(UserError, "MATCH_BACKEND_DENIED"):
                self.matcher().analyze(candidate(), [])
        self.request.assert_not_called()
        self.env.assert_not_called()

    def test_demo_mock_is_deterministic_and_uses_actual_citations(self):
        self.ctx.demo = True
        self.ctx.settings.matching_backend = "mock"
        first, usage = self.matcher().analyze(candidate(), [])
        second, _ = self.matcher().analyze(candidate(), [])
        self.assertEqual(first, second)
        self.assertEqual(usage, {"input": 0, "output": 0, "model": "demo"})
        self.assertTrue(any(item.status == "MATCHED" for item in first.assessments))
        self.module.validate_match(first.model_dump(), self.module.job_facts(candidate()), PROFILE)
        unknown, _ = self.matcher({}).analyze(candidate(), [])
        self.assertTrue(all(item.status == "UNKNOWN" for item in unknown.assessments))
        self.request.assert_not_called()
        self.env.assert_not_called()
        self.assertEqual(self.ledger().summary()["calls"], 0)


if __name__ == "__main__":
    unittest.main()
