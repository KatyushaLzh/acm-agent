from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.acm_agent.config import load_config, save_config
from tools.acm_agent.credentials import ProviderCredentialVault
from tools.acm_agent.provider import ProviderConfigurationError
from tools.acm_agent.provider_registry import provider_definition_hash
from tools.acm_agent.service import AcmService


class ResponsesConnectionTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        self.vault = ProviderCredentialVault(
            root / '.acm' / 'credentials',
            protect=lambda value: b'P' + value,
            unprotect=lambda value: value[1:],
        )
        self.service = AcmService(root, credential_vault=self.vault)
        self.service.setup('fixture', '42', skip_validate=True)

    def create(self, **options):
        with patch('tools.acm_agent.service_ai.discover_models', return_value=['m']):
            result = self.service.ai_connection_upsert(
                display_name='Relay', base_url='https://relay.example/v1',
                api_key='fixture-secret', **options,
            )
        return result['connection_id']

    def test_new_protocol_default_and_explicit_responses_projection(self):
        chat = self.create()
        responses = self.create(adapter='openai_responses')
        rows = {row['id']: row for row in self.service.ai_connections()['connections']}
        self.assertEqual(rows[chat]['adapter'], 'auto')
        self.assertEqual(rows[responses]['adapter'], 'openai_responses')
        self.assertEqual(rows[responses]['base_url'], 'https://relay.example/v1')
        self.assertNotIn('fixture-secret', str(rows))
        from tools.acm_agent.openai_responses import OpenAIResponsesClient
        from tools.acm_agent.openai_compatible import OpenAICompatibleClient
        registry = self.service._provider_registry()
        client = registry.client_for_route(registry.probe_route(responses, 'm'))
        self.assertIsInstance(client, OpenAIResponsesClient)
        self.assertEqual(client.endpoint, 'https://relay.example/v1/responses')
        with self.assertRaisesRegex(ProviderConfigurationError, '协议尚未验证'):
            registry.client_for_route(registry.probe_route(chat, 'm'))

    def test_edit_without_adapter_retains_protocol_and_key(self):
        selected = self.create(adapter='openai_responses')
        with patch('tools.acm_agent.service_ai.discover_models', return_value=['m']):
            self.service.ai_connection_upsert(
                connection_id=selected, display_name='Renamed',
                base_url='https://relay.example/v1', api_key='',
            )
        provider = load_config(self.service.paths)['ai']['providers'][selected]
        self.assertEqual(provider['adapter'], 'openai_responses')
        self.assertEqual(self.vault.load(selected).secret, 'fixture-secret')

    def test_switching_protocol_invalidates_all_model_evidence(self):
        selected = self.create()
        config = load_config(self.service.paths)
        model = config['ai']['providers'][selected]['models']['m']
        old_hash = provider_definition_hash(selected, config['ai']['providers'][selected], 'm')
        model.update(evidence='verified_live', evidence_hash=old_hash,
                     verified_at='2026-09-16T00:00:00Z',
                     verified_capabilities=['text_chat', 'streaming'],
                     verified_reasoning_strengths=['off'])
        config['ai']['providers'][selected]['models']['removed'] = dict(model)
        save_config(self.service.paths, config)
        with patch('tools.acm_agent.service_ai.discover_models', return_value=['m']):
            self.service.ai_connection_upsert(
                connection_id=selected, display_name='Relay', base_url='https://relay.example/v1',
                api_key=None, adapter='openai_responses',
            )
        provider = load_config(self.service.paths)['ai']['providers'][selected]
        self.assertEqual(provider['adapter'], 'openai_responses')
        self.assertNotEqual(provider_definition_hash(selected, provider, 'm'), old_hash)
        with self.assertRaises(ProviderConfigurationError):
            self.service._provider_registry().route(
                'coaching', model_ref={'provider_id': selected, 'model': 'm'},
                reasoning_strength='off',
            )
        for definition in provider['models'].values():
            self.assertEqual(definition['evidence'], 'declared')
            self.assertIsNone(definition['evidence_hash'])
            self.assertEqual(definition['verified_capabilities'], [])
        self.assertNotIn('removed', provider['models'])

    def test_responses_discovery_root_404_fallback(self):
        with patch('tools.acm_agent.service_ai.discover_models', side_effect=[
            ProviderConfigurationError('model_discovery_failed', 'missing', status=404), ['m'],
        ]) as discover:
            result = self.service.ai_connection_upsert(
                display_name='Relay', base_url='https://relay.example',
                api_key='fixture-secret', adapter='openai_responses',
            )
        self.assertEqual([call.args[0] for call in discover.call_args_list],
                         ['https://relay.example', 'https://relay.example/v1'])
        provider = load_config(self.service.paths)['ai']['providers'][result['connection_id']]
        self.assertEqual(provider['base_url'], 'https://relay.example/v1')

    def test_invalid_protocol_and_builtin_switch_fail_before_discovery(self):
        before = self.service.paths.config.read_bytes()
        for selected, adapter, base in [
            (None, 'unknown', 'https://relay.example/v1'),
            ('deepseek', 'openai_responses', 'https://api.deepseek.com'),
        ]:
            with self.subTest(adapter=adapter):
                with patch('tools.acm_agent.service_ai.discover_models') as discover:
                    with self.assertRaises(ProviderConfigurationError):
                        self.service.ai_connection_upsert(
                            display_name='Relay', base_url=base, api_key='fixture-secret',
                            connection_id=selected, adapter=adapter,
                        )
                    discover.assert_not_called()
                self.assertEqual(self.service.paths.config.read_bytes(), before)


if __name__ == '__main__':
    unittest.main()
