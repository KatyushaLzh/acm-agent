from dataclasses import replace
import unittest
from unittest.mock import patch

from tools.acm_agent.provider import ProviderError
from tools.acm_agent.provider_conformance import run_live_conformance, verified_definition_from_report
from tests.test_provider_output_limits import provider, registry
from tests.test_provider_output_negotiation import LimitedClient


class RecoveryClient(LimitedClient):
    def __init__(self, failures, *, limit=200000):
        super().__init__(limit)
        self.failures = list(failures)

    def check(self, kind, options):
        if self.failures:
            self.calls.append((kind, options['max_tokens']))
            raise self.failures.pop(0)
        return super().check(kind, options)


def temporary_failure(code='server_error', *, status=503, retryable=True):
    return ProviderError(code, 'private-token raw upstream max_tokens message',
                         status=status, retryable=retryable,
                         usage={'provider_requests': 1, 'total_tokens': 2})


class ConformanceRecoveryTests(unittest.TestCase):
    def route(self, retries=2):
        reg, _ = registry(provider(limit=200000))
        route = reg.probe_route('test', 'new-model', profile_id='coaching')
        return reg, replace(route, budget={**route.budget, 'max_retries': retries})

    def test_one_503_recovers_and_preserves_exact_usage(self):
        _, route = self.route()
        client = RecoveryClient([temporary_failure()])
        report = run_live_conformance(client, route)
        self.assertTrue(report['passed'])
        self.assertEqual(report['transient_retries'], 1)
        self.assertEqual(report['usage']['provider_requests'], 4)
        self.assertEqual(report['usage']['total_tokens'], 11)
        self.assertEqual([limit for _, limit in client.calls], [200000] * 4)
        self.assertNotIn('output_limit_negotiation', report)

    def test_persistent_503_is_bounded_and_never_issues_evidence(self):
        reg, route = self.route(retries=100)
        client = RecoveryClient([temporary_failure() for _ in range(10)])
        report = run_live_conformance(client, route, required_capabilities=['text_chat'])
        self.assertFalse(report['passed'])
        self.assertEqual(report['transient_retries'], 2)
        self.assertEqual(len(client.calls), 3)
        self.assertEqual(report['usage']['provider_requests'], 3)
        self.assertEqual(report['usage']['total_tokens'], 6)
        case = report['cases'][0]
        self.assertEqual(case['error_http_status'], 503)
        self.assertIn('HTTP', case['error_hint'])
        self.assertNotIn('private-token', str(report))
        self.assertEqual(report['verified_capabilities'], [])
        self.assertNotIn('output_limit_negotiation', report)
        with self.assertRaises(ValueError):
            verified_definition_from_report('test', reg.providers['test'], 'new-model', report)

    def test_disabled_retries_and_nonretryable_errors_stay_single_attempt(self):
        for retries, error in (
            (0, temporary_failure()),
            (2, temporary_failure('authentication_failed', status=401, retryable=False)),
            (2, temporary_failure(retryable=False)),
        ):
            with self.subTest(retries=retries, code=error.code):
                _, route = self.route(retries)
                client = RecoveryClient([error])
                report = run_live_conformance(client, route, required_capabilities=['text_chat'])
                self.assertFalse(report['passed'])
                self.assertEqual(len(client.calls), 1)
                self.assertNotIn('transient_retries', report)

    def test_transient_and_output_limit_recovery_have_independent_counters(self):
        _, route = self.route()
        client = RecoveryClient([temporary_failure()], limit=8192)
        report = run_live_conformance(client, route)
        self.assertTrue(report['passed'])
        self.assertEqual(report['transient_retries'], 1)
        self.assertEqual(len(report['output_limit_negotiation']), 1)
        self.assertEqual(report['negotiated_max_output_tokens'], 8192)
        self.assertEqual(report['usage']['provider_requests'], 5)
        self.assertEqual(report['usage']['total_tokens'], 11)
        self.assertEqual(client.calls[:3], [('text', 200000), ('text', 200000), ('text', 8192)])

    def test_stream_failure_retry_includes_prior_successful_probe_usage(self):
        _, route = self.route()

        class OnceBrokenStream(LimitedClient):
            failed = False

            def check(self, kind, options):
                if kind == 'stream' and not self.failed:
                    self.failed = True
                    self.calls.append((kind, options['max_tokens']))
                    raise temporary_failure()
                return super().check(kind, options)

        client = OnceBrokenStream(200000)
        report = run_live_conformance(client, route)
        self.assertTrue(report['passed'])
        self.assertEqual(report['usage']['provider_requests'], 6)
        self.assertEqual(report['usage']['total_tokens'], 17)
        self.assertEqual(len(client.calls), 6)

    def test_shared_deadline_prevents_transient_retry_and_preserves_usage(self):
        _, route = self.route()
        now = [0.0]

        class ExpiredClient(RecoveryClient):
            def check(self, kind, options):
                now[0] = 1000.0
                return super().check(kind, options)

        client = ExpiredClient([temporary_failure()])
        with patch('tools.acm_agent.provider_conformance.time.monotonic', side_effect=lambda: now[0]):
            report = run_live_conformance(client, route)
        self.assertFalse(report['passed'])
        self.assertEqual(report['usage']['total_tokens'], 2)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.timeout, 120)

    def test_each_case_receives_only_remaining_shared_deadline(self):
        _, route = self.route()
        route = replace(route, budget={**route.budget, 'request_timeout_seconds': 100})
        now = [0.0]
        timeouts = []

        class SlowClient(LimitedClient):
            def check(self, kind, options):
                timeouts.append((kind, options.get('request_timeout', self.timeout)))
                now[0] += 20
                return super().check(kind, options)

        client = SlowClient(200000)
        with patch('tools.acm_agent.provider_conformance.time.monotonic', side_effect=lambda: now[0]):
            report = run_live_conformance(client, route)
        self.assertTrue(report['passed'])
        self.assertEqual(timeouts, [('text', 100), ('json', 80), ('stream', 60)])
        self.assertEqual(client.timeout, 120)

    def test_success_after_deadline_never_passes_or_starts_more_requests(self):
        _, route = self.route()
        route = replace(route, budget={**route.budget, 'request_timeout_seconds': 100})
        now = [0.0]

        class LateClient(LimitedClient):
            def check(self, kind, options):
                now[0] += 101
                return super().check(kind, options)

        client = LateClient(200000)
        with patch('tools.acm_agent.provider_conformance.time.monotonic', side_effect=lambda: now[0]):
            report = run_live_conformance(client, route)
        self.assertFalse(report['passed'])
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(report['usage']['provider_requests'], 1)
        self.assertEqual(report['usage']['total_tokens'], 3)
        self.assertEqual([case['error_code'] for case in report['cases']], ['timeout'] * 3)
        self.assertEqual(client.timeout, 120)

    def test_stream_completion_after_deadline_is_failure_and_restores_timeout(self):
        _, route = self.route()
        route = replace(route, budget={**route.budget, 'request_timeout_seconds': 100})
        now = [0.0]

        class LateStream(LimitedClient):
            def stream_chat(self, messages, **options):
                for event in super().stream_chat(messages, **options):
                    if event.kind == 'done':
                        now[0] = 101
                    yield event

        client = LateStream(200000)
        with patch('tools.acm_agent.provider_conformance.time.monotonic', side_effect=lambda: now[0]):
            report = run_live_conformance(client, route)
        self.assertFalse(report['passed'])
        self.assertEqual(report['cases'][-1]['error_code'], 'timeout')
        self.assertEqual(report['usage']['provider_requests'], 3)
        self.assertEqual(report['usage']['total_tokens'], 9)
        self.assertEqual(client.timeout, 120)

    def test_stream_diagnostics_distinguish_invalid_from_incomplete(self):
        _, route = self.route(0)
        for code, expected in [('invalid_stream', '无效'), ('incomplete_stream', '未完成')]:
            with self.subTest(code=code):
                client = RecoveryClient([temporary_failure(code, status=None, retryable=False)])
                report = run_live_conformance(client, route, required_capabilities=['text_chat'])
                case = report['cases'][0]
                self.assertIn(expected, case['error_hint'])
                self.assertNotIn('凭据', case['error_hint'])
                self.assertNotIn('error_http_status', case)


if __name__ == '__main__':
    unittest.main()
