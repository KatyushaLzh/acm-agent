from __future__ import annotations

import io
import json
import unittest
from unittest.mock import patch

from tools.acm_agent.openai_responses import OpenAIResponsesClient
from tools.acm_agent.provider import CapabilityProfile, ProviderError


def completed(text="OK", **extra):
    return {"id": "resp_1", "model": "resolved", "status": "completed", "output": [
        {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": text}]}
    ], "usage": {"input_tokens": 7, "output_tokens": 1, "total_tokens": 8,
                 "input_tokens_details": {"cached_tokens": 3}}, **extra}


def sse(*events):
    return io.BytesIO("".join("data: " + (e if isinstance(e, str) else json.dumps(e)) + "\n\n" for e in events).encode())


class OpenAIResponsesTests(unittest.TestCase):
    def client(self, *responses, retries=0, **kwargs):
        requests = []
        pending = list(responses)
        def transport(request, timeout):
            requests.append(request)
            item = pending.pop(0)
            if isinstance(item, Exception):
                raise item
            return io.BytesIO(json.dumps(item).encode()) if isinstance(item, dict) else item
        client = OpenAIResponsesClient("private-token", provider_id="relay",
            base_url="https://relay.example/v1", auth={"type": "bearer"},
            transport=transport, retries=retries, sleep=lambda _: None,
            models={"model": CapabilityProfile(text_chat=True, streaming=True, json_object=True,
                function_tools=False, thinking=True, prompt_cache=False, usage_cache_tokens=True)}, **kwargs)
        return client, requests

    def options(self):
        return {"model": "model", "thinking": False, "reasoning_effort": "auto"}

    def test_payload_protocol_auth_roles_and_usage(self):
        client, requests = self.client(completed())
        result = client.chat([{"role": "system", "content": "instruction"},
                              {"role": "assistant", "content": "before"},
                              {"role": "user", "content": "hi"}],
                             model="model", thinking=True, reasoning_effort="none", max_tokens=1024)
        request = requests[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, "https://relay.example/v1/responses")
        self.assertEqual(request.get_header("Authorization"), "Bearer private-token")
        self.assertEqual(payload["input"][0], {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "instruction"}]})
        self.assertEqual(payload["input"][1]["content"][0]["type"], "output_text")
        self.assertEqual(payload["reasoning"], {"effort": "none"})
        self.assertEqual(payload["max_output_tokens"], 1024)
        self.assertFalse(payload["store"])
        self.assertNotIn("messages", payload)
        self.assertNotIn("max_tokens", payload)
        self.assertEqual(result.content, "OK")
        self.assertEqual(result.usage["cache_read_tokens"], 3)
        self.assertEqual(result.usage["provider_requests"], 1)
        self.assertEqual(result.provider_metadata["transport_api"], "responses")

    def test_json_repair_remains_responses_and_accounts_for_usage(self):
        client, requests = self.client(completed("bad"), completed('{"ok":true}'))
        result = client.chat_json([{"role": "user", "content": "Return JSON"}], **self.options())
        self.assertEqual(result.data, {"ok": True})
        self.assertEqual(result.usage["total_tokens"], 16)
        self.assertEqual(result.usage["provider_requests"], 2)
        for request in requests:
            payload = json.loads(request.data)
            self.assertEqual(payload["text"], {"format": {"type": "json_object"}})
            self.assertNotIn("messages", payload)
            self.assertNotIn("thinking", payload)
        self.assertEqual(len(json.loads(requests[1].data)["input"]), 2)

    def test_stream_completed_is_required_and_usage_is_final(self):
        client, requests = self.client(sse(
            {"type": "response.created", "response": {"id": "resp_1", "model": "resolved"}},
            {"type": "response.output_text.delta", "delta": "O"},
            {"type": "response.output_text.delta", "delta": "K"},
            {"type": "response.completed", "response": completed()},
        ))
        events = list(client.stream_chat([{"role": "user", "content": "hi"}], **self.options()))
        self.assertEqual("".join(e.content for e in events), "OK")
        self.assertEqual(events[-1].kind, "done")
        self.assertEqual(events[-1].usage["total_tokens"], 8)
        self.assertEqual(events[-1].usage["provider_requests"], 1)
        self.assertEqual(requests[0].get_header("Accept"), "text/event-stream")
        self.assertNotIn("stream_options", json.loads(requests[0].data))

    def test_done_marker_or_eof_does_not_fake_completion(self):
        for ending in ([], ["[DONE]"]):
            client, requests = self.client(sse({"type": "response.output_text.delta", "delta": "partial"}, *ending), retries=2)
            stream = client.stream_chat([{"role": "user", "content": "hi"}], **self.options())
            self.assertEqual(next(stream).content, "partial")
            with self.assertRaises(ProviderError) as caught:
                list(stream)
            self.assertEqual(caught.exception.code, "incomplete_stream")
            self.assertEqual(len(requests), 1)

    def test_stream_error_mapping_redaction_and_no_partial_replay(self):
        client, requests = self.client(sse(
            {"type": "response.output_text.delta", "delta": "partial"},
            {"type": "error", "code": "server_error", "message": "private-token problem"}), retries=2)
        stream = client.stream_chat([{"role": "user", "content": "hi"}], **self.options())
        next(stream)
        with self.assertRaises(ProviderError) as caught:
            list(stream)
        self.assertEqual(caught.exception.code, "server_error")
        self.assertNotIn("private-token", str(caught.exception))
        self.assertEqual(len(requests), 1)

    def test_retry_before_text_preserves_failed_usage(self):
        client, requests = self.client(sse({"type": "response.failed", "response": {
            "status": "failed", "error": {"type": "server_error", "message": "busy"}, "usage": {"total_tokens": 2}}}),
            sse({"type": "response.completed", "response": completed()}), retries=1)
        events = list(client.stream_chat([{"role": "user", "content": "hi"}], **self.options()))
        self.assertEqual(events[-1].usage["total_tokens"], 10)
        self.assertEqual(events[-1].usage["provider_requests"], 2)

    def test_incomplete_is_error_in_both_modes(self):
        value = completed("partial", status="incomplete", incomplete_details={"reason": "max_output_tokens"})
        for stream in (False, True):
            client, _ = self.client(sse({"type": "response.incomplete", "response": value}) if stream else value)
            with self.assertRaises(ProviderError) as caught:
                if stream:
                    list(client.stream_chat([{"role": "user", "content": "hi"}], **self.options()))
                else:
                    client.chat([{"role": "user", "content": "hi"}], **self.options())
            self.assertEqual(caught.exception.finish_reason, "length")
            self.assertEqual(caught.exception.usage["total_tokens"], 8)

    def test_stream_mismatched_final_text_is_not_success(self):
        client, _ = self.client(sse({"type": "response.output_text.delta", "delta": "wrong"},
                                    {"type": "response.completed", "response": completed()}))
        with self.assertRaises(ProviderError) as caught:
            list(client.stream_chat([{"role": "user", "content": "hi"}], **self.options()))
        self.assertEqual(caught.exception.code, "invalid_stream")

    def test_deadline_applies_to_keepalive(self):
        client, requests = self.client(io.BytesIO(b": ping\n\n"), timeout=1)
        with patch("tools.acm_agent.deepseek.time.monotonic", side_effect=[0, 0, 2]):
            with self.assertRaises(ProviderError) as caught:
                list(client.stream_chat([{"role": "user", "content": "hi"}], **self.options()))
        self.assertEqual(caught.exception.code, "timeout")
        self.assertEqual(len(requests), 1)

    def test_redirect_origin_and_tool_rejection(self):
        response = io.BytesIO(b"{}")
        response.geturl = lambda: "https://evil.example/responses"
        client, _ = self.client(response)
        with self.assertRaises(ProviderError) as caught:
            client.chat([{"role": "user", "content": "hi"}], **self.options())
        self.assertEqual(caught.exception.code, "redirect_blocked")
        with self.assertRaises(ProviderError) as caught:
            client.chat_with_tools([], model="model")
        self.assertEqual(caught.exception.code, "unsupported_capability")

    def test_refusals_fail_in_nonstream_and_stream_modes(self):
        refused = completed()
        refused["output"][0]["content"] = [{"type": "refusal", "refusal": "Cannot comply"}]
        client, _ = self.client(refused)
        with self.assertRaises(ProviderError) as caught:
            client.chat([{"role": "user", "content": "hi"}], **self.options())
        self.assertEqual(caught.exception.code, "content_filter")
        client, _ = self.client(sse({"type": "response.refusal.delta", "delta": "Cannot comply"}))
        with self.assertRaises(ProviderError) as caught:
            list(client.stream_chat([{"role": "user", "content": "hi"}], **self.options()))
        self.assertEqual(caught.exception.code, "content_filter")

    def test_credential_origin_mismatch_prevents_request(self):
        client, requests = self.client(completed(), credential_origin="https://other.example")
        with self.assertRaises(ProviderError) as caught:
            client.chat([{"role": "user", "content": "hi"}], **self.options())
        self.assertEqual(caught.exception.code, "credential_origin_mismatch")
        self.assertEqual(requests, [])

    def test_malformed_stream_events_are_typed_failures(self):
        for event in ([], "not-json", {"type": "response.output_text.delta", "delta": 4},
                      {"type": "response.completed", "response": {"status": "completed", "output": "wrong"}}):
            client, _ = self.client(sse(event))
            with self.assertRaises(ProviderError) as caught:
                list(client.stream_chat([{"role": "user", "content": "hi"}], **self.options()))
            self.assertIn(caught.exception.code, {"invalid_stream", "invalid_response"})


if __name__ == "__main__":
    unittest.main()
