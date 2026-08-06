from __future__ import annotations

import http.client
import json
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest

from tools.acm_agent.service import AcmService
from tools.acm_agent.web import create_server
from tests.test_knowledge_service import SummaryDeepSeek


REPO_ROOT = Path(__file__).resolve().parents[1]


class KnowledgeWebEndToEndTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        plan = self.root / "training" / "data-structures-30d"
        plan.mkdir(parents=True)
        shutil.copy2(REPO_ROOT / "training/data-structures-30d/plan.json", plan / "plan.json")
        shutil.copy2(REPO_ROOT / "training/data-structures-30d/README.md", plan / "README.md")
        self.client = SummaryDeepSeek()
        service = AcmService(
            self.root,
            deepseek_client_factory=lambda: self.client,
            problem_context_fetcher=lambda ref: (
                "HTTP fixture statement",
                "https://codeforces.com/problemset/problem/1/A",
            ),
        )
        service.setup("fixture", "42", skip_validate=True)
        self.server = create_server(
            self.root, service=service, port=0, max_port=0, token="knowledge-token"
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.server.cleanup()
        self.temporary.cleanup()

    def request(self, method: str, path: str, payload=None, *, expected=(200, 202)):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=5)
        headers = {"X-ACM-Token": "knowledge-token"}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        decoded = json.loads(response.read())
        connection.close()
        self.assertIn(response.status, expected, decoded)
        return decoded

    def wait_job(self, job_id: str):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            payload = self.request("GET", f"/api/jobs/{job_id}")["data"]
            if payload["status"] == "succeeded":
                return payload["result"]
            if payload["status"] == "failed":
                self.fail(str(payload))
            time.sleep(0.02)
        self.fail("knowledge job timed out")

    def test_close_preview_refresh_apply_revert_and_unregister(self) -> None:
        templates = self.request("GET", "/api/knowledge/templates")["data"]["templates"]
        target = self.root / "notes.md"
        target.write_text(templates[0]["template"], encoding="utf-8")
        inspected = self.request(
            "POST", "/api/knowledge/targets/inspect",
            {"path": str(target), "preset": "algorithms-v1", "schema_mode": "stored"},
        )["data"]
        registered = self.request(
            "POST", "/api/knowledge/targets",
            {
                "path": str(target),
                "name": "HTTP Notes",
                "preset": "algorithms-v1",
                "schema_mode": "stored",
                "expected_inspection_sha256": inspected["baseline_sha256"],
            },
        )["data"]
        target_id = registered["target_id"]
        before = target.read_bytes()

        self.request("POST", "/api/sessions/start", {"problem": "CF1A"})
        closed = self.request(
            "POST", "/api/sessions/close",
            {
                "problem": "CF1A", "result": "AC", "minutes": 20,
                "hint_level": 2, "failure": "modeling", "notes": "HTTP summary",
            },
        )["data"]
        preview_job = self.request(
            "POST", "/api/jobs/ai/knowledge/preview",
            {"attempt_id": closed["attempt_id"], "target_id": target_id, "schema_mode": "stored"},
        )["data"]
        proposal = self.wait_job(preview_job["job_id"])["proposal"]
        self.assertEqual(target.read_bytes(), before)

        edited = proposal["entry_markdown"].replace("边界图。", "边界状态图。")
        refreshed = self.request(
            "POST", f"/api/knowledge/proposals/{proposal['proposal_id']}/refresh",
            {"entry_markdown": edited, "expected_revision": proposal["revision"]},
        )["data"]["proposal"]
        apply_job = self.request(
            "POST", f"/api/jobs/knowledge/proposals/{proposal['proposal_id']}/apply",
            {"expected_revision": refreshed["revision"]},
        )["data"]
        applied = self.wait_job(apply_job["job_id"])["proposal"]
        self.assertIn("边界状态图", target.read_text(encoding="utf-8"))
        loaded = self.request(
            "GET", f"/api/knowledge/proposals/{proposal['proposal_id']}"
        )["data"]["proposal"]
        self.assertEqual(loaded["status"], "applied")

        revert_job = self.request(
            "POST", f"/api/jobs/knowledge/proposals/{proposal['proposal_id']}/revert",
            {"expected_revision": applied["revision"]},
        )["data"]
        self.wait_job(revert_job["job_id"])
        self.assertEqual(target.read_bytes(), before)
        attempt_items = self.request(
            "GET", f"/api/attempts/{closed['attempt_id']}/knowledge"
        )["data"]["proposals"]
        self.assertEqual(attempt_items[0]["status"], "reverted")

        removed = self.request(
            "DELETE", f"/api/knowledge/targets/{target_id}",
            {"expected_revision": registered["revision"]},
        )["data"]
        self.assertFalse(removed["file_deleted"])
        self.assertTrue(target.is_file())


if __name__ == "__main__":
    unittest.main()
