"""Anonymous Codeforces and Luogu synchronization.

All network access is injectable.  Tests can pass a callable with the signature
``request(url, params, headers)`` returning a decoded JSON object, text, or bytes.
The default implementation uses only :mod:`urllib.request`.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any, Callable, Iterable, Iterator, Mapping, MutableMapping, Protocol, Sequence

from .storage import Database


CF_BASE = "https://codeforces.com/api"
LUOGU_BASE = "https://www.luogu.com.cn"
CF_THROTTLE_SECONDS = 2.1
LUOGU_CATALOG_WORKERS = 4
LUOGU_TAG_WORKERS = 4
LUOGU_TAG_FAILURES_KEY = "tag_enrichment_failures"
LUOGU_TAGLESS_KEY = "tag_enrichment_tagless"
LUOGU_FULL_CATALOG_MIN_PROBLEMS = 10_000
LUOGU_CHALLENGE_COOKIE_RE = re.compile(
    r'["\'](C3VK=[A-Za-z0-9._~-]{1,128});\s*path=/;\s*max-age=\d+;?["\']',
    re.IGNORECASE,
)


def _report_progress(
    callback: Callable[[Mapping[str, Any]], None] | None,
    **progress: Any,
) -> None:
    if callback is None:
        return
    try:
        callback(progress)
    except Exception:
        # Observability must never turn a successful platform sync into a
        # failed one (for example when a web client disconnects).
        return


class PlatformError(RuntimeError):
    """A remote response is unavailable or violates its expected contract."""


class RemoteAPIError(PlatformError):
    pass


class ResponseShapeError(PlatformError):
    pass


def _luogu_challenge_cookie(payload: Any) -> str | None:
    """Return the narrowly-scoped Luogu JS challenge cookie, if present.

    Luogu may answer an anonymous request with a tiny script that only sets a
    short-lived ``C3VK`` cookie and reloads the same URL.  The client must not
    execute arbitrary JavaScript, so accept only that exact cookie contract.
    """

    if isinstance(payload, bytes):
        payload = payload.decode("utf-8-sig", errors="replace")
    if not isinstance(payload, str) or len(payload) > 4096:
        return None
    lowered = payload.lower()
    if "<script" not in lowered or "window.open(" not in lowered or "c3vk=" not in lowered:
        return None
    match = LUOGU_CHALLENGE_COOKIE_RE.search(payload)
    return match.group(1) if match else None


class RequestFunction(Protocol):
    def __call__(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any: ...


def _decode_json(payload: Any) -> Any:
    if isinstance(payload, (dict, list)):
        return payload
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8-sig")
    if isinstance(payload, str):
        try:
            return json.loads(payload)
        except json.JSONDecodeError as exc:
            raise ResponseShapeError(f"response is not valid JSON: {exc}") from exc
    raise ResponseShapeError(f"unsupported response type: {type(payload).__name__}")


class HttpTransport:
    """Minimal retrying HTTP transport, suitable for both platform clients."""

    def __init__(
        self,
        *,
        timeout: float = 20.0,
        retries: int = 2,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.timeout = timeout
        self.retries = retries
        self.sleep = sleep

    def __call__(
        self,
        url: str,
        params: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        if params:
            query = urllib.parse.urlencode(params)
            url = f"{url}{'&' if '?' in url else '?'}{query}"
        request = urllib.request.Request(
            url,
            headers={
                "Accept": "application/json,text/html;q=0.9,*/*;q=0.8",
                "User-Agent": "acm-agent/1.0 (+local learning tool)",
                **dict(headers or {}),
            },
        )
        last_error: BaseException | None = None
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = response.read()
                    content_type = response.headers.get("Content-Type", "")
                if "json" in content_type.lower():
                    return _decode_json(body)
                return body.decode("utf-8-sig")
            except urllib.error.HTTPError as exc:
                last_error = exc
                if exc.code not in (429, 500, 502, 503, 504) or attempt == self.retries:
                    break
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                delay = float(retry_after) if retry_after and retry_after.isdigit() else 2**attempt
                self.sleep(delay)
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = exc
                if attempt == self.retries:
                    break
                self.sleep(2**attempt)
        raise PlatformError(f"request failed for {url}: {last_error}") from last_error


@dataclass(slots=True)
class SyncResult:
    platform: str
    status: str
    submissions: int = 0
    accepted: int = 0
    problems: int = 0
    error: str | None = None
    warnings: list[str] = field(default_factory=list)
    tag_enrichment: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "status": self.status,
            "submissions": self.submissions,
            "accepted": self.accepted,
            "problems": self.problems,
            "error": self.error,
            "warnings": self.warnings,
            "tag_enrichment": self.tag_enrichment,
        }


class CodeforcesClient:
    def __init__(
        self,
        request: RequestFunction | None = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
        throttle_seconds: float = CF_THROTTLE_SECONDS,
    ):
        self.request = request or HttpTransport(sleep=sleep)
        self.sleep = sleep
        self.monotonic = monotonic
        self.throttle_seconds = throttle_seconds
        self._last_request_at: float | None = None

    def _call(self, method: str, **params: Any) -> Any:
        now = self.monotonic()
        if self._last_request_at is not None:
            remaining = self.throttle_seconds - (now - self._last_request_at)
            if remaining > 0:
                self.sleep(remaining)
        try:
            payload = self.request(f"{CF_BASE}/{method}", params, None)
        finally:
            self._last_request_at = self.monotonic()
        data = _decode_json(payload)
        if not isinstance(data, Mapping):
            raise ResponseShapeError(f"Codeforces {method}: expected object")
        if data.get("status") != "OK":
            comment = data.get("comment") or "unknown Codeforces API failure"
            raise RemoteAPIError(f"Codeforces {method} FAILED: {comment}")
        if "result" not in data:
            raise ResponseShapeError(f"Codeforces {method}: missing result")
        return data["result"]

    def user_info(self, handle: str) -> Mapping[str, Any]:
        result = self._call("user.info", handles=handle, checkHistoricHandles="false")
        if not isinstance(result, list) or len(result) != 1 or not isinstance(result[0], Mapping):
            raise ResponseShapeError("Codeforces user.info returned no unique user")
        return result[0]

    def user_status_page(self, handle: str, start: int, count: int) -> list[Mapping[str, Any]]:
        result = self._call("user.status", handle=handle, **{"from": start}, count=count)
        if not isinstance(result, list) or not all(isinstance(x, Mapping) for x in result):
            raise ResponseShapeError("Codeforces user.status returned a non-list result")
        return list(result)

    def new_submissions(
        self,
        handle: str,
        known_ids: set[str] | None = None,
        *,
        page_size: int = 1000,
        max_pages: int | None = None,
    ) -> list[Mapping[str, Any]]:
        """Read newest-first pages, stopping at the first already known id."""
        known = known_ids or set()
        fetched: list[Mapping[str, Any]] = []
        start = 1
        pages = 0
        while True:
            page = self.user_status_page(handle, start, page_size)
            pages += 1
            reached_known = False
            for submission in page:
                sid = str(submission.get("id", ""))
                if not sid:
                    raise ResponseShapeError("Codeforces submission has no id")
                if sid in known:
                    reached_known = True
                    break
                fetched.append(submission)
            if reached_known or len(page) < page_size:
                break
            if max_pages is not None and pages >= max_pages:
                raise PlatformError("Codeforces submission pagination limit reached")
            start += len(page)
        return fetched

    def problemset(self) -> tuple[list[Mapping[str, Any]], list[Mapping[str, Any]]]:
        result = self._call("problemset.problems")
        if not isinstance(result, Mapping):
            raise ResponseShapeError("Codeforces problemset.problems returned non-object")
        problems = result.get("problems")
        stats = result.get("problemStatistics", [])
        if not isinstance(problems, list) or not all(isinstance(x, Mapping) for x in problems):
            raise ResponseShapeError("Codeforces problemset has invalid problems")
        if not isinstance(stats, list):
            raise ResponseShapeError("Codeforces problemset has invalid statistics")
        return list(problems), list(stats)


def cf_problem_id(problem: Mapping[str, Any]) -> str:
    contest = problem.get("contestId")
    index = problem.get("index")
    if contest is None or not isinstance(index, str) or not index:
        raise ResponseShapeError("Codeforces problem is missing contestId/index")
    return f"{contest}{index}"


def _cf_problem(problem: Mapping[str, Any], stat: Mapping[str, Any] | None = None) -> dict[str, Any]:
    problem_id = cf_problem_id(problem)
    contest = problem["contestId"]
    index = problem["index"]
    source = dict(problem)
    if stat and "solvedCount" in stat:
        source["solvedCount"] = stat["solvedCount"]
    return {
        "platform": "codeforces",
        "problem_id": problem_id,
        "name": problem.get("name"),
        "url": f"https://codeforces.com/problemset/problem/{contest}/{index}",
        "rating": problem.get("rating"),
        "difficulty": None,
        "tags": list(problem.get("tags") or []),
        "source": source,
    }


def _cf_submission(submission: Mapping[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    problem_raw = submission.get("problem")
    if not isinstance(problem_raw, Mapping):
        raise ResponseShapeError("Codeforces submission has no problem")
    problem = _cf_problem(problem_raw)
    seconds = submission.get("creationTimeSeconds")
    submitted_at = None
    if isinstance(seconds, (int, float)):
        submitted_at = datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="seconds")
    row = {
        "platform": "codeforces",
        "submission_id": str(submission["id"]),
        "problem_id": problem["problem_id"],
        "verdict": submission.get("verdict"),
        "submitted_at": submitted_at,
        "language": submission.get("programmingLanguage"),
        "raw": dict(submission),
    }
    return problem, row


class _LentilleParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.capture = False
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = dict(attrs)
        if tag.lower() == "script" and values.get("id") == "lentille-context":
            self.capture = True

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() == "script" and self.capture:
            self.capture = False

    def handle_data(self, data: str) -> None:
        if self.capture:
            self.parts.append(data)


def _find_named(node: Any, wanted: str) -> Any | None:
    if isinstance(node, Mapping):
        if wanted in node:
            return node[wanted]
        for value in node.values():
            found = _find_named(value, wanted)
            if found is not None:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_named(value, wanted)
            if found is not None:
                return found
    return None


def parse_lentille_context(payload: Any) -> Mapping[str, Any]:
    """Extract the Luogu ``lentille-context`` object from JSON or HTML."""
    candidate: Any = payload
    if isinstance(payload, bytes):
        candidate = payload.decode("utf-8-sig")
    if isinstance(candidate, str):
        stripped = candidate.lstrip()
        if stripped.startswith(("{", "[")):
            candidate = _decode_json(candidate)
        else:
            parser = _LentilleParser()
            parser.feed(candidate)
            if not parser.parts:
                raise ResponseShapeError("Luogu response has no lentille-context script")
            candidate = _decode_json("".join(parser.parts))
    named = _find_named(candidate, "lentille-context")
    if named is not None:
        candidate = _decode_json(named) if isinstance(named, (str, bytes)) else named
    if not isinstance(candidate, Mapping):
        raise ResponseShapeError("Luogu lentille-context is not an object")
    return candidate


def parse_luogu_passed(payload: Any) -> set[str]:
    context = parse_lentille_context(payload)
    data = context.get("data")
    if not isinstance(data, Mapping):
        data = context.get("currentData")
    if not isinstance(data, Mapping):
        raise ResponseShapeError("Luogu lentille-context.data is missing")
    passed = data.get("passed")
    if passed is None and isinstance(data.get("currentData"), Mapping):
        passed = data["currentData"].get("passed")
    if isinstance(passed, Mapping):
        values: Iterable[Any] = passed.keys()
    elif isinstance(passed, list):
        values = passed
    else:
        raise ResponseShapeError("Luogu passed list is hidden or missing")
    result: set[str] = set()
    for item in values:
        # The live page currently returns objects such as
        # {"pid":"P3372","name":"...","difficulty":4}.  Older fixtures and
        # deployments have also exposed a string list or an id-keyed mapping.
        pid = item.get("pid") if isinstance(item, Mapping) else item
        if isinstance(pid, str) and pid.startswith("P"):
            result.add(pid)
    if passed and not result:
        raise ResponseShapeError("Luogu passed list has an unexpected shape")
    return result


def parse_luogu_user(payload: Any, expected_uid: str | int | None = None) -> Mapping[str, Any]:
    context = parse_lentille_context(payload)
    wanted = str(expected_uid) if expected_uid is not None else None
    candidates: list[Mapping[str, Any]] = []
    for node in _walk(context):
        if not isinstance(node, Mapping):
            continue
        uid = node.get("uid")
        if uid is None:
            continue
        if wanted is None or str(uid) == wanted:
            candidates.append(node)
    if not candidates:
        raise ResponseShapeError("Luogu public profile is hidden or has no matching uid")
    return candidates[0]


def _walk(node: Any) -> Iterator[Any]:
    yield node
    if isinstance(node, Mapping):
        for value in node.values():
            yield from _walk(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk(value)


def parse_luogu_tags(payload: Any) -> dict[int, str]:
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8-sig")
    if isinstance(payload, str) and payload.lstrip().startswith(("{", "[")):
        payload = _decode_json(payload)
    try:
        context: Any = parse_lentille_context(payload)
    except ResponseShapeError:
        context = payload
    result: dict[int, str] = {}
    for node in _walk(context):
        if not isinstance(node, Mapping):
            continue
        raw_id = node.get("id")
        name = node.get("name")
        if isinstance(raw_id, int) and isinstance(name, str) and name:
            result[raw_id] = name
    if not result:
        raise ResponseShapeError("Luogu tag dictionary is missing or empty")
    return result


def parse_luogu_problems(payload: Any, tag_names: Mapping[int, str] | None = None) -> list[dict[str, Any]]:
    context = parse_lentille_context(payload)
    found = _parse_luogu_problem_nodes(context, tag_names)
    if not found:
        raise ResponseShapeError("Luogu problem list contains no recognizable problems")
    return [found[key] for key in sorted(found)]


def _parse_luogu_problem_nodes(
    payload: Any, tag_names: Mapping[int, str] | None = None
) -> dict[str, dict[str, Any]]:
    """Parse supported public Luogu P-series rows without requiring a match."""

    found: dict[str, dict[str, Any]] = {}
    for node in _walk(payload):
        if not isinstance(node, Mapping):
            continue
        pid = node.get("pid")
        if not isinstance(pid, str) or not pid.startswith("P"):
            continue
        raw_tags = node.get("tags") or []
        tags: list[str] = []
        if isinstance(raw_tags, list):
            for item in raw_tags:
                if isinstance(item, str):
                    tags.append(item)
                elif isinstance(item, int):
                    tags.append((tag_names or {}).get(item, str(item)))
                elif isinstance(item, Mapping) and isinstance(item.get("name"), str):
                    tags.append(item["name"])
        found[pid] = {
            "platform": "luogu",
            "problem_id": pid,
            "name": node.get("title") or node.get("name"),
            "url": f"https://www.luogu.com.cn/problem/{pid}",
            "difficulty": node.get("difficulty"),
            "rating": None,
            "tags": tags,
            "source": dict(node),
        }
    return found


def parse_luogu_problem_page(
    payload: Any, tag_names: Mapping[int, str] | None = None
) -> dict[str, Any]:
    """Parse one unfiltered problem-list page and its pagination contract."""

    context = parse_lentille_context(payload)
    candidates: list[Mapping[str, Any]] = []
    for node in _walk(context):
        if not isinstance(node, Mapping) or not isinstance(node.get("result"), list):
            continue
        count = node.get("count")
        per_page = node.get("perPage")
        if (
            isinstance(count, int)
            and not isinstance(count, bool)
            and isinstance(per_page, int)
            and not isinstance(per_page, bool)
        ):
            candidates.append(node)
    if len(candidates) != 1:
        raise ResponseShapeError(
            f"Luogu problem list returned {len(candidates)} pagination objects"
        )
    page = candidates[0]
    count = int(page["count"])
    per_page = int(page["perPage"])
    if count < 0 or per_page <= 0:
        raise ResponseShapeError("Luogu problem-list pagination is invalid")
    raw_rows = page["result"]
    if any(not isinstance(row, Mapping) for row in raw_rows):
        raise ResponseShapeError("Luogu problem-list result contains a non-object row")
    found = _parse_luogu_problem_nodes(raw_rows, tag_names)
    return {
        "problems": [found[key] for key in sorted(found)],
        "count": count,
        "per_page": per_page,
        "raw_count": len(raw_rows),
    }


def parse_luogu_problem(
    payload: Any,
    problem_id: str,
    tag_names: Mapping[int, str] | None = None,
) -> dict[str, Any]:
    """Parse exactly one requested pid from a public problem-page payload."""
    problem_id = str(problem_id).strip().upper()
    context = parse_lentille_context(payload)
    matches = [
        node for node in _walk(context)
        if isinstance(node, Mapping) and node.get("pid") == problem_id
    ]
    if len(matches) != 1:
        raise ResponseShapeError(
            f"Luogu problem page for {problem_id} returned {len(matches)} exact matches"
        )
    node = matches[0]
    raw_tags = node.get("tags") or []
    if not isinstance(raw_tags, list):
        raise ResponseShapeError(f"Luogu problem {problem_id} tags are not an array")
    tags: list[str] = []
    for item in raw_tags:
        if isinstance(item, str):
            tags.append(item)
        elif isinstance(item, int):
            name = (tag_names or {}).get(item)
            if not name:
                raise ResponseShapeError(
                    f"Luogu problem {problem_id} references unknown tag id {item}"
                )
            tags.append(name)
        elif isinstance(item, Mapping) and isinstance(item.get("name"), str):
            tags.append(item["name"])
        else:
            raise ResponseShapeError(f"Luogu problem {problem_id} has an invalid tag item")
    return {
        "platform": "luogu",
        "problem_id": problem_id,
        "name": node.get("title") or node.get("name"),
        "url": f"https://www.luogu.com.cn/problem/{problem_id}",
        "difficulty": node.get("difficulty"),
        "rating": None,
        "tags": tags,
        "source": dict(node),
    }


class LuoguClient:
    def __init__(self, request: RequestFunction | None = None):
        self.request = request or HttpTransport()
        self._cookie_lock = threading.Lock()
        self._challenge_cookie: str | None = None

    def _get(self, path: str, **params: Any) -> Any:
        url = f"{LUOGU_BASE}{path}"
        for challenge_attempt in range(2):
            with self._cookie_lock:
                cookie = self._challenge_cookie
            headers = {"Referer": LUOGU_BASE + "/"}
            if cookie:
                headers["Cookie"] = cookie
            payload = self.request(url, params, headers)
            challenge_cookie = _luogu_challenge_cookie(payload)
            if challenge_cookie is None:
                return payload
            with self._cookie_lock:
                self._challenge_cookie = challenge_cookie
            if challenge_attempt == 1:
                break
        raise RemoteAPIError("Luogu JavaScript cookie challenge persisted after retry")

    def practice(self, uid: str | int) -> set[str]:
        return parse_luogu_passed(self._get(f"/user/{uid}/practice", _contentOnly=1))

    def user_info(self, uid: str | int) -> Mapping[str, Any]:
        return parse_luogu_user(self._get(f"/user/{uid}", _contentOnly=1), uid)

    def tags(self) -> dict[int, str]:
        return parse_luogu_tags(self._get("/_lfe/tags/zh-CN"))

    def problem_page(
        self,
        *,
        page: int = 1,
        difficulty: int | None = None,
        tag: int | str | None = None,
        tag_names: Mapping[int, str] | None = None,
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"page": page, "_contentOnly": 1}
        if difficulty is not None:
            params["difficulty"] = difficulty
        if tag is not None:
            params["tag"] = tag
        return parse_luogu_problems(self._get("/problem/list", **params), tag_names)

    def problem_page_info(
        self,
        *,
        page: int = 1,
        tag_names: Mapping[int, str] | None = None,
    ) -> dict[str, Any]:
        return parse_luogu_problem_page(
            self._get("/problem/list", page=page, _contentOnly=1),
            tag_names,
        )

    def all_problems(
        self,
        *,
        tag_names: Mapping[int, str] | None = None,
        max_pages: int = 1000,
        workers: int = LUOGU_CATALOG_WORKERS,
        progress_callback: Callable[[Mapping[str, Any]], None] | None = None,
    ) -> list[dict[str, Any]]:
        """Fetch every page of the public catalog, retaining supported P IDs."""

        first = self.problem_page_info(page=1, tag_names=tag_names)
        if first["count"] and first["raw_count"] == 0:
            raise ResponseShapeError("Luogu catalog page 1 is unexpectedly empty")
        total_pages = max(1, (first["count"] + first["per_page"] - 1) // first["per_page"])
        if total_pages > max_pages:
            raise ResponseShapeError(
                f"Luogu catalog requires {total_pages} pages, above limit {max_pages}"
            )
        found = {
            row["problem_id"]: row
            for row in first["problems"]
        }
        _report_progress(
            progress_callback,
            phase="catalog",
            platform="luogu",
            step=1,
            total=total_pages,
            completed=1,
            failed=0,
            message=f"洛谷题库 1/{total_pages} 页",
            usable=False,
        )

        def fetch_page(page_number: int) -> tuple[int, dict[str, Any]]:
            return page_number, self.problem_page_info(
                page=page_number, tag_names=tag_names
            )

        page_numbers = list(range(2, total_pages + 1))
        # Small catalogs stay deterministic for fixtures and cheap manual
        # queries. Real full catalogs use bounded concurrency so a slow page
        # does not serialize hundreds of independent HTTP round trips.
        if len(page_numbers) >= 7 and workers > 1:
            executor = ThreadPoolExecutor(
                max_workers=min(max(1, int(workers)), LUOGU_CATALOG_WORKERS),
                thread_name_prefix="acm-luogu-catalog",
            )
            try:
                page_results = executor.map(fetch_page, page_numbers)
                for page_number, current in page_results:
                    if current["raw_count"] == 0:
                        raise ResponseShapeError(
                            f"Luogu catalog page {page_number}/{total_pages} is unexpectedly empty"
                        )
                    for row in current["problems"]:
                        found[row["problem_id"]] = row
                    _report_progress(
                        progress_callback,
                        phase="catalog",
                        platform="luogu",
                        step=page_number,
                        total=total_pages,
                        completed=page_number,
                        failed=0,
                        message=f"洛谷题库 {page_number}/{total_pages} 页",
                        usable=False,
                    )
            finally:
                executor.shutdown(wait=True, cancel_futures=True)
        else:
            for page_number in page_numbers:
                _, current = fetch_page(page_number)
                if current["raw_count"] == 0:
                    raise ResponseShapeError(
                        f"Luogu catalog page {page_number}/{total_pages} is unexpectedly empty"
                    )
                for row in current["problems"]:
                    found[row["problem_id"]] = row
                _report_progress(
                    progress_callback,
                    phase="catalog",
                    platform="luogu",
                    step=page_number,
                    total=total_pages,
                    completed=page_number,
                    failed=0,
                    message=f"洛谷题库 {page_number}/{total_pages} 页",
                    usable=False,
                )
        return [found[key] for key in sorted(found)]

    def problem_by_keyword(
        self, problem_id: str, *, tag_names: Mapping[int, str] | None = None
    ) -> dict[str, Any]:
        """Resolve one public problem-list result with an exact pid guard."""
        problem_id = str(problem_id).strip().upper()
        rows = parse_luogu_problems(
            self._get(
                "/problem/list",
                page=1,
                keyword=problem_id,
                _contentOnly=1,
            ),
            tag_names,
        )
        exact = [row for row in rows if row["problem_id"] == problem_id]
        if len(exact) != 1:
            raise ResponseShapeError(
                f"Luogu keyword query for {problem_id} returned {len(exact)} exact matches"
            )
        return exact[0]

    def problem(
        self, problem_id: str, *, tag_names: Mapping[int, str] | None = None
    ) -> dict[str, Any]:
        """Read a public problem page and require exactly one matching pid."""
        problem_id = str(problem_id).strip().upper()
        return parse_luogu_problem(
            self.problem_payload(problem_id),
            problem_id,
            tag_names,
        )

    def problem_payload(self, problem_id: str) -> Any:
        """Return the anonymous public payload without interpreting numeric tags."""
        problem_id = str(problem_id).strip().upper()
        return self._get(f"/problem/{problem_id}", _contentOnly=1)


def _normalise_preview_tags(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        tag = " ".join(item.split())
        folded = tag.casefold()
        if tag and folded not in seen:
            seen.add(folded)
            result.append(tag)
    return result


LUOGU_TAG_ENRICHMENT_CURSOR_KEY = "tag_enrichment_cursor"
LUOGU_TAG_ENRICHMENT_MAX_BATCH = 50
LUOGU_TAG_ENRICHMENT_CATALOG_FAILURE_BATCH = 10


def _sync_metadata(db: Database, platform: str) -> dict[str, Any]:
    state = db.sync_state(platform)
    if not state:
        return {}
    try:
        decoded = json.loads(state["metadata_json"] or "{}")
    except (json.JSONDecodeError, KeyError, TypeError):
        return {}
    return dict(decoded) if isinstance(decoded, Mapping) else {}


def _luogu_accepted_problems_missing_tags(db: Database) -> list[str]:
    """Return distinct public ACs that have neither raw nor effective tags."""

    rows = db.query(
        """SELECT DISTINCT s.problem_id,p.tags_json
           FROM submissions AS s
           JOIN problems AS p
             ON p.platform=s.platform AND p.problem_id=s.problem_id
           WHERE s.platform='luogu' AND UPPER(TRIM(COALESCE(s.verdict,'')))='AC'
           ORDER BY s.problem_id"""
    )
    missing: list[str] = []
    for row in rows:
        try:
            raw_tags = _normalise_preview_tags(json.loads(row["tags_json"] or "[]"))
        except (json.JSONDecodeError, TypeError):
            raw_tags = []
        problem_id = str(row["problem_id"]).strip().upper()
        if not raw_tags and not db.effective_problem_tags("luogu", problem_id):
            missing.append(problem_id)
    return missing


def _rotated_problem_batch(
    problem_ids: Sequence[str], cursor: str | None, limit: int
) -> list[str]:
    if not problem_ids or limit <= 0:
        return []
    start = 0
    if cursor:
        cursor = str(cursor).strip().upper()
        start = next(
            (index for index, problem_id in enumerate(problem_ids) if problem_id > cursor),
            0,
        )
    rotated = list(problem_ids[start:]) + list(problem_ids[:start])
    return rotated[:limit]


def _safe_platform_error(exc: BaseException) -> str:
    """Keep per-problem diagnostics compact and suitable for API responses."""

    message = " ".join(str(exc).split()) or exc.__class__.__name__
    return message[:300]


def enrich_luogu_accepted_problem_tags(
    db: Database,
    client: LuoguClient | None = None,
    *,
    batch_size: int = LUOGU_TAG_ENRICHMENT_MAX_BATCH,
    full: bool = False,
    workers: int = LUOGU_TAG_WORKERS,
    now: datetime | None = None,
    progress_callback: Callable[[Mapping[str, Any]], None] | None = None,
) -> dict[str, Any]:
    """Incrementally cache public tags for accepted Luogu problems.

    Selection is deterministic and rotates after the last attempted pid.
    ``full=True`` attempts the complete unresolved set in one call; otherwise
    the legacy incremental path remains capped at 50.  A malformed or
    unavailable problem page affects only that pid; the cursor is still
    advanced so one bad page cannot starve the rest of the accepted set.
    """

    client = client or LuoguClient()
    now = now or datetime.now(timezone.utc)
    metadata = _sync_metadata(db, "luogu")
    cursor = metadata.get(LUOGU_TAG_ENRICHMENT_CURSOR_KEY)
    missing = _luogu_accepted_problems_missing_tags(db)
    missing_set = set(missing)
    raw_failures = metadata.get(LUOGU_TAG_FAILURES_KEY)
    failures: dict[str, dict[str, Any]] = {
        str(problem_id).strip().upper(): dict(value)
        for problem_id, value in (raw_failures.items() if isinstance(raw_failures, Mapping) else [])
        if isinstance(value, Mapping) and str(problem_id).strip().upper() in missing_set
    }
    raw_tagless = metadata.get(LUOGU_TAGLESS_KEY)
    if isinstance(raw_tagless, Mapping):
        tagless: dict[str, dict[str, Any]] = {
            str(problem_id).strip().upper(): dict(value)
            for problem_id, value in raw_tagless.items()
            if isinstance(value, Mapping)
            and str(problem_id).strip().upper() in missing_set
        }
    elif isinstance(raw_tagless, list):
        tagless = {
            str(problem_id).strip().upper(): {}
            for problem_id in raw_tagless
            if isinstance(problem_id, str)
            and str(problem_id).strip().upper() in missing_set
        }
    else:
        tagless = {}

    # Migrate the old classification without another network request.  Only
    # the exact diagnostic produced by this parser is safe to reinterpret;
    # transport and schema failures must remain retryable failures.
    for problem_id, failure in list(failures.items()):
        if failure.get("error") != f"Luogu problem {problem_id} has no public tags":
            continue
        tagless[problem_id] = {
            "observed_at": str(failure.get("last_failed_at") or now.isoformat(timespec="seconds")),
            "source": "public_problem_page",
        }
        failures.pop(problem_id, None)

    eligible: list[str] = []
    deferred = 0
    for problem_id in missing:
        if problem_id in tagless:
            continue
        retry_at = _parse_iso(str(failures.get(problem_id, {}).get("next_retry_at") or ""))
        if full and retry_at is not None and retry_at > now:
            deferred += 1
        else:
            eligible.append(problem_id)
    limit = (
        len(eligible)
        if full
        else min(max(int(batch_size), 0), LUOGU_TAG_ENRICHMENT_MAX_BATCH)
    )
    selected = _rotated_problem_batch(eligible, str(cursor) if cursor else None, limit)
    errors: list[dict[str, str]] = []
    resolved_rows: list[dict[str, Any]] = []

    tag_names: dict[int, str] = {}
    cached_tag_names = metadata.get("tags")
    if isinstance(cached_tag_names, Mapping):
        for raw_id, raw_name in cached_tag_names.items():
            try:
                tag_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            if isinstance(raw_name, str) and raw_name:
                tag_names[tag_id] = raw_name
    if selected and not tag_names:
        try:
            tag_names = client.tags()
            metadata["tags"] = {str(key): value for key, value in tag_names.items()}
        except Exception as exc:
            errors.append(
                {
                    "platform": "luogu",
                    "problem_id": "*",
                    "message": _safe_platform_error(exc),
                }
            )

    failed_problem_ids: set[str] = set()

    def fetch_problem(
        problem_id: str,
    ) -> tuple[str, dict[str, Any] | None, bool, BaseException | None]:
        try:
            problem = client.problem(problem_id, tag_names=tag_names)
            returned_id = str(problem.get("problem_id") or "").strip().upper()
            if returned_id != problem_id or str(problem.get("platform") or "").lower() != "luogu":
                raise ResponseShapeError(
                    f"Luogu problem lookup for {problem_id} returned {returned_id or 'no pid'}"
                )
            tags = _normalise_preview_tags(problem.get("tags", []))
            if not tags:
                return problem_id, dict(problem), True, None
            return problem_id, dict(problem), False, None
        except Exception as exc:
            return problem_id, None, False, exc

    if isinstance(client, LuoguClient) and len(selected) >= 4 and workers > 1:
        with ThreadPoolExecutor(
            max_workers=min(max(1, int(workers)), LUOGU_TAG_WORKERS),
            thread_name_prefix="acm-luogu-tags",
        ) as executor:
            fetched = executor.map(fetch_problem, selected)
            fetch_results = list(fetched)
    else:
        fetch_results = [fetch_problem(problem_id) for problem_id in selected]

    for completed, (problem_id, problem, is_tagless, exc) in enumerate(fetch_results, start=1):
        if exc is None and problem is not None:
            resolved_rows.append(problem)
            failures.pop(problem_id, None)
            if is_tagless:
                tagless[problem_id] = {
                    "observed_at": now.isoformat(timespec="seconds"),
                    "source": "public_problem_page",
                }
            else:
                tagless.pop(problem_id, None)
        else:
            failed_problem_ids.add(problem_id)
            previous_attempts = int(failures.get(problem_id, {}).get("attempts") or 0)
            attempts = previous_attempts + 1
            retry_hours = min(24 * (2 ** (attempts - 1)), 24 * 7)
            failures[problem_id] = {
                "attempts": attempts,
                "last_failed_at": now.isoformat(timespec="seconds"),
                "next_retry_at": (now + timedelta(hours=retry_hours)).isoformat(timespec="seconds"),
                "error": _safe_platform_error(exc or PlatformError("unknown failure")),
            }
            errors.append(
                {
                    "platform": "luogu",
                    "problem_id": problem_id,
                    "message": _safe_platform_error(exc or PlatformError("unknown failure")),
                }
            )
        _report_progress(
            progress_callback,
            phase="tags",
            platform="luogu",
            step=completed,
            total=len(selected),
            completed=completed - len(failed_problem_ids),
            failed=len(failed_problem_ids),
            message=f"洛谷标签 {completed}/{len(selected)}",
            usable=True,
        )

    if selected:
        metadata[LUOGU_TAG_ENRICHMENT_CURSOR_KEY] = selected[-1]
    metadata[LUOGU_TAG_FAILURES_KEY] = failures
    metadata[LUOGU_TAGLESS_KEY] = tagless
    with db.atomic():
        db.upsert_problems(resolved_rows)
        db.connection.execute(
            """INSERT INTO sync_state(platform,status,metadata_json)
               VALUES('luogu','stale',?)
               ON CONFLICT(platform) DO UPDATE SET metadata_json=excluded.metadata_json""",
            (json.dumps(metadata, ensure_ascii=False),),
        )

    remaining = sum(
        problem_id not in tagless
        for problem_id in _luogu_accepted_problems_missing_tags(db)
    )
    resolved = sum(problem_id not in failed_problem_ids for problem_id in selected)
    return {
        "attempted": len(selected),
        "resolved": resolved,
        "failed": len(failed_problem_ids),
        "tagless": len(tagless),
        "remaining": remaining,
        "cursor": metadata.get(LUOGU_TAG_ENRICHMENT_CURSOR_KEY),
        "errors": errors,
        **({"deferred": deferred} if deferred else {}),
    }


def preview_plan_task_tags(
    db: Database,
    tasks: Sequence[Mapping[str, Any]],
    *,
    codeforces_client: CodeforcesClient | None = None,
    luogu_client: LuoguClient | None = None,
    overwrite: bool = False,
    refresh_codeforces: bool = True,
) -> dict[str, Any]:
    """Propose tags for plan tasks without mutating the plan document.

    Codeforces uses the local catalog first and refreshes the anonymous official
    problemset at most once. Luogu performs one anonymous public problem-page
    query per distinct pid. Individual remote failures are captured so successful
    proposals survive a partial outage.
    """
    eligible: list[dict[str, Any]] = []
    skipped_nonempty = 0
    for raw in tasks:
        current = _normalise_preview_tags(raw.get("tags", []))
        if current and not overwrite:
            skipped_nonempty += 1
            continue
        platform = str(raw["platform"]).lower()
        problem_id = str(raw["problem_id"]).upper()
        if platform == "codeforces" and not problem_id.startswith("CF"):
            problem_id = f"CF{problem_id}"
        eligible.append(
            {
                "task_key": str(raw["task_key"]),
                "platform": platform,
                "problem_id": problem_id,
                "name": str(raw.get("name") or raw.get("title") or raw["problem_id"]),
                "current_tags": current,
            }
        )

    resolved: dict[tuple[str, str], tuple[list[str], str]] = {}
    errors: list[dict[str, str]] = []
    warnings: list[str] = []

    cf_targets = {
        (task["problem_id"][2:] if task["problem_id"].startswith("CF") else task["problem_id"])
        for task in eligible if task["platform"] == "codeforces"
    }
    local_cf: dict[str, Any] = {str(row["problem_id"]).upper(): row for row in db.problems("codeforces")}
    missing_cf: set[str] = set()
    for problem_id in cf_targets:
        row = local_cf.get(problem_id)
        tags = _normalise_preview_tags(json.loads(row["tags_json"] or "[]")) if row else []
        if tags:
            resolved[("codeforces", f"CF{problem_id}")] = (tags, "sqlite_catalog")
        else:
            missing_cf.add(problem_id)

    if missing_cf and refresh_codeforces:
        try:
            problems, statistics = (codeforces_client or CodeforcesClient()).problemset()
            stats = {
                (str(item.get("contestId")), str(item.get("index"))): item
                for item in statistics if isinstance(item, Mapping)
            }
            refreshed: dict[str, dict[str, Any]] = {}
            for raw in problems:
                try:
                    problem_id = cf_problem_id(raw).upper()
                    row = _cf_problem(raw, stats.get((str(raw.get("contestId")), str(raw.get("index")))))
                except (KeyError, TypeError, ResponseShapeError):
                    continue
                refreshed[problem_id] = row
            with db.atomic():
                db.upsert_problems(refreshed.values())
            for problem_id in missing_cf:
                row = refreshed.get(problem_id)
                tags = _normalise_preview_tags(row.get("tags", [])) if row else []
                if tags:
                    resolved[("codeforces", f"CF{problem_id}")] = (
                        tags,
                        "codeforces_problemset",
                    )
                else:
                    warnings.append(f"Codeforces CF{problem_id} has no public tags")
        except Exception as exc:
            errors.append(
                {
                    "platform": "codeforces",
                    "problem_id": "*",
                    "message": str(exc),
                }
            )
    elif missing_cf:
        warnings.append(
            f"Codeforces catalog misses {len(missing_cf)} problem(s); refresh disabled"
        )

    luogu_targets = sorted(
        {task["problem_id"] for task in eligible if task["platform"] == "luogu"}
    )
    if luogu_targets:
        client = luogu_client or LuoguClient()
        tag_names: dict[int, str] = {}
        try:
            tag_names = client.tags()
        except Exception as exc:
            warnings.append(f"Luogu tag dictionary unavailable: {exc}")
        for problem_id in luogu_targets:
            try:
                row = client.problem(problem_id, tag_names=tag_names)
                tags = _normalise_preview_tags(row.get("tags", []))
                if tags:
                    resolved[("luogu", problem_id)] = (tags, "luogu_problem")
                else:
                    warnings.append(f"Luogu {problem_id} has no public tags")
            except Exception as exc:
                errors.append(
                    {
                        "platform": "luogu",
                        "problem_id": problem_id,
                        "message": str(exc),
                    }
                )

    proposals: list[dict[str, Any]] = []
    by_platform: dict[str, dict[str, int]] = {}
    for task in eligible:
        key = (task["platform"], task["problem_id"])
        tags, source = resolved.get(key, ([], "unresolved"))
        proposals.append({**task, "suggested_tags": tags, "source": source})
        counts = by_platform.setdefault(task["platform"], {"eligible": 0, "suggested": 0})
        counts["eligible"] += 1
        counts["suggested"] += int(bool(tags))
    suggested = sum(bool(proposal["suggested_tags"]) for proposal in proposals)
    total = len(proposals)
    return {
        "proposals": proposals,
        "coverage": {
            "eligible": total,
            "suggested": suggested,
            "unresolved": total - suggested,
            "skipped_nonempty": skipped_nonempty,
            "ratio": suggested / total if total else 1.0,
            "by_platform": by_platform,
        },
        "errors": errors,
        "warnings": warnings,
    }


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _catalog_due(state: Mapping[str, Any] | None, key: str, hours: int, now: datetime) -> bool:
    if not state:
        return True
    try:
        metadata = json.loads(state["metadata_json"] or "{}")
    except (json.JSONDecodeError, KeyError, TypeError):
        return True
    stamp = _parse_iso(metadata.get(key))
    return stamp is None or now - stamp >= timedelta(hours=hours)


def sync_codeforces(
    db: Database,
    handle: str,
    client: CodeforcesClient | None = None,
    *,
    refresh_catalog: bool | None = None,
    now: datetime | None = None,
    validated_user: Mapping[str, Any] | None = None,
    progress_callback: Callable[[Mapping[str, Any]], None] | None = None,
) -> SyncResult:
    """Synchronize one Codeforces account without destructive partial writes."""
    client = client or CodeforcesClient()
    now = now or datetime.now(timezone.utc)
    state = db.sync_state("codeforces")
    if refresh_catalog is None:
        refresh_catalog = _catalog_due(state, "problemset_synced_at", 24, now)
    _report_progress(
        progress_callback,
        phase="account",
        platform="codeforces",
        step=0,
        total=2,
        completed=0,
        failed=0,
        message="正在同步 Codeforces 账号与提交",
        usable=False,
    )
    try:
        user = validated_user or client.user_info(handle)
        raw_submissions = client.new_submissions(handle, db.known_submission_ids("codeforces"))
        submissions: list[dict[str, Any]] = []
        submission_problems: list[dict[str, Any]] = []
        for raw in raw_submissions:
            problem, submission = _cf_submission(raw)
            submission_problems.append(problem)
            submissions.append(submission)
    except Exception as exc:
        db.record_sync_attempt("codeforces", status="failed", error=str(exc), success=False)
        return SyncResult("codeforces", "failed", error=str(exc))

    problems: list[dict[str, Any]] = []
    warnings: list[str] = []
    catalog_ok = not refresh_catalog
    if refresh_catalog:
        _report_progress(
            progress_callback,
            phase="catalog",
            platform="codeforces",
            step=0,
            total=1,
            completed=0,
            failed=0,
            message="正在刷新 Codeforces 全局题库",
            usable=False,
        )
        try:
            catalog, stats = client.problemset()
            stat_by_key = {
                (row.get("contestId"), row.get("index")): row
                for row in stats
                if isinstance(row, Mapping)
            }
            problems = [
                _cf_problem(p, stat_by_key.get((p.get("contestId"), p.get("index"))))
                for p in catalog
            ]
            catalog_ok = True
        except Exception as exc:
            warnings.append(f"catalog refresh failed: {exc}")

    metadata: MutableMapping[str, Any] = {}
    if state:
        try:
            metadata.update(json.loads(state["metadata_json"] or "{}"))
        except (json.JSONDecodeError, TypeError):
            pass
    if refresh_catalog and catalog_ok:
        metadata["problemset_synced_at"] = now.isoformat(timespec="seconds")
    metadata["handle"] = handle
    metadata["rating"] = user.get("rating")
    cursor = str(max((int(x["submission_id"]) for x in submissions), default=0)) or None
    status = "fresh" if not warnings else "partial"
    try:
        with db.atomic():
            db.upsert_account(
                "codeforces",
                handle,
                display_name=str(user.get("handle") or handle),
                rating=user.get("rating"),
                validated_at=now.isoformat(timespec="seconds"),
            )
            db.upsert_problems(problems)
            db.upsert_problems(submission_problems)
            for submission in submissions:
                db.upsert_submission(submission)
            db.connection.execute(
                """INSERT INTO sync_state(platform,status,last_attempt_at,last_success_at,error,cursor,metadata_json)
                   VALUES('codeforces',?,?,?,?,?,?)
                   ON CONFLICT(platform) DO UPDATE SET status=excluded.status,
                     last_attempt_at=excluded.last_attempt_at,last_success_at=excluded.last_success_at,
                     error=excluded.error,cursor=COALESCE(excluded.cursor,sync_state.cursor),
                     metadata_json=excluded.metadata_json""",
                (status, now.isoformat(timespec="seconds"), now.isoformat(timespec="seconds"),
                 warnings[0] if warnings else None, cursor, json.dumps(metadata, ensure_ascii=False)),
            )
    except Exception as exc:
        db.record_sync_attempt("codeforces", status="failed", error=str(exc), success=False)
        return SyncResult("codeforces", "failed", error=str(exc))
    _report_progress(
        progress_callback,
        phase="complete",
        platform="codeforces",
        step=1,
        total=1,
        completed=1,
        failed=0 if status != "partial" else 1,
        message="Codeforces 同步完成" if status == "fresh" else "Codeforces 基础数据可用，题库刷新部分失败",
        usable=True,
    )
    return SyncResult(
        "codeforces",
        status,
        submissions=len(submissions),
        accepted=sum(x.get("verdict") == "OK" for x in submissions),
        problems=len(problems),
        warnings=warnings,
    )


def sync_luogu(
    db: Database,
    uid: str | int,
    client: LuoguClient | None = None,
    *,
    refresh_catalog: bool | None = None,
    candidate_pages: Sequence[int] = (1,),
    candidate_queries: Sequence[Mapping[str, Any]] | None = None,
    full_catalog: bool = False,
    now: datetime | None = None,
    progress_callback: Callable[[Mapping[str, Any]], None] | None = None,
) -> SyncResult:
    """Sync public Luogu ACs; catalog errors produce a usable partial sync."""
    client = client or LuoguClient()
    now = now or datetime.now(timezone.utc)
    state = db.sync_state("luogu")
    legacy_full_catalog_at: str | None = None
    if refresh_catalog is None:
        refresh_catalog = _catalog_due(state, "catalog_synced_at", 24, now)
        if full_catalog:
            refresh_catalog = _catalog_due(state, "full_catalog_synced_at", 24, now)
            if refresh_catalog:
                old_metadata = _sync_metadata(db, "luogu")
                old_stamp = str(old_metadata.get("catalog_synced_at") or "")
                parsed_old_stamp = _parse_iso(old_stamp)
                local_problem_count = int(
                    db.connection.execute(
                        "SELECT COUNT(*) FROM problems WHERE platform='luogu'"
                    ).fetchone()[0]
                )
                if (
                    parsed_old_stamp is not None
                    and parsed_old_stamp <= now
                    and now - parsed_old_stamp < timedelta(hours=24)
                    and local_problem_count >= LUOGU_FULL_CATALOG_MIN_PROBLEMS
                ):
                    refresh_catalog = False
                    legacy_full_catalog_at = old_stamp
    _report_progress(
        progress_callback,
        phase="account",
        platform="luogu",
        step=0,
        total=1,
        completed=0,
        failed=0,
        message="正在同步洛谷公开 AC",
        usable=False,
    )
    try:
        passed = client.practice(uid)
    except Exception as exc:
        db.record_sync_attempt("luogu", status="failed", error=str(exc), success=False)
        return SyncResult("luogu", "failed", error=str(exc))

    accepted_problems = [
        {
            "platform": "luogu",
            "problem_id": pid,
            "url": f"https://www.luogu.com.cn/problem/{pid}",
        }
        for pid in sorted(passed)
    ]
    metadata: MutableMapping[str, Any] = {}
    if state:
        try:
            metadata.update(json.loads(state["metadata_json"] or "{}"))
        except (json.JSONDecodeError, TypeError):
            pass
    if legacy_full_catalog_at:
        metadata["full_catalog_synced_at"] = legacy_full_catalog_at
    metadata["uid"] = str(uid)
    stamp = now.isoformat(timespec="seconds")
    try:
        with db.atomic():
            db.upsert_account(
                "luogu",
                str(uid),
                validated_at=now.isoformat(timespec="seconds"),
            )
            db.upsert_problems(accepted_problems)
            for pid in sorted(passed):
                db.upsert_submission(
                    {
                        "platform": "luogu",
                        "submission_id": f"accepted:{pid}",
                        "problem_id": pid,
                        "verdict": "AC",
                        "submitted_at": None,
                        "language": None,
                        "raw": {"source": "public-practice"},
                    }
                )
            db.connection.execute(
                """INSERT INTO sync_state(platform,status,last_attempt_at,last_success_at,error,cursor,metadata_json)
                   VALUES('luogu',?,?,?,?,NULL,?)
                   ON CONFLICT(platform) DO UPDATE SET status=excluded.status,
                     last_attempt_at=excluded.last_attempt_at,last_success_at=excluded.last_success_at,
                     error=excluded.error,metadata_json=excluded.metadata_json""",
                ("fresh", stamp, stamp, None, json.dumps(metadata, ensure_ascii=False)),
            )
    except Exception as exc:
        db.record_sync_attempt("luogu", status="failed", error=str(exc), success=False)
        return SyncResult("luogu", "failed", error=str(exc))
    _report_progress(
        progress_callback,
        phase="accepted",
        platform="luogu",
        step=len(passed),
        total=len(passed),
        completed=len(passed),
        failed=0,
        message=f"洛谷公开 AC 已可用（{len(passed)} 题）",
        usable=True,
    )

    # The account and public AC set are now durable and usable. Catalog fetches
    # remain an independent all-or-nothing snapshot: no catalog row is written
    # until every requested page has passed its structure guards.
    catalog: list[dict[str, Any]] = []
    warnings: list[str] = []
    tags: dict[int, str] = {}
    catalog_failed = False
    if refresh_catalog:
        try:
            tags = client.tags()
            if full_catalog:
                def catalog_progress(values: Mapping[str, Any]) -> None:
                    _report_progress(progress_callback, **{**dict(values), "usable": True})

                catalog = client.all_problems(
                    tag_names=tags,
                    progress_callback=catalog_progress,
                )
            else:
                queries = candidate_queries or tuple({"page": page} for page in candidate_pages)
                for query_index, query in enumerate(queries, start=1):
                    catalog.extend(
                        client.problem_page(
                            page=int(query.get("page", 1)),
                            difficulty=query.get("difficulty"),
                            tag=query.get("tag"),
                            tag_names=tags,
                        )
                    )
                    _report_progress(
                        progress_callback,
                        phase="catalog",
                        platform="luogu",
                        step=query_index,
                        total=len(queries),
                        completed=query_index,
                        failed=0,
                        message="正在刷新洛谷候选题库",
                        usable=True,
                    )
            metadata["catalog_synced_at"] = stamp
            if full_catalog:
                metadata["full_catalog_synced_at"] = stamp
            metadata["tags"] = {str(key): value for key, value in tags.items()}
            with db.atomic():
                db.upsert_problems(catalog)
                db.connection.execute(
                    "UPDATE sync_state SET metadata_json=? WHERE platform='luogu'",
                    (json.dumps(metadata, ensure_ascii=False),),
                )
        except Exception as exc:
            catalog_failed = True
            warnings.append(f"catalog refresh failed: {exc}")
    tag_enrichment: dict[str, Any] | None = None
    try:
        tag_enrichment = enrich_luogu_accepted_problem_tags(
            db,
            client,
            batch_size=(
                LUOGU_TAG_ENRICHMENT_CATALOG_FAILURE_BATCH
                if catalog_failed
                else LUOGU_TAG_ENRICHMENT_MAX_BATCH
            ),
            full=not catalog_failed,
            now=now,
            progress_callback=progress_callback,
        )
        if tag_enrichment["failed"]:
            warnings.append(
                "tag enrichment failed for "
                f"{tag_enrichment['failed']}/{tag_enrichment['attempted']} problem(s)"
            )
        elif tag_enrichment.get("deferred"):
            warnings.append(
                "tag enrichment deferred for "
                f"{tag_enrichment['deferred']} problem(s) after recent failures"
            )
    except Exception as exc:
        warnings.append(f"tag enrichment failed: {_safe_platform_error(exc)}")

    status = "fresh" if not warnings else "partial"
    if status == "partial":
        with db.atomic():
            db.connection.execute(
                "UPDATE sync_state SET status='partial',error=? WHERE platform='luogu'",
                (warnings[0],),
            )
    _report_progress(
        progress_callback,
        phase="complete",
        platform="luogu",
        step=1,
        total=1,
        completed=1,
        failed=0 if status != "partial" else 1,
        message="洛谷同步完成" if status == "fresh" else "洛谷基础数据可用，部分元数据待重试",
        usable=True,
    )
    return SyncResult(
        "luogu",
        status,
        submissions=len(passed),
        accepted=len(passed),
        problems=len(catalog),
        warnings=warnings,
        tag_enrichment=tag_enrichment,
    )


def freshness(db: Database, platform: str, *, now: datetime | None = None, ttl_hours: int = 6) -> str:
    state = db.sync_state(platform)
    if not state or state["status"] == "failed":
        return "failed"
    success = _parse_iso(state["last_success_at"])
    now = now or datetime.now(timezone.utc)
    return "fresh" if success and now - success < timedelta(hours=ttl_hours) else "stale"
