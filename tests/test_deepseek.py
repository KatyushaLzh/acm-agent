from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from tools.acm_agent.config import CONFIG_VERSION, Paths, load_config
from tools.acm_agent.deepseek import (
    DEEPSEEK_ENDPOINT,
    DeepSeekClient,
    DeepSeekConfigurationError,
    DeepSeekError,
    DeepSeekProtocolError,
)


def completion(
    content: str,
    *,
    model: str = "deepseek-v4-flash",
    finish_reason: str = "stop",
) -> bytes:
    return json.dumps(
        {
            "id": "response-1",
            "model": model,
            "choices": [
                {
                    "finish_reason": finish_reason,
                    "message": {
                        "role": "assistant",
                        "content": content,
                        "reasoning_content": "private chain of thought",
                    },
                }
            ],
            "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        }
    ).encode("utf-8")


class FakeResponse(io.BytesIO):
    def __init__(self, body: bytes, *, status: int = 200):
        super().__init__(body)
        self.status = status


class QueueTransport:
    def __init__(self, *items: object):
        self.items = list(items)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


class BrokenStream:
    status = 200

    def __init__(self, lines: list[bytes], error: BaseException):
        self.lines = lines
        self.error = error

    def __iter__(self):
        yield from self.lines
        raise self.error

    def close(self):
        return None


class BrokenRead:
    status = 200

    def __init__(self, error: BaseException):
        self.error = error

    def read(self):
        raise self.error

    def close(self):
        return None


class ConfigV2Tests(unittest.TestCase):
    def test_v1_is_merged_and_atomically_upgraded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = Paths.for_root(Path(directory))
            paths.ensure()
            old = {
                "version": 1,
                "accounts": {
                    "codeforces": {"handle": "tourist"},
                    "luogu": {"uid": "42"},
                },
                "recommendation": {"count": 7, "custom_option": "preserved"},
                "sync": {"timeout_seconds": 31},
                "extension": {"enabled": True},
            }
            paths.config.write_text(json.dumps(old), encoding="utf-8")

            loaded = load_config(paths)

            self.assertEqual(loaded["version"], CONFIG_VERSION)
            self.assertEqual(loaded["accounts"]["codeforces"]["handle"], "tourist")
            self.assertEqual(loaded["recommendation"]["count"], 7)
            self.assertEqual(loaded["recommendation"]["custom_option"], "preserved")
            self.assertEqual(loaded["extension"], {"enabled": True})
            self.assertEqual(loaded["ai"]["recommendation_model"], "deepseek-v4-flash")
            self.assertEqual(loaded["ai"]["coaching_model"], "deepseek-v4-flash")
            self.assertFalse(loaded["ai"]["recommendation_thinking"])
            self.assertTrue(loaded["ai"]["coaching_thinking"])
            self.assertEqual(loaded["ai"]["reasoning_effort"], "high")
            self.assertEqual(json.loads(paths.config.read_text(encoding="utf-8")), loaded)
            self.assertEqual(list(paths.state_dir.glob("config-*.json")), [])

    def test_v2_missing_nested_defaults_is_repaired(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = Paths.for_root(Path(directory))
            paths.ensure()
            paths.config.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "accounts": {},
                        "ai": {"coaching_model": "deepseek-v4-pro"},
                    }
                ),
                encoding="utf-8",
            )
            loaded = load_config(paths)
            self.assertEqual(loaded["ai"]["coaching_model"], "deepseek-v4-pro")
            self.assertEqual(loaded["ai"]["recommendation_model"], "deepseek-v4-flash")
            self.assertIn("codeforces", loaded["accounts"])

    def test_sensitive_ai_fields_are_removed_during_load(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            paths = Paths.for_root(Path(directory))
            paths.ensure()
            paths.config.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "accounts": {},
                        "ai": {
                            "api_key": "must-not-survive",
                            "deepseek_api_key": "also-must-not-survive",
                            "Authorization": "Bearer must-not-survive",
                            "coaching_model": "deepseek-v4-pro",
                        },
                    }
                ),
                encoding="utf-8",
            )
            loaded = load_config(paths)
            self.assertNotIn("api_key", loaded["ai"])
            self.assertNotIn("Authorization", loaded["ai"])
            self.assertNotIn("must-not-survive", paths.config.read_text(encoding="utf-8"))


