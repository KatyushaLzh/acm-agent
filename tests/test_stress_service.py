from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

from tools.acm_agent.deepseek import DeepSeekClient
from tools.acm_agent.service import AcmService, STRESS_AI_REQUEST_TIMEOUT_SECONDS
from tools.acm_agent.storage import Database


class KeyClient:
    key_detected = True


class FakeCoordinator:
    def __init__(self):
        self.started = []

    def start(self, **kwargs):
        self.started.append(kwargs)
        return {
            "ok": True,
            "run": {"id": "run-1", "status": "pending"},
            "bundle": {"id": "bundle-1", "artifacts": []},
            "usage": {"total_tokens": 12},
        }

    def status(self):
        return {
            "ok": True,
            "sandbox": {"available": True, "backend": "fake", "reason": ""},
            "source_order": [],
            "active_run": None,
        }


class StressServiceTests(unittest.TestCase):
    def test_stress_client_can_use_long_request_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            service = AcmService(Path(temp))
            service._deepseek_api_key = "test-only-key"
            client = service._deepseek_client(
                timeout=STRESS_AI_REQUEST_TIMEOUT_SECONDS
            )
            self.assertIsInstance(client, DeepSeekClient)
            self.assertEqual(client.timeout, 300.0)

    def test_ai_stress_uses_validation_settings_and_redacted_audit(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            client = KeyClient()
            service = AcmService(
                root,
                deepseek_client_factory=lambda: client,
                problem_context_fetcher=lambda ref: (
                    "题面正文与约束",
                    f"https://example.test/{ref.problem_id}",
                ),
            )
            service.setup("private-handle", "4242", skip_validate=True)
            service.ai_settings(
                validation_model="deepseek-v4-pro",
                validation_thinking=True,
                validation_reasoning_effort="max",
            )
            service.start("CF1A")
            fake = FakeCoordinator()
            service._stress = fake

            result = service.ai_stress_start(
                "CF1A",
                seed=42,
                generate_generator=True,
                generate_brute=True,
                prepare_reference=True,
                large_profile=False,
            )
            self.assertEqual(result["ai_run_id"], result["ai_run_id"])
            sent = fake.started[0]
            self.assertEqual(sent["model_settings"]["model"], "deepseek-v4-pro")
            self.assertFalse(sent["model_settings"]["thinking"])
            self.assertEqual(sent["model_settings"]["reasoning_effort"], "max")
            self.assertEqual(sent["preparation_timeout_seconds"], 600)
            self.assertFalse(sent["force_regenerate"])
            self.assertEqual(sent["cache_mode"], "reuse")
            self.assertEqual(sent["generation_mode"], "hybrid")
            self.assertEqual(sent["statement"], "题面正文与约束")
            self.assertFalse(sent["include_large"])
            with Database(service.paths.database) as db:
                row = db.connection.execute(
                    "SELECT request_summary_json,usage_json,preparation_meta_json FROM ai_runs WHERE id=?",
                    (result["ai_run_id"],),
                ).fetchone()
            summary = json.loads(row["request_summary_json"])
            serialized = json.dumps(summary, ensure_ascii=False)
            self.assertFalse(summary["contains_user_source"])
            self.assertFalse(summary["large"])
            self.assertEqual(summary["profile_version"], 2)
            self.assertEqual(summary["generation_mode"], "hybrid")
            self.assertNotIn("private-handle", serialized)
            self.assertNotIn("4242", serialized)
            self.assertNotIn(str(root), serialized)
            self.assertEqual(json.loads(row["usage_json"])["total_tokens"], 12)
            self.assertEqual(
                json.loads(row["preparation_meta_json"])["generation_mode"],
                "hybrid",
            )

    def test_generation_mode_reads_config_and_explicit_override(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            service = AcmService(
                root,
                deepseek_client_factory=KeyClient,
                problem_context_fetcher=lambda ref: ("statement", "https://example.test"),
            )
            service.setup("handle", "4242", skip_validate=True)
            service.start("CF1A")
            config = json.loads(service.paths.config.read_text(encoding="utf-8"))
            config["ai"]["stress_generation_mode"] = "fast"
            service.paths.config.write_text(json.dumps(config), encoding="utf-8")
            fake = FakeCoordinator()
            service._stress = fake

            default_result = service.ai_stress_start("CF1A")

            self.assertEqual(default_result["generation_mode"], "fast")
            self.assertEqual(fake.started[0]["generation_mode"], "fast")
            with Database(service.paths.database) as db:
                db.update_ai_run(
                    default_result["ai_run_id"],
                    status="failed",
                    completed_at="2026-08-05T00:00:00+00:00",
                )
            explicit_result = service.ai_stress_start(
                "CF1A", generation_mode="full_thinking"
            )
            self.assertEqual(explicit_result["generation_mode"], "full_thinking")
            self.assertEqual(fake.started[1]["generation_mode"], "full_thinking")
            status = service.ai_stress_status()
            self.assertEqual(status["settings"]["stress_generation_mode"], "fast")
            self.assertEqual(
                status["generation_modes"], ["fast", "hybrid", "full_thinking"]
            )

            with self.assertRaisesRegex(ValueError, "stress_generation_mode"):
                service.ai_stress_start("CF1A", generation_mode="full-thinking")

    def test_custom_timeout_is_forwarded_and_active_setup_blocks_before_client(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            calls = []

            def forbidden_client():
                calls.append("client")
                raise AssertionError("provider client must not be created")

            service = AcmService(
                root,
                deepseek_client_factory=forbidden_client,
                problem_context_fetcher=lambda ref: ("statement", "https://example.test"),
            )
            service.setup("handle", "4242", skip_validate=True)
            service.start("CF1A")
            with Database(service.paths.database) as db:
                db.acquire_stress_setup_slot(
                    "already-running",
                    model="deepseek-v4-flash",
                    request_summary={},
                    preparation_meta={"owner_pid": os.getpid()},
                )
            second = AcmService(
                root,
                deepseek_client_factory=forbidden_client,
                problem_context_fetcher=lambda ref: ("statement", "https://example.test"),
            )
            with self.assertRaises(Exception) as caught:
                second.ai_stress_start(
                    "CF1A",
                    preparation_timeout_seconds=1800,
                    force_regenerate=True,
                )
            self.assertEqual(getattr(caught.exception, "code", ""), "stress_setup_active")
            self.assertEqual(calls, [])

    def test_cache_mode_normalizes_legacy_force_and_rejects_conflicts(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            service = AcmService(
                root,
                deepseek_client_factory=KeyClient,
                problem_context_fetcher=lambda ref: ("statement", "https://example.test"),
            )
            service.setup("handle", "4242", skip_validate=True)
            service.start("CF1A")
            fake = FakeCoordinator()
            service._stress = fake

            forced = service.ai_stress_start("CF1A", force_regenerate=True)
            self.assertEqual(forced["cache_mode"], "cold")
            self.assertEqual(fake.started[-1]["cache_mode"], "cold")
            with Database(service.paths.database) as db:
                db.update_ai_run(
                    forced["ai_run_id"], status="failed",
                    completed_at="2026-08-06T00:00:00+00:00",
                )
            with self.assertRaisesRegex(ValueError, "只能与 cache_mode=cold"):
                service.ai_stress_start(
                    "CF1A", force_regenerate=True, cache_mode="reuse"
                )


if __name__ == "__main__":
    unittest.main()
