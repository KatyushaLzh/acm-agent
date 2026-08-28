from __future__ import annotations

import io
import json
import os
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from tools.acm_agent.config import Paths, load_config, save_config
from tools.acm_agent.credentials import (
    DeepSeekCredentialStore,
    ProviderCredentialVault,
    UnavailableCredentialVault,
)
from tools.acm_agent.openai_compatible import (
    OpenAICompatibleClient,
    discover_openai_compatible_models,
)
from tools.acm_agent.provider import (
    CapabilityProfile, OUTPUT_TOKEN_LIMIT_MESSAGE, ProviderConfigurationError, ProviderError,
    ProviderProtocolError,
)
from tools.acm_agent.provider_config import (
    default_provider_config,
    default_task_profiles,
    normalize_base_url,
    validate_ai_catalog,
)
from tools.acm_agent.provider_registry import ProviderRegistry, resolve_reasoning_options
from tools.acm_agent.provider_registry import provider_definition_hash
from tools.acm_agent.provider_conformance import (
    run_live_conformance,
    verified_definition_from_report,
)
from tools.acm_agent.deepseek import DeepSeekClient
from tools.acm_agent.storage import Database
from tools.acm_agent.storage_schema import MIGRATIONS, SCHEMA_VERSION
from tools.acm_agent.service import AcmService
import sqlite3


class Response(io.BytesIO):
    def __init__(self, body: bytes, *, status: int = 200, url: str | None = None):
        super().__init__(body)
        self.status = status
        self._url = url

    def geturl(self):
        return self._url or "https://relay.example/v1/chat/completions"


class QueueTransport:
    def __init__(self, *items):
        self.items = list(items)
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request)
        item = self.items.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def responses_result(*, status="completed", content='{"ok":true}', usage=None, reason=None, error=None):
    payload = {
        "id": "resp-1",
        "object": "response",
        "status": status,
        "model": "deepseek-v4-flash",
        "output": [
            {"type": "reasoning", "content": [{"type": "reasoning_text", "text": "private"}]},
            {"type": "message", "content": [{"type": "output_text", "text": content}]},
        ],
        "usage": usage or {
            "input_tokens": 10,
            "input_tokens_details": {"cached_tokens": 7},
            "output_tokens": 4,
            "output_tokens_details": {"reasoning_tokens": 2},
            "total_tokens": 14,
        },
        "error": error,
        "incomplete_details": {"reason": reason} if reason else None,
    }
    return json.dumps(payload).encode()


def completion(content="OK", *, usage=None):
    return json.dumps(
        {
            "id": "r1",
            "model": "relay-model",
            "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": content}}],
            "usage": usage or {
                "prompt_tokens": 3,
                "completion_tokens": 1,
                "total_tokens": 4,
                "prompt_tokens_details": {"cached_tokens": 2},
            },
        }
    ).encode()


def capabilities(**changes):
    values = dict(
        text_chat=True,
        streaming=True,
        json_object=True,
        function_tools=False,
        thinking=False,
        prompt_cache=True,
        usage_cache_tokens=True,
        usage=True,
        stream_usage=True,
        evidence="verified_live",
        verified_capabilities=(
            "text_chat", "streaming", "json_object", "prompt_cache",
            "usage_cache_tokens", "usage", "stream_usage",
        ),
    )
    values.update(changes)
    return CapabilityProfile(**values)


