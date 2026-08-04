"""Anonymous Codeforces and Luogu synchronization.

All network access is injectable.  Tests can pass a callable with the signature
``request(url, params, headers)`` returning a decoded JSON object, text, or bytes.
The default implementation uses only :mod:`urllib.request`.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from typing import Any, Callable, Iterable, Iterator, Mapping, MutableMapping, Protocol, Sequence

from .storage import Database, utc_now


CF_BASE = "https://codeforces.com/api"
LUOGU_BASE = "https://www.luogu.com.cn"
CF_THROTTLE_SECONDS = 2.1


class PlatformError(RuntimeError):
    """A remote response is unavailable or violates its expected contract."""


class RemoteAPIError(PlatformError):
    pass


class ResponseShapeError(PlatformError):
    pass


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

    def as_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "status": self.status,
            "submissions": self.submissions,
            "accepted": self.accepted,
            "problems": self.problems,
            "error": self.error,
            "warnings": self.warnings,
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
    found: dict[str, dict[str, Any]] = {}
    for node in _walk(context):
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
    if not found:
        raise ResponseShapeError("Luogu problem list contains no recognizable problems")
    return [found[key] for key in sorted(found)]


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

    def _get(self, path: str, **params: Any) -> Any:
        return self.request(
            f"{LUOGU_BASE}{path}",
            params,
            {"Referer": LUOGU_BASE + "/"},
        )

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
) -> SyncResult:
    """Synchronize one Codeforces account without destructive partial writes."""
    client = client or CodeforcesClient()
    now = now or datetime.now(timezone.utc)
    state = db.sync_state("codeforces")
    if refresh_catalog is None:
        refresh_catalog = _catalog_due(state, "problemset_synced_at", 24, now)
    try:
        user = client.user_info(handle)
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
    now: datetime | None = None,
) -> SyncResult:
    """Sync public Luogu ACs; catalog errors produce a usable partial sync."""
    client = client or LuoguClient()
    now = now or datetime.now(timezone.utc)
    state = db.sync_state("luogu")
    if refresh_catalog is None:
        refresh_catalog = _catalog_due(state, "catalog_synced_at", 24, now)
    try:
        passed = client.practice(uid)
    except Exception as exc:
        db.record_sync_attempt("luogu", status="failed", error=str(exc), success=False)
        return SyncResult("luogu", "failed", error=str(exc))

    catalog: list[dict[str, Any]] = []
    warnings: list[str] = []
    tags: dict[int, str] = {}
    catalog_ok = not refresh_catalog
    if refresh_catalog:
        try:
            tags = client.tags()
            queries = candidate_queries or tuple({"page": page} for page in candidate_pages)
            for query in queries:
                catalog.extend(
                    client.problem_page(
                        page=int(query.get("page", 1)),
                        difficulty=query.get("difficulty"),
                        tag=query.get("tag"),
                        tag_names=tags,
                    )
                )
            catalog_ok = True
        except Exception as exc:
            warnings.append(f"catalog refresh failed: {exc}")

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
    if refresh_catalog and catalog_ok:
        metadata["catalog_synced_at"] = now.isoformat(timespec="seconds")
        metadata["tags"] = {str(key): value for key, value in tags.items()}
    metadata["uid"] = str(uid)
    status = "fresh" if not warnings else "partial"
    try:
        with db.atomic():
            db.upsert_account(
                "luogu",
                str(uid),
                validated_at=now.isoformat(timespec="seconds"),
            )
            db.upsert_problems(catalog)
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
            stamp = now.isoformat(timespec="seconds")
            db.connection.execute(
                """INSERT INTO sync_state(platform,status,last_attempt_at,last_success_at,error,cursor,metadata_json)
                   VALUES('luogu',?,?,?,?,NULL,?)
                   ON CONFLICT(platform) DO UPDATE SET status=excluded.status,
                     last_attempt_at=excluded.last_attempt_at,last_success_at=excluded.last_success_at,
                     error=excluded.error,metadata_json=excluded.metadata_json""",
                (status, stamp, stamp, warnings[0] if warnings else None, json.dumps(metadata, ensure_ascii=False)),
            )
    except Exception as exc:
        db.record_sync_attempt("luogu", status="failed", error=str(exc), success=False)
        return SyncResult("luogu", "failed", error=str(exc))
    return SyncResult(
        "luogu",
        status,
        submissions=len(passed),
        accepted=len(passed),
        problems=len(catalog),
        warnings=warnings,
    )


def freshness(db: Database, platform: str, *, now: datetime | None = None, ttl_hours: int = 6) -> str:
    state = db.sync_state(platform)
    if not state or state["status"] == "failed":
        return "failed"
    success = _parse_iso(state["last_success_at"])
    now = now or datetime.now(timezone.utc)
    return "fresh" if success and now - success < timedelta(hours=ttl_hours) else "stale"
