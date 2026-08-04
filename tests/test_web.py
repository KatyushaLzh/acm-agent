from __future__ import annotations

import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest import mock

from tools.acm_agent.web import (
    JobManager,
    _restrict_runtime_permissions,
    create_server,
    find_existing_instance,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


class RevisionConflict(Exception):
    pass


class FakeService:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def _return(self, name: str, values: dict[str, object]) -> dict[str, object]:
        self.calls.append((name, values))
        return {"operation": name, **values}

    def bootstrap(self, **values: object) -> dict[str, object]:
        return self._return("bootstrap", values)

    def setup(self, **values: object) -> dict[str, object]:
        return self._return("setup", values)

    def recommendations(self, **values: object) -> dict[str, object]:
        return self._return("recommendations", values)

    def start(self, **values: object) -> dict[str, object]:
        return self._return("start", values)

    def close(self, **values: object) -> dict[str, object]:
        return self._return("close", values)

    def weekly_review(self, **values: object) -> dict[str, object]:
        return self._return("weekly_review", values)

    def plan_check(self, **values: object) -> dict[str, object]:
        return self._return("plan_check", values)

    def plans(self, **values: object) -> dict[str, object]:
        return self._return("plans", values)

    def plan_detail(self, **values: object) -> dict[str, object]:
        return {
            **self._return("plan_detail", values),
            "task_statuses": {
                "task-1": {
                    "judge_result": "WA",
                    "workflow_status": "attempted",
                    "skipped": False,
                    "evidence_source": "platform",
                    "updated_at": "2026-08-04T00:00:00Z",
                }
            },
        }

    def plan_template(self, **values: object) -> dict[str, object]:
        return self._return("plan_template", values)

    def plan_revisions(self, **values: object) -> dict[str, object]:
        return self._return("plan_revisions", values)

    def plan_preview(self, **values: object) -> dict[str, object]:
        return self._return("plan_preview", values)

    def plan_import(self, **values: object) -> dict[str, object]:
        return self._return("plan_import", values)

    def plan_edit(self, **values: object) -> dict[str, object]:
        if values.get("conflict"):
            raise RevisionConflict("expected revision 2, current revision is 3")
        return self._return("plan_edit", values)

    def plan_state(self, **values: object) -> dict[str, object]:
        return self._return("plan_state", values)

    def plan_delete(self, **values: object) -> dict[str, object]:
        return self._return("plan_delete", values)

    def plan_restore(self, **values: object) -> dict[str, object]:
        return self._return("plan_restore", values)

    def plan_tags_preview(self, **values: object) -> dict[str, object]:
        return self._return("plan_tags_preview", values)

    def plan_tags_apply(self, **values: object) -> dict[str, object]:
        return self._return("plan_tags_apply", values)

    def problem_skip(self, **values: object) -> dict[str, object]:
        return self._return("problem_skip", values)

    def problem_unskip(self, **values: object) -> dict[str, object]:
        return self._return("problem_unskip", values)

    def skipped_problems(self, **values: object) -> dict[str, object]:
        return self._return("skipped_problems", values)

    def sync(self, **values: object) -> dict[str, object]:
        if values.get("fail"):
            raise ValueError("fixture sync failed")
        return self._return("sync", values)

    def verify(self, **values: object) -> dict[str, object]:
        return self._return("verify", values)


class WindowsRuntimePermissionTest(unittest.TestCase):
    def test_acl_commands_parse_sid_without_decoding_localized_output_as_utf8(self) -> None:
        whoami = mock.Mock(
            returncode=0,
            stdout=b'\xd2\xbb\xb8\xf6\xd3\xc3\xbb\xa7,"S-1-5-21-123-456-789-1001"\r\n',
        )
        icacls = mock.Mock(returncode=0, stdout=b"\xd2\xd1\xb3\xc9\xb9\xa6\xb4\xa6\xc0\xed\r\n")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "web-runtime.json"
            path.write_text("{}", encoding="utf-8")
            with (
                mock.patch("tools.acm_agent.web.os.name", "nt"),
                mock.patch("tools.acm_agent.web.subprocess.run", side_effect=[whoami, icacls]) as run,
            ):
                _restrict_runtime_permissions(path)

        command = run.call_args_list[1].args[0]
        self.assertIn("*S-1-5-21-123-456-789-1001:(F)", command)


class WebServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.static = self.root / "static"
        self.static.mkdir()
        (self.static / "index.html").write_text("<h1>ACM</h1>", encoding="utf-8")
        (self.static / "app.js").write_text("console.log('ok')", encoding="utf-8")
        self.service = FakeService()
        self.server = create_server(
            self.root,
            service=self.service,
            port=0,
            max_port=0,
            token="test-token",
            static_dir=self.static,
            max_request_bytes=64,
        )
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.thread.join(timeout=2)
        self.server.server_close()
        self.server.cleanup()
        self.temporary.cleanup()

    def request(
        self,
        method: str,
        path: str,
        *,
        payload: object | None = None,
        token: str | None = "test-token",
        headers: dict[str, str] | None = None,
        raw_body: bytes | None = None,
    ) -> tuple[int, dict[str, object], dict[str, str]]:
        connection = http.client.HTTPConnection("127.0.0.1", self.server.port, timeout=2)
        request_headers = dict(headers or {})
        if token is not None:
            request_headers["X-ACM-Token"] = token
        body = raw_body
        if payload is not None:
            body = json.dumps(payload).encode("utf-8")
            request_headers.setdefault("Content-Type", "application/json")
        connection.request(method, path, body=body, headers=request_headers)
        response = connection.getresponse()
        content = response.read()
        response_headers = {key.lower(): value for key, value in response.getheaders()}
        connection.close()
        try:
            decoded: dict[str, object] = json.loads(content)
        except json.JSONDecodeError:
            decoded = {"raw": content.decode("utf-8")}
        return response.status, decoded, response_headers

    def wait_for_job(self, job_id: str) -> dict[str, object]:
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            status, payload, _ = self.request("GET", f"/api/jobs/{job_id}")
            self.assertEqual(status, 200)
            job = payload["data"]
            if job["status"] in {"succeeded", "failed"}:
                return job
            time.sleep(0.01)
        self.fail("job did not finish")

    def test_static_assets_and_security_headers(self) -> None:
        status, _, headers = self.request("GET", "/", token=None)
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "text/html; charset=utf-8")
        self.assertIn("default-src 'self'", headers["content-security-policy"])

        with mock.patch(
            "tools.acm_agent.web.mimetypes.guess_type",
            return_value=("application/javascript", None),
        ):
            status, payload, headers = self.request("GET", "/static/app.js", token=None)
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "text/javascript; charset=utf-8")
        self.assertIn("console.log", payload["raw"])

    def test_bootstrap_is_authenticated_and_augmented(self) -> None:
        status, payload, _ = self.request("GET", "/api/bootstrap")
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["data"]["operation"], "bootstrap")
        self.assertEqual(payload["data"]["web"]["port"], self.server.port)

        status, payload, _ = self.request("GET", "/api/bootstrap", token=None)
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "unauthorized")

    def test_sync_job_succeeds_and_failure_is_reported(self) -> None:
        status, payload, _ = self.request("POST", "/api/jobs/sync", payload={"platform": "all"})
        self.assertEqual(status, 202)
        job = self.wait_for_job(payload["data"]["job_id"])
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(job["result"]["platform"], "all")

        status, payload, _ = self.request("POST", "/api/jobs/sync", payload={"fail": True})
        self.assertEqual(status, 202)
        job = self.wait_for_job(payload["data"]["job_id"])
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error"]["code"], "invalid_request")

        status, payload, _ = self.request(
            "POST",
            "/api/jobs/plans/tags/preview",
            payload={"plan_id": "example", "overwrite": False},
        )
        self.assertEqual(status, 202)
        job = self.wait_for_job(payload["data"]["job_id"])
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(job["result"]["operation"], "plan_tags_preview")
        self.assertFalse(job["result"]["overwrite"])

    def test_synchronous_routes_forward_json_objects(self) -> None:
        routes = {
            "/api/setup": "setup",
            "/api/recommendations": "recommendations",
            "/api/sessions/start": "start",
            "/api/sessions/close": "close",
            "/api/review/week": "weekly_review",
            "/api/plan/check": "plan_check",
            "/api/plans/preview": "plan_preview",
            "/api/plans/import": "plan_import",
            "/api/plans/edit": "plan_edit",
            "/api/plans/state": "plan_state",
            "/api/plans/delete": "plan_delete",
            "/api/plans/restore": "plan_restore",
            "/api/plans/tags/apply": "plan_tags_apply",
            "/api/problems/skip": "problem_skip",
            "/api/problems/unskip": "problem_unskip",
        }
        for path, operation in routes.items():
            with self.subTest(path=path):
                status, payload, _ = self.request("POST", path, payload={"marker": operation})
                self.assertEqual(status, 200)
                self.assertEqual(payload["data"]["operation"], operation)
                self.assertEqual(payload["data"]["marker"], operation)

    def test_skip_routes_use_singular_note_and_web_source(self) -> None:
        status, payload, _ = self.request(
            "POST",
            "/api/problems/skip",
            payload={
                "problem": "CF1A",
                "note": "mastered",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["operation"], "problem_skip")
        self.assertEqual(payload["data"]["note"], "mastered")
        self.assertEqual(payload["data"]["source"], "web")

        status, payload, _ = self.request("GET", "/api/problems/skipped")
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["operation"], "skipped_problems")

    def test_plan_get_routes(self) -> None:
        routes = {
            "/api/plans": ("plans", None),
            "/api/plans/template": ("plan_template", None),
            "/api/plans/example-plan": ("plan_detail", "example-plan"),
            "/api/plans/example-plan/revisions": ("plan_revisions", "example-plan"),
        }
        for path, (operation, plan_id) in routes.items():
            with self.subTest(path=path):
                status, payload, _ = self.request("GET", path)
                self.assertEqual(status, 200)
                self.assertEqual(payload["data"]["operation"], operation)
                if plan_id:
                    self.assertEqual(payload["data"]["plan_id"], plan_id)
                if operation == "plan_detail":
                    self.assertEqual(
                        payload["data"]["task_statuses"]["task-1"]["judge_result"],
                        "WA",
                    )

    def test_plan_revision_conflict_is_http_409(self) -> None:
        status, payload, _ = self.request(
            "POST", "/api/plans/edit", payload={"conflict": True}
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "revision_conflict")

    def test_host_and_origin_are_restricted(self) -> None:
        status, payload, _ = self.request(
            "GET",
            "/api/bootstrap",
            headers={"Host": "attacker.example"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "invalid_host")

        status, payload, _ = self.request(
            "POST",
            "/api/setup",
            payload={},
            headers={"Origin": "https://attacker.example"},
        )
        self.assertEqual(status, 403)
        self.assertEqual(payload["error"]["code"], "invalid_origin")

    def test_json_content_type_shape_and_size_are_validated(self) -> None:
        status, payload, _ = self.request(
            "POST",
            "/api/setup",
            raw_body=b"{}",
            headers={"Content-Type": "text/plain"},
        )
        self.assertEqual(status, 415)
        self.assertEqual(payload["error"]["code"], "unsupported_media_type")

        status, payload, _ = self.request("POST", "/api/setup", payload=[1, 2])
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_json_type")

        status, payload, _ = self.request("POST", "/api/setup", payload={"long": "x" * 100})
        self.assertEqual(status, 413)
        self.assertEqual(payload["error"]["code"], "request_too_large")

    def test_unknown_routes_and_wrong_methods(self) -> None:
        status, payload, _ = self.request("GET", "/api/no-such-route")
        self.assertEqual(status, 404)
        self.assertEqual(payload["error"]["code"], "not_found")

        status, payload, _ = self.request("GET", "/api/recommendations")
        self.assertEqual(status, 405)
        self.assertEqual(payload["error"]["code"], "method_not_allowed")

        status, payload, _ = self.request("POST", "/api/bootstrap", payload={})
        self.assertEqual(status, 405)
        self.assertEqual(payload["error"]["code"], "method_not_allowed")

    def test_runtime_discovery_and_shutdown(self) -> None:
        runtime = find_existing_instance(self.root)
        self.assertIsNotNone(runtime)
        self.assertEqual(runtime["port"], self.server.port)

        status, payload, _ = self.request("POST", "/api/server/shutdown", payload={})
        self.assertEqual(status, 200)
        self.assertTrue(payload["data"]["shutting_down"])
        self.thread.join(timeout=2)
        self.assertFalse(self.thread.is_alive())


class JobManagerTest(unittest.TestCase):
    def test_jobs_execute_serially(self) -> None:
        manager = JobManager(threading.RLock(), capacity=10)
        running = 0
        max_running = 0
        guard = threading.Lock()

        def work() -> dict[str, bool]:
            nonlocal running, max_running
            with guard:
                running += 1
                max_running = max(max_running, running)
            time.sleep(0.03)
            with guard:
                running -= 1
            return {"ok": True}

        try:
            jobs = [manager.submit(f"job-{index}", work) for index in range(3)]
            manager.close(wait=True)
            self.assertEqual(max_running, 1)
            self.assertTrue(
                all(manager.get(job["job_id"])["status"] == "succeeded" for job in jobs)
            )
        finally:
            manager.close(wait=True)

    def test_registry_is_bounded(self) -> None:
        manager = JobManager(threading.RLock(), capacity=2)
        try:
            first = manager.submit("one", lambda: {"n": 1})
            second = manager.submit("two", lambda: {"n": 2})
            third = manager.submit("three", lambda: {"n": 3})
            manager.close(wait=True)
            records = [manager.get(item["job_id"]) for item in (first, second, third)]
            self.assertLessEqual(sum(record is not None for record in records), 2)
            self.assertIsNotNone(records[-1])
        finally:
            # close() is idempotent for ThreadPoolExecutor.
            manager.close(wait=True)


class StaticPlanEditorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.script = (
            REPO_ROOT / "tools/acm_agent/web_static/app.js"
        ).read_text(encoding="utf-8")

    def test_plan_and_stage_editing_are_opt_in(self) -> None:
        self.assertIn('editingPlanMeta: false', self.script)
        self.assertIn('editingStageKey: ""', self.script)
        self.assertIn('data-plan-action="edit-meta"', self.script)
        self.assertIn('data-stage-action="edit"', self.script)
        self.assertIn('renderTaskTable(tasks, false)', self.script)

    def test_simplified_task_editor_only_exposes_problem_name_and_tags(self) -> None:
        self.assertNotIn('data-task-field="level"', self.script)
        self.assertNotIn('data-task-field="required"', self.script)
        self.assertNotIn('data-task-field="unlock_at"', self.script)
        self.assertNotIn('data-task-field="due_date"', self.script)
        self.assertNotIn('data-task-action="up"', self.script)
        self.assertNotIn('data-task-action="down"', self.script)
        self.assertNotIn('data-task-action="move"', self.script)
        self.assertNotIn('移动到…', self.script)
        self.assertIn('data-task-field="name"', self.script)
        self.assertIn('class="problem-link"', self.script)
        self.assertIn('target="_blank" rel="noopener noreferrer"', self.script)

    def test_task_status_columns_are_read_only(self) -> None:
        self.assertIn("task_statuses: asObject(data.task_statuses)", self.script)
        self.assertIn("statuses[task.task_key]", self.script)
        self.assertIn("function taskStatusView(task)", self.script)
        self.assertIn("function renderTaskStatusCells(task)", self.script)
        self.assertIn("判题状态", self.script)
        self.assertIn("Skip", self.script)
        self.assertIn("task-status-cell", self.script)
        self.assertIn("task-skip-cell", self.script)
        self.assertNotIn('data-task-field="judge_result"', self.script)
        self.assertNotIn('data-task-field="workflow_status"', self.script)
        self.assertNotIn('data-task-field="skipped"', self.script)
        self.assertNotIn('data-task-action="status"', self.script)

    def test_platform_ratio_is_derived_and_not_editable(self) -> None:
        self.assertIn("function derivePlanPlatformStats(plan, meta)", self.script)
        self.assertIn("for (const task of tasksOf(stage))", self.script)
        self.assertIn('Codeforces</dt><dd>${escapeHtml(platformValue("codeforces"))}', self.script)
        self.assertNotIn("data-plan-target", self.script)
        self.assertNotIn("next.platform_target", self.script)


if __name__ == "__main__":
    unittest.main()
