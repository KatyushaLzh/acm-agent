from __future__ import annotations

import io
import json
import unittest
from unittest.mock import patch

from tools.acm_agent.deepseek import DeepSeekClient, DeepSeekError, _iter_sse_data


def sse(*chunks):
    return io.BytesIO("".join(
        "data: " + (chunk if isinstance(chunk, str) else json.dumps(chunk)) + "\n\n"
        for chunk in chunks
    ).encode())


class ProviderStreamErrorsTests(unittest.TestCase):
    def client(self, *responses, retries=0, secret="test-key", timeout=30):
        pending = list(responses)
        requests = []

        def transport(request, timeout):
            requests.append(request)
            return pending.pop(0)

        return DeepSeekClient(secret, transport=transport, retries=retries, timeout=timeout, sleep=lambda _: None), requests

    def test_sse_deadline_checks_comments_and_blank_frames(self):
        with patch("tools.acm_agent.deepseek.time.monotonic", side_effect=[0, 1, 2, 3]):
            with self.assertRaises(DeepSeekError) as caught:
                list(_iter_sse_data([b": ping\n", b"\n", b": ping\n"], deadline=3))
        self.assertEqual(caught.exception.code, "timeout")
        self.assertTrue(caught.exception.retryable)
        with patch("tools.acm_agent.deepseek.time.monotonic") as clock:
            self.assertEqual(list(_iter_sse_data([b": ping\n", b"data: [DONE]\n", b"\n"])), ["[DONE]"])
            clock.assert_not_called()

    def test_stream_timeout_includes_connection_setup(self):
        client, requests = self.client(sse("[DONE]"), timeout=2)
        with patch("tools.acm_agent.deepseek.time.monotonic", side_effect=[0, 3]):
            with self.assertRaises(DeepSeekError) as caught:
                list(client.stream_chat([{"role": "user", "content": "hi"}]))
        self.assertEqual(caught.exception.code, "timeout")
        self.assertEqual(len(requests), 1)

    def test_stream_timeout_can_retry_before_output(self):
        client, requests = self.client(
            io.BytesIO(b": keepalive\n\n"),
            sse({"choices": [{"delta": {"content": "OK"}, "finish_reason": "stop"}]}, "[DONE]"),
            timeout=2, retries=1,
        )
        times = iter([0, 0, 3])
        with patch("tools.acm_agent.deepseek.time.monotonic", side_effect=lambda: next(times, 4)):
            events = list(client.stream_chat([{"role": "user", "content": "hi"}]))
        self.assertEqual("".join(e.content for e in events), "OK")
        self.assertEqual(events[-1].kind, "done")
        self.assertEqual(len(requests), 2)

    def test_stream_deadline_preserves_usage_without_replaying_partial_output(self):
        stream = sse(
            {"choices": [{"delta": {"content": "partial"}}], "usage": {"total_tokens": 4}},
        )
        client, requests = self.client(io.BytesIO(stream.getvalue() + b": ping\n\n"), timeout=2, retries=2)
        # Start, initial check, data line, separator, then a late keepalive.
        with patch("tools.acm_agent.deepseek.time.monotonic", side_effect=[0, 0, 0, 0, 3]):
            iterator = client.stream_chat([{"role": "user", "content": "hi"}])
            self.assertEqual(next(iterator).kind, "usage")
            self.assertEqual(next(iterator).content, "partial")
            with self.assertRaises(DeepSeekError) as caught:
                list(iterator)
        self.assertEqual(caught.exception.code, "timeout")
        self.assertEqual(caught.exception.usage["total_tokens"], 4)
        self.assertEqual(len(requests), 1)

    def test_error_envelopes_preserve_provider_error_classification(self):
        for error, expected, retryable in (
            ({"type": "server_error"}, "server_error", True),
            ({"type": "invalid_request_error"}, "invalid_request", False),
            ({"code": "rate_limit_exceeded"}, "rate_limited", True),
            ({"code": "invalid_api_key"}, "authentication_failed", False),
            ({"status_code": 503}, "server_error", True),
            ({"code": 400}, "invalid_request", False),
            ({"code": "insufficient_quota", "type": "rate_limit_error"}, "insufficient_balance", False),
            ({"code": "unknown_failure"}, "provider_error", False),
        ):
            with self.subTest(error=error):
                client, requests = self.client(sse({"error": {**error, "message": "provider detail"}}, "[DONE]"))
                with self.assertRaises(DeepSeekError) as caught:
                    list(client.stream_chat([{"role": "user", "content": "hi"}]))
                self.assertEqual(caught.exception.code, expected)
                self.assertEqual(caught.exception.retryable, retryable)
                self.assertEqual(str(caught.exception), "provider detail")
                self.assertEqual(len(requests), 1)

    def test_reflected_credentials_are_redacted_in_message_and_details(self):
        secret = "private-nonstandard-token"
        client, _ = self.client(sse({"error": {
            "type": "invalid_request_error", "code": secret, "param": secret,
            "message": "Rejected " + secret + " Bearer other-credential",
        }}), secret=secret)
        with self.assertRaises(DeepSeekError) as caught:
            list(client.stream_chat([{"role": "user", "content": "hi"}]))
        serialized = json.dumps(caught.exception.as_dict())
        self.assertNotIn(secret, serialized)
        self.assertNotIn("other-credential", serialized)
        self.assertIn("[REDACTED]", serialized)

    def test_retry_before_output_preserves_error_usage(self):
        client, requests = self.client(
            sse({"error": {"type": "server_error", "message": "try again"}, "usage": {"total_tokens": 2}}),
            sse({"choices": [{"delta": {"content": "OK"}, "finish_reason": "stop"}], "usage": {"total_tokens": 3}}, "[DONE]"),
            retries=1,
        )
        events = list(client.stream_chat([{"role": "user", "content": "hi"}]))
        self.assertEqual("".join(e.content for e in events), "OK")
        self.assertEqual(events[-1].kind, "done")
        self.assertEqual(events[-1].usage["total_tokens"], 5)
        self.assertEqual(events[-1].usage["provider_requests"], 2)
        self.assertEqual(len(requests), 2)

    def test_partial_output_is_not_replayed_or_reported_done(self):
        client, requests = self.client(sse(
            {"choices": [{"delta": {"content": "partial"}}]},
            {"choices": [], "usage": {"total_tokens": 3}},
            {"error": {"type": "server_error", "message": "upstream failed"}},
            "[DONE]",
        ), retries=2)
        iterator = client.stream_chat([{"role": "user", "content": "hi"}])
        self.assertEqual(next(iterator).content, "partial")
        self.assertEqual(next(iterator).kind, "usage")
        with self.assertRaises(DeepSeekError) as caught:
            list(iterator)
        self.assertEqual(caught.exception.code, "server_error")
        self.assertEqual(caught.exception.usage["total_tokens"], 3)
        self.assertEqual(caught.exception.usage["provider_requests"], 1)
        self.assertEqual(len(requests), 1)

    def test_malformed_error_envelopes_still_fail_closed(self):
        for chunk in ({"error": "bad"}, {"event": "unknown"}, {"error": None}):
            with self.subTest(chunk=chunk):
                client, _ = self.client(sse(chunk, "[DONE]"))
                with self.assertRaises(DeepSeekError) as caught:
                    list(client.stream_chat([{"role": "user", "content": "hi"}]))
                self.assertEqual(caught.exception.code, "invalid_stream")

    def test_nonstream_error_envelope_uses_same_mapping_and_telemetry(self):
        client, requests = self.client(io.BytesIO(json.dumps({
            "error": {"type": "invalid_request_error", "message": "max_tokens rejected", "param": "max_tokens"},
            "usage": {"total_tokens": 4},
        }).encode()), retries=2)
        with self.assertRaises(DeepSeekError) as caught:
            client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(caught.exception.code, "invalid_request")
        self.assertEqual(str(caught.exception), "max_tokens rejected")
        self.assertEqual(caught.exception.protocol_details["provider_param"], "max_tokens")
        self.assertEqual(caught.exception.usage["total_tokens"], 4)
        self.assertEqual(caught.exception.usage["provider_requests"], 1)
        self.assertEqual(len(requests), 1)


if __name__ == "__main__":
    unittest.main()
