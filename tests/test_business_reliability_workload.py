from __future__ import annotations

from contextlib import redirect_stderr
import hashlib
import io
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from urllib.request import Request

from tools.acm_agent.business_reliability_workload import (
    BusinessReliabilityRunner, CASES_PER_PROFILE, FIXTURES, MODEL, PROFILES,
    _plan_ids, _task_key, _source_files, _usage_completeness, _validation_messages,
    full_outcome, main, workload_manifest, write_report,
)
from tools.acm_agent.cache_workload import CappedProviderClient, _safe_error_code
from tools.acm_agent.config import load_config
from tools.acm_agent.storage import Database


class NoNetworkProvider:
    key_detected = True
    provider_request_count = 0

    def __getattr__(self, name):
        raise AssertionError("offline test attempted provider access: " + name)


GOOD = {"provider_outcome": "succeeded", "artifact_outcome": "valid",
        "business_outcome": "complete", "usable": True, "apply_ready": True,
        "degraded": False, "repair_attempts": 0}


class BusinessReliabilityWorkloadTests(unittest.TestCase):
    def test_known_error_codes_are_readable_but_arbitrary_values_stay_hashed(self):
        for code in ("response_incomplete", "invalid_ai_ranking", "summary_entry_invalid", "invalid_json_output"):
            self.assertEqual(_safe_error_code(code), code)
        self.assertTrue(_safe_error_code("private_marker_1234").startswith("unclassified_sha256_"))

    def test_fixed_validator_diagnostics_exclude_user_content_and_headers(self):
        value = {"error": {"message": "AI 推荐数量不足", "protocol_details": {
            "validation_errors": ["required field is empty: correctness", "unknown entry keys: private-secret"],
            "headers": {"Authorization": "Bearer private-secret"}}}}
        self.assertEqual(set(_validation_messages(value)), {"AI 推荐数量不足", "required field is empty: correctness"})
        self.assertEqual(_validation_messages({"error": {"message": "AI 推荐数量不足 private-secret"}}), [])
        self.assertEqual(_validation_messages({"validator_messages": ["AI 推荐数量不足"]}), ["AI 推荐数量不足"])

    def test_unknown_usage_is_not_reported_as_zero(self):
        facts = [{"usage": {"input_tokens": 0, "output_tokens": 2, "total_tokens": 2, "cache_read_tokens": 0}},
                 {"usage": {}}, {"usage": {"input_tokens": 5, "output_tokens": -1, "total_tokens": None}}]
        report = _usage_completeness(facts)
        self.assertEqual(report["complete_core_usage_legs"], 1)
        self.assertEqual(report["unknown_or_partial_core_usage_legs"], 2)
        self.assertEqual(report["fields"]["input_tokens"]["known_sum"], 5)
        self.assertIsNone(report["fields"]["input_tokens"]["complete_sum"])
        self.assertEqual(report["fields"]["input_tokens"]["unknown_legs"], 1)
        self.assertEqual(report["fields"]["output_tokens"]["invalid_legs"], 1)
        unknown = _usage_completeness([{"usage": {}}])["fields"]["input_tokens"]
        self.assertIsNone(unknown["known_sum"])
        self.assertIsNone(unknown["complete_sum"])
        known_zero = _usage_completeness([{"usage": {"input_tokens": 0}}])["fields"]["input_tokens"]
        self.assertEqual(known_zero["known_sum"], 0)
        self.assertEqual(known_zero["complete_sum"], 0)

    def test_manifest_precommits_sixty_distinct_cases(self):
        manifest = workload_manifest()
        self.assertEqual(len(manifest["cases"]), 60)
        self.assertEqual(manifest, workload_manifest())
        for profile in PROFILES:
            cases = [c for c in manifest["cases"] if c["profile"] == profile]
            self.assertEqual(len(cases), CASES_PER_PROFILE)
            self.assertEqual(len({c["definition_sha256"] for c in cases}), 10)
        encoded = json.dumps(manifest)
        self.assertNotIn("#include", encoded)
        self.assertNotIn("correct_body", encoded)

    def test_full_success_excludes_partial_cache_hybrid_and_missing_outcomes(self):
        self.assertTrue(full_outcome(GOOD))
        self.assertTrue(full_outcome({**GOOD, "artifact_outcome": "repaired"}))
        for key, bad in (("business_outcome", "hybrid"), ("business_outcome", "cache"),
                         ("business_outcome", "partial"), ("artifact_outcome", "partial"),
                         ("provider_outcome", "not_called"), ("usable", False), ("degraded", True)):
            self.assertFalse(full_outcome({**GOOD, key: bad}), (key, bad))
        self.assertFalse(full_outcome({}))
        for value in (None, "1", 1.0, [], {}):
            self.assertFalse(full_outcome({**GOOD, "usable": value}))
        for value in (None, "0", 0.0, [], {}):
            self.assertFalse(full_outcome({**GOOD, "degraded": value}))

    def test_source_gate_covers_nested_runtime_modules(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            file = root / "tools/acm_agent/subsystem/nested.py"
            file.parent.mkdir(parents=True)
            file.write_text("x = 1")
            self.assertIn("tools/acm_agent/subsystem/nested.py", _source_files(root).values())

    def test_wire_evidence_observes_serialized_http_without_messages_or_credentials(self):
        calls = []
        transport = lambda request, timeout: calls.append(timeout)
        client = SimpleNamespace(_transport=transport)
        runner = BusinessReliabilityRunner(".", CappedProviderClient(client))
        runner._attach_wire_recorder()
        try:
            for url, payload in (
                ("https://api.deepseek.com/chat/completions", {"model": MODEL, "thinking": {"type": "enabled"}, "reasoning_effort": "high", "stream": True, "messages": [{"content": "secret fixture source"}]}),
                ("https://api.deepseek.com/responses", {"model": MODEL, "reasoning": {"effort": "high"}, "input": "secret fixture chat"}),
                ("https://api.deepseek.com/chat/completions", {"model": MODEL, "thinking": {"type": "disabled"}}),
            ):
                request = Request(url, data=json.dumps(payload).encode(), headers={"Authorization": "Bearer secret-key"})
                client._transport(request, 7)
            self.assertEqual(calls, [7, 7, 7])
            self.assertEqual([r["medium_wire_valid"] for r in runner._wire_records], [True, True, False])
            serialized = json.dumps(runner._wire_records)
            self.assertNotIn("secret", serialized)
            self.assertNotIn("Authorization", serialized)
            self.assertNotIn("messages", serialized)
        finally:
            runner._detach_wire_recorder()
        self.assertIs(client._transport, transport)

    def test_plan_key_normalizes_display_ids(self):
        self.assertEqual(_task_key({"platform": "codeforces", "problem_id": "CF101A"}), "codeforces:101A")

    def test_isolation_seed_and_configuration_never_access_live_database(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / ".acm").mkdir()
            sentinel = root / ".acm/state.db"
            sentinel.write_bytes(b"this is deliberately not a sqlite database")
            before = hashlib.sha256(sentinel.read_bytes()).hexdigest()
            runner = BusinessReliabilityRunner(root, CappedProviderClient(NoNetworkProvider(), limit=210))
            try:
                workspace = runner._prepare_case("summary", 8)
                self.assertNotEqual(workspace.service.paths.database, sentinel)
                config = load_config(workspace.service.paths)
                self.assertEqual(config["ai"]["cache"]["exact_profiles"], [])
                self.assertFalse(config["ai"]["cache"]["semantic_enabled"])
                for profile in PROFILES:
                    self.assertEqual(config["ai"]["profiles"][profile]["model"], MODEL)
                    self.assertEqual(config["ai"]["profiles"][profile]["reasoning_strength"], "medium")
                    self.assertEqual(config["ai"]["profiles"][profile]["reasoning_effort"], "high")
                    self.assertEqual(config["ai"]["policy"]["fallbacks"][profile], [])
                    self.assertEqual(config["ai"]["policy"]["budgets"][profile]["max_validation_repairs"], 1)
                self.assertEqual(hashlib.sha256(sentinel.read_bytes()).hexdigest(), before)
            finally:
                for workspace in runner._workspaces:
                    workspace.close()

    @unittest.skipUnless(shutil.which("g++"), "g++ is required for fixture compile gates")
    def test_patch_gate_checks_boundaries_and_rejects_original_bugs(self):
        with tempfile.TemporaryDirectory() as temp:
            runner = BusinessReliabilityRunner(temp, CappedProviderClient(NoNetworkProvider(), limit=210))
            try:
                workspace = runner._prepare_case("patch", 0)
                for index in (0, 4, 8, 9):
                    with self.subTest(fixture=FIXTURES[index].key):
                        self.assertTrue(runner._compile_fixture(workspace, {"candidate_code": FIXTURES[index].source(correct=True)}, index))
                        self.assertFalse(runner._compile_fixture(workspace, {"candidate_code": FIXTURES[index].source(correct=False)}, index))
            finally:
                for workspace in runner._workspaces:
                    workspace.close()

    def test_plan_gate_rejects_wrong_count_order_and_out_of_pool_ids(self):
        runner = BusinessReliabilityRunner(".", CappedProviderClient(NoNetworkProvider()))
        tasks = [{"platform": "codeforces", "problem_id": "CF" + key} for key in _plan_ids(2)]
        value = {"ok": True, "plan": {"stages": [{"tasks": tasks}]}}
        self.assertTrue(runner._local_gate(None, "plan_generate", 2, value))
        for bad in (tasks[:1], list(reversed(tasks)), tasks + tasks[:1]):
            self.assertFalse(runner._local_gate(None, "plan_generate", 2, {"ok": True, "plan": {"stages": [{"tasks": bad}]}}))

    def test_plan_postprocess_imports_and_reads_back_file_and_database(self):
        with tempfile.TemporaryDirectory() as temp:
            runner = BusinessReliabilityRunner(temp, CappedProviderClient(NoNetworkProvider()))
            try:
                workspace = runner._prepare_case("plan_generate", 0)
                plan = {"schema_version": 2, "plan_id": "business-fixture", "title": "Fixture", "description": "",
                        "schedule_mode": "progressive", "stages": [{"stage_key": "s1", "topic": "DP", "kind": "practice",
                        "tasks": [{"task_key": "t1", "platform": "codeforces", "problem_id": "CF101A", "level": "A", "tags": ["dp"]}]}]}
                self.assertTrue(runner._apply_gate(workspace, "plan_generate", 0, {"ok": True, "plan": plan}))
            finally:
                for workspace in runner._workspaces:
                    workspace.close()

    def test_summary_postprocess_applies_and_reads_back_registered_target(self):
        with tempfile.TemporaryDirectory() as temp:
            runner = BusinessReliabilityRunner(temp, CappedProviderClient(NoNetworkProvider()))
            try:
                workspace = runner._prepare_case("summary", 0)
                with Database(workspace.service.paths.database) as db:
                    target = db.markdown_summary_target(workspace.target_id)
                    path = Path(target["path"])
                    baseline = path.read_bytes()
                    db.create_markdown_summary_proposal("local-proposal", attempt_id=workspace.attempt_id,
                        target_path=path, target_existed=True, schema=json.loads(target["schema_json"]),
                        entry={"topic": "Prefix sums", "fields": {}, "confidence": 1.0},
                        candidate_bytes=baseline + b"\n<!-- deterministic offline apply fixture -->\n", diff_text="fixture",
                        target_id=workspace.target_id, target_revision=target["revision"],
                        baseline_hash=hashlib.sha256(baseline).hexdigest(), schema_hash=target["schema_hash"])
                self.assertTrue(runner._apply_gate(workspace, "summary", 0, {"proposal": {"proposal_id": "local-proposal", "revision": 1}}))
            finally:
                for workspace in runner._workspaces:
                    workspace.close()

    def test_failure_artifact_keeps_synthetic_output_but_not_sensitive_metadata(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp) / "report"
            directory.mkdir()
            runner = BusinessReliabilityRunner(temp, CappedProviderClient(NoNetworkProvider()), report_directory=directory)
            try:
                workspace = runner._prepare_case("coaching", 0)
                value = {"_private_output": {"assistant_text": "synthetic model output"},
                         "headers": {"Authorization": "secret-value"}, "credential_slot": "secret-value",
                         "error": {"message": "secret-value"}}
                relative = runner._save_failure_artifact(workspace, value, {"outcome": GOOD, "error_codes": []})
                artifact = (directory / relative).read_text()
                self.assertIn("synthetic model output", artifact)
                self.assertNotIn("secret-value", artifact)
                self.assertNotIn("Authorization", artifact)
            finally:
                for workspace in runner._workspaces:
                    workspace.close()

    @unittest.skipUnless(shutil.which("g++"), "g++ is required for patch apply")
    def test_patch_postprocess_applies_real_source_and_validates_outputs(self):
        with tempfile.TemporaryDirectory() as temp:
            runner = BusinessReliabilityRunner(temp, CappedProviderClient(NoNetworkProvider()))
            try:
                workspace = runner._prepare_case("patch", 0)
                with Database(workspace.service.paths.database) as db:
                    row = db.connection.execute("SELECT path FROM local_files WHERE problem_id='100A'").fetchone()
                    path = runner._owned_file(workspace, row["path"])
                    db.create_ai_patch_proposal("local-patch", platform="codeforces", problem_id="100A",
                        source_path=path, baseline_hash=hashlib.sha256(path.read_bytes()).hexdigest(),
                        candidate_code=FIXTURES[0].source(correct=True), diff_text="fixture", attempt_id=workspace.attempt_id)
                self.assertTrue(runner._apply_gate(workspace, "patch", 0, {"proposal_id": "local-patch"}))
            finally:
                for workspace in runner._workspaces:
                    workspace.close()

    def test_coaching_stream_requires_done_and_fixed_numerical_check(self):
        class Service:
            def ai_conversation_start(self, *args, **kwargs):
                return {"conversation_id": "fixture"}
            def ai_chat_stream(self, *args, **kwargs):
                self.options = kwargs
                yield {"event": "delta", "data": {"content": "边界解释。\nCHECK=0"}}
                yield {"event": "done", "data": {"outcome": GOOD}}
        class Workspace:
            service = Service()
        runner = BusinessReliabilityRunner(".", CappedProviderClient(NoNetworkProvider()))
        result = runner._coaching(Workspace, 0)
        self.assertTrue(result["ok"])
        self.assertTrue(result["local_correct"])
        self.assertEqual(Workspace.service.options["hint_level"], 3)
        self.assertEqual(Workspace.service.options["delivery_mode"], "low_latency")
        self.assertNotIn("content", result)

    def test_journal_resume_recovers_completed_and_refuses_uncertain_paid_case(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "run"
            source = {"critical_files": {"runner": {"sha256": "abc"}}}
            provider = CappedProviderClient(NoNetworkProvider(), limit=210)
            runner = BusinessReliabilityRunner(temp, provider, report_directory=path)
            runner._open_journal(workload_manifest(), source)
            record = {"case_id": "coaching-01", "profile": "coaching", "http_requests": 1}
            runner._journal({"event": "started", "case_id": "coaching-01"})
            runner._journal({"event": "completed", "record": record})
            resumed = BusinessReliabilityRunner(temp, provider, report_directory=path, resume=True)
            resumed._open_journal(workload_manifest(), source)
            self.assertEqual(resumed.records, [record])
            runner._journal({"event": "started", "case_id": "coaching-02"})
            with self.assertRaisesRegex(RuntimeError, "uncertain_paid_case"):
                BusinessReliabilityRunner(temp, provider, report_directory=path, resume=True)._open_journal(workload_manifest(), source)

    def test_resume_rejects_changed_source_before_any_provider_call(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "run"
            provider = CappedProviderClient(NoNetworkProvider(), limit=210)
            runner = BusinessReliabilityRunner(temp, provider, report_directory=path)
            runner._open_journal(workload_manifest(), {"critical_files": {"runner": "before"}})
            with self.assertRaisesRegex(RuntimeError, "source_changed"):
                BusinessReliabilityRunner(temp, provider, report_directory=path, resume=True)._open_journal(workload_manifest(), {"critical_files": {"runner": "after"}})
            self.assertEqual(provider.provider_request_count, 0)

    def test_report_passes_only_when_every_profile_has_nine_and_accounting_matches(self):
        class OfflineRunner(BusinessReliabilityRunner):
            def _attach_wire_recorder(self):
                pass
            def _run_case(self, profile, index):
                record = {"case_id": f"{profile}-{index + 1:02d}", "profile": profile,
                          "complete": index != 9, "http_requests": 1, "http_accounted": True,
                          "routing_valid": True, "wire_medium_verified": True, "no_local_cache_hit": True, "provider_legs": [{"provider_requests": 1, "usage": {}}], "latency_ms": 1}
                self.provider._client.provider_request_count += 1
                self.records.append(record)
                return record
        with tempfile.TemporaryDirectory() as temp, patch("tools.acm_agent.business_reliability_workload._source_evidence", return_value={"critical_files": {}}), patch("tools.acm_agent.business_reliability_workload.shutil.which", return_value="compiler"):
            runner = OfflineRunner(temp, CappedProviderClient(NoNetworkProvider(), limit=210))
            report = runner.run()
            self.assertTrue(report["passed"])
            self.assertEqual(report["http_requests"], 60)
            self.assertTrue(all(p["complete"] == 9 for p in report["profiles"].values()))
            path = write_report(Path(temp), report)
            self.assertEqual(json.loads(path.read_text())["workload_sha256"], report["workload_sha256"])

    def test_missing_http_leg_fails_acceptance_even_with_perfect_results(self):
        class OfflineRunner(BusinessReliabilityRunner):
            def _attach_wire_recorder(self):
                pass
            def _run_case(self, profile, index):
                self.provider._client.provider_request_count += 1
                record = {"case_id": f"{profile}-{index + 1:02d}", "profile": profile,
                          "complete": True, "http_requests": 1, "http_accounted": False,
                          "routing_valid": True, "wire_medium_verified": True, "no_local_cache_hit": True, "provider_legs": [], "latency_ms": 1}
                self.records.append(record)
        with tempfile.TemporaryDirectory() as temp, patch("tools.acm_agent.business_reliability_workload._source_evidence", return_value={"critical_files": {}}), patch("tools.acm_agent.business_reliability_workload.shutil.which", return_value="compiler"):
            report = OfflineRunner(temp, CappedProviderClient(NoNetworkProvider(), limit=210)).run()
            self.assertFalse(report["passed"])
            self.assertFalse(report["gates"]["every_http_accounted"])
            self.assertEqual(report["logical_requests"], 60)
            self.assertIsNone(report["usage_from_legs"]["input_tokens"])

    def test_cli_requires_explicit_live_flag_before_credentials(self):
        with patch("tools.acm_agent.business_reliability_workload._load_live_provider") as loader, redirect_stderr(io.StringIO()):
            with self.assertRaises(SystemExit):
                main([])
            loader.assert_not_called()


if __name__ == "__main__":
    unittest.main()
