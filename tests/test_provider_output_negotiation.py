from copy import deepcopy
from unittest.mock import patch
import unittest

from tools.acm_agent.provider import AIResult, AIJsonResult, AIStreamEvent, ProviderError
from tools.acm_agent.provider_conformance import run_live_conformance, verified_definition_from_report
from tools.acm_agent.provider_registry import ProviderRegistry
from tools.acm_agent.service_ai import ServiceAIMixin
from tests.test_provider_output_limits import provider, registry


class LimitedClient:
    def __init__(self, limit=8192, *, message=None, final_error=None, stream_limit=None):
        self.limit = limit
        self.message = message
        self.final_error = final_error
        self.stream_limit = stream_limit or limit
        self.calls = []
        self.timeout = 120

    def check(self, kind, options):
        self.calls.append((kind, options['max_tokens']))
        limit = self.stream_limit if kind == 'stream' else self.limit
        if options['max_tokens'] > limit:
            raise ProviderError(
                'invalid_request', self.message or f'max_tokens参数非法：限制数值范围[1,{limit}]',
                status=400, usage={'provider_requests': 1},
            )
        if self.final_error:
            raise self.final_error
        return {'provider_requests': 1, 'total_tokens': 3, 'input_tokens': 2, 'output_tokens': 1}

    def chat(self, messages, **options):
        usage = self.check('text', options)
        return AIResult('OK', 'stop', usage, options['model'])

    def chat_json(self, messages, **options):
        usage = self.check('json', options)
        return AIJsonResult('{"ok":true}', 'stop', usage, options['model'], {'ok': True})

    def stream_chat(self, messages, **options):
        usage = self.check('stream', options)
        yield AIStreamEvent('delta', content='OK')
        yield AIStreamEvent('done', finish_reason='stop', usage=usage)


