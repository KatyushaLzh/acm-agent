"""Allowlisted, anonymous source discovery for AI-assisted stress references."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from html import unescape
from html.parser import HTMLParser
import hashlib
import ipaddress
import re
import socket
import time
from typing import Any, Callable, Mapping, Protocol, Sequence
from urllib.parse import parse_qs, quote_plus, urlencode, urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from urllib.robotparser import RobotFileParser


USER_AGENT = "acm-agent/4.0 (+local stress source discovery)"
MAX_PAGES = 12
MAX_TOTAL_BYTES = 2 * 1024 * 1024
MAX_PAGE_BYTES = 512 * 1024
ALLOWED_HOSTS = frozenset(
    {
        "codeforces.com",
        "www.codeforces.com",
        "www.luogu.com.cn",
        "luogu.com.cn",
        "zzk.cnblogs.com",
        "www.cnblogs.com",
        "so.csdn.net",
        "blog.csdn.net",
    }
)
SEARCH_HOSTS = frozenset({"html.duckduckgo.com"})
TIER_HOSTS = {
    "luogu_solutions": frozenset({"www.luogu.com.cn", "luogu.com.cn"}),
    "cnblogs": frozenset({"www.cnblogs.com"}),
    "csdn": frozenset({"blog.csdn.net"}),
}
# Every tier name the crawler accepts.  This is a validation set, not an
# execution order: use source_order_for_platform() for the order actually tried.
SOURCE_ORDER = ("codeforces_official", "luogu_solutions", "cnblogs", "csdn")

# The single source of truth for reference-lookup order, per platform.
#
# Luogu's solution index consumes most of the bounded crawl-page budget (index
# page + article pages + AI audits) before a source is even selected, so a
# complete allowlisted cnblogs source is preferred first; it still passes the
# identical safety, dual-build, oracle, sample and full-preflight gates.  A
# Codeforces problem has no Luogu editorial to read, so that tier is skipped.
PLATFORM_SOURCE_ORDER: dict[str, tuple[str, ...]] = {
    "codeforces": ("codeforces_official", "cnblogs", "csdn"),
    "luogu": ("cnblogs", "luogu_solutions", "csdn"),
}
DEFAULT_SOURCE_ORDER = PLATFORM_SOURCE_ORDER["luogu"]

# Display labels for the same tiers, plus the terminal generated fallback.
SOURCE_TIER_LABELS: dict[str, str] = {
    "codeforces_official": "Codeforces 官方题解",
    "luogu_solutions": "洛谷题解",
    "cnblogs": "博客园",
    "csdn": "CSDN",
}
GENERATED_SOURCE_LABEL = "DeepSeek 生成"


def source_order_for_platform(platform: str) -> tuple[str, ...]:
    """Return the tier order actually attempted for ``platform``."""

    return PLATFORM_SOURCE_ORDER.get(str(platform).strip().casefold(), DEFAULT_SOURCE_ORDER)


def source_order_labels(platform: str) -> list[str]:
    """Return display labels for ``platform``'s real order, generation last."""

    labels = [
        SOURCE_TIER_LABELS.get(tier, tier)
        for tier in source_order_for_platform(platform)
    ]
    labels.append(GENERATED_SOURCE_LABEL)
    return labels


_CHALLENGE_MARKERS = (
    "captcha",
    "verify you are human",
    "访问验证",
    "安全验证",
    "cloudflare ray id",
    "bots use duckduckgo too",
    "anomaly-modal__modal",
)


