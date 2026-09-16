from pathlib import Path
import shutil
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]


class FrontendApiErrorTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("node"), "Node.js is required for frontend API regression tests")
    def test_api_errors_execute_in_node(self):
        result = subprocess.run(
            [shutil.which("node"), str(ROOT / "tests" / "frontend_api_errors.cjs")],
            cwd=ROOT, capture_output=True, text=True, encoding="utf-8", timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
