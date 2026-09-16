from __future__ import annotations

import json
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from tools.acm_agent.config import load_config
from tools.acm_agent.credentials import ProviderCredentialVault
from tools.acm_agent.openai_compatible import discover_openai_compatible_models
from tools.acm_agent.provider import ProviderConfigurationError
from tools.acm_agent.service import AcmService
from tests.test_stage2_provider import QueueTransport, Response


class ConnectionEndpointTests(unittest.TestCase):
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

    def create(self, base='https://relay.example'):
        return self.service.ai_connection_upsert(
            display_name='Relay', base_url=base, api_key='fixture-secret', adapter='openai_compatible', auth={'type': 'bearer'}
        )['connection_id']

    def test_root_fallback_persists_prefix_for_actual_chat(self):
        transport = QueueTransport(
            urllib.error.HTTPError('https://relay.example/models', 404, 'missing', {}, None),
            Response(b'{"data":[{"id":"relay-model"}]}', url='https://relay.example/v1/models'),
            Response(json.dumps({
                'model': 'relay-model',
                'choices': [{'message': {'content': 'OK'}, 'finish_reason': 'stop'}],
                'usage': {'prompt_tokens': 1, 'completion_tokens': 1, 'total_tokens': 2},
            }).encode()),
        )
        with patch('tools.acm_agent.openai_compatible._safe_https_open', transport):
            selected = self.create()
            registry = self.service._provider_registry()
            route = registry.probe_route(selected, 'relay-model')
            result = registry.client_for_route(route).chat(
                [{'role': 'user', 'content': 'OK'}], model='relay-model', thinking=False
            )
        self.assertEqual(result.content, 'OK')
        self.assertEqual([r.full_url for r in transport.requests], [
            'https://relay.example/models', 'https://relay.example/v1/models',
            'https://relay.example/v1/chat/completions',
        ])
        config = load_config(self.service.paths)
        self.assertEqual(config['ai']['providers'][selected]['base_url'], 'https://relay.example/v1')
        self.assertEqual(config['ai']['credential_slots'][selected]['origin'], 'https://relay.example')

    def test_successful_root_is_preserved(self):
        with patch('tools.acm_agent.service_ai.discover_models', return_value=['m']) as discover:
            selected = self.create()
        self.assertEqual(discover.call_count, 1)
        self.assertEqual(load_config(self.service.paths)['ai']['providers'][selected]['base_url'], 'https://relay.example')

    def test_custom_anthropic_prefix_is_accepted(self):
        with patch('tools.acm_agent.service_ai.discover_models', return_value=['m']):
            selected = self.create('https://open.bigmodel.cn/api/anthropic')
        self.assertEqual(load_config(self.service.paths)['ai']['providers'][selected]['base_url'], 'https://open.bigmodel.cn/api/anthropic')

    def test_nonrecoverable_discovery_errors_do_not_probe(self):
        for status in (401, 429, 500):
            with self.subTest(status=status):
                before = self.service.paths.config.read_bytes()
                with patch('tools.acm_agent.service_ai.discover_models', side_effect=ProviderConfigurationError('model_discovery_failed', 'failed', status=status)) as discover:
                    with self.assertRaises(ProviderConfigurationError):
                        self.create()
                self.assertEqual(discover.call_count, 1)
                self.assertEqual(before, self.service.paths.config.read_bytes())

    def test_failed_fallback_keeps_existing_config_and_secret(self):
        with patch('tools.acm_agent.service_ai.discover_models', return_value=['m']):
            selected = self.create()
        before = self.service.paths.config.read_bytes()
        with patch('tools.acm_agent.service_ai.discover_models', side_effect=[
            ProviderConfigurationError('model_discovery_failed', 'missing', status=404),
            ProviderConfigurationError('model_discovery_failed', 'denied', status=401),
        ]):
            with self.assertRaises(ProviderConfigurationError):
                self.service.ai_connection_upsert(
                    connection_id=selected, display_name='Changed',
                    base_url='https://relay.example', api_key='replacement-secret', auth={'type': 'bearer'},
                )
        self.assertEqual(before, self.service.paths.config.read_bytes())
        self.assertEqual(self.vault.load(selected).secret, 'fixture-secret')

    def test_refresh_resolves_root_prefix(self):
        with patch('tools.acm_agent.service_ai.discover_models', return_value=['m']):
            selected = self.create()
        with patch('tools.acm_agent.service_ai.discover_models', side_effect=[
            ProviderConfigurationError('model_discovery_failed', 'missing', status=404), ['m'],
        ]):
            self.service.ai_connection_refresh(connection_id=selected)
        self.assertEqual(load_config(self.service.paths)['ai']['providers'][selected]['base_url'], 'https://relay.example/v1')

    def test_discovery_errors_explain_status_without_reflecting_secrets(self):
        for status in (401, 403, 404, 429, 500):
            with self.subTest(status=status):
                transport = QueueTransport(urllib.error.HTTPError(
                    'https://relay.example/v1/models', status, 'reflected-secret', {}, None
                ))
                with self.assertRaises(ProviderConfigurationError) as caught:
                    discover_openai_compatible_models(
                        base_url='https://relay.example/v1', api_key='fixture-secret', transport=transport
                    )
                self.assertIn(str(status), str(caught.exception))
                self.assertNotIn('secret', str(caught.exception))


if __name__ == '__main__':
    unittest.main()
