from __future__ import annotations

import io
import json
import os
import socket
import tempfile
import threading
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from tools.acm_agent.config import CONFIG_VERSION, Paths, load_config
from tools.acm_agent.deepseek import (
    DEEPSEEK_ENDPOINT,
    DeepSeekCancelScope,
    DeepSeekCancelledError,
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
    usage: dict | None = None,
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
            "usage": usage
            or {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        }
    ).encode("utf-8")


def tool_completion(tool_calls, *, finish_reason="tool_calls") -> bytes:
    return json.dumps(
        {
            "id": "tool-response",
            "model": "deepseek-v4-flash",
            "choices": [
                {
                    "finish_reason": finish_reason,
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "reasoning_content": "hidden",
                        "tool_calls": tool_calls,
                    },
                }
            ],
            "usage": {"prompt_tokens": 4, "completion_tokens": 1, "total_tokens": 5},
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


class FakeClock:
    def __init__(self, now: float = 0.0):
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)

    def sleep(self, seconds: float) -> None:
        self.advance(seconds)


class TimedResponse:
    status = 200

    def __init__(self, clock: FakeClock, *chunks: tuple[float, bytes]):
        self.clock = clock
        self.chunks = list(chunks)
        self.closed = False
        self.close_calls = 0

    def read(self, _size: int = -1):
        if self.closed:
            raise ValueError("response is closed")
        if not self.chunks:
            return b""
        delay, chunk = self.chunks.pop(0)
        self.clock.advance(delay)
        return chunk

    def close(self):
        self.close_calls += 1
        self.closed = True


class TimedQueueTransport(QueueTransport):
    def __init__(self, clock: FakeClock, *items: tuple[float, object]):
        super().__init__(*items)
        self.clock = clock

    def __call__(self, request, timeout):
        self.requests.append((request, timeout))
        delay, item = self.items.pop(0)
        self.clock.advance(delay)
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

    def read(self, _size: int = -1):
        raise self.error

    def close(self):
        return None


class BlockingResponse:
    status = 200

    def __init__(self, body: bytes = b""):
        self.body = body
        self.started = threading.Event()
        self.release = threading.Event()
        self.closed = False
        self.close_calls = 0
        self._sent = False

    def read(self, _size: int = -1):
        self.started.set()
        self.release.wait(2)
        if self.closed:
            raise OSError("response closed")
        if self._sent:
            return b""
        self._sent = True
        return self.body

    def close(self):
        self.close_calls += 1
        self.closed = True
        self.release.set()

    def release_body(self) -> None:
        self.release.set()


class SocketBlockingResponse(BlockingResponse):
    class Socket:
        def __init__(self, release: threading.Event):
            self.release = release
            self.shutdown_calls = []

        def shutdown(self, how):
            self.shutdown_calls.append(how)
            self.release.set()

    def __init__(self, body: bytes = b""):
        super().__init__(body)
        self.fp = type("FP", (), {})()
        self.fp.raw = type("Raw", (), {})()
        self.fp.raw._sock = self.Socket(self.release)

    def close(self):
        # Model the Windows urllib behavior observed in the paid smoke: close
        # marks the wrapper but does not wake a thread blocked in recv.
        self.close_calls += 1
        self.closed = True


