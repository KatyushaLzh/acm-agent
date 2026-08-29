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
from tools.acm_agent.credentials import CredentialStoreError
from tools.acm_agent.storage import PlanRevisionConflict, TagOverrideRevisionConflict


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
        return {
            **self._return("setup", values),
            "validated": not bool(values.get("skip_validate")),
            "accounts": {
                "codeforces": str(values.get("codeforces") or ""),
                "luogu": str(values.get("luogu") or ""),
            },
        }

    def recommendations(self, **values: object) -> dict[str, object]:
        return self._return("recommendations", values)

    def ai_status(self, **values: object) -> dict[str, object]:
        return {
            "ok": True,
            "api_key_detected": False,
            "settings": {},
            "coaching_delivery_mode": "resilient",
            "secure_store": {
                "backend": "dpapi",
                "available": True,
                "error_code": None,
                "message": "Windows DPAPI 可用。",
            },
        }

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

    def ai_providers(self, **values: object) -> dict[str, object]:
        return {"ok": True, "providers": [], "profiles": {}}

    def ai_profiles(self, **values: object) -> dict[str, object]:
        return {"ok": True, "profiles": {}}

    def ai_credentials(self, **values: object) -> dict[str, object]:
        return {"ok": True, "credentials": []}

    def ai_provider_upsert(self, **values: object) -> dict[str, object]:
        return self._return("ai_provider_upsert", values)

    def ai_provider_disable(self, **values: object) -> dict[str, object]:
        return self._return("ai_provider_disable", values)

    def ai_connections(self, **values: object) -> dict[str, object]:
        return {"ok": True, "connections": []}

    def ai_connection_upsert(self, **values: object) -> dict[str, object]:
        values = {key: value for key, value in values.items() if key != "api_key"}
        return self._return("ai_connection_upsert", values)

    def ai_connection_refresh(self, **values: object) -> dict[str, object]:
        return self._return("ai_connection_refresh", values)

    def ai_connection_delete(self, **values: object) -> dict[str, object]:
        return self._return("ai_connection_delete", values)

    def ai_profile_update(self, **values: object) -> dict[str, object]:
        return self._return("ai_profile_update", values)

    def ai_policy_update(self, **values: object) -> dict[str, object]:
        return self._return("ai_policy_update", values)

    def ai_costs(self, **values: object) -> dict[str, object]:
        return {
            "ok": True,
            "audit": {
                "totals": {},
                "deepseek_cost": {
                    "provider_id": "deepseek",
                    "runs": 1,
                    "known_estimated_cny": 0.25,
                    "currency": "CNY",
                    "unknown_cost_runs": 0,
                    "partial_cost_runs": 0,
                },
                "all_model_tokens": {
                    "runs": 2,
                    "total_tokens_known": 300,
                    "input_tokens_known": 240,
                    "output_tokens_known": 60,
                    "unknown_runs": 0,
                },
                "cache_metrics": {
                    "cache_read_tokens_known": 120,
                    "eligible_input_tokens": 240,
                    "hit_rate_percent": 50.0,
                    "observed_runs": 2,
                    "unknown_runs": 0,
                    "invalid_runs": 0,
                },
                "groups": [],
                "recent_runs": [],
            },
        }

    def ai_costs_reprice(self, **values: object) -> dict[str, object]:
        return {
            "ok": True,
            "repricing": {"runs": 0},
            "audit": self.ai_costs()["audit"],
        }

    def ai_cache_status(self, **values: object) -> dict[str, object]:
        self.calls.append(("ai_cache_status", dict(values)))
        return {
            "entries": 3,
            "bytes": 1024,
            "metrics": {
                "local_exact_hit_rate": 0.5,
                "provider_avoidance": 0.6,
            },
        }

    def ai_cache_clear(self, **values: object) -> dict[str, object]:
        return {**self._return("ai_cache_clear", values), "removed": 2}

    def ai_cache_prune(self, **values: object) -> dict[str, object]:
        return {**self._return("ai_cache_prune", values), "removed": 1}

    def ai_credential_slot(self, **values: object) -> dict[str, object]:
        self.calls.append(("ai_credential_slot", dict(values)))
        return {"ok": True, "slot": values.get("slot"), "detected": True}

    def ai_provider_test(self, **values: object) -> dict[str, object]:
        return {"ok": True, **self._return("ai_provider_test", values)}

    def ai_model_verify(self, **values: object) -> dict[str, object]:
        return {"ok": True, **self._return("ai_model_verify", values)}

    def ai_recommendations(self, **values: object) -> dict[str, object]:
        return self._return("ai_recommendations", values)

    def ai_plan_preview(self, **values: object) -> dict[str, object]:
        progress = values.pop("_progress_callback", None)
        if callable(progress):
            progress(
                {
                    "phase": "selecting",
                    "round": 1,
                    "total_rounds": 5,
                    "accepted_count": 1,
                    "requested_count": 1,
                    "message": "第 1/5 轮，已确定 1/1 题",
                }
            )
        return self._return("ai_plan_preview", values)

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

    def ai_conversation_switch(self, **values: object) -> dict[str, object]:
        return {**self._return("ai_conversation_switch", values), "conversation_id": "conv-2"}

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

    def knowledge_preview(self, **values: object) -> dict[str, object]:
        return self._return("knowledge_preview", values)

    def start(self, **values: object) -> dict[str, object]:
        return self._return("start", values)

    def close(self, **values: object) -> dict[str, object]:
        return self._return("close", values)

    def weekly_review(self, **values: object) -> dict[str, object]:
        return self._return("weekly_review", values)

    def review_queue(self, **values: object) -> dict[str, object]:
        return self._return("review_queue", values)

    def review_queue_add(self, **values: object) -> dict[str, object]:
        return self._return("review_queue_add", values)

    def review_queue_remove(self, **values: object) -> dict[str, object]:
        return self._return("review_queue_remove", values)

    def review_queue_clear(self, **values: object) -> dict[str, object]:
        return self._return("review_queue_clear", values)

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
        progress = values.pop("_progress_callback", None)
        if callable(progress):
            progress(
                {
                    "phase": "complete",
                    "platform": str(values.get("platform") or "all"),
                    "step": 1,
                    "total": 1,
                    "completed": 1,
                    "failed": 0,
                    "message": "fixture sync complete",
                    "started_at": "2026-08-25T00:00:00+00:00",
                    "last_activity_at": "2026-08-25T00:00:01+00:00",
                    "usable": True,
                }
            )
        if values.get("fail") or values.get("platform") not in {None, "all", "codeforces", "luogu"}:
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
        self.assertIn("img-src 'self' data: blob:", csp)
        self.assertNotIn("'unsafe-inline'", csp)
        self.assertNotIn("https:", csp)

        status, payload, headers = self.request("GET", "/static/app.js", token=None)
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "text/javascript; charset=utf-8")
        self.assertEqual(headers["cache-control"], "no-store")
        self.assertIn("console.log", payload["raw"])

    def test_stage2_management_http_routes_and_named_credential_no_echo(self) -> None:
        provider_body = {
            "provider_id": "relay", "name": "Relay",
            "base_url": "https://relay.example/v1", "credential_slot": "relay",
            "models": {"relay-model": {"capabilities": {"text_chat": True, "usage": True}}},
        }
        status, payload, _ = self.request("POST", "/api/ai/providers", payload=provider_body)
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["operation"], "ai_provider_upsert")

        secret = "web-stage2-secret"
        status, payload, _ = self.request("POST", "/api/ai/credentials", payload={
            "slot": "relay", "provider_id": "relay", "api_key": secret,
        })
        self.assertEqual(status, 200)
        self.assertNotIn(secret, json.dumps(payload))

        status, payload, _ = self.request("POST", "/api/ai/profiles", payload={
            "profile_id": "recommendation", "provider_id": "relay",
            "model": "relay-model", "thinking": False,
        })
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["operation"], "ai_profile_update")

        status, payload, _ = self.request("POST", "/api/ai/providers/disable", payload={
            "provider_id": "relay",
        })
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["operation"], "ai_provider_disable")

    def test_stage3_policy_and_cost_audit_routes(self) -> None:
        status, payload, _ = self.request("GET", "/api/ai/costs")
        self.assertEqual(status, 200)
        self.assertIn("totals", payload["data"]["audit"])
        self.assertEqual(
            payload["data"]["audit"]["deepseek_cost"]["provider_id"], "deepseek"
        )
        self.assertEqual(
            payload["data"]["audit"]["all_model_tokens"]["total_tokens_known"], 300
        )
        self.assertEqual(
            payload["data"]["audit"]["cache_metrics"]["hit_rate_percent"], 50.0
        )

        policy = {"budgets": {}, "fallbacks": {}, "hard_limits": {}}
        status, payload, _ = self.request(
            "POST", "/api/ai/policy",
            payload={"policy": policy, "coaching_delivery_mode": "low_latency"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["operation"], "ai_policy_update")
        self.assertEqual(payload["data"]["policy"], policy)
        self.assertEqual(payload["data"]["coaching_delivery_mode"], "low_latency")

        status, payload, _ = self.request(
            "POST", "/api/ai/costs/reprice", payload={}
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["repricing"]["runs"], 0)

    def test_simplified_connection_profile_verification_and_conversation_switch_routes(self) -> None:
        selection = {
            "model_ref": {"provider_id": "relay", "model": "same-name"},
            "reasoning_strength": "medium",
        }
        secret = "web-connection-secret"
        status, payload, _ = self.request("POST", "/api/ai/connections", payload={
            "display_name": "Relay", "base_url": "https://relay.example/v1", "api_key": secret,
        })
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["operation"], "ai_connection_upsert")
        self.assertNotIn(secret, json.dumps(payload))

        status, payload, _ = self.request("POST", "/api/ai/profiles", payload={
            "profile_id": "recommendation", **selection,
        })
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["model_ref"], selection["model_ref"])

        status, payload, _ = self.request("POST", "/api/jobs/ai/models/verify", payload={
            "profile_id": "recommendation", **selection,
        })
        self.assertEqual(status, 202)
        job = self.wait_for_job(payload["data"]["job_id"])
        self.assertEqual(job["result"]["operation"], "ai_model_verify")

        status, payload, _ = self.request(
            "POST", "/api/ai/conversations/conv-1/switch", payload=selection
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["conversation_id"], "conv-2")
        self.assertEqual(payload["data"]["model_ref"], selection["model_ref"])

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

    def test_local_cpp_picker_uses_latest_problem_source_directory(self) -> None:
        older = self.root / "2026" / "8" / "24"
        latest = self.root / "2026" / "8" / "25"
        older.mkdir(parents=True)
        latest.mkdir(parents=True)
        (older / "P2617.cpp").write_text("int main() {}\n", encoding="utf-8")
        (latest / "P2617.cpp").write_text("int main() {}\n", encoding="utf-8")
        selected = latest / "generator.cpp"
        selected.write_text("int main() {}\n", encoding="utf-8")
        self.file_picker_result = selected

        status, payload, _ = self.request(
            "POST",
            "/api/local-files/pick",
            payload={"kind": "cpp", "problem": "P2617"},
        )

        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["path"], str(selected))
        self.assertEqual(self.file_picker_calls[-1], ("cpp", latest.resolve()))

        self.file_picker_result = None
        status, _, _ = self.request(
            "POST",
            "/api/local-files/pick",
            payload={"kind": "cpp", "problem": "missing-problem"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(self.file_picker_calls[-1], ("cpp", self.root.resolve()))

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

        for request_payload in (
            {},
            {"kind": "txt"},
            {"kind": "cpp", "extra": True},
            {"kind": "cpp", "problem": 2617},
            {"kind": "markdown", "problem": "P2617"},
        ):
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

        status, payload, _ = self.request(
            "POST", "/api/jobs/sync", payload={"platform": "invalid"}
        )
        self.assertEqual(status, 202)
        job = self.wait_for_job(payload["data"]["job_id"])
        self.assertEqual(job["status"], "failed")
        self.assertEqual(job["error"]["code"], "invalid_request")

        status, payload, _ = self.request(
            "POST",
            "/api/jobs/sync",
            payload={"platform": "all", "_validated_cf_user": {"rating": 9999}},
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")

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

    def test_setup_enters_dashboard_while_initial_sync_runs_without_service_lock(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def blocking_sync(**values: object) -> dict[str, object]:
            progress = values.pop("_progress_callback", None)
            started.set()
            release.wait(timeout=2)
            if callable(progress):
                progress(
                    {
                        "phase": "complete",
                        "platform": "all",
                        "step": 2,
                        "total": 2,
                        "completed": 2,
                        "failed": 0,
                        "message": "fixture sync complete",
                        "started_at": "2026-08-25T00:00:00+00:00",
                        "last_activity_at": "2026-08-25T00:00:01+00:00",
                        "usable": True,
                    }
                )
            return {"operation": "sync", **values}

        self.service.sync = blocking_sync
        try:
            status, payload, _ = self.request(
                "POST",
                "/api/setup",
                payload={"codeforces": "fixture", "luogu": "42"},
            )
            self.assertEqual(status, 200)
            data = payload["data"]
            self.assertTrue(data["validated"])
            self.assertTrue(data["defer_sync"])
            job_id = data["initial_sync_job"]["job_id"]
            self.assertTrue(started.wait(timeout=1))

            # Bootstrap uses the global service lock.  It must remain
            # responsive while the network-bound initial crawl is running.
            before = time.monotonic()
            bootstrap_status, bootstrap_payload, _ = self.request("GET", "/api/bootstrap")
            self.assertEqual(bootstrap_status, 200)
            self.assertLess(time.monotonic() - before, 0.5)
            self.assertEqual(
                bootstrap_payload["data"]["active_sync_job"]["job_id"],
                job_id,
            )

            same_status, same_payload, _ = self.request(
                "POST",
                "/api/setup",
                payload={"codeforces": "fixture", "luogu": "42"},
            )
            self.assertEqual(same_status, 200)
            self.assertEqual(
                same_payload["data"]["initial_sync_job"]["job_id"],
                job_id,
            )

            changed_status, changed_payload, _ = self.request(
                "POST",
                "/api/setup",
                payload={"codeforces": "different", "luogu": "43"},
            )
            self.assertEqual(changed_status, 200)
            self.assertNotEqual(
                changed_payload["data"]["initial_sync_job"]["job_id"],
                job_id,
            )

            release.set()
            job = self.wait_for_job(job_id)
            self.assertEqual(job["status"], "succeeded")
            self.assertEqual(
                job["result"],
                {
                    "operation": "sync",
                    "platform": "all",
                    "full_catalog": True,
                },
            )
            self.assertEqual(job["progress"]["phase"], "complete")
            self.assertTrue(job["progress"]["usable"])
        finally:
            release.set()

    def test_offline_web_setup_does_not_start_initial_sync_job(self) -> None:
        status, payload, _ = self.request(
            "POST",
            "/api/setup",
            payload={
                "codeforces": "fixture",
                "luogu": "42",
                "skip_validate": True,
            },
        )
        self.assertEqual(status, 200)
        self.assertFalse(payload["data"]["validated"])
        self.assertIsNone(payload["data"]["initial_sync_job"])

        status, payload, _ = self.request(
            "POST",
            "/api/setup",
            payload={
                "codeforces": "fixture",
                "luogu": "42",
                "skip_validate": True,
                "_progress_callback": {},
            },
        )
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "invalid_request")

    def test_finite_stress_verify_job_is_preserved(self) -> None:
        request = {
            "problem": "CF1A",
            "generator_file": r"D:\code\fixture\generator.cpp",
            "reference_file": r"D:\code\fixture\reference.cpp",
            "user_file": r"D:\code\fixture\user.cpp",
            "exact": False,
            "debug": False,
            "timeout": 2.0,
            "stress_iterations": 100,
            "seed": 42,
        }
        status, payload, _ = self.request("POST", "/api/jobs/verify", payload=request)
        self.assertEqual(status, 202)
        job = self.wait_for_job(payload["data"]["job_id"])
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(job["result"], {"operation": "verify", **request})

    def test_ai_context_jobs_dynamic_gets_and_sse_contract(self) -> None:
        status, payload, _ = self.request("GET", "/api/ai/status")
        self.assertEqual(status, 200)
        self.assertFalse(payload["data"]["api_key_detected"])
        self.assertEqual(
            payload["data"]["secure_store"],
            {
                "backend": "dpapi",
                "available": True,
                "error_code": None,
                "message": "Windows DPAPI 可用。",
            },
        )

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
            payload={
                "message": "hint", "mode": "hint", "hint_level": 1,
                "delivery_mode": "resilient",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "text/event-stream; charset=utf-8")
        self.assertIn("event: meta", payload["raw"])
        self.assertIn("event: delta", payload["raw"])
        self.assertIn("event: usage", payload["raw"])
        self.assertIn("event: done", payload["raw"])
        self.assertEqual(self.service.calls[-1][1]["delivery_mode"], "resilient")

        status, payload, _ = self.request(
            "POST", "/api/jobs/ai/recommendations", payload={"count": 2}
        )
        self.assertEqual(status, 202)
        job = self.wait_for_job(payload["data"]["job_id"])
        self.assertEqual(job["result"]["operation"], "ai_recommendations")

        status, payload, _ = self.request(
            "POST",
            "/api/jobs/ai/plans/preview",
            payload={"mode": "organize", "text": "CF1A"},
        )
        self.assertEqual(status, 202)
        job = self.wait_for_job(payload["data"]["job_id"])
        self.assertEqual(job["result"]["operation"], "ai_plan_preview")
        self.assertEqual(job["progress"]["round"], 1)
        self.assertEqual(job["progress"]["accepted_count"], 1)

    def test_ai_cache_control_routes_and_force_refresh_passthrough(self) -> None:
        status, payload, _ = self.request("GET", "/api/ai/cache")
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["entries"], 3)
        self.assertEqual(self.service.calls[-1], ("ai_cache_status", {}))

        status, payload, _ = self.request(
            "POST",
            "/api/ai/cache/clear",
            payload={"profile_ids": ["recommendation", "summary"]},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["removed"], 2)
        self.assertEqual(
            self.service.calls[-1],
            ("ai_cache_clear", {"profile_ids": ["recommendation", "summary"]}),
        )

        status, payload, _ = self.request("POST", "/api/ai/cache/prune", payload={})
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["removed"], 1)
        self.assertEqual(self.service.calls[-1], ("ai_cache_prune", {}))

        status, payload, _ = self.request(
            "POST",
            "/api/jobs/ai/recommendations",
            payload={"count": 2, "force_refresh": True},
        )
        self.assertEqual(status, 202)
        job = self.wait_for_job(payload["data"]["job_id"])
        self.assertTrue(job["result"]["force_refresh"])

        status, payload, _ = self.request(
            "POST",
            "/api/jobs/ai/knowledge/preview",
            payload={"attempt_id": 1, "target_id": "target-1", "force_refresh": True},
        )
        self.assertEqual(status, 202)
        job = self.wait_for_job(payload["data"]["job_id"])
        self.assertEqual(job["result"]["operation"], "knowledge_preview")
        self.assertTrue(job["result"]["force_refresh"])

        status, payload, _ = self.request(
            "POST",
            "/api/jobs/ai/plans/preview",
            payload={"mode": "organize", "text": "CF1A", "force_refresh": True},
        )
        self.assertEqual(status, 202)
        job = self.wait_for_job(payload["data"]["job_id"])
        self.assertTrue(job["result"]["force_refresh"])

    def test_sse_iteration_failure_is_a_structured_terminal_event(self) -> None:
        def broken_stream(conversation_id: str, **values: object):
            del conversation_id, values
            yield {"event": "meta", "data": {"message_id": "broken"}}
            raise RuntimeError("private implementation detail")

        self.service.ai_chat_stream = broken_stream
        status, payload, headers = self.request(
            "POST",
            "/api/ai/conversations/conv-1/messages",
            payload={"message": "hint", "mode": "hint", "hint_level": 1},
        )
        self.assertEqual(status, 200)
        self.assertEqual(headers["content-type"], "text/event-stream; charset=utf-8")
        self.assertIn("event: meta", payload["raw"])
        self.assertIn("event: error", payload["raw"])
        self.assertIn('"code":"internal_error"', payload["raw"])
        self.assertNotIn("private implementation detail", payload["raw"])

    def test_delete_cancels_only_queued_job(self) -> None:
        started = threading.Event()
        release = threading.Event()

        def blocking_sync(**values: object) -> dict[str, object]:
            started.set()
            release.wait(timeout=2)
            return {"operation": "sync", **values}

        self.service.sync = blocking_sync
        status, first_payload, _ = self.request(
            "POST", "/api/jobs/sync", payload={"platform": "all"}
        )
        self.assertEqual(status, 202)
        self.assertTrue(started.wait(timeout=1))
        status, second_payload, _ = self.request(
            "POST", "/api/jobs/sync", payload={"platform": "codeforces"}
        )
        self.assertEqual(status, 202)
        first_id = first_payload["data"]["job_id"]
        second_id = second_payload["data"]["job_id"]

        status, payload, _ = self.request("DELETE", f"/api/jobs/{second_id}")
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["status"], "canceled")
        self.assertEqual(payload["data"]["error"]["code"], "job_canceled")

        status, payload, _ = self.request("DELETE", f"/api/jobs/{first_id}")
        self.assertEqual(status, 409)
        self.assertEqual(payload["error"]["code"], "job_not_cancelable")
        release.set()

    def test_retired_persistent_routes_return_404(self) -> None:
        routes = (
            ("GET", "/api/ai/stress/status"),
            ("GET", "/api/stress/runs?problem=CF1A"),
            ("GET", "/api/stress/runs/run-1"),
            ("GET", "/api/stress/bundles/bundle-1"),
            ("POST", "/api/jobs/ai/stress/start"),
            ("POST", "/api/stress/runs/run-1/stop"),
            ("POST", "/api/stress/runs/run-1/resume"),
            ("POST", "/api/stress/runs/run-1/finish"),
            ("POST", "/api/jobs/stress/bundles/bundle-1/revert"),
        )
        for method, path in routes:
            with self.subTest(method=method, path=path):
                payload = {} if method == "POST" else None
                status, response, _ = self.request(method, path, payload=payload)
                self.assertEqual(status, 404)
                self.assertEqual(response["error"]["code"], "not_found")

    def test_synchronous_routes_forward_json_objects(self) -> None:
        routes = {
            "/api/recommendations": "recommendations",
            "/api/sessions/start": "start",
            "/api/sessions/close": "close",
            "/api/review/week": "weekly_review",
            "/api/review/queue/add": "review_queue_add",
            "/api/review/queue/remove": "review_queue_remove",
            "/api/review/queue/clear": "review_queue_clear",
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

    def test_review_queue_routes_use_web_source(self) -> None:
        status, payload, _ = self.request("GET", "/api/review/queue")
        self.assertEqual(status, 200)
        self.assertEqual(payload["data"]["operation"], "review_queue")

        for path, operation, body in (
            ("/api/review/queue/add", "review_queue_add", {"problem": "CF1A", "review_due": "2026-08-29"}),
            ("/api/review/queue/remove", "review_queue_remove", {"problem": "CF1A"}),
            ("/api/review/queue/clear", "review_queue_clear", {"confirm": True}),
        ):
            with self.subTest(path=path):
                status, response, _ = self.request("POST", path, payload=body)
                self.assertEqual(status, 200)
                self.assertEqual(response["data"]["operation"], operation)
                self.assertEqual(response["data"]["source"], "web")

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
    def test_typed_credential_store_errors_preserve_public_code_and_status(self) -> None:
        unavailable = _problem_from_exception(
            CredentialStoreError(
                "系统安全存储不可用。", code="credential_store_unavailable"
            )
        )
        self.assertEqual(unavailable.status, 503)
        self.assertEqual(unavailable.code, "credential_store_unavailable")

        locked = _problem_from_exception(
            CredentialStoreError(
                "系统安全存储已锁定。", code="credential_store_locked"
            )
        )
        self.assertEqual(locked.status, 409)
        self.assertEqual(locked.code, "credential_store_locked")

        legacy = _problem_from_exception(CredentialStoreError("凭据写入失败。"))
        self.assertEqual(legacy.status, 409)
        self.assertEqual(legacy.code, "credential_store_error")

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
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline:
                if all(
                    manager.get(job["job_id"])["status"] == "succeeded"
                    for job in jobs
                ):
                    break
                time.sleep(0.01)
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

    def test_shutdown_cancels_queued_work_and_rejects_new_submissions(self) -> None:
        manager = JobManager(threading.RLock(), capacity=3)
        started = threading.Event()
        release = threading.Event()
        queued_executed = threading.Event()

        def blocking_work() -> dict[str, bool]:
            started.set()
            release.wait(timeout=2)
            return {"ok": True}

        def queued_work() -> dict[str, bool]:
            queued_executed.set()
            return {"ok": True}

        first = manager.submit("running", blocking_work)
        self.assertTrue(started.wait(timeout=1))
        second = manager.submit("queued", queued_work)
        manager.close(wait=False)
        self.assertEqual(manager.get(second["job_id"])["status"], "canceled")
        self.assertFalse(queued_executed.is_set())
        with self.assertRaises(ApiProblem) as raised:
            manager.submit("late", queued_work)
        self.assertEqual(raised.exception.code, "job_manager_stopped")

        release.set()
        manager.close(wait=True)
        self.assertEqual(manager.get(first["job_id"])["status"], "succeeded")
        self.assertFalse(queued_executed.is_set())

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
            "view_appearance.js",
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
            "AbortController", "Array", "Blob", "Boolean", "Date", "Error", "Image",
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
            "#sync-button",
            "#ai-context-button",
            "#plan-check-button",
            "#shutdown-button",
        ):
            start = self.script.index(f'$("{selector}").addEventListener')
            block = self.script[start : start + 900]
            self.assertIn("const button = event.currentTarget", block, selector)
            self.assertNotIn("setBusy(event.currentTarget, false)", block, selector)

    def test_ai_messages_use_safe_markdown_rendering_during_streaming(self) -> None:
        self.assertIn("function renderAssistantMath(node)", self.script)
        self.assertIn('node?.classList.contains("assistant")', self.script)
        self.assertIn("window.renderMathInElement(node", self.script)
        self.assertIn("throwOnError: false", self.script)
        self.assertIn("trust: false", self.script)
        self.assertIn('if (role === "assistant") renderSafeKnowledgeMarkdown(node, content)', self.script)
        self.assertIn('let assistantMarkdown = ""', self.script)
        self.assertIn("assistantMarkdown += item.data.content", self.script)
        self.assertIn("renderSafeKnowledgeMarkdown(assistant, assistantMarkdown)", self.script)
        self.assertNotIn("assistant.textContent += item.data.content", self.script)

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
            'id="ai-gap-fill-button"',
            'id="ai-specialization-button"',
            'id="ai-chat-form"',
            'id="ai-statement-restore"',
            'id="ai-patch-apply"',
        ):
            self.assertIn(identifier, self.html)
        self.assertIn("已分类的去重平台 AC 摘要 + 确定性候选", self.html)
        self.assertIn("不会发送账号、handle、UID、submission ID", self.html)
        self.assertIn("await ensureAiConversation()", self.script)
        self.assertIn('event === "delta"', self.script)
        self.assertIn("window.confirm", self.script)

    def test_persistent_stress_ui_is_retired_but_finite_stress_remains(self) -> None:
        retired_markers = (
            'id="ai-stress-panel"',
            'id="ai-stress-form"',
            'id="ai-stress-run-panel"',
            "/api/ai/stress/status",
            "/api/jobs/ai/stress/start",
            "/api/stress/runs",
            "loadAiStressStatus",
            "scheduleStressPoll",
            "controlStress",
        )
        for marker in retired_markers:
            self.assertNotIn(marker, self.html + self.script + self.styles)

        self.assertIn('name="with_stress" type="checkbox"', self.html)
        self.assertIn('name="stress_iterations" type="number"', self.html)
        self.assertIn('name="seed" type="number"', self.html)
        for name, label in (
            ("generator_file", "数据生成器"),
            ("reference_file", "参考程序"),
            ("user_file", "用户程序"),
        ):
            self.assertIn(f'data-file-picker-field="{name}"', self.html)
            self.assertIn(f'data-file-picker="{name}" data-kind="cpp"', self.html)
            self.assertIn(f'data-file-clear="{name}"', self.html)
            self.assertIn(label, self.html)
            self.assertIn(f'{name}: localFile("{name}")', self.script)
        self.assertIn(".local-stress-file-row", self.styles)
        self.assertIn("grid-template-columns: repeat(3, minmax(0, 1fr))", self.styles)
        self.assertIn('startJob("/api/jobs/verify"', self.script)
        self.assertIn("stress_iterations: Number(form.elements.stress_iterations.value)", self.script)
        self.assertIn("if (result.stress)", self.script)

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

    def test_ai_connection_form_uses_password_input_and_never_browser_storage(self) -> None:
        self.assertIn('id="ai-connection-form"', self.html)
        self.assertIn('name="api_key" type="password"', self.html)
        self.assertIn('autocomplete="new-password"', self.html)
        self.assertNotIn('id="ai-credential-form"', self.html)
        self.assertNotIn('id="ai-key-clear"', self.html)
        self.assertIn('api("/api/ai/connections"', self.script)
        self.assertIn('finally {\n    keyInput.value = "";', self.script)
        self.assertNotIn('localStorage.setItem("deepseek', self.script)
        self.assertNotIn('sessionStorage.setItem("deepseek', self.script)
        self.assertNotIn('localStorage.setItem("api', self.script)
        self.assertNotIn('sessionStorage.setItem("api', self.script)

    def test_local_background_appearance_is_browser_only_and_csp_safe(self) -> None:
        appearance = self.modules["view_appearance.js"]
        app = self.modules["app.js"]
        for marker in (
            'id="app-background-stage"',
            'id="app-background-fill"',
            'id="app-background"',
            'id="background-file-input"',
            'accept="image/jpeg,image/png,image/webp"',
            'id="appearance-opacity"',
            'min="60" max="92" step="4" value="72"',
            'id="background-crop-dialog"',
            'id="background-crop-canvas" tabindex="0"',
            'id="background-crop-zoom"',
            'id="background-crop-apply"',
            'id="background-remove-button"',
            'id="appearance-reset-button"',
        ):
            self.assertIn(marker, self.html)
        for ratio in ('value="16:9"', 'value="16:10"', 'value="4:3"'):
            self.assertGreaterEqual(self.html.count(ratio), 2)

        self.assertIn('from "./view_appearance.js"', app)
        self.assertIn("bindAppearanceEvents();", app)
        self.assertLess(app.index("await loadAppearance();"), app.index("await loadBootstrap();"))
        for marker in (
            'const APPEARANCE_DB_NAME = "acm-agent-ui"',
            "const APPEARANCE_DB_VERSION = 1",
            'const APPEARANCE_STORE_NAME = "appearance"',
            'const SETTINGS_KEY = "settings"',
            'const BACKGROUND_KEY = "background"',
            "const MAX_FILE_BYTES = 20 * 1024 * 1024",
            "const MAX_OUTPUT_EDGE = 3840",
            'cropRatio: "16:9", panelOpacity: 72',
            'canvasToBlob(output, "image/webp", 0.96)',
            'canvasToBlob(output, "image/png")',
            "transaction.oncomplete",
            "URL.revokeObjectURL",
        ):
            self.assertIn(marker, appearance)
        for prohibited in ("/api/", "fetch(", "FormData", ".style.", 'setAttribute("style"'):
            self.assertNotIn(prohibited, appearance)
        self.assertNotIn("style=", self.html)

        self.assertIn('.app-background-stage { position: fixed;', self.styles)
        self.assertIn('inset: 0 auto 0 250px; width: calc(100% - 250px); height: 100%;', self.styles)
        self.assertIn('.app-background-fill { inset: -24px;', self.styles)
        self.assertIn('object-fit: cover; object-position: center; filter: blur(20px)', self.styles)
        self.assertIn('.app-background { object-fit: contain; object-position: center; image-rendering: auto; }', self.styles)
        self.assertIn('.appearance-preview img { display: block; width: 100%; height: 128px; object-fit: contain; object-position: center;', self.styles)
        self.assertIn('backdrop-filter: blur(8px) saturate(.9);', self.styles)
        self.assertIn('.app-background-stage { left: 76px; width: calc(100% - 76px); }', self.styles)
        self.assertIn('.app-background-stage { inset: 0 auto 72px 0; width: 100%; height: calc(100% - 72px); }', self.styles)
        self.assertIn('$("#app-background-fill").src = nextUrl;', appearance)
        self.assertIn('$("#app-background-fill").removeAttribute("src");', appearance)
        self.assertIn('$("#app-background-stage").classList.add("is-active");', appearance)
        self.assertIn('$("#app-background-stage").classList.remove("is-active");', appearance)
        self.assertIn('html[data-has-background="true"] .sidebar { border-color: var(--line-solid); background: var(--surface-solid); }', self.styles)
        self.assertIn('pointer-events: none', self.styles)
        self.assertIn('html[data-has-background="true"]', self.styles)
        for opacity in range(60, 93, 4):
            self.assertIn(f'html[data-panel-opacity="{opacity}"]', self.styles)
            self.assertIn(
                f'html[data-has-background="true"][data-panel-opacity="{opacity}"]',
                self.styles,
            )
        self.assertIn("@media (prefers-reduced-transparency: reduce)", self.styles)
        self.assertIn("@media (forced-colors: active)", self.styles)
        self.assertIn(".settings-grid { display: grid; grid-template-columns: repeat(3", self.styles)


if __name__ == "__main__":
    unittest.main()
