from __future__ import annotations

import http.client
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest

from tools.acm_agent.web import JobManager, create_server, find_existing_instance
from tools.acm_agent.storage import TagOverrideRevisionConflict


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

    def ai_status(self, **values: object) -> dict[str, object]:
        return {"ok": True, "api_key_detected": False, "settings": {}}

    def ai_settings(self, **values: object) -> dict[str, object]:
        return self._return("ai_settings", values)

    def ai_credential(self, **values: object) -> dict[str, object]:
        self.calls.append(("ai_credential", dict(values)))
        return {
            "ok": True,
            "api_key_detected": not bool(values.get("clear")),
            "credential_source": "none" if values.get("clear") else "secure_store",
            "credential_persisted": not bool(values.get("clear")),
            "credential_error": None,
            "settings": {},
        }

    def ai_test(self, **values: object) -> dict[str, object]:
        return self._return("ai_test", values)

    def ai_recommendations(self, **values: object) -> dict[str, object]:
        return self._return("ai_recommendations", values)

    def problem_context(self, **values: object) -> dict[str, object]:
        return self._return("problem_context", values)

    def problem_context_save(self, **values: object) -> dict[str, object]:
        return self._return("problem_context_save", values)

    def problem_context_fetch(self, **values: object) -> dict[str, object]:
        return self._return("problem_context_fetch", values)

    def ai_conversation_start(self, **values: object) -> dict[str, object]:
        return {"conversation_id": "conv-1", **self._return("ai_conversation_start", values)}

    def ai_conversation(self, **values: object) -> dict[str, object]:
        return self._return("ai_conversation", values)

    def ai_chat_stream(self, conversation_id: str, **values: object):
        self.calls.append(("ai_chat_stream", {"conversation_id": conversation_id, **values}))
        yield {"event": "meta", "data": {"conversation_id": conversation_id}}
        yield {"event": "delta", "data": {"content": "fixture"}}
        yield {"event": "usage", "data": {"usage": {"total_tokens": 2}}}
        yield {"event": "done", "data": {"status": "complete"}}

    def ai_patch_preview(self, **values: object) -> dict[str, object]:
        return self._return("ai_patch_preview", values)

    def ai_patch_apply(self, **values: object) -> dict[str, object]:
        return self._return("ai_patch_apply", values)

    def ai_patch_revert(self, **values: object) -> dict[str, object]:
        return self._return("ai_patch_revert", values)

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
        if values.get("override_conflict"):
            raise TagOverrideRevisionConflict(2, 3)
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


class WebServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.static = self.root / "static"
        self.static.mkdir()
        (self.static / "index.html").write_text("<h1>ACM</h1>", encoding="utf-8")
        (self.static / "app.js").write_text("console.log('ok')", encoding="utf-8")
        font_dir = self.static / "vendor" / "katex" / "fonts"
        font_dir.mkdir(parents=True)
        for suffix in ("woff2", "woff", "ttf"):
            (font_dir / f"fixture.{suffix}").write_bytes(b"font-fixture")
        self.service = FakeService()
        self.server = create_server(
            self.root,
            service=self.service,
            port=0,
            max_port=0,
            token="test-token",
            static_dir=self.static,
            max_request_bytes=512,
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
        csp = headers["content-security-policy"]
        self.assertIn("default-src 'self'", csp)
        self.assertIn("script-src 'self'", csp)
        self.assertIn("style-src 'self'", csp)
        self.assertNotIn("'unsafe-inline'", csp)
        self.assertNotIn("https:", csp)

        status, payload, headers = self.request("GET", "/static/app.js", token=None)
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "text/javascript; charset=utf-8")
        self.assertIn("console.log", payload["raw"])

    def test_static_katex_font_mime_types(self) -> None:
        expected = {
            "woff2": "font/woff2",
            "woff": "font/woff",
            "ttf": "font/ttf",
        }
        for suffix, media_type in expected.items():
            with self.subTest(suffix=suffix):
                status, _, headers = self.request(
                    "GET",
                    f"/static/vendor/katex/fonts/fixture.{suffix}",
                    token=None,
                )
                self.assertEqual(status, 200)
                self.assertEqual(headers["content-type"], media_type)
                self.assertIn("default-src 'self'", headers["content-security-policy"])

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
            payload={"plan_id": "example", "overwrite": False, "mode": "cleanup"},
        )
        self.assertEqual(status, 202)
        job = self.wait_for_job(payload["data"]["job_id"])
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(job["result"]["operation"], "plan_tags_preview")
        self.assertFalse(job["result"]["overwrite"])
        self.assertEqual(job["result"]["mode"], "cleanup")

    def test_ai_context_jobs_dynamic_gets_and_sse_contract(self) -> None:
        status, payload, _ = self.request("GET", "/api/ai/status")
        self.assertEqual(status, 200)
        self.assertFalse(payload["data"]["api_key_detected"])

        secret = "sk-http-fixture-must-not-echo"
        status, payload, _ = self.request(
            "POST", "/api/ai/credential", payload={"api_key": secret}
        )
        self.assertEqual(status, 200)
        self.assertTrue(payload["data"]["credential_persisted"])
        self.assertNotIn(secret, json.dumps(payload))
        self.assertEqual(self.service.calls[-1][0], "ai_credential")
        self.assertEqual(self.service.calls[-1][1]["api_key"], secret)

        status, payload, _ = self.request("GET", "/api/problems/CF1A/context")
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["problem"], "CF1A")

        status, payload, _ = self.request(
            "POST", "/api/ai/conversations", payload={"problem": "CF1A"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["conversation_id"], "conv-1")

        status, payload, headers = self.request(
            "POST",
            "/api/ai/conversations/conv-1/messages",
            payload={"message": "hint", "mode": "hint", "hint_level": 1},
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "text/event-stream; charset=utf-8")
        self.assertIn("event: meta", payload["raw"])
        self.assertIn("event: delta", payload["raw"])
        self.assertIn("event: usage", payload["raw"])
        self.assertIn("event: done", payload["raw"])

        status, payload, _ = self.request(
            "POST", "/api/jobs/ai/recommendations", payload={"count": 2}
        )
        self.assertEqual(status, 202)
        job = self.wait_for_job(payload["data"]["job_id"])
        self.assertEqual(job["result"]["operation"], "ai_recommendations")

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

    def test_tag_apply_forwards_override_revision_and_empty_tags(self) -> None:
        status, payload, _ = self.request(
            "POST",
            "/api/plans/tags/apply",
            payload={
                "plan_id": "example",
                "expected_revision": 3,
                "expected_override_revision": 7,
                "proposals": [{"task_key": "task-1", "tags": []}],
            },
        )
        self.assertEqual(status, 200)
        forwarded = payload["data"]
        self.assertEqual(forwarded["operation"], "plan_tags_apply")
        self.assertEqual(forwarded["expected_override_revision"], 7)
        self.assertEqual(forwarded["proposals"], [{"task_key": "task-1", "tags": []}])

        status, payload, _ = self.request(
            "POST", "/api/plans/tags/apply", payload={"override_conflict": True}
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

        status, payload, _ = self.request("POST", "/api/setup", payload={"long": "x" * 600})
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
        self.html = (
            REPO_ROOT / "tools/acm_agent/web_static/index.html"
        ).read_text(encoding="utf-8")

    def test_plan_and_stage_editing_are_opt_in(self) -> None:
        self.assertIn('editingPlanMeta: false', self.script)
        self.assertIn('editingStageKey: ""', self.script)
        self.assertIn('data-plan-action="edit-meta"', self.script)
        self.assertIn('data-stage-action="edit"', self.script)
        self.assertIn('renderTaskTable(tasks, false)', self.script)

    def test_katex_assets_are_same_origin_and_loaded_before_application(self) -> None:
        css = '/static/vendor/katex/katex.min.css'
        katex = '/static/vendor/katex/katex.min.js'
        auto_render = '/static/vendor/katex/contrib/auto-render.min.js'
        app = '/static/app.js'
        for asset in (css, katex, auto_render, app):
            self.assertIn(asset, self.html)
        self.assertLess(self.html.index(katex), self.html.index(auto_render))
        self.assertLess(self.html.index(auto_render), self.html.index(app))
        self.assertIn(f'<script src="{katex}" defer></script>', self.html)
        self.assertIn(f'<script src="{auto_render}" defer></script>', self.html)
        self.assertNotIn("cdn.jsdelivr.net", self.html)
        self.assertNotIn("cdnjs.cloudflare.com", self.html)

    def test_pinned_katex_distribution_is_vendored_with_license_and_fonts(self) -> None:
        vendor = REPO_ROOT / "tools/acm_agent/web_static/vendor/katex"
        self.assertTrue((vendor / "LICENSE").is_file())
        self.assertTrue((vendor / "katex.min.css").is_file())
        self.assertTrue((vendor / "contrib/auto-render.min.js").is_file())
        katex = (vendor / "katex.min.js").read_text(encoding="utf-8")
        self.assertIn('version:"0.18.0"', katex)
        for suffix in ("woff2", "woff", "ttf"):
            self.assertGreater(len(list((vendor / "fonts").glob(f"*.{suffix}"))), 0)

    def test_async_buttons_capture_targets_and_busy_state_is_idempotent(self) -> None:
        self.assertIn('if (button.dataset.busy !== "true")', self.script)
        self.assertIn('delete button.dataset.busyLabel', self.script)
        self.assertIn('delete button.dataset.busyWasDisabled', self.script)
        for selector in (
            "#ai-test-button",
            "#sync-button",
            "#ai-context-button",
            "#plan-check-button",
            "#shutdown-button",
        ):
            start = self.script.index(f'$("{selector}").addEventListener')
            block = self.script[start : start + 900]
            self.assertIn("const button = event.currentTarget", block, selector)
            self.assertNotIn("setBusy(event.currentTarget, false)", block, selector)

    def test_ai_math_renders_assistant_messages_only_after_streaming(self) -> None:
        self.assertIn("function renderAssistantMath(node)", self.script)
        self.assertIn('node?.classList.contains("assistant")', self.script)
        self.assertIn("window.renderMathInElement(node", self.script)
        self.assertIn("throwOnError: false", self.script)
        self.assertIn("trust: false", self.script)
        self.assertIn('if (role === "assistant") renderAssistantMath(node)', self.script)
        self.assertIn("assistant.textContent += item.data.content", self.script)
        self.assertIn("finally {\n    renderAssistantMath(assistant);", self.script)

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

    def test_ai_workbench_is_explicit_and_restores_persisted_conversation(self) -> None:
        for identifier in (
            'id="ai-recommend-button"',
            'id="ai-chat-form"',
            'id="ai-statement-restore"',
            'id="ai-patch-apply"',
        ):
            self.assertIn(identifier, self.html)
        self.assertIn("不会发送账号、notes、聊天、源码或本地路径", self.html)
        self.assertIn("await ensureAiConversation()", self.script)
        self.assertIn('event === "delta"', self.script)
        self.assertIn("window.confirm", self.script)

    def test_patch_preview_shows_only_highlighted_candidate_source(self) -> None:
        self.assertIn('id="ai-patch-code"', self.html)
        self.assertNotIn('id="ai-patch-diff"', self.html)
        self.assertIn("修改后的完整代码", self.html)
        self.assertIn("function highlightCpp(source)", self.script)
        self.assertIn("node.innerHTML = highlightCpp(source)", self.script)
        self.assertIn('typeof data.candidate_code !== "string"', self.script)
        self.assertIn('renderCppSource($("#ai-patch-code"), data.candidate_code)', self.script)
        self.assertNotIn('$("#ai-patch-diff").textContent = data.diff', self.script)
        styles = (
            REPO_ROOT / "tools/acm_agent/web_static/styles.css"
        ).read_text(encoding="utf-8")
        for token_class in (
            ".cpp-keyword",
            ".cpp-type",
            ".cpp-string",
            ".cpp-comment",
            ".cpp-number",
            ".cpp-preprocessor",
        ):
            self.assertIn(token_class, styles)

    def test_ai_key_form_uses_password_input_and_never_browser_storage(self) -> None:
        self.assertIn('id="ai-credential-form"', self.html)
        self.assertIn('name="api_key" type="password"', self.html)
        self.assertIn('autocomplete="new-password"', self.html)
        self.assertIn('id="ai-key-clear"', self.html)
        self.assertIn('api("/api/ai/credential"', self.script)
        self.assertIn('input.value = ""', self.script)
        self.assertNotIn('localStorage.setItem("deepseek', self.script)
        self.assertNotIn('sessionStorage.setItem("deepseek', self.script)
        self.assertNotIn('localStorage.setItem("api', self.script)
        self.assertNotIn('sessionStorage.setItem("api', self.script)


if __name__ == "__main__":
    unittest.main()
