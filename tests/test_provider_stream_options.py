from __future__ import annotations

import io
import json
import unittest

from tools.acm_agent.openai_compatible import OpenAICompatibleClient
from tools.acm_agent.provider import CapabilityProfile


class ProviderStreamOptionsTests(unittest.TestCase):
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