class CancelOnCloseResponse(FakeResponse):
    def __init__(self, body: bytes, scope: DeepSeekCancelScope):
        super().__init__(body)
        self.scope = scope
        self._cancelled_once = False

    def close(self):
        super().close()
        if not self._cancelled_once:
            self._cancelled_once = True
            self.scope.cancel()


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
    def test_tool_loop_is_bounded_and_round_trips_only_registered_functions(self) -> None:
        transport = QueueTransport(
            FakeResponse(
                tool_completion(
                    [
                        {
                            "id": "call-1",
                            "type": "function",
                            "function": {
                                "name": "search_source",
                                "arguments": '{"query":"CF1A"}',
                            },
                        }
                    ]
                )
            ),
            FakeResponse(completion('{"selected":"candidate-1"}')),
        )
        calls = []
        result = DeepSeekClient("key", transport=transport, retries=0).chat_with_tools(
            [{"role": "user", "content": "search"}],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "search_source",
                        "description": "Search an allowlisted source",
                        "parameters": {
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                            "required": ["query"],
                        },
                    },
                }
            ],
            tool_handler=lambda name, args: calls.append((name, args)) or {"items": ["candidate-1"]},
        )
        self.assertEqual(calls, [("search_source", {"query": "CF1A"})])
        self.assertEqual(result.tool_rounds, 1)
        self.assertEqual(result.tool_calls, 1)
        self.assertEqual(result.usage["total_tokens"], 10)
        second = json.loads(transport.requests[1][0].data.decode("utf-8"))
        self.assertEqual(second["messages"][-2]["tool_calls"][0]["id"], "call-1")
        self.assertEqual(second["messages"][-1]["role"], "tool")
        self.assertNotIn("hidden", json.dumps(second))

    def test_tool_loop_rejects_unknown_tool_and_bad_arguments(self) -> None:
        definition = [
            {
                "type": "function",
                "function": {"name": "allowed", "parameters": {"type": "object"}},
            }
        ]
        unknown = FakeResponse(
            tool_completion(
                [
                    {
                        "id": "call-x",
                        "type": "function",
                        "function": {"name": "other", "arguments": "{}"},
                    }
                ]
            )
        )
        with self.assertRaises(DeepSeekProtocolError) as caught:
            DeepSeekClient("key", transport=QueueTransport(unknown), retries=0).chat_with_tools(
                [{"role": "user", "content": "go"}],
                tools=definition,
                tool_handler=lambda *_: {},
            )
        self.assertEqual(caught.exception.code, "invalid_tool_call")

    def test_chat_uses_fixed_endpoint_bearer_and_hides_reasoning(self) -> None:
        transport = QueueTransport(FakeResponse(completion("answer")))
        client = DeepSeekClient(
            "secret-key", transport=transport, timeout=7, retries=0
        )

        result = client.chat(
            [{"role": "user", "content": "hello"}],
            thinking=True,
            reasoning_effort="max",
            temperature=0.7,
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
        self.assertNotIn("temperature", payload)
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
        client = DeepSeekClient(
            "key", transport=transport, retries=2, sleep=sleeps.append
        )
        result = client.chat([{"role": "user", "content": "hi"}])
        self.assertEqual(result.content, "ok")
        self.assertEqual(len(transport.requests), 2)
        self.assertEqual(client.provider_request_count, 2)
        self.assertEqual(sleeps, [1.0])

        retry_events = []
        reset = urllib.error.URLError(ConnectionResetError(10054, "connection reset"))
        transport = QueueTransport(reset, reset, reset)
        with self.assertRaises(DeepSeekError) as exhausted:
            DeepSeekClient(
                "key", transport=transport, retries=2, sleep=lambda _delay: None
            ).chat(
                [{"role": "user", "content": "hi"}],
                retry_callback=lambda attempt, total, code, delay: retry_events.append(
                    (attempt, total, code, delay)
                ),
            )
        self.assertEqual(exhausted.exception.code, "network_error")
        self.assertIn("automatic retries exhausted: 2", str(exhausted.exception))
        self.assertEqual(
            retry_events,
            [(1, 2, "network_error", 1.0), (2, 2, "network_error", 2.0)],
        )

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
        client = DeepSeekClient("key", transport=transport, retries=0)
        result = client.chat_json(
            [{"role": "system", "content": "Return one JSON object."}]
        )
        first_payload = json.loads(transport.requests[0][0].data.decode("utf-8"))
        second_payload = json.loads(transport.requests[1][0].data.decode("utf-8"))
        self.assertEqual(first_payload["response_format"], {"type": "json_object"})
        self.assertEqual(second_payload["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning_effort", second_payload)
        self.assertIn("exactly one complete JSON object", second_payload["messages"][-1]["content"])
        self.assertEqual(len(transport.requests), 2)
        self.assertEqual(client.provider_request_count, 2)
        self.assertEqual(result.data, {"ranked": ["CF1A"]})
        self.assertEqual(result.content, '{"ranked":["CF1A"]}')

    def test_usage_preserves_nested_and_flat_reasoning_tokens_without_content(self) -> None:
        transport = QueueTransport(
            FakeResponse(
                completion(
                    "",
                    usage={
                        "total_tokens": 7,
                        "completion_tokens_details": {
                            "reasoning_tokens": 2,
                            "reasoning_content": "must-not-survive",
                        },
                    },
                )
            ),
            FakeResponse(
                completion(
                    '{"ok":true}',
                    usage={
                        "total_tokens": 11,
                        "completion_tokens_details": {"reasoning_tokens": 3},
                    },
                )
            ),
        )
        result = DeepSeekClient("key", transport=transport, retries=0).chat_json(
            [{"role": "system", "content": "Return JSON."}]
        )

        self.assertEqual(result.usage["total_tokens"], 18)
        self.assertEqual(result.usage["reasoning_tokens"], 5)
        self.assertEqual(
            result.usage["completion_tokens_details"]["reasoning_tokens"], 5
        )
        self.assertNotIn("reasoning_content", json.dumps(result.usage))

    def test_json_output_supports_per_request_cost_limits(self) -> None:
        transport = QueueTransport(FakeResponse(completion('{"ok":true}')))
        result = DeepSeekClient(
            "key", transport=transport, timeout=60, retries=2
        ).chat_json(
            [{"role": "system", "content": "Return one JSON object."}],
            request_timeout=3.5,
            request_retries=0,
            json_retries=0,
        )
        self.assertEqual(result.data, {"ok": True})
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(transport.requests[0][1], 3.5)

        invalid = QueueTransport(FakeResponse(completion("not json")))
        with self.assertRaises(DeepSeekProtocolError):
            DeepSeekClient("key", transport=invalid, retries=2).chat_json(
                [{"role": "system", "content": "Return JSON."}],
                request_timeout=4,
                request_retries=0,
                json_retries=0,
            )
        self.assertEqual(len(invalid.requests), 1)

    def test_absolute_deadline_closes_repeated_chunk_body(self) -> None:
        clock = FakeClock()
        body = completion("too late")
        response = TimedResponse(
            clock,
            (0.9, b" "),
            (0.9, b" "),
            (0.9, body),
        )
        transport = QueueTransport(response)
        client = DeepSeekClient(
            "key",
            transport=transport,
            timeout=999,
            retries=0,
            monotonic=clock,
        )

        with self.assertRaises(DeepSeekError) as caught:
            client.chat(
                [{"role": "user", "content": "hi"}],
                deadline=2.5,
            )

        self.assertEqual(caught.exception.code, "timeout")
        self.assertFalse(caught.exception.retryable)
        self.assertTrue(response.closed)
        self.assertGreaterEqual(response.close_calls, 1)
        self.assertEqual(transport.requests[0][1], 2.5)

    def test_network_retry_uses_remaining_deadline_and_request_cap(self) -> None:
        cap_clock = FakeClock()
        cap_transport = QueueTransport(FakeResponse(completion("ok")))
        capped = DeepSeekClient(
            "key",
            transport=cap_transport,
            timeout=999,
            retries=0,
            monotonic=cap_clock,
        ).chat(
            [{"role": "user", "content": "hi"}],
            total_timeout=1000,
        )
        self.assertEqual(capped.content, "ok")
        self.assertEqual(cap_transport.requests[0][1], 300.0)

        clock = FakeClock(10)
        transport = TimedQueueTransport(
            clock,
            (1.0, urllib.error.URLError("offline")),
            (1.0, FakeResponse(completion("recovered"))),
        )
        result = DeepSeekClient(
            "key",
            transport=transport,
            timeout=999,
            retries=1,
            sleep=clock.sleep,
            monotonic=clock,
        ).chat(
            [{"role": "user", "content": "hi"}],
            deadline=15,
        )

        self.assertEqual(result.content, "recovered")
        self.assertEqual([timeout for _request, timeout in transport.requests], [5.0, 3.0])
        self.assertEqual(clock.now, 13.0)

    def test_json_recovery_does_not_reset_total_timeout_and_keeps_usage(self) -> None:
        clock = FakeClock()
        first = TimedResponse(clock, (4.0, completion("")))
        second = TimedResponse(clock, (2.0, completion('{"ok":true}')))
        transport = QueueTransport(first, second)
        client = DeepSeekClient(
            "key",
            transport=transport,
            timeout=999,
            retries=0,
            monotonic=clock,
        )

        with self.assertRaises(DeepSeekError) as caught:
            client.chat_json(
                [{"role": "system", "content": "Return JSON."}],
                total_timeout=5,
            )

        error = caught.exception
        self.assertEqual(error.code, "timeout")
        self.assertEqual(error.usage["total_tokens"], 10)
        self.assertEqual(error.finish_reason, "stop")
        self.assertEqual(error.model, "deepseek-v4-flash")
        self.assertEqual(error.response_id, "response-1")
        self.assertEqual(error.protocol_details["json_attempts_completed"], 1)
        self.assertEqual([timeout for _request, timeout in transport.requests], [5.0, 1.0])
        self.assertTrue(second.closed)

    def test_cancel_scope_closes_live_response_immediately(self) -> None:
        scope = DeepSeekCancelScope()
        response = BlockingResponse(completion("unused"))
        transport = QueueTransport(response)
        errors: list[BaseException] = []

        def run() -> None:
            try:
                DeepSeekClient("key", transport=transport, retries=2).chat(
                    [{"role": "user", "content": "hi"}],
                    cancel_scope=scope,
                )
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=run)
        worker.start()
        self.assertTrue(response.started.wait(1))
        scope.cancel()
        worker.join(1)

        self.assertFalse(worker.is_alive())
        self.assertTrue(scope.cancelled)
        self.assertTrue(scope.event.is_set())
        self.assertTrue(response.closed)
        self.assertGreaterEqual(response.close_calls, 1)
        self.assertEqual(len(errors), 1)
        self.assertIsInstance(errors[0], DeepSeekCancelledError)
        self.assertEqual(errors[0].code, "request_cancelled")
        self.assertFalse(errors[0].retryable)

    def test_cancel_scope_shuts_down_socket_before_close_on_windows_style_response(self) -> None:
        scope = DeepSeekCancelScope()
        response = SocketBlockingResponse(completion("unused"))
        errors: list[BaseException] = []

        def run() -> None:
            try:
                DeepSeekClient(
                    "key", transport=QueueTransport(response), retries=0
                ).chat([{"role": "user", "content": "hi"}], cancel_scope=scope)
            except BaseException as exc:
                errors.append(exc)

        worker = threading.Thread(target=run)
        worker.start()
        self.assertTrue(response.started.wait(1))
        scope.cancel()
        worker.join(1)
        self.assertFalse(worker.is_alive())
        self.assertTrue(response.fp.raw._sock.shutdown_calls)
        self.assertTrue(
            all(value == socket.SHUT_RDWR for value in response.fp.raw._sock.shutdown_calls)
        )
        self.assertIsInstance(errors[0], DeepSeekCancelledError)

    def test_cancelled_network_retry_stops_before_second_transport(self) -> None:
        scope = DeepSeekCancelScope()
        sleeps: list[float] = []
        transport = QueueTransport(
            urllib.error.URLError("offline"),
            FakeResponse(completion("must not run")),
        )
        with self.assertRaises(DeepSeekCancelledError) as caught:
            DeepSeekClient(
                "key", transport=transport, retries=2, sleep=sleeps.append
            ).chat(
                [{"role": "user", "content": "hi"}],
                cancel_scope=scope,
                retry_callback=lambda *_args: scope.cancel(),
            )
        self.assertEqual(caught.exception.code, "request_cancelled")
        self.assertEqual(len(transport.requests), 1)
        self.assertEqual(sleeps, [])

    def test_cancel_before_json_recovery_preserves_first_usage(self) -> None:
        scope = DeepSeekCancelScope()
        transport = QueueTransport(
            FakeResponse(completion("")),
            FakeResponse(completion('{"unused":true}')),
        )

        class CancelAfterFirstClient(DeepSeekClient):
            def _nonstream(self, *args, **kwargs):
                result = super()._nonstream(*args, **kwargs)
                scope.cancel()
                return result

        with self.assertRaises(DeepSeekCancelledError) as caught:
            CancelAfterFirstClient("key", transport=transport, retries=0).chat_json(
                [{"role": "system", "content": "Return JSON."}],
                cancel_scope=scope,
            )
        self.assertEqual(caught.exception.usage["total_tokens"], 5)
        self.assertEqual(caught.exception.finish_reason, "stop")
        self.assertEqual(caught.exception.protocol_details["json_attempts_completed"], 1)
        self.assertEqual(len(transport.requests), 1)

    def test_cancelling_one_scope_does_not_close_other_live_response(self) -> None:
        scope_a = DeepSeekCancelScope()
        scope_b = DeepSeekCancelScope()
        response_a = BlockingResponse(completion("a"))
        response_b = BlockingResponse(completion("b"))
        errors: dict[str, BaseException] = {}
        results: dict[str, str] = {}

        def run(name: str, scope: DeepSeekCancelScope, response: BlockingResponse) -> None:
            try:
                result = DeepSeekClient(
                    "key", transport=QueueTransport(response), retries=0
                ).chat(
                    [{"role": "user", "content": name}],
                    cancel_scope=scope,
                )
                results[name] = result.content
            except BaseException as exc:
                errors[name] = exc

        first = threading.Thread(target=run, args=("a", scope_a, response_a))
        second = threading.Thread(target=run, args=("b", scope_b, response_b))
        first.start()
        second.start()
        self.assertTrue(response_a.started.wait(1))
        self.assertTrue(response_b.started.wait(1))

        scope_a.cancel()
        first.join(1)
        self.assertIsInstance(errors.get("a"), DeepSeekCancelledError)
        self.assertFalse(scope_b.cancelled)
        self.assertFalse(response_b.closed)

        response_b.release_body()
        second.join(1)
        self.assertFalse(second.is_alive())
        self.assertEqual(results.get("b"), "b")
        self.assertNotIn("b", errors)

    def test_thinking_tool_loop_round_trips_reasoning_only_in_memory(self) -> None:
        transport = QueueTransport(
            FakeResponse(
                tool_completion(
                    [{
                        "id": "call-1",
                        "type": "function",
                        "function": {"name": "search_source", "arguments": "{}"},
                    }]
                )
            ),
            FakeResponse(completion('{"selected_candidate_id":null}')),
        )
        result = DeepSeekClient("key", transport=transport, retries=0).chat_with_tools(
            [{"role": "user", "content": "search"}],
            tools=[{"type": "function", "function": {"name": "search_source", "parameters": {"type": "object"}}}],
            tool_handler=lambda _name, _args: {"items": []},
            thinking=True,
        )
        second = json.loads(transport.requests[1][0].data.decode("utf-8"))
        self.assertEqual(second["messages"][-2]["reasoning_content"], "hidden")
        self.assertNotIn("hidden", result.content)

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
