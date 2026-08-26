from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from tools.acm_agent.config import (
    CONFIG_VERSION,
    DEFAULT_CONFIG,
    Paths,
    load_config,
    save_config,
)


class ConfigTests(unittest.TestCase):
    def test_v12_upgrade_adds_repair_budget_delivery_and_failure_cooldown(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = Paths.for_root(Path(temporary))
            paths.ensure()
            old = json.loads(json.dumps(DEFAULT_CONFIG))
            old["version"] = 12
            old["ai"].pop("coaching_delivery_mode", None)
            old["ai"]["cache"].pop("flight_failure_cooldown_seconds", None)
            for budget in old["ai"]["policy"]["budgets"].values():
                budget.pop("max_validation_repairs", None)
            old["ai"]["policy"]["budgets"]["summary"]["max_requests"] = 3
            paths.config.write_text(json.dumps(old), encoding="utf-8")

            loaded = load_config(paths)
            self.assertEqual(loaded["version"], CONFIG_VERSION)
            self.assertEqual(loaded["ai"]["coaching_delivery_mode"], "resilient")
            self.assertEqual(
                loaded["ai"]["cache"]["flight_failure_cooldown_seconds"], 30
            )
            self.assertTrue(all(
                budget["max_validation_repairs"] == 1
                for budget in loaded["ai"]["policy"]["budgets"].values()
            ))
            self.assertEqual(
                loaded["ai"]["policy"]["budgets"]["summary"]["max_requests"], 3
            )
            self.assertEqual(load_config(paths), loaded)

    def test_v13_deepseek_auto_migrates_to_off_but_relay_auto_stays_default(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = Paths.for_root(Path(temporary))
            paths.ensure()
            old = json.loads(json.dumps(DEFAULT_CONFIG))
            old["version"] = 13
            relay_model = json.loads(json.dumps(
                old["ai"]["providers"]["deepseek"]["models"]["deepseek-v4-flash"]
            ))
            relay_model.update(
                evidence="declared", evidence_hash=None, verified_at=None,
                verified_capabilities=[], verified_reasoning_strengths=[],
            )
            old["ai"]["providers"]["relay"] = {
                "name": "Relay", "adapter": "openai_compatible",
                "base_url": "https://relay.example/v1", "credential_slot": "relay",
                "auth": {"type": "bearer"}, "enabled": True,
                "models": {"relay-model": relay_model},
            }
            old["ai"]["credential_slots"]["relay"] = {
                "provider_id": "relay", "origin": "https://relay.example",
                "auth": {"type": "bearer"}, "environment_variable": "",
            }
            old["ai"]["profiles"]["summary"].update(
                provider_id="relay", model="relay-model", reasoning_strength="auto"
            )
            old["ai"]["policy"]["fallbacks"]["recommendation"] = [{
                "provider_id": "deepseek", "model": "deepseek-v4-pro",
                "reasoning_strength": "auto",
            }]
            paths.config.write_text(json.dumps(old), encoding="utf-8")

            loaded = load_config(paths)

            self.assertEqual(loaded["version"], CONFIG_VERSION)
            self.assertEqual(
                loaded["ai"]["profiles"]["recommendation"]["reasoning_strength"], "off"
            )
            self.assertEqual(
                loaded["ai"]["profiles"]["summary"]["reasoning_strength"], "auto"
            )
            self.assertEqual(
                loaded["ai"]["policy"]["fallbacks"]["recommendation"][0]["reasoning_strength"],
                "off",
            )

    def test_v14_usd_hard_limits_migrate_once_to_cny(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = Paths.for_root(Path(temporary))
            paths.ensure()
            old = json.loads(json.dumps(DEFAULT_CONFIG))
            old["version"] = 14
            old["ai"]["policy"]["hard_limits"] = {
                "daily_usd": 1.25,
                "monthly_usd": 10.0,
            }
            paths.config.write_text(json.dumps(old), encoding="utf-8")

            loaded = load_config(paths)

            self.assertEqual(loaded["version"], CONFIG_VERSION)
            self.assertEqual(
                loaded["ai"]["policy"]["hard_limits"],
                {"daily_cny": 9.0, "monthly_cny": 72.0},
            )
            self.assertEqual(load_config(paths), loaded)

    def test_config_import_does_not_load_provider_transport(self) -> None:
        repository = Path(__file__).resolve().parents[1]
        completed = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import sys; import tools.acm_agent.config; "
                    "assert 'tools.acm_agent.deepseek' not in sys.modules"
                ),
            ],
            cwd=repository,
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_paths_stay_under_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = Paths.for_root(Path(directory))
            self.assertEqual(paths.state_dir.parent, Path(directory).resolve())
            self.assertEqual(paths.database.parent, paths.state_dir)

    def test_round_trip_is_utf8_and_atomic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = Paths.for_root(Path(directory))
            config = json.loads(json.dumps(DEFAULT_CONFIG))
            config["accounts"]["codeforces"]["handle"] = "tourist"
            save_config(paths, config)
            self.assertEqual(load_config(paths), config)
            self.assertEqual(list(paths.state_dir.glob("config-*.json")), [])

    def test_missing_required_config_has_actionable_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = Paths.for_root(Path(directory))
            with self.assertRaisesRegex(FileNotFoundError, "acm.ps1 init"):
                load_config(paths)

    def test_sensitive_ai_keys_are_rejected_inside_sequences_on_save_and_load(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = Paths.for_root(Path(directory))
            config = json.loads(json.dumps(DEFAULT_CONFIG))
            config["ai"]["extension"] = [{"token": "must-never-persist"}]
            with self.assertRaisesRegex(ValueError, r"ai\.extension\[0\]\.token"):
                save_config(paths, config)
            self.assertFalse(paths.config.exists())

            paths.ensure()
            paths.config.write_text(json.dumps(config), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, r"ai\.extension\[0\]\.token"):
                load_config(paths)

    def test_v8_migration_removes_retired_persistent_verify_settings(self) -> None:
        for old_version in (1, 2, 3, 4, 5, 6, 7):
            with self.subTest(old_version=old_version), tempfile.TemporaryDirectory() as directory:
                paths = Paths.for_root(Path(directory))
                paths.ensure()
                paths.config.write_text(
                    json.dumps(
                        {
                            "version": old_version,
                            "accounts": {},
                            "ai": {
                                "coaching_model": "deepseek-v4-pro",
                                "validation_model": "deepseek-v4-flash",
                                "validation_thinking": True,
                                "validation_reasoning_effort": "max",
                                "stress_prepare_timeout_seconds": 300,
                                "stress_generation_mode": "cold",
                            },
                        }
                    ),
                    encoding="utf-8",
                )

                config = load_config(paths)

                self.assertEqual(CONFIG_VERSION, 15)
                self.assertEqual(config["version"], CONFIG_VERSION)
                for retired_key in (
                    "validation_model",
                    "validation_thinking",
                    "validation_reasoning_effort",
                    "stress_prepare_timeout_seconds",
                    "stress_generation_mode",
                ):
                    self.assertNotIn(retired_key, config["ai"])
                self.assertEqual(config["ai"]["coaching_model"], "deepseek-v4-pro")
                self.assertEqual(json.loads(paths.config.read_text())["version"], CONFIG_VERSION)

    def test_v9_profiles_migrate_to_reasoning_strength_and_keep_legacy_projection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = Paths.for_root(Path(directory))
            paths.ensure()
            old = json.loads(json.dumps(DEFAULT_CONFIG))
            old["version"] = 9
            strengths = {
                "recommendation": (False, "max", "off"),
                "plan_organize": (True, "high", "medium"),
                "plan_generate": (True, "max", "high"),
                "coaching": (True, "high", "medium"),
                "patch": (False, "high", "off"),
                "summary": (True, "max", "high"),
            }
            for profile_id, (thinking, effort, _) in strengths.items():
                profile = old["ai"]["profiles"][profile_id]
                profile.pop("reasoning_strength", None)
                profile["thinking"] = thinking
                profile["reasoning_effort"] = effort
            paths.config.write_text(json.dumps(old), encoding="utf-8")

            config = load_config(paths)

            self.assertEqual(config["version"], CONFIG_VERSION)
            for profile_id, (_, _, expected) in strengths.items():
                profile = config["ai"]["profiles"][profile_id]
                self.assertEqual(profile["reasoning_strength"], expected)
                self.assertEqual(profile["thinking"], expected not in {"auto", "off"})
                self.assertEqual(
                    profile["reasoning_effort"],
                    "max" if expected == "high" else "high",
                )

    def test_v9_relay_reasoning_migration_preserves_provider_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = Paths.for_root(Path(directory))
            paths.ensure()
            old = json.loads(json.dumps(DEFAULT_CONFIG))
            old["version"] = 9
            deepseek_model = old["ai"]["providers"]["deepseek"]["models"][
                "deepseek-v4-flash"
            ]
            deepseek_model["evidence"] = "declared"
            deepseek_model["verified_capabilities"] = []
            deepseek_model["verified_reasoning_strengths"] = []
            old["ai"]["providers"]["relay"] = {
                "name": "Relay",
                "adapter": "openai_compatible",
                "base_url": "https://relay.example/v1",
                "credential_slot": "relay",
                "auth": {"type": "bearer"},
                "enabled": True,
                "models": {"shared-model": deepseek_model},
            }
            old["ai"]["credential_slots"]["relay"] = {
                "provider_id": "relay",
                "origin": "https://relay.example",
                "auth": {"type": "bearer"},
                "environment_variable": "",
            }
            for profile in old["ai"]["profiles"].values():
                profile.update(
                    provider_id="relay",
                    model="shared-model",
                    thinking=True,
                    reasoning_effort="high",
                )
                profile.pop("reasoning_strength", None)
            paths.config.write_text(json.dumps(old), encoding="utf-8")

            config = load_config(paths)

            self.assertIn("relay", config["ai"]["providers"])
            for profile in config["ai"]["profiles"].values():
                self.assertEqual(profile["provider_id"], "relay")
                self.assertEqual(profile["model"], "shared-model")
                self.assertEqual(profile["reasoning_strength"], "medium")


if __name__ == "__main__":
    unittest.main()
