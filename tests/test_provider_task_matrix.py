"""Real wire adapters -> persisted conformance -> governed task/validator matrix.

Only HTTP transport and the patch compiler subprocess are replaced. No provider
result, routing, conformance, JSON parsing, or semantic validator is mocked.
"""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.acm_agent.provider import ProviderError
from tools.acm_agent.provider_config import TASK_PROFILE_IDS
from tools.acm_agent.provider_registry import ProviderRegistry
from tools.acm_agent.provider_conformance import run_live_conformance, verified_definition_from_report
from tools.acm_agent.provider_governance import GovernedProviderClient
from tools.acm_agent.ai_plan_import import validate_generated_problem_ids, validate_organize_ir
from tools.acm_agent.knowledge import get_builtin_schema, validate_structured_entry
from tools.acm_agent.service_ai import _validate_recommendation_payload, _validate_patch_payload, _validate_coaching_content
from tests.test_provider_output_limits import provider, registry


class WireTransport:
    def __init__(self, adapter):
        self.adapter, self.requests, self.answer, self.truncated = adapter, [], None, False

    def __call__(self, request, timeout):
        payload = json.loads(request.data)
        self.requests.append((request, payload))
        text = self.answer or ('{"ok":true}' if "JSON" in json.dumps(payload) or "json" in json.dumps(payload) else "OK")
        usage = {"input_tokens": 5, "output_tokens": 2, "total_tokens": 7}
        if self.adapter == "anthropic":
            document = {"type": "message", "id": "msg1", "model": "new-model", "stop_reason": "max_tokens" if self.truncated else "end_turn", "content": [{"type": "text", "text": text}], "usage": usage}
            events = [{"type": "message_start", "message": {"id": "msg1", "model": "new-model", "usage": {"input_tokens": 5}}},
                      {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
                      {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}},
                      {"type": "content_block_stop", "index": 0},
                      {"type": "message_delta", "delta": {"stop_reason": document["stop_reason"]}, "usage": {"output_tokens": 2}},
                      {"type": "message_stop"}]
        elif self.adapter == "openai_responses":
            document = {"id": "r1", "model": "new-model", "status": "incomplete" if self.truncated else "completed", "output": [{"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}], "usage": usage}
            if self.truncated:
                document["incomplete_details"] = {"reason": "max_output_tokens"}
            events = [{"type": "response.output_text.delta", "delta": text}, {"type": "response.completed", "response": document}]
        else:
            usage = {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}
            document = {"id": "c1", "model": "new-model", "choices": [{"message": {"role": "assistant", "content": text}, "finish_reason": "length" if self.truncated else "stop"}], "usage": usage}
            events = [{"id": "c1", "model": "new-model", "choices": [{"delta": {"content": text}, "finish_reason": None}]},
                      {"choices": [{"delta": {}, "finish_reason": "stop"}], "usage": usage}, "[DONE]"]
        if payload.get("stream"):
            return io.BytesIO("".join("data: " + (e if isinstance(e, str) else json.dumps(e)) + "\n\n" for e in events).encode())
        return io.BytesIO(json.dumps(document).encode())


class ProviderTaskMatrixTests(unittest.TestCase):
    adapters = ("openai_compatible", "openai_responses", "anthropic")

    def verified_registry(self, adapter, mode, streaming):
        definition = provider(limit=8192)
        definition["adapter"] = adapter
        definition["models"]["new-model"]["wire_profile"] = {"adapter": adapter, "structured_output": mode, "streaming": streaming}
        reg, config = registry(definition, budget=8192)
        config["credential_slots"]["test"]["environment_variable"] = "ACM_MATRIX_KEY"
        reg = ProviderRegistry(config)
        transport = WireTransport(adapter)
        route = reg.probe_route("test", "new-model", profile_id="coaching")
        client = reg.client_for_route(route)
        client._transport = transport
        report = run_live_conformance(client, route)
        self.assertTrue(report["passed"], report)
        config["providers"]["test"]["models"]["new-model"] = verified_definition_from_report("test", reg.providers["test"], "new-model", report)
        # Exercise an actual disk serialization/reload, including evidence hash.
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ai.json"
            path.write_text(json.dumps(config), encoding="utf-8")
            reloaded = ProviderRegistry(json.loads(path.read_text(encoding="utf-8")))
        return reloaded, transport

    def governed(self, reg, transport, task):
        route = reg.route(task, model_ref={"provider_id": "test", "model": "new-model"}, reasoning_strength="auto")
        def factory(route, timeout):
            client = reg.client_for_route(route, timeout=timeout)
            client._transport = transport
            return client
        return GovernedProviderClient([route], factory, sleep=lambda _: None)

    def fixture(self, task):
        if task == "recommendation":
            valid = {"focus_topics": ["dp"], "ranked": [{"problem_key": "luogu:P1000", "topic": "dp", "ai_reason": "practice", "training_focus": "state"}], "risk_warning": ""}
            validator = lambda value: _validate_recommendation_payload(value, outbound=[{"problem_key": "luogu:P1000", "knowledge_topics": ["dp"]}], tier_topics=["dp"], selected_count=1, difficulty_targets={"review": 1000, "main": 1000, "challenge": 1000})
        elif task == "plan_organize":
            valid = {"title": "Practice", "groups": [{"topic": "dp", "due_date": None, "problem_keys": ["luogu:P1000"]}]}
            validator = lambda value: validate_organize_ir(value, allowed_problem_keys=["luogu:P1000"])
        elif task == "plan_generate":
            valid, validator = {"problem_ids": ["P1000"]}, validate_generated_problem_ids
        elif task == "patch":
            valid = {"diagnosis": "return corrected", "replacement_code": "// Fix the return value for successful completion.\nint main() { return 0; }"}
            validator = lambda value: _validate_patch_payload(value, original_source="int main() { return 1; }")
        else:
            schema = get_builtin_schema("algorithms-v1")
            valid = {"topic": "dp", "title": "State transitions", "fields": {field["key"]: "A useful observation" for field in schema["fields"]}}
            validator = lambda value: validate_structured_entry(value, schema)
        return valid, validator

    def test_six_tasks_native_and_degraded_after_evidence_reload(self):
        with patch.dict(os.environ, {"ACM_MATRIX_KEY": "test-key"}), patch("tools.acm_agent.service_ai._compile_candidate", return_value=(True, "compiler delegated")):
            for adapter in self.adapters:
                for mode, streaming in (("native_schema", "native"), ("native_schema", "buffered"), ("prompt_json", "native"), ("prompt_json", "buffered")):
                    with self.subTest(adapter=adapter, mode=mode, streaming=streaming):
                        reg, transport = self.verified_registry(adapter, mode, streaming)
                        for task in TASK_PROFILE_IDS:
                            with self.subTest(task=task):
                                client = self.governed(reg, transport, task)
                                if task == "coaching":
                                    transport.answer = "可以先检查边界条件。"
                                    output = list(client.stream_chat([{"role": "user", "content": "Help"}]))
                                    self.assertEqual(output[-1].kind, "done")
                                    _validate_coaching_content("".join(e.content for e in output), hint_level=4)
                                    self.assertEqual(transport.requests[-1][1]["stream"], streaming == "native")
                                else:
                                    valid, validator = self.fixture(task)
                                    transport.answer = json.dumps(valid)
                                    result = client.structured([{"role": "user", "content": task}], json_schema={"type": "object"}, schema_name=task)
                                    self.assertEqual(result.data, valid)
                                    validator(result.data)
                                    payload = transport.requests[-1][1]
                                    native = "output_config" in payload if adapter == "anthropic" else ("text" in payload and "format" in payload["text"]) if adapter == "openai_responses" else "response_format" in payload
                                    self.assertEqual(native, mode == "native_schema")
                                    if mode == "prompt_json":
                                        self.assertIn("schema", json.dumps(payload).lower())
                                    # Valid JSON with invalid business semantics is rejected locally.
                                    transport.answer = '{"unauthorized":true}'
                                    rejected = client.structured([{"role": "user", "content": task}], json_schema={"type": "object"}, schema_name=task)
                                    with self.assertRaises(ValueError):
                                        validator(rejected.data)
                                endpoint = {"anthropic": "/messages", "openai_responses": "/responses", "openai_compatible": "/chat/completions"}[adapter]
                                self.assertTrue(transport.requests[-1][0].full_url.endswith(endpoint))
                                self.assertGreater(client.request_attempts, 0)

    def test_truncated_structured_result_never_reaches_business_validator(self):
        with patch.dict(os.environ, {"ACM_MATRIX_KEY": "test-key"}):
            for adapter in self.adapters:
                for mode in ("native_schema", "prompt_json"):
                    with self.subTest(adapter=adapter, mode=mode):
                        reg, transport = self.verified_registry(adapter, mode, "native")
                        client = self.governed(reg, transport, "plan_generate")
                        transport.answer, transport.truncated = '{"problem_ids":["P1000"]}', True
                        before = len(transport.requests)
                        with self.assertRaises(ProviderError) as caught:
                            client.structured([{"role": "user", "content": "generate"}], json_schema={"type": "object"}, schema_name="plan")
                        self.assertEqual(caught.exception.finish_reason, "length")
                        self.assertEqual(len(transport.requests) - before, 1)
                        self.assertEqual(client.governance_snapshot["validation_repairs"], 0)


if __name__ == "__main__":
    unittest.main()
