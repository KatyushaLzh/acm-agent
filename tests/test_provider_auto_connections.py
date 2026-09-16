from __future__ import annotations
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch
from tools.acm_agent.provider_detection import DetectionBudget, discover_models, normalize_endpoint, protocol_candidates
from tools.acm_agent.provider import ProviderError, ProviderConfigurationError
from tools.acm_agent.config import load_config
from tools.acm_agent.credentials import ProviderCredentialVault
from tools.acm_agent.service import AcmService
from tests.test_stage2_provider import QueueTransport, Response


class AutoConnectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.vault = ProviderCredentialVault(root / '.acm' / 'credentials', protect=lambda v: b'P'+v, unprotect=lambda v: v[1:])
        self.service = AcmService(root, credential_vault=self.vault)
        self.service.setup('fixture', '42', skip_validate=True)

    def test_normalize_full_endpoint_and_custom_prefix(self):
        self.assertEqual(normalize_endpoint('https://relay.example/custom/v1/messages'), ('https://relay.example/custom/v1', 'anthropic'))
        self.assertEqual(protocol_candidates('https://relay.example/custom', 'auto'), [('openai_compatible', 'https://relay.example/custom'), ('openai_responses', 'https://relay.example/custom'), ('anthropic', 'https://relay.example/custom')])

    def test_manual_model_without_models_api(self):
        with patch('tools.acm_agent.service_ai.discover_models', side_effect=ProviderConfigurationError('model_discovery_failed', 'missing', status=404)):
            result = self.service.ai_connection_upsert(display_name='Relay', base_url='https://relay.example/v1', api_key='test', manual_models=['custom'])
        provider = load_config(self.service.paths)['ai']['providers'][result['connection_id']]
        self.assertEqual(provider['state'], 'needs_verification')
        self.assertEqual(provider['models']['custom']['source'], 'manual')
        self.assertEqual(provider['adapter'], 'auto')

    def test_missing_models_saved_pending(self):
        with patch('tools.acm_agent.service_ai.discover_models', side_effect=ProviderConfigurationError('invalid_models_response', 'missing')):
            result = self.service.ai_connection_upsert(display_name='Relay', base_url='https://relay.example/v1', api_key='test')
        self.assertEqual(result['state'], 'needs_model')
        self.assertEqual(result['required_input'], ['model'])

    def test_authentication_failure_preserves_config(self):
        before = self.service.paths.config.read_bytes()
        with patch('tools.acm_agent.service_ai.discover_models', side_effect=ProviderConfigurationError('model_discovery_failed', 'denied', status=401)):
            with self.assertRaises(ProviderConfigurationError):
                self.service.ai_connection_upsert(display_name='Relay', base_url='https://relay.example/v1', api_key='test')
        self.assertEqual(self.service.paths.config.read_bytes(), before)

    def test_refresh_preserves_manual_models(self):
        with patch('tools.acm_agent.service_ai.discover_models', return_value=['discovered']):
            result = self.service.ai_connection_upsert(display_name='Relay', base_url='https://relay.example/v1', api_key='test', manual_models=['custom'])
        with patch('tools.acm_agent.service_ai.discover_models', return_value=['new']):
            self.service.ai_connection_refresh(connection_id=result['connection_id'])
        models = load_config(self.service.paths)['ai']['providers'][result['connection_id']]['models']
        self.assertTrue(models['custom']['available'])
        self.assertNotIn('discovered', models)
        self.assertIn('new', models)

    def test_refresh_removes_flash_and_keeps_profiles_readable(self):
        with patch('tools.acm_agent.service_ai.discover_models', return_value=['deepseek-v4-flash', 'deepseek-v4-pro']):
            self.service.ai_connection_upsert(connection_id='deepseek', display_name='DeepSeek', base_url='https://api.deepseek.com', api_key='test')
        before = load_config(self.service.paths)['ai']['profiles']
        with patch('tools.acm_agent.service_ai.discover_models', return_value=['deepseek-v4-pro']):
            self.service.ai_connection_refresh(connection_id='deepseek')
        config = load_config(self.service.paths)
        self.assertNotIn('deepseek-v4-flash', config['ai']['providers']['deepseek']['models'])
        self.assertEqual(config['ai']['profiles'], before)
        with self.assertRaises(ProviderConfigurationError) as caught:
            self.service._provider_registry().route('coaching', model_ref={'provider_id': 'deepseek', 'model': 'deepseek-v4-flash'})
        self.assertEqual(caught.exception.code, 'invalid_model')

    def test_failed_refresh_does_not_delete_models(self):
        with patch('tools.acm_agent.service_ai.discover_models', return_value=['deepseek-v4-flash']):
            created = self.service.ai_connection_upsert(display_name='Relay', base_url='https://relay.example/v1', api_key='test')
        before = self.service.paths.config.read_bytes()
        with patch('tools.acm_agent.service_ai.discover_models', side_effect=ProviderConfigurationError('invalid_models_response', 'bad directory')):
            with self.assertRaises(ProviderConfigurationError):
                self.service.ai_connection_refresh(connection_id=created['connection_id'])
        self.assertEqual(self.service.paths.config.read_bytes(), before)

    def test_empty_successful_directory_removes_discovered_models(self):
        with patch('tools.acm_agent.service_ai.discover_models', return_value=['m']):
            created = self.service.ai_connection_upsert(display_name='Relay', base_url='https://relay.example/v1', api_key='test')
        with patch('tools.acm_agent.service_ai.discover_models', return_value=[]):
            result = self.service.ai_connection_refresh(connection_id=created['connection_id'])
        self.assertEqual(result['state'], 'needs_model')
        self.assertEqual(load_config(self.service.paths)['ai']['providers'][created['connection_id']]['models'], {})

    def test_discovery_pagination_auth_and_budget(self):
        transport = QueueTransport(Response(b'{"data":[{"id":"a"}],"has_more":true,"last_id":"a"}', url='https://relay.example/v1/models'), Response(b'{"data":[{"id":"b"}],"has_more":false}', url='https://relay.example/v1/models?after_id=a'))
        budget = DetectionBudget(requests=2)
        with patch('tools.acm_agent.openai_compatible._safe_https_open', transport):
            self.assertEqual(discover_models('https://relay.example/v1', 'test', adapter='anthropic', budget=budget), ['a','b'])
        self.assertEqual(budget.requests, 2)
        self.assertEqual(transport.requests[0].get_header('X-api-key'), 'test')
        with self.assertRaises(ProviderError):
            budget.transport(transport)(transport.requests[0], 1)

    def test_single_model_detection_verifies_and_persists_wire(self):
        import json
        import io
        def reply(content):
            return Response(json.dumps({'model': 'm', 'choices': [{'message': {'content': content}, 'finish_reason': 'stop'}], 'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2}}).encode())
        transport = QueueTransport(Response(b'{"data":[{"id":"m"}]}', url='https://relay.example/v1/models'), urllib.error.HTTPError('https://relay.example/v1/chat/completions', 400, 'bad request', {}, io.BytesIO(b'{"error":{"message":"Unsupported parameter: temperature"}}')), reply('OK'), reply('{"ok": true}'), Response(b'data: {"model":"m","choices":[{"delta":{"content":"OK"},"finish_reason":null}]}\n\ndata: {"choices":[{"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":1,"completion_tokens":1,"total_tokens":2}}\n\ndata: [DONE]\n\n'))
        with patch('tools.acm_agent.openai_compatible._safe_https_open', transport):
            result = self.service.ai_connection_detect(display_name='Relay', base_url='https://relay.example/v1', api_key='test')
        self.assertTrue(result['verification']['ok'])
        self.assertEqual(result['state'], 'ready')
        self.assertEqual(result['requests'], 5)
        provider = load_config(self.service.paths)['ai']['providers'][result['connection_id']]
        self.assertEqual(provider['models']['m']['wire_profile']['adapter'], 'openai_compatible')
        self.service._provider_registry().route('recommendation', model_ref={'provider_id': result['connection_id'], 'model': 'm'}, reasoning_strength='auto')

    def test_edit_detection_failure_restores_configuration_and_key(self):
        for failure in (False, ProviderError('budget_exceeded', 'budget exhausted', retryable=True)):
            with self.subTest(failure=type(failure).__name__):
                with patch('tools.acm_agent.service_ai.discover_models', return_value=['m']):
                    created = self.service.ai_connection_upsert(display_name='Original', base_url='https://relay.example/v1', api_key='original-key')
                selected = created['connection_id']
                before = self.service.paths.config.read_bytes()
                with patch('tools.acm_agent.service_ai.discover_models', return_value=['m']), patch.object(self.service, '_verify_connection_model', side_effect=failure if isinstance(failure, Exception) else None, return_value={'ok': False}):
                    arguments = dict(connection_id=selected, display_name='Changed', base_url='https://other.example/v1', api_key='replacement-key')
                    if isinstance(failure, Exception):
                        with self.assertRaises(ProviderError):
                            self.service.ai_connection_detect(**arguments)
                    else:
                        result = self.service.ai_connection_detect(**arguments)
                        self.assertFalse(result['ok'])
                        self.assertTrue(result['rolled_back'])
                self.assertEqual(self.service.paths.config.read_bytes(), before)
                self.assertEqual(self.vault.load(selected).secret, 'original-key')
                self.assertEqual(self.vault.load(selected).origin, 'https://relay.example')

    def test_header_or_auth_mode_change_invalidates_model_evidence(self):
        with patch('tools.acm_agent.service_ai.discover_models', return_value=['m']):
            result = self.service.ai_connection_upsert(display_name='Relay', base_url='https://relay.example/v1', api_key='test')
        selected = result['connection_id']
        from tools.acm_agent.config import save_config
        config = load_config(self.service.paths)
        config['ai']['providers'][selected]['models']['m'].update(evidence='verified_live', evidence_hash='old-hash', verified_capabilities=['text_chat'])
        save_config(self.service.paths, config)
        with patch('tools.acm_agent.service_ai.discover_models', return_value=['m']):
            self.service.ai_connection_upsert(connection_id=selected, display_name='Relay', base_url='https://relay.example/v1', api_key=None, auth={'type': 'bearer'}, headers={'anthropic-version': '2023-06-01'})
        model = load_config(self.service.paths)['ai']['providers'][selected]['models']['m']
        self.assertEqual(model['evidence'], 'declared')
        self.assertIsNone(model['evidence_hash'])

    def test_auto_discovers_alternate_auth_on_same_address(self):
        import json
        document = {'data': [{'id': 'm', 'capabilities': {'thinking': {'supported': True, 'types': {'adaptive': {'supported': True}, 'enabled': {'supported': True}}}}}]}
        transport = QueueTransport(urllib.error.HTTPError('https://unknown.example/v1/models', 401, 'unauthorized', {}, None), Response(json.dumps(document).encode(), url='https://unknown.example/v1/models'))
        with patch('tools.acm_agent.openai_compatible._safe_https_open', transport):
            result = self.service.ai_connection_upsert(display_name='Anthropic relay', base_url='https://unknown.example/v1', api_key='test')
        self.assertEqual([request.full_url for request in transport.requests], ['https://unknown.example/v1/models'] * 2)
        self.assertEqual(transport.requests[0].get_header('Authorization'), 'Bearer test')
        self.assertEqual(transport.requests[1].get_header('X-api-key'), 'test')
        model = load_config(self.service.paths)['ai']['providers'][result['connection_id']]['models']['m']
        self.assertEqual(model['wire_profile']['adapter'], 'anthropic')
        self.assertEqual(model['wire_profile']['auth'], {'type': 'header', 'header': 'x-api-key'})
        self.assertEqual(model['wire_profile']['reasoning_mode'], 'adaptive')

    def test_auto_auth_both_unauthorized_and_explicit_auth_stop(self):
        for auth, expected_calls in ((None, 2), ({'type': 'bearer'}, 1)):
            with self.subTest(auth=auth):
                before = self.service.paths.config.read_bytes()
                with patch('tools.acm_agent.service_ai.discover_models', side_effect=ProviderConfigurationError('model_discovery_failed', 'unauthorized', status=401)) as discover:
                    with self.assertRaises(ProviderConfigurationError):
                        self.service.ai_connection_upsert(display_name='Relay', base_url='https://relay.example/v1', api_key='bad', auth=auth)
                self.assertEqual(discover.call_count, expected_calls)
                self.assertEqual(self.service.paths.config.read_bytes(), before)

    def test_verification_does_not_overwrite_concurrent_model_edit(self):
        from tools.acm_agent.config import save_config
        with patch('tools.acm_agent.service_ai.discover_models', return_value=['m']):
            created = self.service.ai_connection_upsert(display_name='Relay', base_url='https://relay.example/v1', api_key='test')
        selected = created['connection_id']
        def concurrent_edit(client, route, **kwargs):
            config = load_config(self.service.paths)
            config['ai']['providers'][selected]['models']['m']['wire_profile'] = {'adapter': 'anthropic', 'base_url': 'https://relay.example/changed'}
            save_config(self.service.paths, config)
            return {'passed': True}
        with patch('tools.acm_agent.service_ai.run_live_conformance', side_effect=concurrent_edit):
            with self.assertRaisesRegex(ProviderConfigurationError, '验证期间已改变'):
                self.service.ai_model_verify(profile_id='recommendation', model_ref={'provider_id': selected, 'model': 'm'}, reasoning_strength='auto')
        model = load_config(self.service.paths)['ai']['providers'][selected]['models']['m']
        self.assertEqual(model['wire_profile']['base_url'], 'https://relay.example/changed')
        self.assertEqual(model['evidence'], 'declared')

    def test_display_name_is_required(self):
        with self.assertRaisesRegex(ProviderConfigurationError, '显示名称'):
            self.service.ai_connection_upsert(display_name='', base_url='https://relay.example', api_key='test')

if __name__ == '__main__':
    unittest.main()
