from pathlib import Path
import shutil
import subprocess
import unittest
from unittest.mock import patch
from tools.acm_agent.cli import build_parser

ROOT = Path(__file__).resolve().parents[1]

class FrontendProviderDetectionTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required")
    def test_effective_capabilities_and_automatic_verification(self):
        result = subprocess.run([shutil.which("node"), str(ROOT / "tests/frontend_provider_detection.cjs")], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_cli_detect_builds_connection_payload(self):
        args = build_parser().parse_args(["ai", "connection", "detect", "--name", "Test", "--base-url", "https://example.com", "--adapter", "anthropic", "--model", "m1", "--model", "m2", "--auth-type", "header", "--json"])
        with patch("tools.acm_agent.cli.getpass.getpass", return_value="secret"), patch("tools.acm_agent.cli._service") as service, patch("tools.acm_agent.cli._emit"):
            args.handler(args, None)
        values = service.return_value.ai_connection_detect.call_args.kwargs
        self.assertEqual(values["manual_models"], ["m1", "m2"])
        self.assertEqual(values["auth"], {"type": "header", "header": "x-api-key"})
        self.assertEqual(values["display_name"], "Test")
        self.assertEqual(values["adapter"], "anthropic")

    def test_task_buttons_verify_migrated_selections(self):
        source = (ROOT / "tools/acm_agent/web_static/view_ai.js").read_text(encoding="utf-8")
        for profile in ("summary", "recommendation", "coaching"):
            self.assertIn(f'await ensureSelectionVerified("{profile}", aiRequestSelection("{profile}"))', source)
        self.assertNotIn('if (typeof profile.ready === "boolean") return profile.ready;', source)
        plan = (ROOT / "tools/acm_agent/web_static/view_plan_ai_import.js").read_text(encoding="utf-8")
        self.assertIn('await ensureSelectionVerified(profileId, selection)', plan)

    def test_default_fields_stay_outside_advanced_settings(self):
        markup = (ROOT / "tools/acm_agent/web_static/index.html").read_text(encoding="utf-8")
        form = markup.split('id="ai-connection-form"', 1)[1].split('</form>', 1)[0]
        basic, advanced = form.split('<details', 1)
        for name in ("display_name", "base_url", "api_key"):
            self.assertIn(f'name="{name}"', basic)
        self.assertIn('value="auto"', advanced)
        self.assertIn('value="anthropic"', advanced)

if __name__ == "__main__":
    unittest.main()