class SourceSearchError(RuntimeError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class FetchTransport(Protocol):
    def __call__(self, request: Request, timeout: float) -> Any: ...


@dataclass(frozen=True, slots=True)
class SourceCandidate:
    candidate_id: str
    tier: str
    url: str
    title: str
    excerpt: str
    code: str | None
    content_sha256: str
    complete_cpp: bool
    license: str | None = None
    static_audit: dict[str, Any] | None = None

    def to_dict(self, *, include_content: bool = True) -> dict[str, Any]:
        payload = asdict(self)
        if not include_content:
            payload.pop("excerpt", None)
            payload.pop("code", None)
        return payload


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.text_parts: list[str] = []
        self.links: list[tuple[str, str]] = []
        self.code_blocks: list[str] = []
        self._title = False
        self._script = False
        self._style = False
        self._anchor: str | None = None
        self._anchor_text: list[str] = []
        self._code_depth = 0
        self._code_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): value or "" for key, value in attrs}
        lowered = tag.casefold()
        if lowered == "title":
            self._title = True
        elif lowered == "script":
            self._script = True
        elif lowered == "style":
            self._style = True
        elif lowered == "a":
            self._anchor = values.get("href") or None
            self._anchor_text = []
        elif lowered in {"pre", "code"}:
            self._code_depth += 1
            if self._code_depth == 1:
                self._code_text = []

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.casefold()
        if lowered == "title":
            self._title = False
        elif lowered == "script":
            self._script = False
        elif lowered == "style":
            self._style = False
        elif lowered == "a":
            if self._anchor:
                self.links.append((self._anchor, " ".join(self._anchor_text).strip()))
            self._anchor = None
            self._anchor_text = []
        elif lowered in {"pre", "code"} and self._code_depth:
            self._code_depth -= 1
            if self._code_depth == 0:
                block = "".join(self._code_text).strip()
                if block:
                    self.code_blocks.append(block)
                self._code_text = []

    def handle_data(self, data: str) -> None:
        if self._script or self._style:
            return
        if self._title:
            self.title_parts.append(data)
        if self._anchor is not None:
            self._anchor_text.append(data)
        if self._code_depth:
            self._code_text.append(data)
        self.text_parts.append(data)


def _is_complete_cpp(value: str) -> bool:
    return bool(
        re.search(r"#\s*include\s*[<\"]", value)
        and re.search(r"\b(?:int|signed)\s+main\s*\(", value)
    )


def _public_https_url(
    url: str,
    *,
    resolver: Callable[..., Sequence[Any]] = socket.getaddrinfo,
) -> str:
    parsed = urlsplit(str(url))
    host = (parsed.hostname or "").casefold()
    if (
        parsed.scheme != "https"
        or host not in ALLOWED_HOSTS | SEARCH_HOSTS
        or parsed.username
        or parsed.password
    ):
        raise SourceSearchError("source_url_rejected", "Source URL is not allowlisted HTTPS")
    try:
        addresses = resolver(host, parsed.port or 443, type=socket.SOCK_STREAM)
    except OSError as exc:
        raise SourceSearchError("source_dns_failed", f"Source host lookup failed: {exc}") from None
    if not addresses:
        raise SourceSearchError("source_dns_failed", "Source host returned no addresses")
    for item in addresses:
        address = ipaddress.ip_address(item[4][0])
        if not address.is_global:
            raise SourceSearchError("source_url_rejected", "Source host resolved to a non-public address")
    return parsed.geturl()


