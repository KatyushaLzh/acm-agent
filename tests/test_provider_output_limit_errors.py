import unittest

from tools.acm_agent.provider import ProviderError
from tools.acm_agent.provider_output_limits import next_output_token_limit


class OutputTokenLimitErrorTests(unittest.TestCase):
    def limit(self, message, requested=200_000, **kwargs):
        return next_output_token_limit(ProviderError("invalid_request", message, **kwargs), requested)

    def test_provider_neutral_explicit_bounds(self):
        for message, expected in (
            ("max_tokens must be in [1,131072]", 131072),
            ("max_completion_tokens must be between 1 and 8192", 8192),
            ("max_output_tokens must be at most 4096", 4096),
            ("max_tokens must be less than or equal to 8192", 8192),
            ("max_tokens must be <= 32768", 32768),
            ("max_tokens maximum 8192", 8192),
            ("max_tokens: max allowed 8192", 8192),
            ("max_tokens is too large: 200000; maximum supported output tokens is 8192", 8192),
            ("max_tokens must be <= 8,192", 8192),
            ("max_tokens must be at most 8192. You requested 200000.", 8192),
            ("max_tokens 范围是 [1, 65536]", 65536),
            ("max_tokens参数非法：限制数值范围[1,131072]", 131072),
            ("max_tokens 不能超过 4096", 4096),
            ("max_tokens参数不能超过8192个Token", 8192),
            ("max_tokens too large; maximum completion length is 8192 tokens.", 8192),
            ("max_tokens must be <= 64", 64),
            ("max_tokens must be <= 1", 1),
        ):
            for status in (None, 400, 422):
                with self.subTest(message=message, status=status):
                    self.assertEqual(self.limit(message, status=status), expected)

    def test_numberless_backoff_has_floor(self):
        for requested, expected in ((200_000, 100_000), (1024, 512), (511, 256), (257, 256), (256, None), (1, None)):
            with self.subTest(requested=requested):
                self.assertEqual(self.limit("max_tokens is too large", requested), expected)

    def test_unrelated_errors_do_not_learn_output_limit(self):
        for message in (
            "maximum context length is 8192; max_tokens is too large",
            "max_tokens exceeds maximum context window of 8192",
            "max_tokens <= 8192 but input_tokens exceed the limit",
            "Unsupported parameter max_tokens; use max_completion_tokens <= 8192",
            "max_tokens is not supported; maximum is 8192",
            "unknown parameter max_output_tokens; maximum 8192",
            "max_tokens must be at least 8192",
            "max_tokens must be >= 8192",
            "max_tokens minimum 8192",
            "max_tokens invalid request",
            "temperature must be <= 1",
            "max_tokens not a valid parameter; maximum 8192",
        ):
            with self.subTest(message=message):
                self.assertIsNone(self.limit(message))

    def test_status_and_error_codes_are_gates(self):
        for code, status in (("authentication", 400), ("rate_limited", 422), ("invalid_request", 401), ("invalid_request", 429), ("invalid_request", 500)):
            with self.subTest(code=code, status=status):
                error = ProviderError(code, "max_tokens must be <= 8192", status=status)
                self.assertIsNone(next_output_token_limit(error, 200_000))

    def test_invalid_numeric_constraints_and_inputs(self):
        for suffix in ("0", "-1", "+8192", "1.5", "8e3", "8192abc", "9" * 100, "200000", "300000"):
            with self.subTest(suffix=suffix):
                self.assertIsNone(self.limit("max_tokens must be <= " + suffix))
        for requested in (0, -1, 1, True, 8192.5, "200000", None):
            with self.subTest(requested=requested):
                self.assertIsNone(self.limit("max_tokens must be <= 8192", requested))
        self.assertIsNone(self.limit("max_tokens must be in [9000, 8192]"))
        self.assertIsNone(self.limit("max_tokens is too large; maximum 300000"))

    def test_untrusted_prose_is_bounded_and_never_evaluated(self):
        self.assertIsNone(self.limit("max_tokens " + "x" * 8192 + " maximum 8192"))
        self.assertIsNone(self.limit("max_tokens " + "x" * 400 + " maximum 8192"))
        self.assertIsNone(self.limit("max_tokens <= __import__('os').system('whoami')"))
        self.assertIsNone(self.limit("max_tokens <= ${8192}"))


if __name__ == "__main__":
    unittest.main()