class ProviderConfigTests(unittest.TestCase):
    def test_v8_migrates_exactly_six_profiles(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = Paths.for_root(Path(temporary))
            paths.ensure()
            paths.config.write_text(
                json.dumps(
                    {
                        "version": 8,
                        "ai": {
                            "recommendation_model": "deepseek-v4-pro",
                            "coaching_model": "deepseek-v4-flash",
                            "summary_model": "deepseek-v4-pro",
                            "coaching_thinking": False,
                            "summary_thinking": True,
                            "reasoning_effort": "max",
                            "summary_reasoning_effort": "high",
                        },
                    }
                ),
                encoding="utf-8",
            )
            config = load_config(paths)
            profiles = config["ai"]["profiles"]
            self.assertEqual(set(profiles), set(default_task_profiles()))
            self.assertEqual(profiles["recommendation"]["model"], "deepseek-v4-pro")
            self.assertFalse(profiles["plan_organize"]["thinking"])
            self.assertTrue(profiles["plan_generate"]["thinking"])
            self.assertFalse(profiles["patch"]["thinking"])
            self.assertEqual(profiles["summary"]["model"], "deepseek-v4-pro")

    def test_endpoint_rejects_ambiguous_and_private_values(self):
        self.assertEqual(normalize_base_url("HTTPS://Relay.Example:443/v1/"), "https://relay.example/v1")
        for value in (
            "http://relay.example/v1",
            "https://127.0.0.1/v1",
            "https://relay.example/v1/../evil",
            "https://relay.example/v1/%2e%2e/evil",
            "https://relay.example/v1\\evil",
            "https://relay.example/v1\n",
        ):
            with self.subTest(value=value), self.assertRaises(ProviderConfigurationError):
                normalize_base_url(value)

    def test_normalized_provider_ids_must_not_collide(self):
        provider = {
            "name": "Relay", "adapter": "openai_compatible",
            "base_url": "https://relay.example/v1", "credential_slot": "relay",
            "auth": {"type": "bearer"}, "enabled": True,
            "models": {"relay-model": {"capabilities": {"text_chat": True, "usage": True}}},
        }
        with self.assertRaisesRegex(ProviderConfigurationError, "collide"):
            validate_ai_catalog(
                {"Relay": provider, "relay": provider}, default_task_profiles()
            )

    def test_builtin_evidence_and_deepseek_adapter_are_reserved(self):
        relay = {
            "name": "Relay", "adapter": "openai_compatible",
            "base_url": "https://relay.example/v1", "credential_slot": "relay",
            "auth": {"type": "bearer"}, "enabled": True,
            "models": {"relay-model": {
                "capabilities": {"text_chat": True, "usage": True},
                "evidence": "verified_builtin",
            }},
        }
        with self.assertRaisesRegex(ProviderConfigurationError, "reserved"):
            validate_ai_catalog({"relay": relay}, default_task_profiles())
        deepseek = default_provider_config()["deepseek"]
        deepseek["base_url"] = "https://api.deepseek.com/custom"
        with self.assertRaisesRegex(ProviderConfigurationError, "official HTTPS root"):
            validate_ai_catalog({"deepseek": deepseek}, default_task_profiles())

    def test_profile_capability_gate_is_fail_closed(self):
        ai = {
            "providers": default_provider_config(),
            "profiles": default_task_profiles(),
            "credential_slots": {
                "deepseek": {
                    "provider_id": "deepseek",
                    "origin": "https://api.deepseek.com",
                    "auth": {"type": "bearer"},
                    "environment_variable": "",
                }
            },
        }
        ai["providers"]["deepseek"]["models"]["deepseek-v4-flash"]["capabilities"]["streaming"] = False
        registry = ProviderRegistry(ai)
        with self.assertRaisesRegex(ProviderConfigurationError, "streaming"):
            registry.route("coaching")

    def test_public_reasoning_strength_maps_deepseek_and_rejects_low(self):
        self.assertEqual(resolve_reasoning_options("deepseek", "auto"), (False, "auto"))
        self.assertEqual(resolve_reasoning_options("deepseek", "off"), (False, "high"))
        self.assertEqual(resolve_reasoning_options("deepseek", "medium"), (True, "high"))
        self.assertEqual(resolve_reasoning_options("deepseek", "high"), (True, "max"))
        with self.assertRaisesRegex(ProviderConfigurationError, "does not support low"):
            resolve_reasoning_options("deepseek", "low")
        self.assertEqual(
            resolve_reasoning_options("openai_compatible", "low"), (True, "low")
        )
        self.assertEqual(
            resolve_reasoning_options("openai_compatible", "auto"), (False, "auto")
        )
        self.assertEqual(
            resolve_reasoning_options("openai_compatible", "off"), (True, "none")
        )

    def test_only_official_flash_declares_json_schema(self):
        providers = default_provider_config()
        flash = providers["deepseek"]["models"]["deepseek-v4-flash"]["capabilities"]
        pro = providers["deepseek"]["models"]["deepseek-v4-pro"]["capabilities"]
        self.assertTrue(flash["json_schema"])
        self.assertFalse(pro["json_schema"])


class DeepSeekResponsesTests(unittest.TestCase):
    def test_length_provider_error_uses_unified_unavailable_message(self):
        error = ProviderError(
            "response_incomplete",
            "provider-specific wording",
            finish_reason="length",
        )

        self.assertEqual(str(error), OUTPUT_TOKEN_LIMIT_MESSAGE)
        self.assertEqual(error.as_dict()["message"], OUTPUT_TOKEN_LIMIT_MESSAGE)

    def test_chat_auto_omits_controls_while_off_is_explicit(self):
        client = DeepSeekClient(api_key="secret", transport=QueueTransport(), retries=0)
        automatic = client._payload(
            [{"role": "user", "content": "x"}],
            model="deepseek-v4-flash", thinking=False, reasoning_effort="auto",
            stream=False,
        )
        disabled = client._payload(
            [{"role": "user", "content": "x"}],
            model="deepseek-v4-flash", thinking=False, reasoning_effort="high",
            stream=False,
        )
        self.assertNotIn("thinking", automatic)
        self.assertNotIn("reasoning_effort", automatic)
        self.assertEqual(disabled["thinking"], {"type": "disabled"})
        self.assertNotIn("reasoning_effort", disabled)

    def test_flash_structured_uses_responses_schema_and_normalizes_usage(self):
        transport = QueueTransport(Response(responses_result()))
        client = DeepSeekClient(api_key="secret", transport=transport, retries=0)
        result = client.structured(
            [{"role": "system", "content": "stable"}, {"role": "user", "content": "x"}],
            model="deepseek-v4-flash",
            json_schema={
                "type": "object",
                "properties": {"ok": {"type": "boolean"}},
                "required": ["ok"],
                "additionalProperties": False,
            },
            schema_name="test_schema",
            thinking=True,
            reasoning_effort="high",
        )
        request = transport.requests[0]
        payload = json.loads(request.data)
        self.assertEqual(request.full_url, "https://api.deepseek.com/responses")
        self.assertEqual(payload["text"]["format"]["type"], "json_schema")
        self.assertEqual(payload["text"]["format"]["name"], "test_schema")
        self.assertEqual(payload["reasoning"], {"effort": "high"})
        self.assertEqual(result.data, {"ok": True})
        self.assertNotIn("private", result.content)
        self.assertEqual(result.usage["cache_read_tokens"], 7)
        self.assertEqual(result.usage["reasoning_tokens"], 2)
        self.assertEqual(result.provider_metadata["transport_api"], "responses")

    def test_responses_incomplete_and_failed_are_typed_without_blind_retry(self):
        incomplete = DeepSeekClient(
            api_key="secret",
            transport=QueueTransport(
                Response(responses_result(status="incomplete", reason="max_output_tokens"))
            ),
            retries=0,
        )
        with self.assertRaises(ProviderProtocolError) as captured:
            incomplete.structured(
                [{"role": "user", "content": "x"}],
                model="deepseek-v4-flash",
                json_schema={"type": "object"},
                schema_name="result",
            )
        self.assertEqual(captured.exception.code, "response_incomplete")
        self.assertEqual(captured.exception.finish_reason, "length")
        self.assertFalse(captured.exception.retryable)
        self.assertEqual(captured.exception.usage["cache_read_tokens"], 7)

        failed = DeepSeekClient(
            api_key="secret",
            transport=QueueTransport(
                Response(responses_result(
                    status="failed", error={"code": "resource_exhausted", "message": "busy"}
                ))
            ),
            retries=0,
        )
        with self.assertRaises(ProviderError) as captured:
            failed.structured(
                [{"role": "user", "content": "x"}],
                model="deepseek-v4-flash",
                json_schema={"type": "object"},
                schema_name="result",
            )
        self.assertTrue(captured.exception.retryable)
        self.assertEqual(captured.exception.code, "resource_failure")

    def test_pro_structured_keeps_chat_json_wire(self):
        deepseek_transport = QueueTransport(Response(completion('{"ok":true}')))
        result = DeepSeekClient(
            api_key="secret", transport=deepseek_transport, retries=0
        ).structured(
            [{"role": "user", "content": "return JSON"}],
            model="deepseek-v4-pro",
            json_schema={"type": "object"},
            schema_name="result",
        )
        self.assertEqual(result.data, {"ok": True})
        self.assertEqual(
            deepseek_transport.requests[0].full_url,
            "https://api.deepseek.com/chat/completions",
        )


    def test_route_accepts_cross_provider_model_ref_and_requires_tier_evidence(self):
        providers = default_provider_config()
        relay = {
            "name": "Relay", "adapter": "openai_compatible",
            "base_url": "https://relay.example/v1", "credential_slot": "relay",
            "auth": {"type": "bearer"}, "enabled": True,
            "models": {"deepseek-v4-flash": {
                "capabilities": {
                    "text_chat": True, "json_object": True, "thinking": True,
                    "usage": True,
                },
                "evidence": "verified_live",
                "verified_capabilities": ["text_chat", "json_object", "thinking", "usage"],
                "verified_reasoning_strengths": ["medium"],
            }},
        }
        relay["models"]["deepseek-v4-flash"]["evidence_hash"] = provider_definition_hash(
            "relay", relay, "deepseek-v4-flash"
        )
        providers["relay"] = relay
        ai = {
            "providers": providers,
            "profiles": default_task_profiles(),
            "credential_slots": {
                "deepseek": {
                    "provider_id": "deepseek", "origin": "https://api.deepseek.com",
                    "auth": {"type": "bearer"}, "environment_variable": "",
                },
                "relay": {
                    "provider_id": "relay", "origin": "https://relay.example",
                    "auth": {"type": "bearer"}, "environment_variable": "",
                },
            },
        }
        registry = ProviderRegistry(ai)
        route = registry.route(
            "recommendation",
            model_ref={"provider_id": "relay", "model": "deepseek-v4-flash"},
            reasoning_strength="medium",
        )
        self.assertEqual((route.provider_id, route.reasoning_effort), ("relay", "medium"))
        with self.assertRaisesRegex(ProviderConfigurationError, "reasoning strength"):
            registry.route(
                "recommendation",
                model_ref={"provider_id": "relay", "model": "deepseek-v4-flash"},
                reasoning_strength="high",
            )

    def test_live_evidence_only_unlocks_capabilities_covered_by_probe(self):
        provider = {
            "name": "Relay", "adapter": "openai_compatible",
            "base_url": "https://relay.example/v1", "credential_slot": "relay",
            "auth": {"type": "bearer"}, "enabled": True,
            "models": {"relay-model": {
                "capabilities": {"text_chat": True, "json_object": True, "usage": True},
                "evidence": "verified_live", "verified_capabilities": ["text_chat", "usage"],
            }},
        }
        provider["models"]["relay-model"]["evidence_hash"] = provider_definition_hash(
            "relay", provider, "relay-model"
        )
        profiles = default_task_profiles()
        for profile in profiles.values():
            profile.update(
                provider_id="relay", model="relay-model", reasoning_strength="auto"
            )
        ai = {
            "providers": {"relay": provider}, "profiles": profiles,
            "credential_slots": {"relay": {
                "provider_id": "relay", "origin": "https://relay.example",
                "auth": {"type": "bearer"}, "environment_variable": "",
            }},
        }
        with self.assertRaisesRegex(ProviderConfigurationError, "not covered"):
            ProviderRegistry(ai).route("recommendation")


class ProviderCredentialVaultTests(unittest.TestCase):
    def test_named_slots_bind_origin_and_never_repr_secret(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = ProviderCredentialVault(
                Path(temporary), protect=lambda value: b"P" + value, unprotect=lambda value: value[1:]
            )
            credential = vault.save(
                "relay", "top-secret-value", provider_id="relay",
                origin="https://relay.example", auth={"type": "bearer"},
            )
            self.assertNotIn("top-secret-value", repr(credential))
            self.assertNotIn("top-secret-value", json.dumps(vault.status("relay")))
            with self.assertRaisesRegex(Exception, "不匹配"):
                vault.load_bound(
                    "relay", provider_id="relay", origin="https://other.example",
                    auth={"type": "bearer"},
                )

    def test_failed_staged_roundtrip_preserves_existing_secret(self):
        with tempfile.TemporaryDirectory() as temporary:
            unprotect = lambda value: value[1:]
            vault = ProviderCredentialVault(
                Path(temporary), protect=lambda value: b"P" + value, unprotect=unprotect
            )
            vault.save(
                "relay", "old-secret", provider_id="relay",
                origin="https://relay.example", auth={"type": "bearer"},
            )
            vault._unprotect = lambda _value: (_ for _ in ()).throw(RuntimeError("broken"))
            with self.assertRaisesRegex(Exception, "写入前校验失败"):
                vault.save(
                    "relay", "new-secret", provider_id="relay",
                    origin="https://relay.example", auth={"type": "bearer"},
                )
            vault._unprotect = unprotect
            self.assertEqual(vault.load("relay").secret, "old-secret")

    def test_explicit_stage_can_be_discarded_or_committed(self):
        with tempfile.TemporaryDirectory() as temporary:
            vault = ProviderCredentialVault(
                Path(temporary),
                protect=lambda value: b"P" + value,
                unprotect=lambda value: value[1:],
            )
            staged = vault.stage(
                "relay", "candidate", provider_id="relay",
                origin="https://relay.example", auth={"type": "bearer"},
            )
            self.assertIsNone(vault.load("relay"))
            self.assertTrue(vault.discard(staged))
            self.assertIsNone(vault.load("relay"))
            staged = vault.stage(
                "relay", "candidate", provider_id="relay",
                origin="https://relay.example", auth={"type": "bearer"},
            )
            self.assertEqual(vault.commit(staged).secret, "candidate")
            self.assertEqual(vault.load("relay").secret, "candidate")

    def test_legacy_deepseek_migration_is_verified_and_archive_conflicts_fail_closed(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            legacy_path = root / "deepseek-key.dpapi"
            protect = lambda value: b"P" + value
            unprotect = lambda value: value[1:]
            DeepSeekCredentialStore(
                legacy_path, protect=protect, unprotect=unprotect
            ).save("legacy-secret")
            vault = ProviderCredentialVault(
                root / "credentials", legacy_deepseek_path=legacy_path,
                protect=protect, unprotect=unprotect,
            )
            self.assertTrue(vault.migrate_legacy_deepseek())
            self.assertEqual(vault.load("deepseek").secret, "legacy-secret")
            self.assertFalse(legacy_path.exists())
            self.assertTrue(legacy_path.with_suffix(".dpapi.migrated").exists())

            DeepSeekCredentialStore(
                legacy_path, protect=protect, unprotect=unprotect
            ).save("different-secret")
            with self.assertRaisesRegex(Exception, "冲突"):
                vault.migrate_legacy_deepseek()
            self.assertTrue(legacy_path.exists())
            self.assertEqual(vault.load("deepseek").secret, "legacy-secret")


class OpenAICompatibleTests(unittest.TestCase):
    def client(self, transport, **changes):
        profile = capabilities(**changes)
        return OpenAICompatibleClient(
            "top-secret-value",
            provider_id="relay",
            base_url="https://relay.example/v1",
            auth={"type": "bearer"},
            models={"relay-model": profile},
            credential_origin="https://relay.example",
            transport=transport,
            retries=1,
            sleep=lambda _seconds: None,
        )

    def test_text_json_stream_usage_and_cache_normalization(self):
        stream = b"\n".join(
            [
                b'data: {"id":"s1","model":"relay-model","choices":[{"delta":{"content":"OK"},"finish_reason":null}]}',
                b"",
                b'data: {"id":"s1","model":"relay-model","choices":[],"usage":{"prompt_tokens":3,"completion_tokens":1,"total_tokens":4,"prompt_tokens_details":{"cached_tokens":2}}}',
                b"",
                b"data: [DONE]",
                b"",
            ]
        )
        transport = QueueTransport(
            Response(completion()),
            Response(completion('{"ok":true}')),
            Response(stream),
        )
        client = self.client(transport)
        text = client.chat([{"role": "user", "content": "hello"}], model="relay-model")
        structured = client.chat_json(
            [{"role": "user", "content": "return JSON"}], model="relay-model", json_retries=0
        )
        events = list(client.stream_chat([{"role": "user", "content": "hello"}], model="relay-model", thinking=False))
        self.assertEqual(text.usage["cache_read_tokens"], 2)
        self.assertTrue(structured.data["ok"])
        self.assertEqual(events[-1].kind, "done")
        self.assertEqual(events[-1].usage["total_tokens"], 4)
        request = transport.requests[0]
        self.assertEqual(request.full_url, "https://relay.example/v1/chat/completions")
        self.assertEqual(request.get_header("Authorization"), "Bearer top-secret-value")

    def test_structured_keeps_relay_on_chat_json(self):
        transport = QueueTransport(Response(completion('{"ok":true}')))
        result = self.client(transport).structured(
            [{"role": "user", "content": "return JSON"}],
            model="relay-model",
            json_schema={"type": "object"},
            schema_name="result",
        )
        self.assertEqual(result.data, {"ok": True})
        self.assertEqual(
            transport.requests[0].full_url,
            "https://relay.example/v1/chat/completions",
        )
        self.assertEqual(result.provider_metadata["transport_api"], "chat_completions")

    def test_json_accepts_only_a_single_whole_response_markdown_fence(self):
        fenced = self.client(QueueTransport(Response(completion(
            '```json\n{"ok":true}\n```'
        )))).chat_json(
            [{"role": "user", "content": "return JSON"}],
            model="relay-model",
            json_retries=0,
        )

        self.assertEqual(fenced.data, {"ok": True})
        self.assertEqual(fenced.usage["protocol_repairs"], 1)
        self.assertEqual(fenced.provider_metadata["protocol_repairs"], 1)

        invalid_envelopes = (
            'Here is the result:\n```json\n{"ok":true}\n```',
            '```json\n{"ok":true}\n```\ntrailing text',
            '```json\n{"ok":true}\n```\n```json\n{"ok":true}\n```',
            '```json\n[true]\n```',
        )
        for content in invalid_envelopes:
            with self.subTest(content=content):
                client = self.client(QueueTransport(Response(completion(content))))
                with self.assertRaises(ProviderProtocolError):
                    client.chat_json(
                        [{"role": "user", "content": "return JSON"}],
                        model="relay-model",
                        json_retries=0,
                    )

    def test_retry_and_redirect_origin_are_fail_closed(self):
        unavailable = urllib.error.HTTPError(
            "https://relay.example/v1/chat/completions", 503, "busy", {}, io.BytesIO(b"{}")
        )
        transport = QueueTransport(unavailable, Response(completion()))
        self.assertEqual(
            self.client(transport).chat([{"role": "user", "content": "x"}], model="relay-model").content,
            "OK",
        )
        self.assertEqual(len(transport.requests), 2)
        redirected = QueueTransport(Response(completion(), url="https://evil.example/v1/chat/completions"))
        with self.assertRaisesRegex(ProviderConfigurationError, "URL"):
            self.client(redirected).chat([{"role": "user", "content": "x"}], model="relay-model")
        self.assertEqual(len(redirected.requests), 1)

    def test_openai_reasoning_effort_distinguishes_auto_and_off(self):
        transport = QueueTransport(
            Response(completion()), Response(completion()), Response(completion())
        )
        client = self.client(
            transport, thinking=True,
            verified_reasoning_strengths=("off", "low", "medium", "high"),
        )
        client.chat(
            [{"role": "user", "content": "x"}], model="relay-model",
            thinking=False, reasoning_effort="auto",
        )
        client.chat(
            [{"role": "user", "content": "x"}], model="relay-model",
            thinking=True, reasoning_effort="none", temperature=0,
        )
        client.chat(
            [{"role": "user", "content": "x"}], model="relay-model",
            thinking=True, reasoning_effort="medium",
        )
        automatic = json.loads(transport.requests[0].data)
        disabled = json.loads(transport.requests[1].data)
        reasoned = json.loads(transport.requests[2].data)
        self.assertNotIn("reasoning_effort", automatic)
        self.assertEqual(disabled["reasoning_effort"], "none")
        self.assertEqual(disabled["temperature"], 0)
        self.assertNotIn("thinking", reasoned)
        self.assertEqual(reasoned["reasoning_effort"], "medium")

    def test_model_discovery_is_bounded_and_rejects_redirects(self):
        endpoint = "https://relay.example/v1/models"
        transport = QueueTransport(
            Response(json.dumps({"data": [{"id": "same"}, {"id": "same"}, {"id": "other"}]}).encode(), url=endpoint)
        )
        self.assertEqual(
            discover_openai_compatible_models(
                base_url="https://relay.example/v1", api_key="secret", transport=transport
            ),
            ["same", "other"],
        )
        self.assertEqual(transport.requests[0].method, "GET")
        self.assertEqual(transport.requests[0].get_header("Authorization"), "Bearer secret")
        redirected = QueueTransport(
            Response(b'{"data":[{"id":"x"}]}', url="https://evil.example/models")
        )
        with self.assertRaisesRegex(ProviderConfigurationError, "URL"):
            discover_openai_compatible_models(
                base_url="https://relay.example/v1", api_key="secret", transport=redirected
            )

    def test_model_discovery_rejects_private_dns_and_oversized_catalogs(self):
        with patch(
            "tools.acm_agent.openai_compatible.socket.getaddrinfo",
            return_value=[(2, 1, 6, "", ("127.0.0.1", 443))],
        ), self.assertRaisesRegex(ProviderConfigurationError, "non-public"):
            discover_openai_compatible_models(
                base_url="https://relay.example/v1", api_key="secret"
            )
        endpoint = "https://relay.example/v1/models"
        oversized = QueueTransport(
            Response(b"{" + b" " * 1_048_576 + b"}", url=endpoint)
        )
        with self.assertRaisesRegex(ProviderConfigurationError, "too large"):
            discover_openai_compatible_models(
                base_url="https://relay.example/v1", api_key="secret", transport=oversized
            )
        too_many = QueueTransport(Response(json.dumps({
            "data": [{"id": f"model-{index}"} for index in range(513)]
        }).encode(), url=endpoint))
        with self.assertRaisesRegex(ProviderConfigurationError, "too many"):
            discover_openai_compatible_models(
                base_url="https://relay.example/v1", api_key="secret", transport=too_many
            )

    def test_invalid_utf8_and_premature_eof_preserve_protocol_error_usage(self):
        invalid = QueueTransport(Response(b"data: {\xff}\n\n"))
        with self.assertRaises(ProviderProtocolError):
            list(self.client(invalid).stream_chat([{"role": "user", "content": "x"}], model="relay-model", thinking=False))
        partial = QueueTransport(Response(b'data: {"choices":[],"usage":{"total_tokens":6}}\n\n'))
        with self.assertRaises(ProviderProtocolError) as captured:
            list(self.client(partial).stream_chat([{"role": "user", "content": "x"}], model="relay-model", thinking=False))
        self.assertEqual(captured.exception.usage["total_tokens"], 6)
        self.assertEqual(captured.exception.requested_model, "relay-model")

    def test_live_conformance_aggregates_calls_and_requires_cache_telemetry(self):
        stream = b"\n".join([
            b'data: {"choices":[{"delta":{"content":"OK"},"finish_reason":"stop"}]}', b"",
            b'data: {"choices":[],"usage":{"prompt_tokens":3,"completion_tokens":1,"total_tokens":4,"prompt_tokens_details":{"cached_tokens":0}}}', b"",
            b"data: [DONE]", b"",
        ])
        client = self.client(QueueTransport(
            Response(completion()), Response(completion('{"ok":true}')), Response(stream)
        ))
        provider = {
            "name": "Relay", "adapter": "openai_compatible",
            "base_url": "https://relay.example/v1", "credential_slot": "relay",
            "auth": {"type": "bearer"}, "enabled": True,
            "models": {"relay-model": {"capabilities": {
                "text_chat": True, "streaming": True, "json_object": True,
                "prompt_cache": True, "usage_cache_tokens": True,
                "usage": True, "stream_usage": True,
            }}},
        }
        profiles = default_task_profiles()
        for profile in profiles.values():
            profile.update(
                provider_id="relay", model="relay-model", reasoning_strength="auto"
            )
        route = ProviderRegistry({
            "providers": {"relay": provider}, "profiles": profiles,
            "credential_slots": {"relay": {
                "provider_id": "relay", "origin": "https://relay.example",
                "auth": {"type": "bearer"}, "environment_variable": "",
            }},
        }).probe_route("relay", "relay-model")
        report = run_live_conformance(client, route)
        self.assertTrue(report["passed"])
        self.assertEqual(report["usage"]["provider_requests"], 3)
        self.assertEqual(report["usage"]["total_tokens"], 12)
        self.assertIn("usage_cache_tokens", report["verified_capabilities"])
        self.assertNotIn("thinking", report["verified_capabilities"])
        self.assertNotIn("prompt_cache", report["verified_capabilities"])

    def test_text_conformance_checks_wire_content_not_exact_model_wording(self):
        verbose = self.client(QueueTransport(Response(completion(
            "I completed the protocol liveness check. OK"
        ))))
        blank = self.client(QueueTransport(Response(completion(""))))
        provider = {
            "name": "Relay", "adapter": "openai_compatible",
            "base_url": "https://relay.example/v1", "credential_slot": "relay",
            "auth": {"type": "bearer"}, "enabled": True,
            "models": {"relay-model": {"capabilities": {
                "text_chat": True, "usage": True,
            }}},
        }
        profiles = default_task_profiles()
        for profile in profiles.values():
            profile.update(
                provider_id="relay", model="relay-model", reasoning_strength="auto"
            )
        route = ProviderRegistry({
            "providers": {"relay": provider}, "profiles": profiles,
            "credential_slots": {"relay": {
                "provider_id": "relay", "origin": "https://relay.example",
                "auth": {"type": "bearer"}, "environment_variable": "",
            }},
        }).probe_route("relay", "relay-model")

        verbose_report = run_live_conformance(
            verbose, route, required_capabilities=("text_chat", "usage")
        )
        blank_report = run_live_conformance(
            blank, route, required_capabilities=("text_chat", "usage")
        )

        self.assertTrue(verbose_report["passed"])
        self.assertFalse(blank_report["passed"])

    def test_targeted_conformance_issues_unforgeable_tier_evidence(self):
        client = self.client(QueueTransport(Response(completion())), thinking=True)
        provider = {
            "name": "Relay", "adapter": "openai_compatible",
            "base_url": "https://relay.example/v1", "credential_slot": "relay",
            "auth": {"type": "bearer"}, "enabled": True,
            "models": {"relay-model": {"capabilities": {
                "text_chat": True, "thinking": True, "usage": True,
            }}},
        }
        profiles = default_task_profiles()
        for profile in profiles.values():
            profile.update(
                provider_id="relay", model="relay-model", reasoning_strength="auto"
            )
        route = ProviderRegistry({
            "providers": {"relay": provider}, "profiles": profiles,
            "credential_slots": {"relay": {
                "provider_id": "relay", "origin": "https://relay.example",
                "auth": {"type": "bearer"}, "environment_variable": "",
            }},
        }).probe_route("relay", "relay-model", reasoning_strength="medium")
        report = run_live_conformance(
            client, route, required_capabilities=("text_chat", "usage")
        )
        definition = verified_definition_from_report("relay", provider, "relay-model", report)
        self.assertEqual(definition["verified_reasoning_strengths"], ["medium"])
        with self.assertRaisesRegex(ValueError, "untrusted"):
            verified_definition_from_report(
                "relay", provider, "relay-model", dict(report)
            )


class Stage2StorageMigrationTests(unittest.TestCase):
    def test_v18_to_v19_backfills_only_provable_route_facts_and_creates_backup(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "state.db"
            connection = sqlite3.connect(path)
            try:
                for version in range(1, 19):
                    connection.executescript(MIGRATIONS[version])
                    connection.execute(f"PRAGMA user_version={version}")
                connection.execute(
                    """INSERT INTO ai_runs(
                           id,kind,model,request_summary_json,status,usage_json,
                           error_json,created_at,telemetry_json,estimated_cost_json)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        "legacy", "plan_import", "deepseek-v4-pro",
                        '{"mode":"organize"}', "complete",
                        '{"prompt_cache_hit_tokens":7,"total_tokens":11}',
                        "{}", "2026-08-25T00:00:00+00:00", "{}", "{}",
                    ),
                )
                connection.commit()
            finally:
                connection.close()
            with Database(path) as database:
                row = database.ai_run("legacy")
                self.assertGreaterEqual(SCHEMA_VERSION, 19)
                self.assertEqual(row["provider_id"], "deepseek")
                self.assertEqual(row["profile_id"], "plan_organize")
                self.assertEqual(row["requested_model"], "deepseek-v4-pro")
                self.assertIsNone(row["resolved_model"])
                self.assertEqual(row["cache_read_tokens"], 7)
                self.assertIsNone(row["cache_write_tokens"])
            self.assertTrue(path.with_name("state.db.v18.bak").is_file())


class Stage2ServiceManagementTests(unittest.TestCase):
    def test_unavailable_secure_store_allows_explicit_environment_fallback(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            service = AcmService(
                root,
                credential_vault=UnavailableCredentialVault("fixture unavailable"),
            )
            service.setup("fixture", "42", skip_validate=True)
            with patch.dict(os.environ, {"DEEPSEEK_API_KEY": "environment-secret"}):
                credential = next(
                    item for item in service.ai_credentials()["credentials"]
                    if item["slot"] == "deepseek"
                )
                self.assertEqual(credential["source"], "environment")
                self.assertTrue(credential["detected"])
                self.assertIsNone(credential["error"])
                self.assertEqual(
                    credential["secure_store_error_code"],
                    "credential_store_unavailable",
                )
                secret, source = service._provider_registry()._secret(
                    "deepseek",
                    load_config(service.paths)["ai"]["providers"]["deepseek"],
                )
                self.assertEqual(secret, "environment-secret")
                self.assertEqual(source, "environment")
            self.assertFalse(service.ai_status()["secure_store"]["available"])

    def test_simplified_connection_discovers_models_keeps_blank_key_and_marks_missing(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = ProviderCredentialVault(
                root / ".acm" / "credentials",
                protect=lambda value: b"P" + value,
                unprotect=lambda value: value[1:],
            )
            service = AcmService(root, credential_vault=vault)
            service.setup("fixture", "42", skip_validate=True)
            with patch(
                "tools.acm_agent.service_ai.discover_openai_compatible_models",
                return_value=["shared-name", "long-model-v1"],
            ):
                created = service.ai_connection_upsert(
                    display_name="Relay",
                    base_url="https://relay.example/v1",
                    api_key="connection-secret",
                )
            connection_id = created["connection_id"]
            self.assertNotIn(
                "connection-secret",
                service.paths.config.read_text(encoding="utf-8"),
            )
            with patch(
                "tools.acm_agent.service_ai.discover_openai_compatible_models",
                return_value=["shared-name"],
            ):
                service.ai_connection_upsert(
                    connection_id=connection_id,
                    display_name="Relay renamed",
                    base_url="https://relay.example/v1",
                    api_key="",
                )
            connection = next(
                item for item in service.ai_connections()["connections"]
                if item["id"] == connection_id
            )
            models = {item["id"]: item for item in connection["models"]}
            self.assertTrue(models["shared-name"]["available"])
            self.assertFalse(models["long-model-v1"]["available"])
            self.assertEqual(vault.load(connection_id).secret, "connection-secret")
            config = load_config(service.paths)
            config["ai"]["profiles"]["recommendation"].update(
                provider_id=connection_id,
                model="shared-name",
                reasoning_strength="auto",
            )
            save_config(service.paths, config)
            with self.assertRaisesRegex(ProviderConfigurationError, "recommendation"):
                service.ai_connection_delete(connection_id=connection_id)

    def test_simplified_connection_discovery_failure_rolls_back_config_and_credential(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = ProviderCredentialVault(
                root / ".acm" / "credentials",
                protect=lambda value: b"P" + value,
                unprotect=lambda value: value[1:],
            )
            service = AcmService(root, credential_vault=vault)
            service.setup("fixture", "42", skip_validate=True)
            before = service.paths.config.read_bytes()
            with patch(
                "tools.acm_agent.service_ai.discover_openai_compatible_models",
                side_effect=ProviderConfigurationError(
                    "model_discovery_failed", "no standard /models"
                ),
            ):
                with self.assertRaisesRegex(ProviderConfigurationError, "/models"):
                    service.ai_connection_upsert(
                        display_name="Broken",
                        base_url="https://broken.example/v1",
                        api_key="candidate-secret",
                    )
            self.assertEqual(service.paths.config.read_bytes(), before)
            self.assertEqual(
                sorted(path.name for path in (root / ".acm" / "credentials").glob("*")),
                [],
            )

    def test_service_manages_provider_named_credential_and_six_profiles_without_echo(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = ProviderCredentialVault(
                root / ".acm" / "credentials",
                protect=lambda value: b"P" + value,
                unprotect=lambda value: value[1:],
            )
            service = AcmService(root, credential_vault=vault)
            service.setup("fixture", "42", skip_validate=True)
            self.assertEqual(set(service.ai_profiles()["profiles"]), set(default_task_profiles()))
            service.ai_provider_upsert(
                provider_id="relay",
                name="Relay",
                base_url="https://relay.example/v1",
                credential_slot="relay",
                models={
                    "relay-model": {
                        "evidence": "verified_builtin",
                        "capabilities": {
                            "text_chat": True,
                            "streaming": True,
                            "json_object": True,
                            "function_tools": False,
                            "thinking": False,
                            "prompt_cache": False,
                            "usage_cache_tokens": False,
                            "usage": True,
                            "stream_usage": True,
                        }
                    }
                },
            )
            secret = "stage2-service-secret"
            payload = service.ai_credential_slot(
                slot="relay", provider_id="relay", api_key=secret
            )
            self.assertNotIn(secret, json.dumps(payload))
            self.assertNotIn(secret, (root / ".acm" / "config.json").read_text(encoding="utf-8"))
            providers = service.ai_providers()["providers"]
            relay = next(item for item in providers if item["id"] == "relay")
            self.assertTrue(relay["credential"]["persisted"])
            self.assertEqual(relay["models"][0]["evidence"], "declared")
            with self.assertRaisesRegex(ProviderConfigurationError, "capability|conformance"):
                service.ai_profile_update(
                    profile_id="coaching",
                    provider_id="relay",
                    model="relay-model",
                    thinking=False,
                )
            service.ai_credential_slot(slot="relay", provider_id="relay", clear=True)
            self.assertFalse(vault.status("relay")["persisted"])

    def test_slot_rotation_can_clear_old_credential_and_env_auth_rebind_is_blocked(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            vault = ProviderCredentialVault(
                root / ".acm" / "credentials",
                protect=lambda value: b"P" + value,
                unprotect=lambda value: value[1:],
            )
            service = AcmService(root, credential_vault=vault)
            service.setup("fixture", "42", skip_validate=True)
            models = {"relay-model": {"capabilities": {"text_chat": True, "usage": True}}}
            service.ai_provider_upsert(
                provider_id="relay", name="Relay", base_url="https://relay.example/v1",
                credential_slot="oldslot", models=models, environment_variable="RELAY_KEY",
            )
            service.ai_credential_slot(
                slot="oldslot", provider_id="relay", api_key="old-secret"
            )
            with self.assertRaisesRegex(ProviderConfigurationError, "origin/auth"):
                service.ai_provider_upsert(
                    provider_id="relay", name="Relay", base_url="https://relay.example/v1",
                    credential_slot="oldslot", models=models, auth_type="header",
                    header_name="X-API-Key", environment_variable="RELAY_KEY",
                )
            with self.assertRaisesRegex(ProviderConfigurationError, "cannot be rebound"):
                service.ai_provider_upsert(
                    provider_id="relay", name="Relay", base_url="https://other.example/v1",
                    credential_slot="newslot", models=models,
                    environment_variable="RELAY_KEY",
                )
            service.ai_provider_upsert(
                provider_id="relay", name="Relay", base_url="https://relay.example/v1",
                credential_slot="newslot", models=models,
            )
            service.ai_credential_slot(slot="oldslot", provider_id="relay", clear=True)
            self.assertFalse(vault.status("oldslot")["persisted"])
            self.assertNotIn(
                "oldslot", load_config(service.paths)["ai"]["credential_slots"]
            )


if __name__ == "__main__":
    unittest.main()