class _SafeRedirect(HTTPRedirectHandler):
    def __init__(self, validator: Callable[[str], str]) -> None:
        super().__init__()
        self._validator = validator

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        self._validator(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class AllowlistedCrawler:
    def __init__(
        self,
        *,
        transport: FetchTransport | None = None,
        resolver: Callable[..., Sequence[Any]] = socket.getaddrinfo,
        timeout: float = 15.0,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        deadline: float | None = None,
        min_host_interval: float = 1.0,
        cancel_scope: Any | None = None,
    ) -> None:
        self._resolver = resolver
        self._validate = lambda value: _public_https_url(value, resolver=self._resolver)
        if transport is None:
            opener = build_opener(_SafeRedirect(self._validate))
            self._transport = lambda request, timeout: opener.open(request, timeout=timeout)
        else:
            self._transport = transport
        self.timeout = float(timeout)
        self._sleep = sleep
        self._clock = clock
        self.deadline = deadline
        self.cancel_scope = cancel_scope
        self._interval = max(0.0, float(min_host_interval))
        self._last_request: dict[str, float] = {}
        self._robots: dict[str, RobotFileParser | None] = {}
        self.pages = 0
        self.bytes = 0
        self._candidates: dict[str, SourceCandidate] = {}

    def _check_cancelled(self) -> None:
        scope = self.cancel_scope
        if scope is None:
            return
        checker = getattr(scope, "raise_if_cancelled", None)
        try:
            if callable(checker):
                checker()
            elif bool(getattr(scope, "cancelled", False)):
                raise RuntimeError("cancelled")
        except Exception as exc:
            if str(getattr(exc, "code", "")) == "request_cancelled" or bool(
                getattr(scope, "cancelled", False)
            ):
                raise SourceSearchError(
                    "request_cancelled", "Source crawl was cancelled"
                ) from None
            raise

    def _read(
        self,
        url: str,
        *,
        robots: bool = True,
        form: Mapping[str, str] | None = None,
    ) -> tuple[str, str]:
        self._check_cancelled()
        safe_url = self._validate(url)
        host = (urlsplit(safe_url).hostname or "").casefold()
        if robots and not self._robots_allowed(safe_url):
            raise SourceSearchError("robots_denied", "Source robots policy disallows this URL")
        if self.pages >= MAX_PAGES:
            raise SourceSearchError("source_page_limit", "Source page limit reached")
        remaining = (
            float(self.deadline) - float(self._clock())
            if self.deadline is not None
            else float("inf")
        )
        if remaining <= 0:
            raise SourceSearchError(
                "stress_prepare_budget_exhausted",
                "Source crawl exceeded the stress preparation deadline",
            )
        elapsed = self._clock() - self._last_request.get(host, 0.0)
        if elapsed < self._interval:
            delay = self._interval - elapsed
            if delay >= remaining:
                raise SourceSearchError(
                    "stress_prepare_budget_exhausted",
                    "Source crawl rate limit would exceed the preparation deadline",
                )
            event = getattr(self.cancel_scope, "event", None)
            if event is not None and callable(getattr(event, "wait", None)):
                event.wait(delay)
                self._check_cancelled()
            else:
                self._sleep(delay)
        request = Request(
            safe_url,
            data=(urlencode(dict(form)).encode("utf-8") if form is not None else None),
            headers={"Accept": "text/html,application/xhtml+xml", "User-Agent": USER_AGENT},
        )
        response = None
        try:
            request_timeout = min(self.timeout, max(0.1, remaining))
            response = self._transport(request, request_timeout)
            register = getattr(self.cancel_scope, "register_response", None)
            if callable(register):
                register(response)
            self._check_cancelled()
            final_url = self._validate(getattr(response, "geturl", lambda: safe_url)())
            body = response.read(MAX_PAGE_BYTES + 1)
            self._check_cancelled()
        except SourceSearchError:
            raise
        except Exception as exc:
            self._check_cancelled()
            raise SourceSearchError("source_fetch_failed", f"Source fetch failed: {exc}") from None
        finally:
            unregister = getattr(self.cancel_scope, "unregister_response", None)
            if response is not None and callable(unregister):
                unregister(response)
            close = getattr(response, "close", None)
            if callable(close):
                close()
            self._last_request[host] = self._clock()
        if len(body) > MAX_PAGE_BYTES or self.bytes + len(body) > MAX_TOTAL_BYTES:
            raise SourceSearchError("source_content_limit", "Source content exceeded its byte limit")
        self.pages += 1
        self.bytes += len(body)
        text = body.decode("utf-8-sig", errors="replace")
        lowered = text.casefold()
        if any(marker in lowered for marker in _CHALLENGE_MARKERS):
            raise SourceSearchError("source_challenge", "Source returned an access challenge")
        return final_url, text

    def _robots_allowed(self, url: str) -> bool:
        host = (urlsplit(url).hostname or "").casefold()
        if host not in self._robots:
            parser = RobotFileParser()
            robots_url = f"https://{host}/robots.txt"
            parser.set_url(robots_url)
            try:
                _, text = self._read(robots_url, robots=False)
                parser.parse(text.splitlines())
                self._robots[host] = parser
            except SourceSearchError:
                self._robots[host] = None
        cached = self._robots[host]
        return True if cached is None else cached.can_fetch(USER_AGENT, url)

    @staticmethod
    def _parse(html: str) -> _PageParser:
        parser = _PageParser()
        parser.feed(html)
        parser.close()
        return parser

    def _candidate(self, tier: str, url: str, html: str) -> SourceCandidate:
        parsed = self._parse(html)
        title = " ".join(" ".join(parsed.title_parts).split())[:300]
        excerpt = "\n".join(
            part for part in (" ".join(item.split()) for item in parsed.text_parts) if part
        )[:65536]
        blocks = sorted(parsed.code_blocks, key=len, reverse=True)
        complete = next((unescape(item) for item in blocks if _is_complete_cpp(item)), None)
        digest = hashlib.sha256(html.encode("utf-8")).hexdigest()
        identifier = hashlib.sha256(f"{tier}\0{url}\0{digest}".encode()).hexdigest()[:24]
        candidate = SourceCandidate(
            identifier,
            tier,
            url,
            title,
            excerpt,
            complete,
            digest,
            complete_cpp=complete is not None,
        )
        self._candidates[identifier] = candidate
        return candidate

    def candidate(self, candidate_id: str) -> SourceCandidate:
        try:
            return self._candidates[str(candidate_id)]
        except KeyError:
            raise SourceSearchError("candidate_not_found", "Source candidate was not found") from None

    @staticmethod
    def _search_result_target(
        href: str, tier: str, *, base_url: str = "https://html.duckduckgo.com/html/"
    ) -> str | None:
        absolute = urljoin(base_url, href)
        parsed = urlsplit(absolute)
        if (parsed.hostname or "").casefold() in {
            "duckduckgo.com",
            "www.duckduckgo.com",
            "html.duckduckgo.com",
        }:
            values = parse_qs(parsed.query).get("uddg", [])
            if not values:
                return None
            absolute = values[0]
            parsed = urlsplit(absolute)
        host = (parsed.hostname or "").casefold()
        if parsed.scheme != "https" or host not in TIER_HOSTS[tier]:
            return None
        if tier == "luogu_solutions" and not (
            parsed.path.startswith("/article/")
            or parsed.path.startswith("/problem/solution/")
        ):
            return None
        return parsed.geturl()

    def _public_search_links(
        self,
        tier: str,
        *,
        problem_id: str,
        title: str,
    ) -> list[str]:
        scope = {
            "luogu_solutions": "site:luogu.com.cn/article",
            "cnblogs": "site:www.cnblogs.com",
            "csdn": "site:blog.csdn.net",
        }[tier]
        query = " ".join(
            part for part in (scope, problem_id.strip(), title.strip()) if part
        )
        if tier == "luogu_solutions":
            search_url = (
                "https://www.luogu.com.cn/article?keyword="
                + quote_plus(problem_id.strip())
            )
            _, html = self._read(search_url)
        else:
            search_url = "https://html.duckduckgo.com/html/"
            try:
                _, html = self._read(search_url, form={"q": query})
            except SourceSearchError as exc:
                if exc.code in {
                    "request_cancelled",
                    "stress_prepare_budget_exhausted",
                    "source_page_limit",
                    "source_content_limit",
                }:
                    raise
                search_url = (
                    "https://html.duckduckgo.com/html/?q=" + quote_plus(query)
                )
                _, html = self._read(search_url)
        parser = self._parse(html)
        links: list[str] = []
        for href, _ in parser.links:
            target = self._search_result_target(href, tier, base_url=search_url)
            if target and target not in links:
                links.append(target)
            if len(links) >= 4:
                break
        if not links and tier in {"cnblogs", "csdn"} and title.strip():
            fallback_query = " ".join(
                part for part in (scope, problem_id.strip()) if part
            )
            fallback_url = (
                "https://html.duckduckgo.com/html/?q="
                + quote_plus(fallback_query)
            )
            try:
                _, fallback_html = self._read(fallback_url)
            except SourceSearchError as exc:
                if exc.code in {
                    "request_cancelled",
                    "stress_prepare_budget_exhausted",
                }:
                    raise
            else:
                for href, _ in self._parse(fallback_html).links:
                    target = self._search_result_target(
                        href, tier, base_url=fallback_url
                    )
                    if target and target not in links:
                        links.append(target)
                    if len(links) >= 4:
                        break
        return links

    def search(
        self,
        tier: str,
        *,
        problem_id: str,
        title: str = "",
    ) -> list[SourceCandidate]:
        if tier not in SOURCE_ORDER:
            raise SourceSearchError("source_tier_rejected", "Unknown source tier")
        if tier == "codeforces_official":
            match = re.fullmatch(r"CF(\d+)([A-Z][A-Z0-9]*)", problem_id.upper())
            if not match:
                return []
            page_url = f"https://codeforces.com/contest/{match.group(1)}"
            _, html = self._read(page_url)
            parser = self._parse(html)
            links = [
                urljoin(page_url, href)
                for href, text in parser.links
                if re.search(r"tutorial|editorial|题解", text, re.I)
                and re.search(r"/blog/entry/\d+", href)
            ]
        elif tier == "luogu_solutions":
            if not re.fullmatch(r"P\d+", problem_id.upper()):
                return []
            links = self._public_search_links(
                tier, problem_id=problem_id, title=title
            )
            links.append(
                f"https://www.luogu.com.cn/problem/solution/{problem_id.upper()}"
            )
        elif tier == "cnblogs":
            links = self._public_search_links(
                tier, problem_id=problem_id, title=title
            )
        else:
            links = self._public_search_links(
                tier, problem_id=problem_id, title=title
            )
        results: list[SourceCandidate] = []
        seen: set[str] = set()
        for url in links:
            self._check_cancelled()
            if url in seen or len(results) >= 4:
                continue
            seen.add(url)
            try:
                final_url, html = self._read(url)
            except SourceSearchError:
                continue
            candidate = self._candidate(tier, final_url, html)
            haystack = f"{candidate.title}\n{candidate.excerpt}".casefold()
            exact_problem = problem_id.casefold() in haystack
            if tier == "codeforces_official":
                contest, index = match.group(1), match.group(2)
                page_links = self._parse(html).links
                exact_problem = exact_problem or any(
                    re.fullmatch(
                        rf"/(?:contest/{re.escape(contest)}/problem|problemset/problem/{re.escape(contest)})/{re.escape(index)}",
                        urlsplit(urljoin(final_url, href)).path,
                        re.I,
                    )
                    for href, _ in page_links
                )
            if exact_problem:
                results.append(candidate)
        return results


SEARCH_TOOL = {
    "type": "function",
    "function": {
        "name": "search_source",
        "description": "Search the currently allowed source tier for an exact problem.",
        "parameters": {
            "type": "object",
            "properties": {
                "tier": {"type": "string", "enum": list(SOURCE_ORDER)},
                "problem_id": {"type": "string"},
                "title": {"type": "string"},
            },
            "required": ["tier", "problem_id"],
            "additionalProperties": False,
        },
        "strict": True,
    },
}


__all__ = [
    "ALLOWED_HOSTS",
    "AllowlistedCrawler",
    "DEFAULT_SOURCE_ORDER",
    "GENERATED_SOURCE_LABEL",
    "PLATFORM_SOURCE_ORDER",
    "SEARCH_TOOL",
    "SOURCE_ORDER",
    "SOURCE_TIER_LABELS",
    "SourceCandidate",
    "SourceSearchError",
    "source_order_for_platform",
    "source_order_labels",
]