class DeepSeekClientTests(unittest.TestCase):
    def test_chat_uses_fixed_endpoint_bearer_and_hides_reasoning(self) -> None:
        transport = QueueTransport(FakeResponse(completion("answer")))
        client = DeepSeekClient(
            "secret-key", transport=transport, timeout=7, retries=0
        )

        result = client.chat(
            [{"role": "user", "content": "hello"}],
            thinking=True,
            reasoning_effort="max",
        )

        request, timeout = transport.requests[0]
        payload = json.loads(request.data.decode("utf-8"))
        self.assertEqual(request.full_url, DEEPSEEK_ENDPOINT)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-key")
        self.assertEqual(timeout, 7)
        self.assertEqual(payload["model"], "deepseek-v4-flash")
        self.assertEqual(payload["thinking"], {"type": "enabled"})
        self.assertEqual(payload["reasoning_effort"], "max")
        self.assertEqual(result.content, "answer")
        self.assertEqual(result.finish_reason, "stop")
        self.assertEqual(result.usage["total_tokens"], 5)
        self.assertEqual(result.model, "deepseek-v4-flash")
        self.assertNotIn("private chain", repr(result))
        self.assertNotIn("secret-key", repr(client))

    def test_default_reads_environment_without_exposing_key(self) -> None:
        transport = QueueTransport(FakeResponse(completion("ok")))
        with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "env-secret"}, clear=False):
            client = DeepSeekClient(transport=transport, retries=0)
        self.assertTrue(client.key_detected)
        self.assertNotIn("env-secret", repr(client))
        client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(
            transport.requests[0][0].get_header("Authorization"),
            "Bearer env-secret",
        )

    def test_missing_key_and_invalid_model_are_local_errors(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            client = DeepSeekClient(transport=QueueTransport(), retries=0)
        with self.assertRaises(DeepSeekConfigurationError) as missing:
            client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(missing.exception.code, "missing_api_key")
        with self.assertRaises(DeepSeekConfigurationError) as invalid:
            DeepSeekClient("x", transport=QueueTransport()).chat(
                [{"role": "user", "content": "hi"}], model="deepseek-chat"
            )
        self.assertEqual(invalid.exception.code, "invalid_model")

    def test_retryable_http_status_retries_but_auth_does_not(self) -> None:
        rate_limited = urllib.error.HTTPError(
            DEEPSEEK_ENDPOINT,
            429,
            "rate limited",
            {},
            io.BytesIO(b'{"error":{"message":"slow down"}}'),
        )
        sleeps: list[float] = []
        transport = QueueTransport(rate_limited, FakeResponse(completion("ok")))
        result = DeepSeekClient(
            "key", transport=transport, retries=2, sleep=sleeps.append
        ).chat([{"role": "user", "content": "hi"}])
        self.assertEqual(result.content, "ok")
        self.assertEqual(len(transport.requests), 2)
        self.assertEqual(sleeps, [1.0])

        auth = urllib.error.HTTPError(
            DEEPSEEK_ENDPOINT,
            401,
            "unauthorized",
            {},
            io.BytesIO(
                b'{"error":{"message":"bad Bearer secret-value and sk-abcdefghijk"}}'
            ),
        )
        transport = QueueTransport(auth, FakeResponse(completion("unused")))
        with self.assertRaises(DeepSeekError) as caught:
            DeepSeekClient("secret-value", transport=transport, retries=2).chat(
                [{"role": "user", "content": "hi"}]
            )
        self.assertEqual(caught.exception.status, 401)
        self.assertFalse(caught.exception.retryable)
        self.assertEqual(len(transport.requests), 1)
        self.assertNotIn("secret-value", str(caught.exception))
        self.assertNotIn("sk-abcdefghijk", str(caught.exception))

    def test_json_output_sets_contract_and_retries_empty_once(self) -> None:
        transport = QueueTransport(
            FakeResponse(completion("")),
            FakeResponse(completion('{"ranked":["CF1A"]}')),
        )
        result = DeepSeekClient("key", transport=transport, retries=0).chat_json(
            [{"role": "system", "content": "Return one JSON object."}]
        )
        first_payload = json.loads(transport.requests[0][0].data.decode("utf-8"))
        self.assertEqual(first_payload["response_format"], {"type": "json_object"})
        self.assertEqual(len(transport.requests), 2)
        self.assertEqual(result.data, {"ranked": ["CF1A"]})
        self.assertEqual(result.content, '{"ranked":["CF1A"]}')

    def test_json_output_requires_instruction_and_rejects_two_bad_results(self) -> None:
        client = DeepSeekClient("key", transport=QueueTransport(), retries=0)
        with self.assertRaises(DeepSeekConfigurationError) as caught:
            client.chat_json([{"role": "user", "content": "rank these"}])
        self.assertEqual(caught.exception.code, "missing_json_instruction")

        transport = QueueTransport(
            FakeResponse(completion("not json")),
            FakeResponse(completion("[]")),
        )
        with self.assertRaises(DeepSeekProtocolError) as malformed:
            DeepSeekClient("key", transport=transport, retries=0).chat_json(
                [{"role": "user", "content": "Return JSON."}]
            )
        self.assertEqual(malformed.exception.code, "invalid_json_output")
        self.assertEqual(len(transport.requests), 2)

    def test_stream_ignores_keepalive_and_reasoning_then_emits_terminal_usage(self) -> None:
        stream = b"".join(
            [
                b": keep-alive\n\n",
                b'data: {"id":"s1","model":"deepseek-v4-pro","choices":[{"delta":{"role":"assistant","reasoning_content":"hidden","content":""},"finish_reason":null}],"usage":null}\n\n',
                b'data: {"id":"s1","model":"deepseek-v4-pro","choices":[{"delta":{"content":"Hello"},"finish_reason":null}],"usage":null}\n\n',
                b'data: {"id":"s1","model":"deepseek-v4-pro","choices":[{"delta":{"content":"!"},"finish_reason":"stop"}],"usage":null}\n\n',
                b'data: {"id":"s1","model":"deepseek-v4-pro","choices":[],"usage":{"total_tokens":9}}\n\n',
                b"data: [DONE]\n\n",
            ]
        )
        transport = QueueTransport(FakeResponse(stream))
        events = list(
            DeepSeekClient("key", transport=transport, retries=0).stream_chat(
                [{"role": "user", "content": "help"}],
                model="deepseek-v4-pro",
            )
        )
        self.assertEqual(
            [event.kind for event in events],
            ["heartbeat", "delta", "delta", "usage", "done"],
        )
        self.assertEqual("".join(event.content for event in events), "Hello!")
        self.assertEqual(events[-1].finish_reason, "stop")
        self.assertEqual(events[-1].usage, {"total_tokens": 9})
        self.assertNotIn("hidden", repr(events))
        payload = json.loads(transport.requests[0][0].data.decode("utf-8"))
        self.assertTrue(payload["stream"])
        self.assertEqual(payload["stream_options"], {"include_usage": True})

    def test_stream_retries_only_before_content(self) -> None:
        complete = FakeResponse(
            b'data: {"choices":[{"delta":{"content":"ok"},"finish_reason":"stop"}]}\n\n'
            b"data: [DONE]\n\n"
        )
        sleeps: list[float] = []
        transport = QueueTransport(urllib.error.URLError("offline"), complete)
        events = list(
            DeepSeekClient(
                "key", transport=transport, retries=2, sleep=sleeps.append
            ).stream_chat([{"role": "user", "content": "hi"}])
        )
        self.assertEqual([event.kind for event in events], ["delta", "done"])
        self.assertEqual(len(transport.requests), 2)
        self.assertEqual(sleeps, [1.0])

        broken = BrokenStream(
            [
                b'data: {"choices":[{"delta":{"content":"partial"},"finish_reason":null}]}\n',
                b"\n",
            ],
            urllib.error.URLError("dropped"),
        )
        unused_complete = FakeResponse(
            b'data: {"choices":[{"delta":{"content":"unused"},"finish_reason":"stop"}]}\n\n'
            b"data: [DONE]\n\n"
        )
        transport = QueueTransport(broken, unused_complete)
        iterator = DeepSeekClient("key", transport=transport, retries=2).stream_chat(
            [{"role": "user", "content": "hi"}]
        )
        self.assertEqual(next(iterator).content, "partial")
        with self.assertRaises(DeepSeekError) as caught:
            list(iterator)
        self.assertEqual(caught.exception.code, "network_error")
        self.assertEqual(len(transport.requests), 1)

    def test_response_read_failure_is_mapped_and_retried(self) -> None:
        sleeps: list[float] = []
        transport = QueueTransport(
            BrokenRead(TimeoutError("read timed out")),
            FakeResponse(completion("ok")),
        )
        result = DeepSeekClient(
            "key", transport=transport, retries=1, sleep=sleeps.append
        ).chat([{"role": "user", "content": "hi"}])
        self.assertEqual(result.content, "ok")
        self.assertEqual(len(transport.requests), 2)
        self.assertEqual(sleeps, [1.0])


if __name__ == "__main__":
    unittest.main()
