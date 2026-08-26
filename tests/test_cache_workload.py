from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from tools.acm_agent.cache_workload import (
    CappedProviderClient,
    PROVIDER_LIMIT,
    ProviderRequestLimitExceeded,
    Stage4WorkloadRunner,
    WORKLOAD_PATH,
    _report_count_consistency,
    _source_evidence,
    write_report,
)
from tools.acm_agent.provider_governance import _accepts_keyword


class _Provider:
    key_detected = True

    def capabilities(self, model):
        return {"model": model}

    def test_connection(self, model):
        return {"model": model}

    def chat(self, messages, **options):
        return (messages, options)

    def chat_json(self, messages, **options):
        return (messages, options)

    def structured(self, messages, **options):
        return (messages, options)

    def stream_chat(self, messages, **options):
        return iter((messages, options))


class CacheWorkloadTests(unittest.TestCase):
    def test_manifest_uses_exactly_twenty_requests_for_reliability_and_cache(self) -> None:
        workload = json.loads(WORKLOAD_PATH.read_text(encoding="utf-8"))
        self.assertEqual(workload["safety"]["provider_request_hard_limit"], 20)
        self.assertEqual(workload["safety"]["provider_request_target"], 20)
        self.assertEqual(workload["safety"]["business_base_provider_requests"], 12)
        self.assertEqual(
            workload["safety"]["adaptive_retry_repair_or_probe_budget"], 8
        )
        self.assertEqual(
            workload["phases"]["correctness_cache_disabled"]["base_provider_requests"],
            8,
        )
        self.assertEqual(workload["phases"]["exact_cache"]["base_provider_requests"], 3)
        self.assertEqual(
            workload["phases"]["singleflight"]["expected_provider_requests"], 1
        )
        self.assertEqual(workload["acceptance"]["logical_requests"], 16)
        self.assertEqual(workload["acceptance"]["provider_requests_exactly"], 20)
        self.assertEqual(
            workload["phases"]["provider_kv_probe"]["maximum_provider_requests"],
            8,
        )
        self.assertFalse(workload["safety"]["fallbacks"])
        self.assertEqual(workload["safety"]["transport_retries_per_logical_request"], 1)
        self.assertEqual(workload["safety"]["validation_repairs_per_logical_request"], 1)

    def test_cap_blocks_before_the_twenty_first_call(self) -> None:
        provider = CappedProviderClient(_Provider(), limit=PROVIDER_LIMIT)
        for _ in range(PROVIDER_LIMIT):
            provider.chat([], model="deepseek-v4-flash")
        self.assertEqual(provider.provider_request_count, PROVIDER_LIMIT)
        with self.assertRaises(ProviderRequestLimitExceeded):
            provider.structured(
                [],
                model="deepseek-v4-flash",
                json_schema={"type": "object"},
                schema_name="test",
            )
        self.assertEqual(provider.provider_request_count, PROVIDER_LIMIT)

    def test_cap_wrapper_preserves_underlying_governance_signatures(self) -> None:
        provider = CappedProviderClient(_Provider())
        self.assertTrue(_accepts_keyword(provider.structured, "request_retries"))
        self.assertFalse(_accepts_keyword(provider.structured, "json_retries"))
        self.assertTrue(_accepts_keyword(provider.chat_json, "json_retries"))
        self.assertFalse(_accepts_keyword(provider.chat, "json_retries"))
        self.assertFalse(_accepts_keyword(provider.stream_chat, "request_retries"))

    def test_concurrent_records_attribute_singleflight_request_only_to_leader(self) -> None:
        class CountingProvider:
            provider_request_count = 0

        provider = CountingProvider()
        runner = Stage4WorkloadRunner(Path.cwd(), provider)  # type: ignore[arg-type]
        barrier = threading.Barrier(2)

        def value(index: int):
            barrier.wait(timeout=5)
            if index == 1:
                provider.provider_request_count += 1
                time.sleep(0.05)
                return {
                    "ok": True,
                    "usage": {"provider_requests": 1, "input_tokens": 10},
                    "local_cache": {"status": "miss"},
                    "outcome": {"provider_outcome": "succeeded"},
                }
            time.sleep(0.02)
            return {
                "ok": True,
                "usage": {"provider_requests": 0},
                "local_cache": {"status": "coalesced"},
                "outcome": {"provider_outcome": "not_called"},
            }

        def record(index: int):
            return runner._record(
                "singleflight", "plan_organize", index,
                lambda: value(index), lambda result: bool(result["ok"]),
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(record, (1, 2)))

        by_index = {row["logical_index"]: row for row in runner.records}
        self.assertEqual(by_index[1]["provider_requests"], 1)
        self.assertEqual(by_index[2]["provider_requests"], 0)
        self.assertEqual(sum(row["provider_requests"] for row in runner.records), 1)
        self.assertEqual(
            {row["provider_request_window_delta"] for row in runner.records}, {1}
        )

    def test_provider_evidence_counts_are_phase_profile_and_attempt_consistent(self) -> None:
        evidence = [
            {
                "evidence_kind": "business_provider_leg",
                "phase": "correctness-cache-disabled",
                "profile": "coaching",
                "status": "complete",
                "purpose": "initial",
                "provider_requests": 1,
                "usage": {"provider_requests": 1, "input_tokens": 10},
                "estimated_cost": {"status": "known", "currency": "CNY", "amount": 0.1},
            },
            {
                "evidence_kind": "business_provider_leg",
                "phase": "singleflight",
                "profile": "plan_organize",
                "status": "complete",
                "purpose": "initial",
                "provider_requests": 1,
                "usage": {"provider_requests": 1, "input_tokens": 20},
                "estimated_cost": {"status": "known", "currency": "CNY", "amount": 0.2},
            },
            {
                "evidence_kind": "provider_cache_probe",
                "phase": "provider-kv-probe",
                "profile": "provider_kv_probe",
                "status": "failed",
                "purpose": "provider_cache_probe",
                "provider_requests": 2,
                "usage": {"provider_requests": 2},
                "estimated_cost": {"status": "unknown"},
            },
        ]
        counts = Stage4WorkloadRunner._provider_leg_counts(evidence)
        singleflight = Stage4WorkloadRunner._phase_provider_facts(
            "singleflight", evidence
        )
        probes = Stage4WorkloadRunner._phase_provider_facts(
            "provider-kv-probe", evidence
        )
        profiles = Stage4WorkloadRunner._provider_requests_by_profile(evidence)
        self.assertEqual(counts["total"], 4)
        self.assertEqual(counts["succeeded"], 2)
        self.assertEqual(counts["failed"], 2)
        self.assertEqual(singleflight["provider_requests"], 1)
        self.assertEqual(probes["provider_requests"], 2)
        self.assertEqual(profiles["coaching"], 1)
        self.assertEqual(profiles["plan_organize"], 1)
        self.assertEqual(
            counts["total"],
            sum(item["provider_requests"] for item in evidence),
        )
        consistency = _report_count_consistency(
            [
                {"provider_requests": 1},
                {"provider_requests": 1},
                {"provider_requests": 0},
            ],
            evidence,
            top_level_provider_requests=4,
        )
        self.assertTrue(consistency["consistent"])
        self.assertEqual(consistency["provider_leg_evidence_requests"], 4)
        self.assertEqual(consistency["run_attributed_provider_requests"], 2)
        self.assertEqual(consistency["business_provider_leg_requests"], 2)
        self.assertEqual(consistency["probe_provider_requests"], 2)
        self.assertEqual(
            sum(consistency["phase_provider_requests"].values()), 4
        )

    def test_source_evidence_fingerprints_dirty_worktree_without_content_or_paths(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            subprocess.run(["git", "init", "-q"], cwd=root, check=True)
            subprocess.run(
                ["git", "config", "user.email", "stage4@example.invalid"],
                cwd=root,
                check=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Stage4 Test"], cwd=root, check=True
            )
            tracked = root / "runner.py"
            tracked.write_text("safe = True\n", encoding="utf-8")
            subprocess.run(["git", "add", "runner.py"], cwd=root, check=True)
            subprocess.run(["git", "commit", "-qm", "fixture"], cwd=root, check=True)
            tracked.write_text("safe = False\n", encoding="utf-8")
            secret_marker = "credential-and-prompt-must-not-leak"
            (root / "private.txt").write_text(secret_marker, encoding="utf-8")

            evidence = _source_evidence(
                root, critical_files={"runner": "runner.py"}
            )
            serialized = json.dumps(evidence, sort_keys=True)

        self.assertTrue(evidence["dirty"])
        self.assertTrue(evidence["tracked_dirty"])
        self.assertEqual(len(evidence["head"]), 40)
        self.assertEqual(len(evidence["tracked_diff_sha256"]), 64)
        self.assertEqual(len(evidence["tracked_tree_sha256"]), 64)
        self.assertEqual(len(evidence["worktree_sha256"]), 64)
        self.assertEqual(len(evidence["source_snapshot_sha256"]), 64)
        self.assertNotIn(secret_marker, serialized)
        self.assertNotIn("private.txt", serialized)
        self.assertNotIn(str(root), serialized)

    def test_report_writer_keeps_only_pre_sanitized_payload(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            report = {
                "workload_version": "test",
                "workload_sha256": "0" * 64,
                "provider": "deepseek",
                "model": "deepseek-v4-flash",
                "provider_request_target": 20,
                "provider_request_limit": 20,
                "configuration_sha256": "1" * 64,
                "git_hash": "2" * 40,
                "source_evidence": {
                    "source_snapshot_sha256": "3" * 64,
                    "dirty": True,
                    "tracked_diff_sha256": "4" * 64,
                },
                "stage_verified": True,
                "runs": [{"profile": "summary", "usage": {"input_tokens": 1}}],
            }
            directory = write_report(root, report)
            saved = json.loads((directory / "report.json").read_text(encoding="utf-8"))
            manifest = json.loads((directory / "workload.json").read_text(encoding="utf-8"))
        self.assertEqual(saved, report)
        self.assertEqual(set(manifest), {
            "workload_version",
            "workload_sha256",
            "provider",
            "model",
            "provider_request_target",
            "provider_request_limit",
            "configuration_sha256",
            "git_hash",
            "source_snapshot_sha256",
            "source_dirty",
            "tracked_diff_sha256",
        })


if __name__ == "__main__":
    unittest.main()