class OutputNegotiationTests(unittest.TestCase):
    def test_unknown_model_learns_limit_and_production_reuses_persisted_evidence(self):
        reg, ai = registry(provider(model='brand-new-model'))
        route = reg.probe_route('test', 'brand-new-model', profile_id='coaching')
        client = LimitedClient(6144)
        report = run_live_conformance(client, route)
        self.assertTrue(report['passed'])
        self.assertEqual(client.calls, [('text', 16384), ('text', 6144), ('json', 6144), ('stream', 6144)])
        self.assertEqual(report['usage']['provider_requests'], 4)
        self.assertEqual(report['usage']['total_tokens'], 9)
        self.assertEqual(client.timeout, 120)
        definition = verified_definition_from_report('test', reg.providers['test'], route.model, report)
        self.assertEqual(definition['capabilities']['max_output_tokens'], 6144)
        ai['providers']['test']['models'][route.model] = definition
        saved = ProviderRegistry(ai)
        production = saved.route('coaching', model_ref={'provider_id': 'test', 'model': route.model}, reasoning_strength='auto')
        self.assertEqual(production.budget['max_output_tokens'], 6144)
        self.assertEqual(saved.policy['budgets']['coaching']['max_output_tokens'], 200000)
        again = LimitedClient(6144)
        second = run_live_conformance(again, saved.probe_route('test', route.model, profile_id='coaching'))
        self.assertTrue(second['passed'])
        self.assertEqual(len(again.calls), 3)
        self.assertNotIn('output_limit_negotiation', second)

    def test_unknown_model_uses_verified_working_budget_not_inherited_large_budget(self):
        reg, ai = registry(provider(model='never-seen-before'))
        route = reg.probe_route('test', 'never-seen-before', profile_id='coaching')
        client = LimitedClient(32000)
        report = run_live_conformance(client, route)
        self.assertTrue(report['passed'])
        self.assertEqual(client.calls, [('text', 16384), ('json', 16384), ('stream', 16384)])
        self.assertNotIn('output_limit_negotiation', report)
        self.assertEqual(report['verified_max_output_tokens'], 16384)
        definition = verified_definition_from_report('test', reg.providers['test'], route.model, report)
        self.assertEqual(definition['capabilities']['max_output_tokens'], 16384)
        self.assertEqual(ai['policy']['budgets']['coaching']['max_output_tokens'], 200000)

    def test_stream_stricter_than_text_retests_all_cases_at_final_limit(self):
        reg, _ = registry(provider())
        client = LimitedClient(32768, stream_limit=8192)
        report = run_live_conformance(client, reg.probe_route('test', 'new-model', profile_id='coaching'))
        self.assertTrue(report['passed'])
        self.assertEqual(client.calls[-3:], [('text', 8192), ('json', 8192), ('stream', 8192)])
        self.assertEqual(report['negotiated_max_output_tokens'], 8192)
        self.assertEqual(report['usage']['provider_requests'], len(client.calls))

    def test_numberless_explicit_limit_backoff_is_bounded_and_verified(self):
        reg, _ = registry(provider())
        client = LimitedClient(4000, message='max_tokens is too large')
        report = run_live_conformance(client, reg.probe_route('test', 'new-model', profile_id='coaching'))
        self.assertTrue(report['passed'])
        self.assertLessEqual(report['negotiated_max_output_tokens'], 4000)
        self.assertLessEqual(len(report['output_limit_negotiation']), 10)
        self.assertEqual(report['usage']['provider_requests'], len(client.calls))

    def test_failure_after_negotiation_never_issues_limit_or_capability_evidence(self):
        reg, _ = registry(provider())
        client = LimitedClient(8192, final_error=ProviderError('authentication_failed', 'secret', status=401))
        report = run_live_conformance(client, reg.probe_route('test', 'new-model', profile_id='coaching'))
        self.assertFalse(report['passed'])
        self.assertNotIn('negotiated_max_output_tokens', report)
        self.assertNotIn('secret', str(report))
        with self.assertRaises(ValueError):
            verified_definition_from_report('test', reg.providers['test'], 'new-model', report)

    def test_unrelated_bad_requests_are_not_negotiated(self):
        reg, _ = registry(provider())
        for message in ('max_tokens unsupported parameter', 'max_tokens and prompt exceed context length', 'temperature invalid'):
            with self.subTest(message=message):
                client = LimitedClient(1, message=message)
                report = run_live_conformance(client, reg.probe_route('test', 'new-model', profile_id='coaching'))
                self.assertFalse(report['passed'])
                self.assertNotIn('output_limit_negotiation', report)
                self.assertEqual(len(client.calls), 3)

    def test_recovery_exhaustion_does_not_turn_failure_into_success(self):
        reg, _ = registry(provider())
        client = LimitedClient(1, message='max_tokens is too large')
        report = run_live_conformance(client, reg.probe_route('test', 'new-model', profile_id='coaching'))
        self.assertFalse(report['passed'])
        self.assertNotIn('negotiated_max_output_tokens', report)
        self.assertLessEqual(len(report['output_limit_negotiation']), 10)
        self.assertEqual(report['usage']['provider_requests'], len(client.calls))

    def test_negotiation_stops_at_shared_deadline(self):
        reg, _ = registry(provider())
        route = reg.probe_route('test', 'new-model', profile_id='coaching')
        client = LimitedClient()
        clock = [0]
        original_check = client.check
        def slow_check(kind, options):
            clock[0] = 1000
            return original_check(kind, options)
        client.check = slow_check
        with patch('tools.acm_agent.provider_conformance.time.monotonic', side_effect=lambda: clock[0]):
            with self.assertRaises(ProviderError) as caught:
                run_live_conformance(client, route)
        self.assertEqual(caught.exception.code, 'timeout')
        self.assertEqual(caught.exception.usage['provider_requests'], 1)
        self.assertEqual(len(client.calls), 1)
        self.assertEqual(client.timeout, 120)

    def test_stale_or_forged_report_cannot_change_caps(self):
        reg, _ = registry(provider())
        report = run_live_conformance(LimitedClient(), reg.probe_route('test', 'new-model', profile_id='coaching'))
        changed = deepcopy(reg.providers['test'])
        changed['base_url'] = 'https://other.example/v1'
        with self.assertRaises(ValueError):
            verified_definition_from_report('test', changed, 'new-model', report)
        with self.assertRaises(ValueError):
            verified_definition_from_report('test', reg.providers['test'], 'new-model', dict(report))

    def test_discovery_refresh_preserves_limit_but_changed_endpoint_discards_it(self):
        reg, _ = registry(provider())
        report = run_live_conformance(LimitedClient(), reg.probe_route('test', 'new-model', profile_id='coaching'))
        definition = verified_definition_from_report('test', reg.providers['test'], 'new-model', report)
        for preserve in (True, False):
            result = ServiceAIMixin._discovered_model_catalog(
                {'new-model': definition}, ['new-model'], preserve_evidence=preserve,
            )
            self.assertEqual(result['new-model']['capabilities'].get('max_output_tokens'), 8192 if preserve else None)


if __name__ == '__main__':
    unittest.main()
