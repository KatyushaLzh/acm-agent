from __future__ import annotations

import io
import json
import unittest
import urllib.error
from copy import deepcopy

from tools.acm_agent.openai_compatible import OpenAICompatibleClient
from tools.acm_agent.provider import ProviderError
from tools.acm_agent.provider_config import capability_profile, TASK_PROFILE_IDS
from tools.acm_agent.provider_conformance import run_live_conformance, verified_definition_from_report
from tools.acm_agent.provider_registry import ProviderRegistry, provider_definition_hash
from tools.acm_agent.provider_governance import GovernedProviderClient
from tests.test_provider_output_limits import provider, registry


class CompatibilityTests(unittest.TestCase):
    def setup_client(self, rejects=(), *, usage=False, budget=12, initial_wire=None):
        definition = provider(limit=16384)
        definition['models']['new-model']['wire_profile'] = initial_wire or {}
        reg, ai = registry(definition)
        for task in TASK_PROFILE_IDS:
            ai['policy']['budgets'][task]['max_requests'] = budget
        reg = ProviderRegistry(ai)
        requests = []
        def transport(request, timeout):
            payload = json.loads(request.data)
            requests.append(payload)
            for field in rejects:
                if field in payload and (field != 'stream' or payload[field]):
                    message = 'unsupported parameter ' + field
                    raise urllib.error.HTTPError(request.full_url, 400, 'bad', {},
                        io.BytesIO(json.dumps({'error': {'message': message}}).encode()))
            text = '{"ok":true}' if any('json' in m['content'].lower() for m in payload['messages']) else 'OK'
            counts = {'usage': {'prompt_tokens': 2, 'completion_tokens': 1, 'total_tokens': 3}} if usage else {}
            if payload.get('stream'):
                data = {'choices': [{'delta': {'content': text}, 'finish_reason': 'stop'}], **counts}
                return io.BytesIO(('data: '+json.dumps(data)+'\n\ndata: [DONE]\n\n').encode())
            return io.BytesIO(json.dumps({'model': 'new-model', 'choices': [
                {'message': {'content': text}, 'finish_reason': 'stop'}], **counts}).encode())
        client = OpenAICompatibleClient('test-key', provider_id='test', base_url=definition['base_url'],
            auth=definition['auth'], models={'new-model': capability_profile(reg.providers['test'], 'new-model')},
            wire_profile=initial_wire, transport=transport, retries=0)
        return reg, ai, client, requests

    def test_parameter_negotiation_persists_effective_not_native_capabilities(self):
        reg, ai, client, calls = self.setup_client(('temperature', 'max_tokens', 'response_format', 'stream_options', 'stream'))
        route = reg.probe_route('test', 'new-model')
        report = run_live_conformance(client, route)
        self.assertTrue(report['passed'], report)
        self.assertLessEqual(len(calls), 12)
        self.assertEqual(report['usage']['provider_requests'], len(calls))
        self.assertNotIn('total_tokens', report['usage'])
        saved = verified_definition_from_report('test', reg.providers['test'], 'new-model', report)
        self.assertFalse(saved['capabilities']['json_object'])
        self.assertFalse(saved['capabilities']['streaming'])
        self.assertTrue(saved['effective_capabilities']['json_object'])
        self.assertTrue(saved['effective_capabilities']['streaming'])
        self.assertFalse(saved['effective_capabilities']['usage'])
        ai['providers']['test']['models']['new-model'] = saved
        production = ProviderRegistry(ai)
        for task in TASK_PROFILE_IDS:
            production.route(task, model_ref={'provider_id': 'test', 'model': 'new-model'}, reasoning_strength='auto')
        self.assertEqual(saved['wire_profile']['streaming'], 'buffered')
        self.assertEqual(saved['wire_profile']['token_parameter'], 'max_completion_tokens')
        first_hash = provider_definition_hash('test', production.providers['test'], 'new-model')
        changed = deepcopy(production.providers['test'])
        changed['models']['new-model']['wire_profile']['structured_output'] = 'json_object'
        self.assertNotEqual(first_hash, provider_definition_hash('test', changed, 'new-model'))

    def test_probe_budget_bounds_real_http_attempts(self):
        reg, ai, client, calls = self.setup_client(('response_format',), budget=2)
        report = run_live_conformance(client, reg.probe_route('test', 'new-model'))
        self.assertFalse(report['passed'])
        self.assertEqual(len(calls), 2)

    def test_buffered_governed_stream_finishes_once(self):
        reg, ai, client, calls = self.setup_client(initial_wire={'streaming': 'buffered'})
        route = reg.probe_route('test', 'new-model', profile_id='coaching')
        governed = GovernedProviderClient([route], lambda *args: client)
        events = list(governed.stream_chat([{'role': 'user', 'content': 'hello'}], model='new-model'))
        self.assertEqual(sum(e.kind == 'done' for e in events), 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(governed.governance_snapshot['usage_completeness'], 'unknown')

    def test_schema_then_json_then_prompt_negotiation(self):
        reg, ai, client, calls = self.setup_client(('response_format',), initial_wire={'structured_output': 'native_schema'})
        report = run_live_conformance(client, reg.probe_route('test', 'new-model'))
        self.assertTrue(report['passed'], report)
        self.assertEqual([item['structured_output'] for item in report['compatibility_changes']], ['json_object', 'prompt_json'])

    def test_observed_token_budget_stops_followup_probe(self):
        reg, ai, client, calls = self.setup_client(usage=True)
        route = reg.probe_route('test', 'new-model')
        from dataclasses import replace
        route = replace(route, budget={**route.budget, 'max_total_tokens': 3})
        report = run_live_conformance(client, route)
        self.assertFalse(report['passed'])
        self.assertEqual(len(calls), 1)

    def test_version18_migration_preserves_routes_and_invalidates_live_evidence(self):
        from tools.acm_agent.config import DEFAULT_CONFIG, _upgrade_config
        source = deepcopy(DEFAULT_CONFIG)
        source['version'] = 17
        reg, ai, client, calls = self.setup_client()
        report = run_live_conformance(client, reg.probe_route('test', 'new-model'))
        ai['providers']['test']['models']['new-model'] = verified_definition_from_report('test', reg.providers['test'], 'new-model', report)
        source['ai'].update(ai)
        original_profiles = deepcopy(source['ai']['profiles'])
        original_slots = deepcopy(source['ai']['credential_slots'])
        changed, _ = _upgrade_config(source)
        self.assertEqual(changed['version'], 18)
        self.assertEqual(changed['ai']['profiles'], original_profiles)
        self.assertEqual(changed['ai']['credential_slots'], original_slots)
        self.assertEqual(changed['ai']['providers']['test']['models']['new-model']['evidence'], 'declared')
        self.assertEqual(source['version'], 17)

    def test_mixed_usage_keeps_partial_state(self):
        reg, ai, client, calls = self.setup_client()
        from tools.acm_agent.provider import AIResult
        from unittest.mock import Mock
        route = reg.probe_route('test', 'new-model')
        mocked = Mock()
        mocked.chat.side_effect = [AIResult('OK','stop',{},'new-model'),
                                  AIResult('OK','stop',{'total_tokens':3},'new-model')]
        governed = GovernedProviderClient([route], lambda *args: mocked)
        governed.chat([{'role':'user','content':'hello'}])
        governed.chat([{'role':'user','content':'hello'}])
        self.assertEqual(governed.governance_snapshot['usage_completeness'], 'partial')
        self.assertFalse(governed.governance_snapshot['total_token_budget_complete'])


if __name__ == '__main__':
    unittest.main()
