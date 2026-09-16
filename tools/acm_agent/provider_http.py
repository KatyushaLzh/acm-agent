"""Provider-neutral, bounded HTTP and SSE utilities (no protocol parsing)."""
from __future__ import annotations

import ipaddress
import json
import re
import socket
import time
import urllib.error
import urllib.request
from contextlib import contextmanager
from typing import Any, Iterator, Mapping
from urllib.parse import urlsplit

from .provider import ProviderConfigurationError, ProviderError, ProviderProtocolError
from .provider_config import normalize_base_url


class RejectRedirects(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(RejectRedirects())


def safe_https_open(request: urllib.request.Request, timeout: float) -> Any:
    parsed = urlsplit(request.full_url)
    validation_url = request.full_url.split("?", 1)[0]
    # Config validation rejects this full endpoint because a *base* is expected;
    # transport validation accepts endpoints while retaining all path safeguards.
    if validation_url.endswith("/chat/completions"):
        validation_url = validation_url[:-len("/chat/completions")]
    normalize_base_url(validation_url)
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, parsed.port or 443, type=socket.SOCK_STREAM)}
    except OSError:
        raise ProviderConfigurationError("endpoint_resolution_failed", "provider DNS resolution failed") from None
    if not addresses or any(not ipaddress.ip_address(address.split("%", 1)[0]).is_global for address in addresses):
        raise ProviderConfigurationError("unsafe_endpoint", "provider resolved to a non-public address")
    return _OPENER.open(request, timeout=timeout)


@contextmanager
def managed_response(response: Any) -> Iterator[Any]:
    try:
        yield response
    finally:
        close = getattr(response, "close", None)
        if callable(close):
            close()


def http_error(status: int, body: bytes, secret: str = "") -> ProviderError:
    # Only field names enter telemetry; provider bodies can echo secrets/prompts.
    details: dict[str, Any] = {}
    try:
        document = json.loads(body)
        error = document.get("error", {}) if isinstance(document, Mapping) else {}
        message = str(error.get("message", "")) if isinstance(error, Mapping) else ""
        parameter = str(error.get("param", "")) if isinstance(error, Mapping) else ""
        for field in ("temperature", "stream_options", "max_completion_tokens", "max_tokens", "output_config.format", "output_config", "thinking", "stream"):
            if parameter == field or (re.search(r"unsupported|not supported|unknown|unrecognized|unexpected|not permitted|not allowed", message, re.I) and field in message):
                details["unsupported_parameter"] = field
                if field == "thinking":
                    for mode in ("adaptive", "enabled", "disabled"):
                        if re.search(r"\b" + mode + r"\b", message):
                            details["unsupported_thinking_mode"] = mode
                            break
                break
    except (ValueError, TypeError):
        pass
    code = {400: "invalid_request", 401: "authentication_failed", 402: "insufficient_balance", 403: "permission_denied", 404: "endpoint_not_found", 408: "timeout", 422: "invalid_request", 429: "rate_limited", 529: "server_error"}.get(status, "server_error" if status >= 500 else "http_error")
    return ProviderError(code, f"Provider request failed (HTTP {status})", status=status,
                         retryable=status in {408, 429, 500, 502, 503, 504, 529}, protocol_details=details)


def open_response(request, timeout, transport, *, secret=""):
    try:
        response = transport(request, timeout)
    except urllib.error.HTTPError as exc:
        with managed_response(exc):
            raise http_error(exc.code, exc.read(65536), secret) from None
    except (urllib.error.URLError, OSError, TimeoutError):
        raise ProviderError("network_error", "Provider network or TLS request failed", retryable=True) from None
    try:
        final_url = getattr(response, "geturl", lambda: request.full_url)()
        if final_url != request.full_url:
            raise ProviderConfigurationError("redirect_blocked", "Provider redirects are not allowed")
        status = getattr(response, "status", 200)
        if status >= 300:
            raise http_error(status, response.read(65536), secret)
    except BaseException:
        close = getattr(response, "close", None)
        if callable(close):
            close()
        raise
    return response


def read_json(response, limit=8_388_608):
    raw = response.read(limit + 1)
    if len(raw) > limit:
        raise ProviderProtocolError("response_too_large", "Provider response exceeds the size limit")
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        raise ProviderProtocolError("invalid_response", "Provider response is not JSON") from None
    if not isinstance(value, dict):
        raise ProviderProtocolError("invalid_response", "Provider response must be an object")
    return value


def iter_sse(response, *, deadline: float | None = None) -> Iterator[tuple[str, dict[str, Any]]]:
    """Parse bounded UTF-8 SSE events; preserve event boundaries and multiline data."""
    name, data, size = "", [], 0
    while True:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProviderError("deadline_exceeded", "Provider stream deadline exceeded", retryable=True)
            # urllib HTTPResponse exposes its socket under fp.raw._sock. Tighten
            # every blocking read to the *remaining* deadline, not a fresh window.
            raw = getattr(getattr(response, "fp", None), "raw", None)
            sock = getattr(raw, "_sock", None)
            if sock is not None and callable(getattr(sock, "settimeout", None)):
                sock.settimeout(remaining)
        raw = response.readline(1_048_577)
        if deadline is not None and time.monotonic() >= deadline:
            raise ProviderError("deadline_exceeded", "Provider stream deadline exceeded", retryable=True)
        if not raw:
            return  # Unterminated event is deliberately discarded.
        size += len(raw)
        if size > 1_048_576:
            raise ProviderProtocolError("invalid_stream", "SSE event exceeds the size limit")
        try:
            line = raw.decode("utf-8").rstrip("\r\n")
        except UnicodeError:
            raise ProviderProtocolError("invalid_stream", "SSE is not UTF-8") from None
        if not line:
            if data:
                try:
                    value = json.loads("\n".join(data))
                except ValueError:
                    raise ProviderProtocolError("invalid_stream", "SSE data is not JSON") from None
                if not isinstance(value, dict):
                    raise ProviderProtocolError("invalid_stream", "SSE data must be an object")
                yield name, value
            name, data, size = "", [], 0
        elif line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip(" "))
