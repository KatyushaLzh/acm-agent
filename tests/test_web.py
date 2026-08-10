from __future__ import annotations

import http.client
import json
from pathlib import Path
import re
import tempfile
import threading
import time
import unittest

from tools.acm_agent.web import (
    ApiProblem,
    FileDialogUnavailable,
    JobManager,
    _problem_from_exception,
    _split_host_header,
    _valid_host,
    create_server,
    find_existing_instance,
)
from tools.acm_agent.storage import PlanRevisionConflict, TagOverrideRevisionConflict
from tools.acm_agent.storage_common import (
    StressArtifactCandidateConflict,
    StressArtifactProofConflict,
    StressBundleCertificationConflict,
    StressPreparationCacheConflict,
    StressSetupSlotConflict,
)
from tools.acm_agent.stress_ai_core import StressPreparationError


REPO_ROOT = Path(__file__).resolve().parents[1]


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

    def workspace_template(self, **values: object) -> dict[str, object]:
        return self._return("workspace_template", values)

    def plan_revisions(self, **values: object) -> dict[str, object]:
        return self._return("plan_revisions", values)

    def plan_preview(self, **values: object) -> dict[str, object]:
        return self._return("plan_preview", values)

    def plan_import(self, **values: object) -> dict[str, object]:
        return self._return("plan_import", values)

    def plan_edit(self, **values: object) -> dict[str, object]:
        if values.get("conflict"):
            raise PlanRevisionConflict("fixture", 2, 3)
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

    def ai_stress_status(self, **values: object) -> dict[str, object]:
        return {"available": True, "sandbox": {"available": True, "backend": "fixture"}}

    def ai_stress_start(self, **values: object) -> dict[str, object]:
        progress_callback = values.pop("progress_callback", None)
        if not callable(progress_callback):
            raise AssertionError("stress progress callback was not injected")
        if values.get("fail_parallel_generator"):
            for step, (stage, label) in enumerate(
                (
                    ("contract", "提取 contract"),
                    ("generator", "并行准备 generator"),
                    ("brute", "并行准备 brute"),
                    ("reference", "并行准备 reference"),
                ),
                start=1,
            ):
                progress_callback(stage, label, step, 4)
            raise StressPreparationError(
                "stress_artifact_stage_failed",
                '一个或多个 helper 准备失败: {"secret":"RAW_TOP_LEVEL_MUST_NOT_LEAK"}',
                details={
                    "primary_failure": {
                        "role": "generator",
                        "substage": "blueprint",
                        "path": "cases[2].coverage_tags",
                        "attempts": 2,
                        "message": "generator blueprint coverage 未闭合",
                        "blueprint": {"raw": "RAW_BLUEPRINT_MUST_NOT_LEAK"},
                        "reasoning": "RAW_REASONING_MUST_NOT_LEAK",
                        "secret": "TOP_SECRET_MUST_NOT_LEAK",
                    },
                    "roles": {
                        "generator": {
                            "stage": "prepare_generator",
                            "role": "generator",
                            "substage": "blueprint",
                            "path": "cases[2].coverage_tags",
                            "attempts": 2,
                            "message": "generator blueprint coverage 未闭合",
                        }
                    },
                },
            )
        for step, (stage, label) in enumerate(
            (
                ("sandbox", "检查隔离环境"),
                ("contract", "让 DeepSeek 提取对拍契约"),
                ("generator", "生成 generator"),
                ("brute", "生成 brute"),
                ("reference", "搜索或生成对拍代码"),
                ("audit", "AI 静态复核"),
                ("preflight", "small 随机预验 16/16"),
                ("helpers", "安全替换三个 helper"),
                ("run", "创建持续对拍 run"),
            ),
            start=1,
        ):
            progress_callback(stage, label, step, 9)
            if values.get("fail_preflight") and stage == "preflight":
                raise StressPreparationError(
                    "stress_preflight_failed",
                    "brute 调试版本触发越界断言",
                    details={
                        "artifact": "brute",
                        "profile": "small",
                        "case_kind": "random",
                        "seed": 2596,
                    },
                )
        values["progress_callback_injected"] = True
        return self._return("ai_stress_start", values)

    def stress_runs(self, **values: object) -> dict[str, object]:
        return {"runs": [{"id": "run-1", "status": "running"}], **self._return("stress_runs", values)}

    def stress_run(self, **values: object) -> dict[str, object]:
        return {"id": values["run_id"], "status": "running", **self._return("stress_run", values)}

    def stress_stop(self, **values: object) -> dict[str, object]:
        return {"id": values["run_id"], "status": "stopped", **self._return("stress_stop", values)}

    def stress_resume(self, **values: object) -> dict[str, object]:
        return {"id": values["run_id"], "status": "running", **self._return("stress_resume", values)}

    def stress_finish(self, **values: object) -> dict[str, object]:
        return {"id": values["run_id"], "status": "completed", **self._return("stress_finish", values)}

    def stress_bundle(self, **values: object) -> dict[str, object]:
        return {"id": values["bundle_id"], **self._return("stress_bundle", values)}

    def stress_bundle_revert(self, **values: object) -> dict[str, object]:
        return self._return("stress_bundle_revert", values)


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
        self.file_picker_result: str | Path | None = None
        self.file_picker_calls: list[tuple[str, Path]] = []

        def file_picker(kind: str, initial_dir: Path) -> str | Path | None:
            self.file_picker_calls.append((kind, initial_dir))
            return self.file_picker_result

        self.server = create_server(
            self.root,
            service=self.service,
            port=0,
            max_port=0,
            token="test-token",
            static_dir=self.static,
            max_request_bytes=512,
            file_picker=file_picker,
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

    def test_local_file_picker_selects_cpp_and_markdown_or_cancels(self) -> None:
        with tempfile.TemporaryDirectory() as external_directory:
            cpp_path = Path(external_directory) / "reference.cpp"
            cpp_path.write_text("int main() {}\n", encoding="utf-8")
            self.file_picker_result = cpp_path
            status, payload, _ = self.request(
                "POST", "/api/local-files/pick", payload={"kind": "cpp"}
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                payload["data"],
                {
                    "cancelled": False,
                    "path": str(cpp_path),
                    "name": "reference.cpp",
                },
            )
            self.assertEqual(self.file_picker_calls[-1], ("cpp", self.root.resolve()))

            markdown_path = Path(external_directory) / "new-notes.md"
            self.file_picker_result = markdown_path
            status, payload, _ = self.request(
                "POST", "/api/local-files/pick", payload={"kind": "markdown"}
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                payload["data"],
                {
                    "cancelled": False,
                    "path": str(markdown_path),
                    "name": "new-notes.md",
                },
            )
            self.assertFalse(markdown_path.exists())

        self.file_picker_result = None
        status, payload, _ = self.request(
            "POST", "/api/local-files/pick", payload={"kind": "cpp"}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"], {"cancelled": True})

    def test_local_file_picker_inherits_auth_and_validates_request(self) -> None:
        status, payload, _ = self.request(
            "POST",
            "/api/local-files/pick",
            payload={"kind": "cpp"},
            token=None,
        )
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "unauthorized")
        self.assertEqual(self.file_picker_calls, [])

        for request_payload in ({}, {"kind": "txt"}, {"kind": "cpp", "extra": True}):
            with self.subTest(payload=request_payload):
                status, payload, _ = self.request(
                    "POST", "/api/local-files/pick", payload=request_payload
                )
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["code"], "invalid_request")

    def test_local_file_picker_reports_busy_and_unavailable(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        def blocking_picker(kind: str, initial_dir: Path) -> None:
            entered.set()
            release.wait(timeout=2)
            return None

        self.server.file_picker = blocking_picker
        first_response: list[tuple[int, dict[str, object], dict[str, str]]] = []
        first_thread = threading.Thread(
            target=lambda: first_response.append(
                self.request("POST", "/api/local-files/pick", payload={"kind": "cpp"})
            ),
            daemon=True,
        )
        first_thread.start()
        self.assertTrue(entered.wait(timeout=1))
        status, payload, _ = self.request(
            "POST", "/api/local-files/pick", payload={"kind": "markdown"}
        )
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "file_dialog_busy")
        release.set()
        first_thread.join(timeout=2)
        self.assertEqual(first_response[0][0], 200)
        self.assertEqual(first_response[0][1]["data"], {"cancelled": True})

        def unavailable_picker(kind: str, initial_dir: Path) -> None:
            raise FileDialogUnavailable("fixture desktop unavailable")

        self.server.file_picker = unavailable_picker
        status, payload, _ = self.request(
            "POST", "/api/local-files/pick", payload={"kind": "cpp"}
        )
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"]["code"], "file_dialog_unavailable")

    def test_local_file_picker_rejects_unsafe_or_invalid_returned_paths(self) -> None:
        invalid_paths: list[tuple[str, str | Path]] = [
            ("cpp", "relative.cpp"),
            ("cpp", r"\\server\share\reference.cpp"),
            ("cpp", self.root / "missing.cpp"),
            ("cpp", self.root / "wrong.txt"),
            ("markdown", self.root / "wrong.txt"),
            ("markdown", self.root / "missing-parent" / "notes.md"),
        ]
        cpp_directory = self.root / "directory.cpp"
        cpp_directory.mkdir()
        invalid_paths.append(("cpp", cpp_directory))
        md_directory = self.root / "directory.md"
        md_directory.mkdir()
        invalid_paths.append(("markdown", md_directory))

        for kind, returned_path in invalid_paths:
            with self.subTest(kind=kind, path=str(returned_path)):
                self.server.file_picker = lambda _kind, _root, value=returned_path: value
                status, payload, _ = self.request(
                    "POST", "/api/local-files/pick", payload={"kind": kind}
                )
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"]["code"], "invalid_file_selection")

        real_cpp = self.root / "real.cpp"
        real_cpp.write_text("int main() {}\n", encoding="utf-8")
        symlink_cpp = self.root / "symlink.cpp"
        try:
            symlink_cpp.symlink_to(real_cpp)
        except OSError:
            pass
        else:
            self.server.file_picker = lambda _kind, _root: symlink_cpp
            status, payload, _ = self.request(
                "POST", "/api/local-files/pick", payload={"kind": "cpp"}
            )
            self.assertEqual(status, 400)
            self.assertEqual(payload["error"]["code"], "invalid_file_selection")

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

    def test_ai_stress_routes_and_dynamic_controls(self) -> None:
        status, payload, _ = self.request("GET", "/api/ai/stress/status")
        self.assertEqual(status, 200)
        self.assertTrue(payload["data"]["sandbox"]["available"])

        status, payload, _ = self.request("GET", "/api/stress/runs?problem=CF1A")
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["problem"], "CF1A")

        status, payload, _ = self.request("GET", "/api/stress/runs/run-1")
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["run_id"], "run-1")

        status, payload, _ = self.request("GET", "/api/stress/bundles/bundle-1")
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["bundle_id"], "bundle-1")

        stress_payload = {
            "problem": "CF1A",
            "generate_generator": True,
            "generate_brute": True,
            "prepare_reference": True,
            "large_profile": True,
            "include_validator": True,
            "allow_validator_degradation": False,
            "unvalidated_large": False,
            "minimal_verification": False,
            "compare": "token",
            "generation_mode": "full_thinking",
            "progress_callback": "client-must-not-control-callback",
        }
        status, payload, _ = self.request("POST", "/api/jobs/ai/stress/start", payload=stress_payload)
        self.assertEqual(status, 202)
        job = self.wait_for_job(payload["data"]["job_id"])
        self.assertEqual(job["result"]["operation"], "ai_stress_start")
        self.assertEqual(job["result"]["problem"], "CF1A")
        self.assertTrue(job["result"]["large_profile"])
        self.assertTrue(job["result"]["include_validator"])
        self.assertFalse(job["result"]["allow_validator_degradation"])
        self.assertFalse(job["result"]["unvalidated_large"])
        self.assertFalse(job["result"]["minimal_verification"])
        self.assertEqual(job["result"]["generation_mode"], "full_thinking")
        self.assertTrue(job["result"]["progress_callback_injected"])
        self.assertEqual(
            job["progress"],
            {
                "stage": "run",
                "label": "创建持续对拍 run",
                "step": 9,
                "total": 9,
                "updated_at": job["progress"]["updated_at"],
            },
        )

        status, payload, _ = self.request(
            "POST",
            "/api/jobs/ai/stress/start",
            payload={
                "problem": "CF1A",
                "generate_generator": True,
                "prepare_reference_primary": True,
                "prepare_reference_secondary": True,
                "large_profile": True,
                "include_validator": False,
                "minimal_verification": True,
                "unvalidated_large": True,
            },
        )
        self.assertEqual(status, 202)
        minimal_job = self.wait_for_job(payload["data"]["job_id"])
        self.assertTrue(minimal_job["result"]["minimal_verification"])
        self.assertTrue(minimal_job["result"]["large_profile"])
        self.assertFalse(minimal_job["result"]["include_validator"])
        self.assertTrue(minimal_job["result"]["unvalidated_large"])

        failed_payload = {**stress_payload, "fail_preflight": True}
        status, payload, _ = self.request(
            "POST", "/api/jobs/ai/stress/start", payload=failed_payload
        )
        self.assertEqual(status, 202)
        failed = self.wait_for_job(payload["data"]["job_id"])
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(
            {
                key: failed["error"][key]
                for key in ("artifact", "profile", "case_kind", "seed")
            },
            {
                "artifact": "brute",
                "profile": "small",
                "case_kind": "random",
                "seed": 2596,
            },
        )
        self.assertEqual(failed["error"]["stage"], "preflight")
        self.assertEqual(failed["error"]["stage_label"], "small 随机预验 16/16")
        self.assertTrue(failed["error"]["helpers_unchanged"])
        self.assertFalse(failed["error"]["run_created"])

        parallel_payload = {**stress_payload, "fail_parallel_generator": True}
        status, payload, _ = self.request(
            "POST", "/api/jobs/ai/stress/start", payload=parallel_payload
        )
        self.assertEqual(status, 202)
        parallel_failed = self.wait_for_job(payload["data"]["job_id"])
        self.assertEqual(parallel_failed["status"], "failed")
        # The last parallel progress belongs to reference, but the provider's
        # structured primary failure identifies generator blueprint as root.
        self.assertEqual(parallel_failed["progress"]["stage"], "reference")
        self.assertEqual(parallel_failed["progress"]["step"], 4)
        self.assertEqual(parallel_failed["progress"]["total"], 4)
        root_error = parallel_failed["error"]
        self.assertEqual(root_error["stage_label"], "并行准备 reference")
        self.assertEqual(
            root_error["root_cause_label"],
            "generator · blueprint · cases[2].coverage_tags · attempt 2",
        )
        self.assertEqual(
            root_error["root_cause_message"],
            "generator blueprint coverage 未闭合",
        )
        serialized_error = json.dumps(root_error, ensure_ascii=False)
        self.assertNotIn("RAW_BLUEPRINT_MUST_NOT_LEAK", serialized_error)
        self.assertNotIn("RAW_REASONING_MUST_NOT_LEAK", serialized_error)
        self.assertNotIn("TOP_SECRET_MUST_NOT_LEAK", serialized_error)
        self.assertNotIn("RAW_TOP_LEVEL_MUST_NOT_LEAK", serialized_error)
        self.assertTrue(root_error["helpers_unchanged"])
        self.assertFalse(root_error["run_created"])

        for action, expected in (
            ("stop", "stress_stop"),
            ("resume", "stress_resume"),
            ("finish", "stress_finish"),
        ):
            status, payload, _ = self.request("POST", f"/api/stress/runs/run-1/{action}", payload={})
            self.assertEqual(status, 200)
            self.assertEqual(payload["data"]["operation"], expected)

        status, payload, _ = self.request("POST", "/api/jobs/stress/bundles/bundle-1/revert", payload={})
        self.assertEqual(status, 202)
        job = self.wait_for_job(payload["data"]["job_id"])
        self.assertEqual(job["result"]["operation"], "stress_bundle_revert")

        status, payload, _ = self.request(
            "POST",
            "/api/jobs/ai/stress/start",
            payload={"problem": "CF1A", "medium_profile": False},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")
        self.assertIn("medium_profile", payload["error"]["message"])

        status, payload, _ = self.request(
            "POST",
            "/api/jobs/ai/stress/start",
            payload={
                "problem": "CF1A",
                "large_profile": True,
                "medium_profile": False,
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")

    def test_ai_stress_has_no_interactive_wait_or_callback_injection(self) -> None:
        status, payload, _ = self.request(
            "POST",
            "/api/jobs/ai/stress/start",
            payload={
                "problem": "CF1A",
                "generate_generator": True,
            },
        )
        self.assertEqual(status, 202)
        job = self.wait_for_job(payload["data"]["job_id"])
        self.assertEqual(job["status"], "succeeded")
        self.assertNotIn("waiting", job)
        self.assertNotIn("user_hint_provider", job["result"])
        self.assertNotIn("contract_reviewer", job["result"])

    def test_ai_stress_rejects_removed_fields_and_endpoints(self) -> None:
        for field in ("review_contract", "no_hints"):
            with self.subTest(field=field):
                status, response, _ = self.request(
                    "POST",
                    "/api/jobs/ai/stress/start",
                    payload={"problem": "CF1A", field: True},
                )
                self.assertEqual(status, 400)
                self.assertEqual(response["error"]["code"], "invalid_request")
                self.assertIn(field, response["error"]["message"])

        for suffix in ("hint", "contract-review", "cancel"):
            with self.subTest(suffix=suffix):
                status, response, _ = self.request(
                    "POST", f"/api/jobs/ai/stress/fixture/{suffix}", payload={}
                )
                self.assertEqual(status, 405)
                self.assertEqual(response["error"]["code"], "method_not_allowed")

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

    def test_workspace_template_get_route(self) -> None:
        status, payload, _ = self.request("GET", "/api/workspace/template")
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["operation"], "workspace_template")

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

    def test_malformed_credentials_answer_instead_of_dropping_the_connection(self) -> None:
        # http.server decodes headers as latin-1, so a client can put any byte
        # >0x7F into a header value.  Both guards below used to raise out of
        # _authorize() -- which only catches ApiProblem -- and out of the
        # handler entirely, so the client saw a dropped connection with no
        # HTTP response rather than a refusal.  The Host guard runs before the
        # token check, so that path was reachable unauthenticated.
        status, payload, _ = self.request("GET", "/api/bootstrap", token="tokén")
        self.assertEqual(status, 401)
        self.assertEqual(payload["error"]["code"], "unauthorized")

        for port_text in ("²", "³", "¹"):
            with self.subTest(port=port_text):
                status, payload, _ = self.request(
                    "GET",
                    "/api/bootstrap",
                    headers={"Host": f"127.0.0.1:{port_text}"},
                )
                self.assertEqual(status, 403)
                self.assertEqual(payload["error"]["code"], "invalid_host")

        # The server still answers normally afterwards: the malformed requests
        # must not have killed the listener or wedged the handler thread.
        status, payload, _ = self.request("GET", "/api/bootstrap")
        self.assertEqual(status, 200)

    def test_host_port_accepts_only_ascii_digits(self) -> None:
        # str.isdigit() is True for latin-1 superscripts that int() rejects, so
        # guarding int() with isdigit() alone still raised ValueError.
        self.assertEqual(_split_host_header("127.0.0.1:8765"), ("127.0.0.1", 8765))
        self.assertEqual(_split_host_header("[::1]:8765"), ("::1", 8765))
        for hostile in ("127.0.0.1:²", "[::1]:³", "127.0.0.1:¹"):
            with self.subTest(host=hostile):
                self.assertEqual(_split_host_header(hostile), ("", None))
                self.assertFalse(_valid_host(hostile, 8765))

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

        for path in (
            "/api/knowledge/proposals/proposal-1/refresh",
            "/api/jobs/knowledge/proposals/proposal-1/apply",
            "/api/jobs/stress/bundles/bundle-1/revert",
            "/api/stress/runs/run-1/stop",
            "/api/ai/conversations/conv-1/messages",
        ):
            with self.subTest(path=path):
                status, response, _ = self.request("GET", path)
                self.assertEqual(status, 405)
                self.assertEqual(response["error"]["code"], "method_not_allowed")

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
    def test_stress_persistence_conflicts_have_typed_http_409_mapping(self) -> None:
        cases = (
            (StressSetupSlotConflict("run-2", "run-1"), "stress_setup_active"),
            (
                StressPreparationCacheConflict("cache-key"),
                "stress_preparation_cache_conflict",
            ),
            (
                StressArtifactCandidateConflict("candidate-id"),
                "stress_artifact_candidate_conflict",
            ),
            (
                StressArtifactProofConflict("proof-key"),
                "stress_artifact_proof_conflict",
            ),
            (
                StressBundleCertificationConflict("cert-key"),
                "stress_bundle_certification_conflict",
            ),
        )
        for error, code in cases:
            with self.subTest(code=code):
                problem = _problem_from_exception(error)
                self.assertEqual(problem.status, 409)
                self.assertEqual(problem.code, code)

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
        started = threading.Event()
        release = threading.Event()

        def blocking_work() -> dict[str, int]:
            started.set()
            release.wait(timeout=2)
            return {"n": 1}

        try:
            first = manager.submit("one", blocking_work)
            self.assertTrue(started.wait(timeout=1))
            second = manager.submit("two", lambda: {"n": 2})
            with self.assertRaises(ApiProblem) as raised:
                manager.submit("three", lambda: {"n": 3})
            self.assertEqual(raised.exception.status, 503)
            self.assertEqual(raised.exception.code, "job_capacity_exhausted")
            self.assertIsNotNone(manager.get(first["job_id"]))
            self.assertIsNotNone(manager.get(second["job_id"]))

            release.set()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if manager.get(second["job_id"])["status"] == "succeeded":
                    break
                time.sleep(0.01)
            third = manager.submit("three", lambda: {"n": 3})
            self.assertIsNotNone(manager.get(third["job_id"]))
            self.assertLessEqual(
                sum(
                    manager.get(item["job_id"]) is not None
                    for item in (first, second, third)
                ),
                2,
            )
        finally:
            release.set()
            # close() is idempotent for ThreadPoolExecutor.
            manager.close(wait=True)

    def test_progress_is_normalized_and_failure_keeps_stage(self) -> None:
        manager = JobManager(threading.RLock(), capacity=10)

        def work(progress_callback):
            progress_callback(" contract ", " 提取对拍契约 ", 2, 7)
            raise ValueError("fixture contract failure")

        try:
            submitted = manager.submit("stress", work, with_progress=True)
            manager.close(wait=True)
            job = manager.get(submitted["job_id"])
            self.assertEqual(job["status"], "failed")
            self.assertEqual(
                job["progress"],
                {
                    "stage": "contract",
                    "label": "提取对拍契约",
                    "step": 2,
                    "total": 7,
                    "updated_at": job["progress"]["updated_at"],
                },
            )
            self.assertEqual(job["error"]["stage"], "contract")
            self.assertEqual(job["error"]["stage_label"], "提取对拍契约")
            self.assertEqual(job["error"]["message"], "fixture contract failure")
        finally:
            manager.close(wait=True)

    def test_strict_validator_failure_hides_diagnostics(self) -> None:
        manager = JobManager(threading.RLock(), capacity=10)

        def work():
            raise StressPreparationError(
                "stress_validator_positive_probe_failed",
                "hidden probe rejected seed 42",
                details={
                    "validator_strict_certification_failed": True,
                    "artifact": "validator",
                    "profile": "contract_probe",
                    "seed": 42,
                    "roles": {"validator": {"message": "hidden diagnostic"}},
                },
            )

        try:
            submitted = manager.submit("ai_stress_start", work)
            manager.close(wait=True)
            job = manager.get(submitted["job_id"])
            self.assertEqual(job["status"], "failed")
            self.assertEqual(
                job["error"],
                {
                    "code": "validator_strict_certification_failed",
                    "message": (
                        "validator 严格认证未通过，已终止 AI 对拍；"
                        "helper 未应用，run 未创建。"
                    ),
                    "validator_strict_certification_failed": True,
                    "helpers_unchanged": True,
                    "run_created": False,
                },
            )
            serialized = json.dumps(job, ensure_ascii=False)
            self.assertNotIn("seed", serialized)
            self.assertNotIn("contract_probe", serialized)
            self.assertNotIn("hidden diagnostic", serialized)
        finally:
            manager.close(wait=True)


class StaticPlanEditorTest(unittest.TestCase):
    def setUp(self) -> None:
        self.static = REPO_ROOT / "tools/acm_agent/web_static"
        self.modules = {
            path.name: path.read_text(encoding="utf-8")
            for path in sorted(self.static.glob("*.js"))
        }
        self.script = "\n".join(self.modules.values())
        self.html = (
            self.static / "index.html"
        ).read_text(encoding="utf-8")
        self.styles = (
            self.static / "styles.css"
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
        self.assertIn(f'<script type="module" src="{app}"></script>', self.html)
        self.assertNotIn("cdn.jsdelivr.net", self.html)
        self.assertNotIn("cdnjs.cloudflare.com", self.html)

    def test_native_modules_are_small_same_origin_and_acyclic(self) -> None:
        required = {
            "app.js",
            "core.js",
            "sse.js",
            "view_ai.js",
            "view_plans.js",
            "view_review.js",
            "view_today.js",
            "view_workbench.js",
        }
        self.assertTrue(required <= set(self.modules))
        imports = {
            name: {
                match.group(1).removeprefix("./")
                for match in re.finditer(r'from\s+["\'](\./[^"\']+)["\']', script)
            }
            for name, script in self.modules.items()
        }
        for name, dependencies in imports.items():
            self.assertTrue(dependencies <= set(self.modules), (name, dependencies - set(self.modules)))

        visiting: set[str] = set()
        visited: set[str] = set()

        def visit(name: str) -> None:
            self.assertNotIn(name, visiting, f"cyclic ESM dependency at {name}")
            if name in visited:
                return
            visiting.add(name)
            for dependency in imports[name]:
                visit(dependency)
            visiting.remove(name)
            visited.add(name)

        for name in self.modules:
            visit(name)

        self.assertEqual(self.script.count("async function api("), 1)
        self.assertEqual(self.modules["core.js"].count("fetch("), 1)
        self.assertEqual(self.modules["sse.js"].count("fetch("), 1)
        for name in set(self.modules) - {"core.js", "sse.js"}:
            self.assertNotIn("fetch(", self.modules[name], name)

    def test_native_module_bindings_have_no_unresolved_calls(self) -> None:
        exports: dict[str, set[str]] = {}
        for name, script in self.modules.items():
            exported: set[str] = set()
            for match in re.finditer(r"export\s*\{(.*?)\}\s*;", script, re.S):
                exported.update(
                    item.strip().split(" as ")[-1]
                    for item in match.group(1).split(",")
                    if item.strip()
                )
            exports[name] = exported

        browser_globals = {
            "AbortController", "Array", "Blob", "Boolean", "Date", "Error",
            "Event", "FileReader", "FormData", "Intl", "JSON", "Map", "Math",
            "Number", "Object", "Promise", "Set", "String", "TextDecoder",
            "URL", "URLSearchParams", "Uint8Array", "clearTimeout",
            "decodeURIComponent", "encodeURIComponent", "fetch", "parseFloat",
            "parseInt", "setTimeout",
        }
        ignored_calls = {"catch", "for", "function", "if", "switch", "while"}

        for name, script in self.modules.items():
            declared = set(browser_globals)
            for match in re.finditer(
                r'import\s*\{(.*?)\}\s*from\s*["\'](\./[^"\']+)["\']',
                script,
                re.S,
            ):
                dependency = match.group(2).removeprefix("./")
                imported = {
                    item.strip().split(" as ")[0]
                    for item in match.group(1).split(",")
                    if item.strip()
                }
                self.assertTrue(
                    imported <= exports[dependency],
                    (name, dependency, imported - exports[dependency]),
                )
                declared.update(
                    item.strip().split(" as ")[-1]
                    for item in match.group(1).split(",")
                    if item.strip()
                )
            declared.update(
                match.group(1)
                for match in re.finditer(
                    r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)",
                    script,
                )
            )
            declared.update(
                match.group(1)
                for match in re.finditer(
                    r"\bfunction\s+([A-Za-z_$][\w$]*)",
                    script,
                )
            )
            parameter_groups = [
                match.group(1)
                for pattern in (
                    r"\bfunction\s+[A-Za-z_$][\w$]*\s*\((.*?)\)",
                    r"\((.*?)\)\s*=>",
                    r"(?m)^\s*(?:async\s+)?[A-Za-z_$][\w$]*\s*\((.*?)\)\s*\{",
                    r"\bcatch\s*\((.*?)\)",
                )
                for match in re.finditer(pattern, script, re.S)
            ]
            for parameters in parameter_groups:
                declared.update(re.findall(r"[A-Za-z_$][\w$]*", parameters))
            declared.update(
                match.group(1)
                for match in re.finditer(r"\b([A-Za-z_$][\w$]*)\s*=>", script)
            )
            declared.update(
                match.group(1)
                for match in re.finditer(
                    r"(?m)^\s*([A-Za-z_$][\w$]*)\s*\([^)]*\)\s*\{",
                    script,
                )
            )
            calls = {
                match.group(1)
                for match in re.finditer(
                    r"(?<![.\w$])([A-Za-z_$][\w$]*)\s*\(",
                    script,
                )
            } - ignored_calls
            self.assertFalse(calls - declared, (name, calls - declared))

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

    def test_ai_stress_ui_is_explicit_and_exposes_persistent_controls(self) -> None:
        self.assertIn('id="ai-stress-panel"', self.html)
        self.assertIn('name="enabled" type="checkbox"', self.html)
        for option in (
            "generate_generator",
            "prepare_reference_primary",
            "prepare_reference_secondary",
            "large_profile",
        ):
            self.assertIn(f'name="{option}" type="checkbox" checked', self.html)
        self.assertNotIn('name="minimal_verification"', self.html)
        validator_label = (
            "启用 validator（启用后升级为完整严格认证，"
            "正确性更高但生成成功率显著下降）"
        )
        self.assertIn('name="include_validator" type="checkbox"', self.html)
        self.assertNotIn('name="include_validator" type="checkbox" checked', self.html)
        self.assertIn(validator_label, self.html)
        for identifier in (
            'id="ai-stress-stop"',
            'id="ai-stress-resume"',
            'id="ai-stress-finish"',
            'id="ai-stress-artifacts"',
            'id="ai-stress-revert"',
        ):
            self.assertIn(identifier, self.html)
        self.assertIn('api("/api/ai/stress/status")', self.script)
        self.assertIn('api("/api/stress/runs")', self.script)
        self.assertIn('"/api/jobs/ai/stress/start"', self.script)
        self.assertIn("scheduleStressPoll(id)", self.script)
        self.assertIn("function stressResumable(status)", self.script)
        self.assertIn('"mismatch", "oracle_conflict"', self.script)
        self.assertIn("function stressBundleOf(payload)", self.script)
        self.assertIn("stressBundleOf(await api(`/api/stress/bundles/", self.script)
        self.assertIn("function stressSourceLink(url)", self.script)
        self.assertIn("相关数据已经保存到: ${sourceDirectory || failure}", self.script)
        self.assertIn("run.user_source_path", self.script)
        self.assertIn('target="_blank" rel="noopener noreferrer"', self.script)
        self.assertNotIn("textContent = JSON.stringify(bundle, null, 2)", self.script)
        self.assertIn("查看 helper 来源", self.html)
        self.assertIn("#ai-stress-stop:disabled, #ai-stress-resume:disabled, #ai-stress-finish:disabled { cursor: default; }", self.styles)
        self.assertIn("复用现有 helper，从保存的 next seed 继续", self.script)
        self.assertIn("function stressFinishable(status)", self.script)
        self.assertIn('controlStress("finish", event.currentTarget)', self.script)
        self.assertIn("run.config?.rate_base_total ?? 0", self.script)
        self.assertIn("total - rateBaseTotal", self.script)
        self.assertIn('["累计", total]', self.script)
        self.assertIn('["本轮 / 速度"', self.script)
        self.assertIn('["large", run.large_count ?? run.large_cases ?? 0]', self.script)
        self.assertNotIn("medium_count", self.script)
        self.assertNotIn("medium_cases", self.script)
        self.assertNotIn("medium（旧版）", self.script)
        self.assertIn("retiredProfile || !stressResumable(status)", self.script)
        self.assertIn("该运行协议已停用，历史状态仅供查看，不能继续。", self.script)
        self.assertIn("32 MiB 输入、16 MiB 输出", self.html)
        self.assertIn("function jobProgressLabel(progress, fallback)", self.script)
        self.assertIn("function jobFailureDetails(error)", self.script)
        self.assertIn("function renderStressPreparationFailure(message)", self.script)
        self.assertIn("renderStressPreparationFailure(error.message)", self.script)
        self.assertIn("(_progress, _job, currentLabel) => setBusy(button, true, currentLabel)", self.script)
        self.assertIn('`阶段“${stageLabel}”失败：${message}`', self.script)
        self.assertIn("const rootCauseLabel = String(error.root_cause_label", self.script)
        self.assertIn("const rootCauseMessage = String(error.root_cause_message", self.script)
        self.assertIn("根因${rootCauseLabel ?", self.script)
        self.assertIn("const summary = rootCauseMessage", self.script)
        self.assertIn("error.helpers_unchanged === true", self.script)
        self.assertIn("error.run_created === false", self.script)
        self.assertIn("旧 helper 未修改，run 未创建。", self.script)
        self.assertIn('artifact: "产物"', self.script)
        self.assertIn('case_kind: "case"', self.script)
        self.assertIn(
            'name="preparation_timeout_seconds" type="number" min="60" max="1800" step="1" value="600"',
            self.html,
        )
        self.assertIn('name="cache_mode"', self.html)
        self.assertIn('<option value="cold">Cold', self.html)
        self.assertNotIn("固定使用非 thinking 模式", self.html)
        self.assertIn('name="generation_mode"', self.html)
        self.assertIn('<option value="hybrid" selected>', self.html)
        self.assertIn("第 8 分钟硬停止 provider", self.html)
        self.assertIn("preparation_timeout_seconds: preparationTimeout", self.script)
        self.assertIn("cache_mode: form.elements.cache_mode.value", self.script)
        self.assertIn("generation_mode: form.elements.generation_mode.value", self.script)
        self.assertIn("const includeValidator = form.elements.include_validator.checked", self.script)
        self.assertIn("include_validator: includeValidator", self.script)
        self.assertIn("minimal_verification: minimalVerification", self.script)
        self.assertIn("const minimalVerification = !includeValidator", self.script)
        self.assertIn("unvalidated_large: minimalVerification && largeProfile", self.script)
        self.assertNotIn("function syncAiStressVerificationMode", self.script)
        self.assertNotIn('["helper 来源"', self.script)
        self.assertNotIn('metrics.push(["认证"', self.script)
        self.assertNotIn("仅三方 oracle 判定，无 large", self.script)
        self.assertIn("function stressElapsedEnd(run, status)", self.script)
        self.assertIn('normalizedStatus === "stop_requested"', self.script)
        self.assertIn("run.completed_at || run.updated_at", self.script)
        self.assertIn("async function waitForStressPause(runId, initialPayload)", self.script)
        self.assertIn("Date.now() + 10000", self.script)
        self.assertIn("result = await waitForStressPause(state.stressRunId, result)", self.script)
        self.assertIn("if (includeValidator)", self.script)
        self.assertIn("payload.allow_validator_degradation = false", self.script)
        self.assertIn("payload.unvalidated_large = false", self.script)
        self.assertIn(
            '"validator 严格认证未通过，已终止 AI 对拍；helper 未应用，run 未创建。"',
            self.script,
        )
        self.assertIn("jobError.validator_strict_certification_failed === true", self.script)
        self.assertIn("includeValidator && isStrictValidatorCertificationFailure(error)", self.script)
        self.assertIn('role === "validator" ? "" : String(item.source_url', self.script)
        self.assertIn("preparationTimeout < 60 || preparationTimeout > 1800", self.script)
        self.assertIn("Fast 降级 ${fastFallback ?", self.script)
        self.assertIn("推理 token ${Math.max(0, reasoningTokens).toFixed(0)}", self.script)
        self.assertIn("可用剩余 ${Math.max(0, usableRemaining).toFixed(1)}s", self.script)
        self.assertIn("验证预留 ${Math.max(0, reservedValidation).toFixed(1)}s", self.script)
        self.assertIn("剩余 ${Math.max(0, remaining).toFixed(1)}s", self.script)
        self.assertIn("阶段耗时 ${Math.max(0, stageElapsed).toFixed(1)}s", self.script)
        self.assertIn("软预算 ${Math.max(0, softBudget).toFixed(1)}s", self.script)

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
