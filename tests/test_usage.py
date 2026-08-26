from __future__ import annotations

import unittest

from tools.acm_agent.usage import merge_usage, normalize_usage


class UsageTests(unittest.TestCase):
    def test_normalizes_provider_token_aliases_and_cache_details(self) -> None:
        usage = normalize_usage({
            "prompt_tokens": 10,
            "completion_tokens": 4,
            "prompt_cache_hit_tokens": 3,
            "prompt_cache_miss_tokens": 7,
            "completion_tokens_details": {"reasoning_tokens": 2},
            "reasoning_content": "must not survive",
        })
        self.assertEqual(usage["input_tokens"], 10)
        self.assertEqual(usage["output_tokens"], 4)
        self.assertEqual(usage["total_tokens"], 14)
        self.assertEqual(usage["cache_read_tokens"], 3)
        self.assertEqual(usage["cache_miss_tokens"], 7)
        self.assertNotIn("cache_write_tokens", usage)
        self.assertEqual(usage["reasoning_tokens"], 2)
        self.assertNotIn("reasoning_content", usage)

    def test_unknown_is_not_invented_as_zero_and_real_zero_is_preserved(self) -> None:
        self.assertNotIn("cache_read_tokens", normalize_usage({"prompt_tokens": 1}))
        self.assertEqual(
            normalize_usage({"prompt_cache_hit_tokens": 0})["cache_read_tokens"], 0
        )

    def test_untrusted_usage_cannot_echo_secrets_or_content(self) -> None:
        usage = normalize_usage({
            "total_tokens": 4,
            "authorization": "Bearer top-secret-value",
            "raw_prompt": "private prompt",
            "nested": {"token": "top-secret-value"},
            "prompt_tokens_details": {
                "cached_tokens": 2,
                "raw_prompt": "private prompt",
            },
        })
        encoded = repr(usage)
        self.assertEqual(usage["total_tokens"], 4)
        self.assertEqual(usage["prompt_tokens_details"], {"cached_tokens": 2})
        self.assertNotIn("top-secret-value", encoded)
        self.assertNotIn("private prompt", encoded)

    def test_merge_accumulates_canonical_usage(self) -> None:
        target: dict[str, object] = {}
        merge_usage(target, {"prompt_tokens": 5, "completion_tokens": 2})
        merge_usage(target, {"input_tokens": 3, "output_tokens": 1})
        self.assertEqual(target["input_tokens"], 8)
        self.assertEqual(target["output_tokens"], 3)
        self.assertEqual(target["total_tokens"], 11)


if __name__ == "__main__":
    unittest.main()
