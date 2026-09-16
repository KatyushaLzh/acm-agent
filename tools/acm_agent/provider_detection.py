"""Bounded same-origin provider discovery and protocol candidates."""
from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Mapping

from .provider import ProviderConfigurationError, ProviderError
from .provider_config import normalize_base_url, endpoint_origin, validate_auth, validate_model_id
from . import openai_compatible

PROTOCOLS = ('openai_compatible', 'openai_responses', 'anthropic')


def normalize_endpoint(value: str) -> tuple[str, str | None]:
    raw = str(value).strip().rstrip("/")
    parsed = urllib.parse.urlsplit(raw)
    suffix_hint = None
    for suffix, protocol in (("/chat/completions", PROTOCOLS[0]), ("/responses", PROTOCOLS[1]), ("/messages", PROTOCOLS[2])):
        if parsed.path.endswith(suffix):
            raw = urllib.parse.urlunsplit(parsed._replace(path=parsed.path[:-len(suffix)]))
            suffix_hint = protocol
            break
    base = normalize_base_url(raw)
    if suffix_hint:
        return base, suffix_hint
    for suffix, protocol in (('/chat/completions', PROTOCOLS[0]), ('/responses', PROTOCOLS[1]), ('/messages', PROTOCOLS[2])):
        if base.endswith(suffix):
            return base[:-len(suffix)], protocol
    return base, 'anthropic' if base.endswith('/anthropic') or urllib.parse.urlsplit(base).hostname == 'api.anthropic.com' else None


def protocol_candidates(base: str, adapter: str, hint: str | None = None) -> list[tuple[str, str]]:
    protocols = list(PROTOCOLS) if adapter == 'auto' else [adapter]
    if hint in protocols:
        protocols.remove(hint)
        protocols.insert(0, hint)
    bases = [base, base + '/v1'] if base == endpoint_origin(base) else [base]
    return [(protocol, address) for protocol in protocols for address in bases]


def protocol_auth(adapter: str, auth: Mapping[str, Any] | None = None) -> dict[str, str]:
    return validate_auth(auth or ({'type': 'header', 'header': 'x-api-key'} if adapter == 'anthropic' else {'type': 'bearer'}))


class DetectionBudget:
    def __init__(self, requests: int = 12, seconds: float = 120):
        self.limit = requests
        self.deadline = time.monotonic() + seconds
        self.requests = 0

    def transport(self, opener):
        def bounded(request, timeout):
            remaining = self.deadline - time.monotonic()
            if self.requests >= self.limit or remaining <= 0:
                raise ProviderError('budget_exceeded', '连接检测预算已用尽，请重试。', retryable=True)
            self.requests += 1
            return opener(request, min(float(timeout), remaining))
        return bounded

    def wrap(self, client):
        if hasattr(client, '_transport'):
            client._transport = self.transport(client._transport)
        return client


class DiscoveredModels(list):
    def __init__(self, values, metadata=None):
        super().__init__(values)
        self.metadata = metadata or {}


def discover_models(base: str, secret: str, *, adapter: str, auth=None, headers=None, budget=None):
    if adapter == 'anthropic':
        from .anthropic import discover_anthropic_models
        opener = openai_compatible._safe_https_open
        if budget is not None:
            opener = budget.transport(opener)
        try:
            result = discover_anthropic_models(base_url=base, api_key=secret, auth=auth,
                headers=headers, transport=opener, include_metadata=True)
            return DiscoveredModels(result['ids'], result['metadata'])
        except ProviderError as exc:
            if exc.code in {'budget_exceeded', 'request_budget_exceeded', 'deadline_exceeded'}:
                raise
            raise ProviderConfigurationError('model_discovery_failed' if exc.status else 'invalid_models_response',
                '模型列表请求失败，请检查协议、鉴权或填写模型 ID。', status=exc.status, retryable=exc.retryable) from None
    selected_auth = protocol_auth(adapter, auth)
    request_headers = {'Accept': 'application/json', **dict(headers or {})}
    if adapter == 'anthropic':
        request_headers.setdefault('anthropic-version', '2023-06-01')
    if selected_auth['type'] == 'bearer':
        request_headers['Authorization'] = 'Bearer ' + secret
    else:
        request_headers[selected_auth['header']] = secret
    opener = openai_compatible._safe_https_open
    if budget is not None:
        opener = budget.transport(opener)
    models = []
    metadata = {}
    cursor = None
    seen_cursors = set()
    while True:
        endpoint = base.rstrip('/') + '/models'
        if cursor:
            endpoint += '?' + urllib.parse.urlencode({'after_id': cursor})
        request = urllib.request.Request(endpoint, headers=request_headers, method='GET')
        try:
            with opener(request, 15) as response:
                if callable(getattr(response, 'geturl', None)) and response.geturl().rstrip('/') != endpoint:
                    raise ProviderConfigurationError('redirect_blocked', '模型列表请求不允许重定向')
                body = response.read(2 * 1024 * 1024 + 1)
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            raise ProviderConfigurationError('model_discovery_failed', f'模型列表请求失败（HTTP {status}）', status=status, retryable=status == 429 or status >= 500) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ProviderConfigurationError('model_discovery_failed', '模型列表网络请求失败', retryable=True) from None
        try:
            if len(body) > 2 * 1024 * 1024:
                raise ValueError()
            document = json.loads(body)
            data = document['data']
            if not isinstance(data, list):
                raise ValueError()
            for item in data:
                model = validate_model_id(item['id'])
                if model not in models:
                    models.append(model)
                    raw = item.get("capabilities")
                    if isinstance(raw, Mapping):
                        def safe(value):
                            if isinstance(value, Mapping):
                                return {key: safe(val) for key, val in value.items() if key in {"thinking", "types", "enabled", "adaptive", "supported", "structured_outputs"}}
                            return value if isinstance(value, bool) else None
                        metadata[model] = {"capabilities": safe(raw)}
                    for field in ("max_input_tokens", "max_tokens"):
                        limit = item.get(field)
                        if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
                            metadata.setdefault(model, {})[field] = limit
            if len(models) > 4096:
                raise ValueError()
        except (ValueError, KeyError, TypeError):
            raise ProviderConfigurationError('invalid_models_response', '模型列表必须包含 data[].id') from None
        if adapter != 'anthropic' or not document.get('has_more'):
            return DiscoveredModels(models, metadata)
        cursor = document.get('last_id')
        if not isinstance(cursor, str) or not cursor or cursor in seen_cursors:
            raise ProviderConfigurationError('invalid_models_response', '模型分页游标无效')
        seen_cursors.add(cursor)
