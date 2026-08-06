from __future__ import annotations

import os
import unittest

from tools.acm_agent.stress_sources import AllowlistedCrawler


@unittest.skipUnless(
    os.environ.get("RUN_STRESS_NETWORK_SMOKE") == "1",
    "set RUN_STRESS_NETWORK_SMOKE=1 to exercise the real allowlisted crawler",
)
class StressNetworkSmokeTests(unittest.TestCase):
    def test_codeforces_public_page_uses_real_allowlist_and_robots_path(self) -> None:
        crawler = AllowlistedCrawler(timeout=20.0, min_host_interval=0)
        final_url, html = crawler._read("https://codeforces.com/contest/1")
        self.assertTrue(final_url.startswith("https://codeforces.com/"))
        self.assertIn("codeforces", html.casefold())
        self.assertGreaterEqual(crawler.pages, 2)  # robots.txt plus the public page
        self.assertLessEqual(crawler.bytes, 2 * 1024 * 1024)


if __name__ == "__main__":
    unittest.main()
