from __future__ import annotations

from dataclasses import dataclass
import hashlib
import http.client
import json
from pathlib import Path
import shutil
import tempfile
import threading
import time
import unittest

from tools.acm_agent.platforms import SyncResult
from tools.acm_agent.service import AcmService
from tools.acm_agent.web import create_server


REPO_ROOT = Path(__file__).resolve().parents[1]


@dataclass
class _VerifyResult:
    problem_id: str
    passed: bool = True

    def to_dict(self) -> dict[str, object]:
        return {
            "problem_id": self.problem_id,
            "source": "fixture.cpp",
            "passed": self.passed,
            "compiled": True,
            "compile_command": ["g++", "fixture.cpp"],
            "compile_output": "",
            "sanitizer": "not_requested",
            "cases": [],
            "stress": "not_available",
            "stress_iterations": 0,
            "failure_dir": None,
            "warnings": [],
        }


def _fixture_sync(platform: str):
    def run(db, _account, **_kwargs):
        db.record_sync_attempt(platform, status="fresh", success=True)
        return SyncResult(platform=platform, status="fresh")

    return run


class WebEndToEndTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        plan = self.root / "training" / "data-structures-30d"
        plan.mkdir(parents=True)
        shutil.copy2(REPO_ROOT / "training/data-structures-30d/plan.json", plan / "plan.json")
        shutil.copy2(REPO_ROOT / "training/data-structures-30d/README.md", plan / "README.md")
        (self.root / "algorithms.md").write_text("# Algorithms\n", encoding="utf-8")
        (self.root / "tricks.md").write_text("# Tricks\n", encoding="utf-8")
        self.before = self._knowledge_hashes()

        service = AcmService(
            self.root,
            sync_codeforces_fn=_fixture_sync("codeforces"),
            sync_luogu_fn=_fixture_sync("luogu"),
            verify_fn=lambda _root, problem, **_kwargs: _VerifyResult(str(problem)),
        )
        self.server = create_server(
            self.root,
            service=service,
            port=0,
            max_port=0,
            token="e2e-token",
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.server.cleanup()
        self.temporary.cleanup()

    def _knowledge_hashes(self) -> tuple[str, str]:
        return tuple(
            hashlib.sha256((self.root / name).read_bytes()).hexdigest()
            for name in ("algorithms.md", "tricks.md")
        )

    def request(self, method: str, path: str, payload: dict[str, object] | None = None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=10)
        headers = {"X-ACM-Token": "e2e-token"}
        body = None
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        try:
            connection.request(method, path, body=body, headers=headers)
            response = connection.getresponse()
            decoded = json.loads(response.read())
        finally:
            connection.close()
        self.assertIn(response.status, {200, 202}, decoded)
        self.assertTrue(decoded["ok"], decoded)
        return decoded["data"]

    def wait_job(self, job_id: str) -> dict[str, object]:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            job = self.request("GET", f"/api/jobs/{job_id}")
            if job["status"] == "succeeded":
                return job["result"]
            if job["status"] == "failed":
                self.fail(str(job))
            time.sleep(0.01)
        self.fail("background job timed out")

    def test_setup_sync_recommend_start_verify_close_and_review(self) -> None:
        bootstrap = self.request("GET", "/api/bootstrap")
        self.assertFalse(bootstrap["configured"])

        self.request(
            "POST",
            "/api/setup",
            {"codeforces": "fixture", "luogu": "42", "skip_validate": True},
        )
        sync_job = self.request("POST", "/api/jobs/sync", {"platform": "all"})
        sync_result = self.wait_job(sync_job["job_id"])
        self.assertTrue(sync_result["ok"])

        recommendation = self.request(
            "POST", "/api/recommendations", {"mode": "new", "count": 3}
        )
        self.assertEqual(recommendation["recommendation_basis"], "synced")
        problem = recommendation["recommendations"][0]["problem_id"]

        started = self.request(
            "POST", "/api/sessions/start", {"problem": problem, "with_stress": False}
        )
        self.assertTrue(Path(started["source"]).is_file())

        verify_job = self.request(
            "POST", "/api/jobs/verify", {"problem": problem, "exact": False}
        )
        self.assertTrue(self.wait_job(verify_job["job_id"])["passed"])

        closed = self.request(
            "POST",
            "/api/sessions/close",
            {
                "problem": problem,
                "result": "AC",
                "minutes": 25,
                "hint_level": 2,
                "failure": "modeling",
                "notes": "HTTP fixture",
            },
        )
        self.assertTrue(closed["archive_candidate"])
        review = self.request("POST", "/api/review/week", {})
        self.assertEqual(review["sessions"], 1)
        self.assertEqual(self._knowledge_hashes(), self.before)

    def test_plan_import_edit_recommend_and_delete(self) -> None:
        self.request(
            "POST",
            "/api/setup",
            {"codeforces": "fixture", "luogu": "42", "skip_validate": True},
        )
        template = self.request("GET", "/api/plans/template")["plan"]
        template["plan_id"] = "http-plan"
        template["title"] = "HTTP 题单"
        preview = self.request("POST", "/api/plans/preview", {"plan": template})
        self.assertFalse(preview["duplicate"])
        imported = self.request("POST", "/api/plans/import", {"plan": template})
        self.assertEqual(imported["plan_id"], "http-plan")

        document = imported["plan"]
        document["stages"][0]["due_date"] = "2026-08-05"
        edited = self.request(
            "POST",
            "/api/plans/edit",
            {
                "plan_id": "http-plan",
                "expected_revision": imported["revision"],
                "plan": document,
            },
        )
        detail = self.request("GET", "/api/plans/http-plan")
        self.assertEqual(
            detail["plan"]["stages"][0]["due_date"], "2026-08-05"
        )
        self.assertNotIn("due_date", detail["plan"]["stages"][0]["tasks"][0])
        recommended = self.request(
            "POST",
            "/api/recommendations",
            {
                "mode": "new",
                "count": 3,
                "source_mode": "plan_only",
                "plan_ids": ["http-plan"],
            },
        )
        self.assertTrue(recommended["recommendations"])
        self.assertTrue(
            all(
                source["plan_id"] == "http-plan"
                for row in recommended["recommendations"]
                for source in row["plan_sources"]
            )
        )
        deleted = self.request(
            "POST",
            "/api/plans/delete",
            {"plan_id": "http-plan", "expected_revision": edited["revision"]},
        )
        self.assertTrue(deleted["history_preserved"])


if __name__ == "__main__":
    unittest.main()
