from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import tempfile
import unittest

from tools.acm_agent.config import load_config, save_config
from tools.acm_agent.provider import ProviderConfigurationError
from tools.acm_agent.provider_config import TASK_PROFILE_IDS
from tools.acm_agent.provider_registry import ProviderRegistry
from tools.acm_agent.service import AcmService
from tools.acm_agent.service_ai import ServiceAIMixin


class PolicyStaleRouteTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.service = AcmService(Path(temporary.name))
        self.service.setup('fixture', '42', skip_validate=True)
        self.config = load_config(self.service.paths)
        self.ai = self.config['ai']
        # Arbitrary IDs ensure this is a lifecycle fix, not a DeepSeek alias patch.
        template = deepcopy(next(iter(self.ai['providers']['deepseek']['models'].values())))
        template['evidence'] = 'verified_builtin'
        self.ai['providers']['deepseek']['models'] = {
            'current-model': template, 'other-model': deepcopy(template),
        }
        for profile in self.ai['profiles'].values():
            profile.update(model='current-model', reasoning_strength='auto')
        self.retired = dict(provider_id='deepseek', model='retired-model', reasoning_strength='auto')
        self.persist()

    def persist(self):
        save_config(self.service.paths, self.config)

    def update(self, policy=None, **kwargs):
        return self.service.ai_policy_update(policy=policy or deepcopy(self.ai['policy']), **kwargs)

    def assert_rejected_without_write(self, policy, code):
        before = self.service.paths.config.read_bytes()
        with self.assertRaises(ProviderConfigurationError) as caught:
            self.update(policy, coaching_delivery_mode='low_latency')
        self.assertEqual(caught.exception.code, code)
        self.assertEqual(self.service.paths.config.read_bytes(), before)

    def test_missing_primaries_allow_budget_and_delivery_update_but_not_execution(self):
        for profile in self.ai['profiles'].values():
            profile['model'] = 'retired-model'
        self.persist()
        policy = deepcopy(self.ai['policy'])
        policy['budgets']['patch']['request_timeout_seconds'] = 101
        result = self.update(policy, coaching_delivery_mode='low_latency')
        self.assertEqual(len(result['route_warnings']), len(TASK_PROFILE_IDS))
        saved = load_config(self.service.paths)['ai']
        self.assertEqual(saved['policy']['budgets']['patch']['request_timeout_seconds'], 101)
        self.assertEqual(saved['coaching_delivery_mode'], 'low_latency')
        self.assertEqual(saved['profiles'], self.ai['profiles'])
        with self.assertRaises(ProviderConfigurationError):
            ProviderRegistry(saved).route_plan('patch')

    def test_legacy_service_entrypoint_uses_same_edit_validation(self):
        self.ai['profiles']['summary']['model'] = 'retired-model'
        self.persist()
        result = ServiceAIMixin.ai_policy_update(self.service, policy=self.ai['policy'])
        self.assertEqual(result['route_warnings'][0]['profile_id'], 'summary')

    def test_existing_missing_fallback_is_preserved_with_warning_and_runtime_gate(self):
        self.ai['policy']['fallbacks']['patch'] = [self.retired]
        self.persist()
        result = self.update()
        warning = result['route_warnings'][0]
        self.assertEqual((warning['route_kind'], warning['code']), ('fallback', 'invalid_model'))
        saved = load_config(self.service.paths)['ai']
        self.assertEqual(saved['policy']['fallbacks']['patch'], [self.retired])
        with self.assertRaises(ProviderConfigurationError):
            ProviderRegistry(saved).route_plan('patch')

    def test_remove_or_replace_stale_fallback_even_with_stale_primary_elsewhere(self):
        self.ai['profiles']['summary']['model'] = 'retired-model'
        self.ai['policy']['fallbacks']['patch'] = [self.retired]
        self.persist()
        for routes in ([], [dict(self.retired, model='other-model')]):
            with self.subTest(routes=routes):
                policy = deepcopy(self.ai['policy'])
                policy['fallbacks']['patch'] = routes
                self.assertTrue(self.update(policy)['ok'])
                self.assertEqual(load_config(self.service.paths)['ai']['policy']['fallbacks']['patch'], routes)

    def test_new_missing_fallback_rejected_even_when_primary_is_stale(self):
        self.ai['profiles']['patch']['model'] = 'another-retired-model'
        self.persist()
        policy = deepcopy(self.ai['policy'])
        policy['fallbacks']['patch'] = [self.retired]
        self.assert_rejected_without_write(policy, 'invalid_model')

    def test_changed_strength_of_retired_fallback_is_new_choice(self):
        self.ai['policy']['fallbacks']['patch'] = [self.retired]
        self.persist()
        policy = deepcopy(self.ai['policy'])
        policy['fallbacks']['patch'][0]['reasoning_strength'] = 'medium'
        self.assert_rejected_without_write(policy, 'invalid_model')

    def test_cannot_copy_retired_fallback_to_another_task(self):
        self.ai['policy']['fallbacks']['patch'] = [self.retired]
        self.persist()
        policy = deepcopy(self.ai['policy'])
        policy['fallbacks']['summary'] = [self.retired]
        self.assert_rejected_without_write(policy, 'invalid_model')

    def test_existing_unavailable_fallback_is_editable_new_one_rejected(self):
        self.ai['providers']['deepseek']['models']['other-model']['available'] = False
        self.ai['policy']['fallbacks']['patch'] = [dict(self.retired, model='other-model')]
        self.persist()
        self.assertEqual(self.update()['route_warnings'][0]['code'], 'model_unavailable')
        policy = deepcopy(self.ai['policy'])
        policy['fallbacks']['summary'] = [dict(self.retired, model='other-model')]
        self.assert_rejected_without_write(policy, 'model_unavailable')

    def test_unverified_new_fallback_rejected_existing_one_warns(self):
        model = self.ai['providers']['deepseek']['models']['other-model']
        model.update(evidence='declared', verified_reasoning_strengths=[], verified_capabilities=[])
        self.persist()
        policy = deepcopy(self.ai['policy'])
        policy['fallbacks']['patch'] = [dict(self.retired, model='other-model')]
        self.assert_rejected_without_write(policy, 'unverified_capability')
        self.ai['policy'] = policy
        self.persist()
        self.assertEqual(self.update()['route_warnings'][0]['code'], 'unverified_capability')

    def test_disabled_provider_does_not_lock_existing_settings(self):
        provider = deepcopy(self.ai['providers']['deepseek'])
        provider.update(enabled=False, credential_slot='retired-provider',
                        adapter='openai_compatible', models={}, state='needs_model')
        self.ai['providers']['retired-provider'] = provider
        self.ai['credential_slots']['retired-provider'] = dict(
            self.ai['credential_slots']['deepseek'], provider_id='retired-provider',
        )
        self.ai['policy']['fallbacks']['patch'] = [dict(self.retired, provider_id='retired-provider')]
        self.persist()
        self.assertEqual(self.update()['route_warnings'][0]['code'], 'invalid_provider')

    def test_malformed_budget_and_forbidden_coaching_fallback_still_rejected(self):
        self.ai['profiles']['patch']['model'] = 'retired-model'
        self.persist()
        policy = deepcopy(self.ai['policy'])
        policy['budgets']['patch']['max_output_tokens'] = -1
        self.assert_rejected_without_write(policy, 'invalid_budget')
        policy = deepcopy(self.ai['policy'])
        policy['fallbacks']['coaching'] = [self.retired]
        self.assert_rejected_without_write(policy, 'invalid_fallback')

    def test_fallback_duplicate_of_primary_still_rejected(self):
        policy = deepcopy(self.ai['policy'])
        policy['fallbacks']['patch'] = [dict(self.retired, model='current-model')]
        self.assert_rejected_without_write(policy, 'invalid_fallback')


if __name__ == '__main__':
    unittest.main()
