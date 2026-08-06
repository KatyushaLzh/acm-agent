from __future__ import annotations

import json
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

    def test_v1_to_v3_validation_settings_inherit_coaching_choices(self) -> None:
        for old_version in (1, 2, 3):
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
                                "coaching_thinking": False,
                                "reasoning_effort": "max",
                            },
                        }
                    ),
                    encoding="utf-8",
                )

                config = load_config(paths)

                self.assertEqual(CONFIG_VERSION, 7)
                self.assertEqual(config["version"], 7)
                self.assertEqual(config["ai"]["validation_model"], "deepseek-v4-pro")
                self.assertFalse(config["ai"]["validation_thinking"])
                self.assertEqual(config["ai"]["validation_reasoning_effort"], "max")
                self.assertEqual(json.loads(paths.config.read_text())["version"], 7)

    def test_new_config_defaults_validation_to_flash(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = load_config(Paths.for_root(Path(directory)), required=False)
            self.assertEqual(config["ai"]["validation_model"], "deepseek-v4-flash")
            self.assertTrue(config["ai"]["validation_thinking"])
            self.assertEqual(config["ai"]["validation_reasoning_effort"], "high")
            self.assertEqual(config["ai"]["stress_prepare_timeout_seconds"], 600)
            self.assertEqual(config["ai"]["stress_generation_mode"], "hybrid")

    def test_v4_migration_adds_default_stress_prepare_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = Paths.for_root(Path(directory))
            paths.ensure()
            paths.config.write_text(
                json.dumps({"version": 4, "accounts": {}, "ai": {}}),
                encoding="utf-8",
            )

            config = load_config(paths)

            self.assertEqual(config["version"], 7)
            self.assertEqual(config["ai"]["stress_prepare_timeout_seconds"], 600)
            self.assertEqual(config["ai"]["stress_generation_mode"], "hybrid")

    def test_v5_migration_adds_default_stress_generation_mode(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = Paths.for_root(Path(directory))
            paths.ensure()
            config = json.loads(json.dumps(DEFAULT_CONFIG))
            config["version"] = 5
            config["ai"].pop("stress_generation_mode")
            paths.config.write_text(json.dumps(config), encoding="utf-8")

            loaded = load_config(paths)

            self.assertEqual(loaded["version"], 7)
            self.assertEqual(loaded["ai"]["stress_generation_mode"], "hybrid")

    def test_stress_generation_mode_accepts_enum_and_rejects_other_values(self) -> None:
        for value in ("fast", "hybrid", "full_thinking"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                paths = Paths.for_root(Path(directory))
                paths.ensure()
                config = json.loads(json.dumps(DEFAULT_CONFIG))
                config["ai"]["stress_generation_mode"] = value
                paths.config.write_text(json.dumps(config), encoding="utf-8")
                self.assertEqual(load_config(paths)["ai"]["stress_generation_mode"], value)

        for value in ("full-thinking", "thinking", "", None, True):
            with self.subTest(invalid=value), tempfile.TemporaryDirectory() as directory:
                paths = Paths.for_root(Path(directory))
                paths.ensure()
                config = json.loads(json.dumps(DEFAULT_CONFIG))
                config["ai"]["stress_generation_mode"] = value
                paths.config.write_text(json.dumps(config), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "stress_generation_mode"):
                    load_config(paths)

    def test_stress_prepare_timeout_rejects_non_integer_and_out_of_range(self) -> None:
        for value in (59, 1801, 300.5, True, "300"):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                paths = Paths.for_root(Path(directory))
                paths.ensure()
                config = json.loads(json.dumps(DEFAULT_CONFIG))
                config["ai"]["stress_prepare_timeout_seconds"] = value
                paths.config.write_text(json.dumps(config), encoding="utf-8")
                with self.assertRaisesRegex(ValueError, "stress_prepare_timeout_seconds"):
                    load_config(paths)

    def test_stress_prepare_timeout_accepts_documented_bounds_and_default(self) -> None:
        self.assertEqual(DEFAULT_CONFIG["ai"]["stress_prepare_timeout_seconds"], 600)
        for value in (60, 300, 600, 1800):
            with self.subTest(value=value), tempfile.TemporaryDirectory() as directory:
                paths = Paths.for_root(Path(directory))
                paths.ensure()
                config = json.loads(json.dumps(DEFAULT_CONFIG))
                config["ai"]["stress_prepare_timeout_seconds"] = value
                paths.config.write_text(json.dumps(config), encoding="utf-8")
                self.assertEqual(
                    load_config(paths)["ai"]["stress_prepare_timeout_seconds"],
                    value,
                )

    def test_v6_migration_preserves_explicit_stress_prepare_timeout(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = Paths.for_root(Path(directory))
            paths.ensure()
            config = json.loads(json.dumps(DEFAULT_CONFIG))
            config["version"] = 6
            config["ai"]["stress_prepare_timeout_seconds"] = 300
            paths.config.write_text(json.dumps(config), encoding="utf-8")

            loaded = load_config(paths)

            self.assertEqual(loaded["version"], 7)
            self.assertEqual(loaded["ai"]["stress_prepare_timeout_seconds"], 300)


if __name__ == "__main__":
    unittest.main()
