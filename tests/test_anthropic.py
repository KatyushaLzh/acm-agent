from __future__ import annotations

import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from tools.acm_agent.anthropic import AnthropicClient, discover_anthropic_models
from tools.acm_agent.provider import CapabilityProfile, ProviderError


def completed(**extra):
    return {"type": "message", "id": "msg_1", "model": "resolved", "stop_reason": "end_turn", "content": [{"type": "text", "text": "OK"}], "usage": {"input_tokens": 10, "output_tokens": 2, "cache_read_input_tokens": 4, "cache_creation_input_tokens": 3}, **extra}


def stream(*events):
    return io.BytesIO("".join("event: " + event["type"] + "\ndata: " + json.dumps(event) + "\n\n" for event in events).encode())


def events():
    return [
        {"type": "message_start", "message": {"model": "resolved", "id": "msg_1", "usage": {"input_tokens": 10, "output_tokens": 1, "cache_read_input_tokens": 4}}},
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
        {"type": "ping"},
        {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "OK"}},
        {"type": "content_block_stop", "index": 0},
        {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 2}},
        {"type": "message_stop"},
    ]


class AnthropicTests(unittest.TestCase):
    def client(self, *responses, **kwargs):
        requests, pending = [], list(responses)
        def transport(request, timeout):
            requests.append(request)
            response = pending.pop(0)
            if isinstance(response, Exception):
                raise response
            return io.BytesIO(json.dumps(response).encode()) if isinstance(response, dict) else response
        return AnthropicClient("secret-test", provider_id="test", base_url=kwargs.pop("base_url", "https://provider.example"), models={"model": CapabilityProfile(True, True, True, False, True, True, True)}, transport=transport, **kwargs), requests

    def chat(self, client, **options):
        return client.chat([{"role": "user", "content": "hi"}], model="model", **options)

    def test_roles_multiblock_and_cache_usage(self):
        client, requests = self.client(completed(content=[{"type": "thinking", "thinking": "private"}, {"type": "text", "text": "a"}, {"type": "text", "text": "b"}]))
        result = client.chat([{"role": "system", "content": "rule"}, {"role": "user", "content": "hi"}], model="model")
        payload = json.loads(requests[0].data)
        self.assertEqual(payload["system"], "rule")
        self.assertEqual(result.content, "ab")
        self.assertEqual(result.usage["input_tokens"], 17)
        self.assertEqual(result.usage["total_tokens"], 19)
        self.assertEqual(result.usage["cache_read_tokens"], 4)
        self.assertEqual(requests[0].full_url, "https://provider.example/v1/messages")
        self.assertEqual(requests[0].get_header("X-api-key"), "secret-test")
        self.assertEqual(requests[0].get_header("Anthropic-version"), "2023-06-01")

    def test_bearer_and_custom_prefix(self):
        client, requests = self.client(completed(), auth={"type": "bearer"}, base_url="https://provider.example/api/custom/messages")
        self.chat(client)
        self.assertEqual(requests[0].full_url, "https://provider.example/api/custom/messages")
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer secret-test")

    def test_missing_usage_is_unknown(self):
        client, _ = self.client(completed(usage={}))
        result = self.chat(client)
        self.assertNotIn("input_tokens", result.usage)
        self.assertFalse(result.provider_metadata["usage_complete"])

    def test_thinking_usage_and_safe_custom_headers(self):
        client, requests = self.client(completed(usage={"input_tokens": 4, "output_tokens": 12, "output_tokens_details": {"thinking_tokens": 9}}), headers={"anthropic-version": "2023-06-01", "anthropic-workspace-id": "workspace"})
        result = self.chat(client)
        self.assertEqual(result.usage["reasoning_tokens"], 9)
        self.assertEqual(requests[0].get_header("Anthropic-workspace-id"), "workspace")

    def test_auto_thinking_omits_controls(self):
        client, requests = self.client(completed())
        self.chat(client, thinking=True, reasoning_effort="auto")
        self.assertNotIn("thinking", json.loads(requests[0].data))

    def test_explicit_off_sends_disabled(self):
        client, requests = self.client(completed())
        self.chat(client, thinking=True, reasoning_effort="none", temperature=0)
        payload = json.loads(requests[0].data)
        self.assertEqual(payload["thinking"], {"type": "disabled"})
        self.assertEqual(payload["temperature"], 0)

    def test_adaptive_and_manual_thinking(self):
        client, requests = self.client(completed())
        self.chat(client, thinking=True, reasoning_effort="high")
        self.assertEqual(json.loads(requests[0].data)["output_config"], {"effort": "high"})
        client, requests = self.client(completed(), wire_profile={"reasoning_mode": "manual"})
        with self.assertRaises(ProviderError):
            self.chat(client, thinking=True, reasoning_effort="medium", max_tokens=4096)
        self.assertEqual(len(requests), 0)
        self.chat(client, thinking=True, reasoning_effort="medium", max_tokens=5120)
        self.assertEqual(json.loads(requests[0].data)["thinking"]["budget_tokens"], 4096)

    def test_schema_and_prompt_fallback(self):
        for mode in ("native_schema", "prompt_json"):
            with self.subTest(mode=mode):
                client, requests = self.client(completed(content=[{"type": "text", "text": '{"ok":true}'}]), wire_profile={"structured_output": mode})
                result = client.structured([{"role": "user", "content": "json"}], json_schema={"type": "object"}, schema_name="ok", model="model")
                payload = json.loads(requests[0].data)
                self.assertEqual(result.data, {"ok": True})
                self.assertEqual("output_config" in payload, mode == "native_schema")
                if mode == "prompt_json":
                    self.assertIn('"type": "object"', payload["system"])
                    self.assertIn("Return only a JSON object", payload["system"])

    def test_request_size_limit_before_transport(self):
        client, requests = self.client(completed())
        with self.assertRaises(ProviderError) as caught:
            client.chat([{"role": "user", "content": "x" * 8_388_609}], model="model")
        self.assertEqual(caught.exception.code, "request_too_large")
        self.assertEqual(caught.exception.usage["provider_requests"], 0)
        self.assertFalse(requests)

    def test_invalid_json_has_usage_and_no_internal_retry(self):
        client, requests = self.client(completed())
        with self.assertRaises(ProviderError) as caught:
            client.chat_json([{"role": "user", "content": "json"}], model="model")
        self.assertEqual(caught.exception.usage["provider_requests"], 1)
        self.assertEqual(len(requests), 1)

    def test_nonterminal_refusal_tools_and_length_fail(self):
        for reason in (None, "max_tokens", "refusal", "tool_use", "pause_turn"):
            with self.subTest(reason=reason):
                client, _ = self.client(completed(stop_reason=reason))
                with self.assertRaises(ProviderError) as caught:
                    self.chat(client)
                self.assertEqual(caught.exception.usage["total_tokens"], 19)

    def test_stream_cumulative_usage_exactly_once(self):
        client, _ = self.client(stream(*events()))
        output = list(client.stream_chat([{"role": "user", "content": "hi"}], model="model"))
        self.assertEqual([event.kind for event in output], ["delta", "done"])
        self.assertEqual(output[-1].usage["output_tokens"], 2)
        self.assertEqual(output[-1].usage["input_tokens"], 14)
        self.assertEqual(output[-1].usage["provider_requests"], 1)

    def test_stream_missing_terminal_preserves_usage(self):
        client, requests = self.client(stream(*events()[:-1]))
        with self.assertRaises(ProviderError) as caught:
            list(client.stream_chat([{"role": "user", "content": "hi"}], model="model"))
        self.assertEqual(caught.exception.code, "incomplete_stream")
        self.assertEqual(caught.exception.usage["output_tokens"], 2)
        self.assertEqual(len(requests), 1)

    def test_stream_rejects_malformed_block_order_and_tools(self):
        invalid = [
            events()[1:],
            events()[:1] + [events()[3]],
            events()[:2] + [events()[-2], events()[-1]],
            events()[:1] + [{"type": "content_block_start", "index": 0, "content_block": {"type": "tool_use"}}],
            events()[:1] + [{"type": "content_block_start", "index": 0, "content_block": {"type": "thinking"}}, events()[3]],
        ]
        for sequence in invalid:
            with self.subTest(sequence=sequence):
                client, requests = self.client(stream(*sequence))
                with self.assertRaises(ProviderError):
                    list(client.stream_chat([{"role": "user", "content": "hi"}], model="model"))
                self.assertEqual(len(requests), 1)

    def test_stream_thinking_is_never_answer_text(self):
        sequence = events()
        sequence[1:1] = [
            {"type": "content_block_start", "index": 1, "content_block": {"type": "thinking"}},
            {"type": "content_block_delta", "index": 1, "delta": {"type": "thinking_delta", "thinking": "private reasoning"}},
            {"type": "content_block_stop", "index": 1},
        ]
        client, _ = self.client(stream(*sequence))
        output = list(client.stream_chat([{"role": "user", "content": "hi"}], model="model"))
        self.assertEqual("".join(event.content for event in output), "OK")

    def test_stream_error_after_delta_never_retries(self):
        client, requests = self.client(stream(*events()[:4], {"type": "error", "error": {"type": "overloaded_error"}}), retries=5)
        with self.assertRaises(ProviderError) as caught:
            list(client.stream_chat([{"role": "user", "content": "hi"}], model="model"))
        self.assertFalse(caught.exception.retryable)
        self.assertTrue(caught.exception.protocol_details["emitted_content"])
        self.assertEqual(len(requests), 1)

    def test_stream_enforces_deadline_between_events(self):
        client, requests = self.client(stream(*events()))
        with patch("tools.acm_agent.provider_http.time.monotonic", side_effect=[0, 1000]):
            with self.assertRaises(ProviderError) as caught:
                list(client.stream_chat([{"role": "user", "content": "hi"}], model="model", request_timeout=1))
        self.assertEqual(caught.exception.code, "deadline_exceeded")
        self.assertEqual(caught.exception.usage["provider_requests"], 1)

    def test_error_identifies_unsupported_thinking_without_echo(self):
        body = {"error": {"message": "unsupported thinking.type adaptive; secret-test"}}
        client, _ = self.client(urllib.error.HTTPError("https://provider.example", 400, "err", {}, io.BytesIO(json.dumps(body).encode())))
        with self.assertRaises(ProviderError) as caught:
            self.chat(client)
        self.assertEqual(caught.exception.protocol_details, {"unsupported_parameter": "thinking", "unsupported_thinking_mode": "adaptive"})
        self.assertNotIn("secret-test", str(caught.exception))

    def test_buffered_stream(self):
        client, requests = self.client(completed(), wire_profile={"streaming": "buffered"})
        output = list(client.stream_chat([{"role": "user", "content": "hi"}], model="model"))
        self.assertEqual([event.kind for event in output], ["delta", "done"])
        self.assertFalse(json.loads(requests[0].data)["stream"])

    def test_http_529_and_safe_parameter_error(self):
        for status, body, code in ((529, {}, "server_error"), (400, {"error": {"message": "unsupported temperature secret-test"}}, "invalid_request")):
            client, requests = self.client(urllib.error.HTTPError("https://provider.example", status, "err", {}, io.BytesIO(json.dumps(body).encode())))
            with self.assertRaises(ProviderError) as caught:
                self.chat(client)
            self.assertEqual(caught.exception.code, code)
            self.assertNotIn("secret-test", str(caught.exception))
            if status == 400:
                self.assertEqual(caught.exception.protocol_details["unsupported_parameter"], "temperature")
            self.assertEqual(len(requests), 1)

    def test_discovery_pagination(self):
        requests = []
        pages = [{"data": [{"id": "a"}], "has_more": True, "last_id": "a"}, {"data": [{"id": "b"}], "has_more": False}]
        def transport(request, timeout):
            requests.append(request)
            return io.BytesIO(json.dumps(pages.pop(0)).encode())
        result = discover_anthropic_models(base_url="https://provider.example", api_key="key", transport=transport, include_metadata=True)
        self.assertEqual(result["ids"], ["a", "b"])
        self.assertTrue(requests[-1].full_url.endswith("?after_id=a"))

    def test_discovery_metadata_and_loop_guard(self):
        def transport(request, timeout):
            return io.BytesIO(json.dumps({"data": [{"id": "a", "capabilities": {"thinking": {"types": ["adaptive", "enabled"], "supported": True}}, "max_tokens": 64000, "max_input_tokens": 200000}], "has_more": False}).encode())
        result = discover_anthropic_models(base_url="https://provider.example", api_key="key", transport=transport, include_metadata=True)
        self.assertEqual(result["metadata"]["a"]["max_tokens"], 64000)
        self.assertEqual(result["metadata"]["a"]["capabilities"]["thinking"]["types"], ["adaptive", "enabled"])
        def repeat(request, timeout):
            return io.BytesIO(b'{"data":[],"has_more":true,"last_id":"same"}')
        with self.assertRaises(ProviderError):
            discover_anthropic_models(base_url="https://provider.example", api_key="key", transport=repeat)


if __name__ == "__main__":
    unittest.main()
