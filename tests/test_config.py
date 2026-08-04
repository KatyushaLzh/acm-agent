from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.acm_agent.config import DEFAULT_CONFIG, Paths, load_config, save_config


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


if __name__ == "__main__":
    unittest.main()
