from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tools.acm_agent.config import DEFAULT_CONFIG, Paths, load_config


LEGACY = {
    profile: {"max_output_tokens": output, "request_timeout_seconds": timeout,
              "max_retries": 1, "max_validation_repairs": 1,
              "max_requests": 3, "max_total_tokens": total}
    for profile, output, timeout, total in (
        ("recommendation", 4096, 120.0, 300000),
        ("coaching", 8192, 120.0, 200000),
        ("summary", 8192, 180.0, 240000),
    )
}


class ConfigBudgetUpgradeTests(unittest.TestCase):
    def fixture(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        paths = Paths.for_root(Path(temporary.name))
        paths.ensure()
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["version"] = 16
        config["ai"]["policy"]["budgets"].update(copy.deepcopy(LEGACY))
        return paths, config

    def test_v16_full_defaults_upgrade_preserves_routes_limits_and_other_profiles(self):
        paths, config = self.fixture()
        config["ai"]["policy"]["hard_limits"] = {"daily_cny": 1.0, "monthly_cny": 5.0}
        paths.config.write_text(json.dumps(config), encoding="utf-8")
        result = load_config(paths)
        self.assertEqual(result["version"], 17)
        self.assertEqual(result["ai"]["profiles"], config["ai"]["profiles"])
        self.assertEqual(result["ai"]["policy"]["hard_limits"], config["ai"]["policy"]["hard_limits"])
        for profile, previous in config["ai"]["policy"]["budgets"].items():
            expected = dict(previous)
            if profile in LEGACY:
                expected.update(max_output_tokens=16384, request_timeout_seconds=300.0)
            self.assertEqual(result["ai"]["policy"]["budgets"][profile], expected)
        with mock.patch("tools.acm_agent.config.save_config") as save:
            self.assertEqual(load_config(paths), result)
            save.assert_not_called()

    def test_any_custom_v16_budget_field_preserves_entire_profile(self):
        for profile in LEGACY:
            for field, custom in (("max_output_tokens", 3000), ("request_timeout_seconds", 75.0),
                                  ("max_total_tokens", 50000), ("max_requests", 2),
                                  ("max_retries", 0), ("max_validation_repairs", 0)):
                with self.subTest(profile=profile, field=field):
                    paths, config = self.fixture()
                    budget = config["ai"]["policy"]["budgets"][profile]
                    budget[field] = custom
                    paths.config.write_text(json.dumps(config), encoding="utf-8")
                    result = load_config(paths)
                    self.assertEqual(result["ai"]["policy"]["budgets"][profile], budget)

    def test_absent_budget_gets_new_defaults_but_explicit_partial_budget_stays_bounded(self):
        paths, config = self.fixture()
        budgets = config["ai"]["policy"]["budgets"]
        del budgets["summary"]
        budgets["recommendation"] = {"max_output_tokens": 4096, "request_timeout_seconds": 120.0}
        paths.config.write_text(json.dumps(config), encoding="utf-8")
        result = load_config(paths)["ai"]["policy"]["budgets"]
        self.assertEqual(result["summary"]["max_output_tokens"], 16384)
        self.assertEqual(result["recommendation"]["max_output_tokens"], 4096)
        self.assertEqual(result["recommendation"]["request_timeout_seconds"], 120.0)


if __name__ == "__main__":
    unittest.main()
