from __future__ import annotations

import io
import json
import unittest
import urllib.error
from unittest.mock import patch

from tools.acm_agent.openai_compatible import OpenAICompatibleClient
from tools.acm_agent.provider import CapabilityProfile, ProviderError


class ProviderStreamOptionsTests(unittest.TestCase):
    def client(self, transport, *, streaming=True, wire=None, retries=0):
        return OpenAICompatibleClient(
            "test-key", provider_id="relay", base_url="https://relay.example/v1",
            auth={"type": "bearer"}, transport=transport, retries=retries,
            sleep=lambda _: None, wire_profile=wire,
            models={"model": CapabilityProfile(
                text_chat=True, streaming=streaming, json_object=True,
                function_tools=False, thinking=True, prompt_cache=False,
                usage_cache_tokens=False,
            )},
        )

    def stream_body(self, *, content="OK", finish_reason="stop", done=True):
        chunks = [
            {"id": "response-1", "model": "model", "choices": [
                {"delta": {"reasoning_content": "private reasoning"}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": content[:2]}, "finish_reason": None}]},
            {"choices": [{"delta": {"content": content[2:]}, "finish_reason": finish_reason}]},
            {"choices": [], "usage": {
                "prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10,
                "prompt_tokens_details": {"cached_tokens": 4},
            }},
        ]
        body = ": keepalive\n\n" + "".join(
            "data: " + json.dumps(chunk) + "\n\n" for chunk in chunks
        )
        return (body + ("data: [DONE]\n\n" if done else "")).encode()

    def test_sync_text_and_schema_collect_complete_stream_in_one_request(self):
        for structured in (False, True):
            with self.subTest(structured=structured):
                requests = []
                content = '{"ok":true}' if structured else "complete answer"

                def transport(request, timeout):
                    payload = json.loads(request.data)
                    requests.append(payload)
                    body = self.stream_body(content=content) if payload["stream"] else b": keepalive\n\n"
                    return io.BytesIO(body)

                client = self.client(transport, wire={"structured_output": "native_schema"})
                messages = [{"role": "user", "content": "Return JSON" if structured else "Reply"}]
                result = client.structured(
                    messages, model="model", json_schema={"type": "object"}, schema_name="result",
                ) if structured else client.chat(messages, model="model")
                self.assertEqual(result.content, content)
                if structured:
                    self.assertEqual(result.data, {"ok": True})
                    self.assertTrue(requests[0]["response_format"]["json_schema"]["strict"])
                self.assertEqual(result.finish_reason, "stop")
                self.assertEqual(result.response_id, "response-1")
                self.assertEqual(result.model, "model")
                self.assertEqual(result.usage["total_tokens"], 10)
                self.assertEqual(result.usage["cache_read_tokens"], 4)
                self.assertEqual(result.usage["provider_requests"], 1)
                self.assertEqual(len(requests), 1)
                self.assertNotIn("private reasoning", repr(result))

    def test_sync_rejects_unterminated_or_unfinished_stream(self):
        for done, reason in ((False, "stop"), (True, None)):
            with self.subTest(done=done, reason=reason):
                response = io.BytesIO(self.stream_body(finish_reason=reason, done=done))
                client = self.client(lambda *_: response)
                with self.assertRaises(ProviderError) as caught:
                    client.chat([{"role": "user", "content": "Reply"}], model="model")
                self.assertEqual(caught.exception.code, "incomplete_stream")
                self.assertEqual(caught.exception.usage["total_tokens"], 10)
                self.assertTrue(response.closed)

    def test_sync_preserves_length_for_structured_governance(self):
        client = self.client(lambda *_: io.BytesIO(self.stream_body(
            content='{"ok":true}', finish_reason="length",
        )), wire={"structured_output": "native_schema"})
        result = client.structured(
            [{"role": "user", "content": "Return JSON"}], model="model",
            json_schema={"type": "object"}, schema_name="result",
        )
        self.assertEqual(result.finish_reason, "length")
        self.assertEqual(result.usage["total_tokens"], 10)

    def test_explicit_nonstream_capability_and_buffered_mode_remain_usable(self):
        for streaming, wire in ((False, {}), (True, {"streaming": "buffered"})):
            with self.subTest(streaming=streaming, wire=wire):
                requests = []

                def transport(request, timeout):
                    payload = json.loads(request.data)
                    requests.append(payload)
                    self.assertFalse(payload["stream"])
                    return io.BytesIO(json.dumps({"model": "model", "choices": [
                        {"message": {"content": "answer"}, "finish_reason": "stop"}],
                    }).encode())

                result = self.client(transport, streaming=streaming, wire=wire).chat(
                    [{"role": "user", "content": "Reply"}], model="model",
                )
                self.assertEqual(result.content, "answer")
                self.assertEqual(len(requests), 1)

    def test_sync_retry_override_preserves_timeout_and_callback(self):
        for allowed_retries in (0, 1):
            with self.subTest(allowed_retries=allowed_retries):
                timeouts, callbacks = [], []

                def transport(request, timeout):
                    timeouts.append(timeout)
                    if len(timeouts) == 1:
                        raise urllib.error.HTTPError(request.full_url, 503, "busy", {}, io.BytesIO(b"{}"))
                    return io.BytesIO(self.stream_body())

                client = self.client(transport, retries=3)
                options = {"model": "model", "request_retries": allowed_retries,
                           "request_timeout": 2.5, "retry_callback": lambda *args: callbacks.append(args)}
                if allowed_retries:
                    self.assertEqual(client.chat([{"role": "user", "content": "Reply"}], **options).content, "OK")
                else:
                    with self.assertRaises(ProviderError):
                        client.chat([{"role": "user", "content": "Reply"}], **options)
                self.assertEqual(timeouts, [2.5] * (allowed_retries + 1))
                self.assertEqual(len(callbacks), allowed_retries)

    def test_sync_timeout_includes_keepalive_frames(self):
        client = self.client(lambda *_: io.BytesIO(self.stream_body()))
        elapsed = [0.0]

        def clock():
            elapsed[0] += 0.1
            return elapsed[0]

        with patch("tools.acm_agent.deepseek.time.monotonic", side_effect=clock):
            with self.assertRaises(ProviderError) as caught:
                client.chat([{"role": "user", "content": "Reply"}], model="model", request_timeout=0.15)
        self.assertEqual(caught.exception.code, "timeout")
        self.assertEqual(caught.exception.usage["provider_requests"], 1)

    def test_stream_usage_extension_follows_declared_capability(self):
        for supports_usage in (False, True):
            with self.subTest(stream_usage=supports_usage):
                requests = []

                def transport(request, timeout):
                    self.assertEqual(request.get_header("Accept"), "text/event-stream")
                    payload = json.loads(request.data)
                    requests.append(payload)
                    # Emulate a strict basic-streaming provider that rejects
                    # extensions it has not declared.
                    if not supports_usage:
                        self.assertNotIn("stream_options", payload)
                    else:
                        self.assertEqual(payload["stream_options"], {"include_usage": True})
                    chunks = [
                        {"choices": [{"delta": {"content": "OK"}, "finish_reason": "stop"}]},
                    ]
                    if supports_usage:
                        chunks.append({"choices": [], "usage": {
                            "prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3,
                        }})
                    body = "".join("data: " + json.dumps(chunk) + "\n\n" for chunk in chunks)
                    return io.BytesIO((body + "data: [DONE]\n\n").encode())

                client = OpenAICompatibleClient(
                    "test-key", provider_id="relay", base_url="https://relay.example/v1",
                    auth={"type": "bearer"}, transport=transport, retries=0,
                    models={"model": CapabilityProfile(
                        text_chat=True, streaming=True, json_object=False,
                        function_tools=False, thinking=False, prompt_cache=False,
                        usage_cache_tokens=False, stream_usage=supports_usage,
                    )},
                )
                events = list(client.stream_chat(
                    [{"role": "user", "content": "Reply OK"}], model="model",
                    thinking=False, reasoning_effort="auto",
                ))
                self.assertEqual(len(requests), 1)
                self.assertTrue(requests[0]["stream"])
                self.assertEqual("".join(event.content for event in events), "OK")
                self.assertEqual(events[-1].kind, "done")
                if supports_usage:
                    self.assertEqual(events[-1].usage["total_tokens"], 3)


if __name__ == "__main__":
    unittest.main()
