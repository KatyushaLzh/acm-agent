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

                self.assertEqual(CONFIG_VERSION, 8)
                self.assertEqual(config["version"], 8)
                for retired_key in (
                    "validation_model",
                    "validation_thinking",
                    "validation_reasoning_effort",
                    "stress_prepare_timeout_seconds",
                    "stress_generation_mode",
                ):
                    self.assertNotIn(retired_key, config["ai"])
                self.assertEqual(config["ai"]["coaching_model"], "deepseek-v4-pro")
                self.assertEqual(json.loads(paths.config.read_text())["version"], 8)


if __name__ == "__main__":
    unittest.main()
