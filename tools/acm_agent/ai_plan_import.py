"""Deterministic gates and lowering for AI-assisted plan imports.

The model is intentionally restricted to a compact intermediate representation.
Only this module constructs plan-v2 documents, canonical problem URLs, and stable
keys consumed by :class:`PlanManager`.
"""

from __future__ import annotations

from collections import defaultdict, deque
from datetime import date
import hashlib
import json
import re
from typing import Any, Iterable, Mapping, Sequence

from .plan import VALID_LEVELS, canonical_problem_key, problem_url
from .recommend import LUOGU_CF_EQUIVALENT
from .topic_taxonomy import classify_tags


MAX_ORGANIZE_PROBLEMS = 200
MAX_GENERATED_PROBLEMS = 30
MAX_GENERATION_CANDIDATES = 120
DEFAULT_GENERATED_PROBLEMS = 12
MAX_GENERATION_ROUNDS = 5
MAX_STALLED_GENERATION_ROUNDS = 2


class AIPlanImportError(ValueError):
    """Raised when user input or model IR violates the import contract."""


_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:"
    r"codeforces\.com/(?:problemset/problem|contest)/(\d+)/(?:problem/)?([A-Z]\d*)"
    r"|luogu\.com\.cn/problem/(P\d+)"
    r")(?:/)?(?:[?#][^\s<>\]）]*)?(?=$|[\s<>\]）)'\".,;:!，。；：！？])",
    re.IGNORECASE,
)
_OFFICIAL_URL_RE = re.compile(
    r"https?://(?:www\.)?(?:"
    r"codeforces\.com/(?:problemset/problem|contest)/"
    r"|luogu\.com\.cn/problem/"
    r")[^\s<>\]）]+",
    re.IGNORECASE,
)
_ID_RE = re.compile(r"(?<![A-Z0-9])(?:CF\s*\d+\s*[A-Z]\d*|P\s*\d+)(?![A-Z0-9])", re.I)
_STRICT_GENERATED_ID_RE = re.compile(r"(?:CF[1-9]\d*[A-Z]\d*|P[1-9]\d*)", re.I)


def _problem_ref(platform: str, problem_id: str) -> dict[str, str]:
    normalized = canonical_problem_key(problem_id.replace(" ", ""), platform)
    normalized_platform, display_id = normalized.split(":", 1)
    return {
        "problem_key": normalized,
        "platform": normalized_platform,
        "problem_id": display_id,
    }


def extract_problem_refs(text: str, *, limit: int = MAX_ORGANIZE_PROBLEMS) -> dict[str, Any]:
    """Extract CF/Luogu references in source order and report duplicates."""

    if not isinstance(text, str) or not text.strip():
        raise AIPlanImportError("text 必须是非空字符串")
    matches: list[tuple[int, dict[str, str]]] = []
    url_spans: list[tuple[int, int]] = []
    official_url_spans: list[tuple[int, int]] = []
    for match in _URL_RE.finditer(text):
        url_spans.append(match.span())
        if match.group(3):
            item = _problem_ref("luogu", match.group(3))
        else:
            item = _problem_ref("codeforces", f"CF{match.group(1)}{match.group(2)}")
        matches.append((match.start(), item))
    invalid_links: list[str] = []
    for match in _OFFICIAL_URL_RE.finditer(text):
        official_url_spans.append(match.span())
        if any(start == match.start() for start, _end in url_spans):
            continue
        link = match.group(0).rstrip(".,;:!?，。；：！？)]}")
        if link and link not in invalid_links:
            invalid_links.append(link)
    for match in _ID_RE.finditer(text):
        if any(start <= match.start() < end for start, end in official_url_spans):
            continue
        token = re.sub(r"\s+", "", match.group(0)).upper()
        platform = "codeforces" if token.startswith("CF") else "luogu"
        matches.append((match.start(), _problem_ref(platform, token)))
    matches.sort(key=lambda item: item[0])

    refs: list[dict[str, str]] = []
    seen: set[str] = set()
    duplicates: list[str] = []
    for _position, item in matches:
        key = item["problem_key"]
        if key in seen:
            if key not in duplicates:
                duplicates.append(key)
            continue
        seen.add(key)
        refs.append(item)
    if not refs:
        raise AIPlanImportError("没有识别到 Codeforces 或洛谷题号")
    if len(refs) > int(limit):
        raise AIPlanImportError(f"一次最多整理 {int(limit)} 道题，当前识别到 {len(refs)} 道")
    return {
        "problems": refs,
        "duplicates": duplicates,
        "invalid_links": invalid_links,
    }


def _object(value: Any, *, label: str, allowed: set[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise AIPlanImportError(f"{label} 必须是对象")
    extra = set(value) - allowed
    if extra:
        raise AIPlanImportError(f"{label} 包含不允许字段: {', '.join(sorted(extra))}")
    return value


def _text(value: Any, *, label: str, required: bool = False) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str):
        raise AIPlanImportError(f"{label} 必须是字符串")
    result = " ".join(value.split())
    if required and not result:
        raise AIPlanImportError(f"{label} 不能为空")
    return result


def _date(value: Any, *, label: str) -> str | None:
    if value in (None, ""):
        return None
    text = _text(value, label=label, required=True)
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError as exc:
        raise AIPlanImportError(f"{label} 必须是 ISO 日期") from exc


def _validate_dates(stages: Sequence[Mapping[str, Any]]) -> None:
    dates = [stage.get("due_date") for stage in stages]
    present = [value for value in dates if value is not None]
    if present and len(present) != len(dates):
        raise AIPlanImportError("阶段截止日期不能混合填写与留空")
    if present and present != sorted(present):
        raise AIPlanImportError("阶段截止日期必须非递减")


def validate_organize_ir(
    value: Any, *, allowed_problem_keys: Sequence[str]
) -> dict[str, Any]:
    """Validate an organize response and require a perfect input permutation."""

    root = _object(
        value,
        label="AI 整理结果",
        allowed={"title", "description", "stages"},
    )
    raw_stages = root.get("stages")
    if not isinstance(raw_stages, list) or not raw_stages:
        raise AIPlanImportError("AI 整理结果必须包含非空 stages 数组")
    stages: list[dict[str, Any]] = []
    selected: list[str] = []
    allowed = set(allowed_problem_keys)
    for stage_index, raw_stage in enumerate(raw_stages, 1):
        stage = _object(
            raw_stage,
            label=f"stages[{stage_index}]",
            allowed={"topic", "due_date", "problems"},
        )
        raw_problems = stage.get("problems")
        if not isinstance(raw_problems, list) or not raw_problems:
            raise AIPlanImportError(f"stages[{stage_index}].problems 必须是非空数组")
        problems: list[dict[str, str]] = []
        for problem_index, raw_problem in enumerate(raw_problems, 1):
            problem = _object(
                raw_problem,
                label=f"stages[{stage_index}].problems[{problem_index}]",
                allowed={"problem_key", "level", "note"},
            )
            key = _text(problem.get("problem_key"), label="problem_key", required=True)
            if key not in allowed:
                raise AIPlanImportError(f"AI 整理结果包含未知题目: {key}")
            level = _text(problem.get("level") or "B", label="level").upper()
            if level not in VALID_LEVELS:
                raise AIPlanImportError(f"AI 整理结果包含非法 level: {level}")
            problems.append(
                {
                    "problem_key": key,
                    "level": level,
                    "note": _text(problem.get("note"), label="note"),
                }
            )
            selected.append(key)
        stages.append(
            {
                "topic": _text(stage.get("topic"), label="topic", required=True),
                "due_date": _date(stage.get("due_date"), label="due_date"),
                "problems": problems,
            }
        )
    if len(selected) != len(set(selected)):
        raise AIPlanImportError("AI 整理结果包含重复题目")
    if set(selected) != allowed or len(selected) != len(allowed_problem_keys):
        raise AIPlanImportError("AI 整理结果遗漏或增加了题目")
    _validate_dates(stages)
    return {
        "title": _text(root.get("title"), label="title", required=True),
        "description": _text(root.get("description"), label="description"),
        "stages": stages,
    }


def deterministic_organize_ir(problem_keys: Sequence[str]) -> dict[str, Any]:
    return {
        "title": "AI 快速导入题单",
        "description": "根据输入顺序生成的确定性题单",
        "stages": [
            {
                "topic": "全部题目",
                "due_date": None,
                "problems": [
                    {"problem_key": key, "level": "B", "note": ""}
                    for key in problem_keys
                ],
            }
        ],
    }


def validate_generation_intent(value: Any) -> dict[str, Any]:
    root = _object(
        value,
        label="AI 生成意图",
        allowed={
            "title", "description", "stages", "platforms", "topics",
            "difficulty_min", "difficulty_max",
        },
    )
    raw_platforms = root.get("platforms", ["codeforces", "luogu"])
    if not isinstance(raw_platforms, list) or not raw_platforms:
        raise AIPlanImportError("platforms 必须是非空数组")
    platforms = list(dict.fromkeys(_text(item, label="platform", required=True).lower() for item in raw_platforms))
    if any(item not in {"codeforces", "luogu"} for item in platforms):
        raise AIPlanImportError("platforms 只能包含 codeforces 或 luogu")
    raw_topics = root.get("topics", [])
    if not isinstance(raw_topics, list) or any(not isinstance(item, str) for item in raw_topics):
        raise AIPlanImportError("topics 必须是字符串数组")
    topics = list(dict.fromkeys(_text(item, label="topic", required=True) for item in raw_topics))
    raw_stages = root.get("stages")
    if not isinstance(raw_stages, list) or not raw_stages:
        raise AIPlanImportError("stages 必须是非空数组")
    stages: list[dict[str, Any]] = []
    for index, raw_stage in enumerate(raw_stages, 1):
        stage = _object(raw_stage, label=f"stages[{index}]", allowed={"topic", "due_date"})
        stages.append({
            "topic": _text(stage.get("topic"), label="topic", required=True),
            "due_date": _date(stage.get("due_date"), label="due_date"),
        })
    _validate_dates(stages)

    def bound(name: str) -> int | None:
        value = root.get(name)
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise AIPlanImportError(f"{name} 必须是整数或 null")
        result = int(value)
        if result < 0:
            raise AIPlanImportError(f"{name} 不能为负数")
        return result

    minimum, maximum = bound("difficulty_min"), bound("difficulty_max")
    if minimum is not None and maximum is not None and minimum > maximum:
        raise AIPlanImportError("difficulty_min 不能大于 difficulty_max")
    return {
        "title": _text(root.get("title"), label="title", required=True),
        "description": _text(root.get("description"), label="description"),
        "stages": stages,
        "platforms": platforms,
        "topics": topics,
        "difficulty_min": minimum,
        "difficulty_max": maximum,
    }


def _row_value(row: Mapping[str, Any], key: str) -> Any:
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def _catalog_record(row: Mapping[str, Any], statuses: Mapping[str, str]) -> dict[str, Any] | None:
    platform = str(_row_value(row, "platform") or "").lower()
    problem_id = str(_row_value(row, "problem_id") or "").upper()
    if platform == "codeforces" and not problem_id.startswith("CF"):
        problem_id = f"CF{problem_id}"
    try:
        key = canonical_problem_key(problem_id, platform)
    except ValueError:
        return None
    try:
        tags = json.loads(str(_row_value(row, "tags_json") or "[]"))
    except json.JSONDecodeError:
        tags = []
    if not isinstance(tags, list):
        tags = []
    public_tags = list(dict.fromkeys(str(tag).strip() for tag in tags if str(tag).strip()))
    classified = classify_tags(public_tags)
    if isinstance(classified, Mapping):
        knowledge_topics = list(classified.get("topics", classified.get("classified", [])) or [])
    else:
        knowledge_topics = list(getattr(classified, "topics", getattr(classified, "classified", [])) or [])
    rating = _row_value(row, "rating")
    difficulty = _row_value(row, "difficulty")
    equivalent = (
        int(rating) if platform == "codeforces" and rating is not None
        else LUOGU_CF_EQUIVALENT.get(int(difficulty))
        if platform == "luogu" and difficulty is not None
        else None
    )
    return {
        "problem_key": key,
        "platform": platform,
        "problem_id": problem_id,
        "name": str(_row_value(row, "name") or problem_id),
        "tags": public_tags,
        "knowledge_topics": knowledge_topics,
        "difficulty": equivalent,
        "status": statuses.get(key, "available"),
    }


def catalog_index(
    rows: Iterable[Mapping[str, Any]], *, statuses: Mapping[str, str] | None = None
) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in rows:
        item = _catalog_record(row, statuses or {})
        if item is not None:
            result[item["problem_key"]] = item
    return result


def build_generation_candidates(
    rows: Iterable[Mapping[str, Any]],
    *,
    intent: Mapping[str, Any],
    statuses: Mapping[str, str],
    include_completed: bool,
    limit: int = MAX_GENERATION_CANDIDATES,
) -> list[dict[str, Any]]:
    """Build a stable, stratified, local-only candidate pool."""

    platforms = set(intent.get("platforms") or ("codeforces", "luogu"))
    wanted_topics = {str(item).casefold() for item in intent.get("topics") or []}
    minimum = intent.get("difficulty_min")
    maximum = intent.get("difficulty_max")
    records: list[dict[str, Any]] = []
    for row in rows:
        item = _catalog_record(row, statuses)
        if item is None or item["platform"] not in platforms:
            continue
        status = item["status"]
        if status == "active" or (not include_completed and status in {"accepted", "skipped"}):
            continue
        searchable_topics = {
            str(topic).casefold() for topic in [*item["knowledge_topics"], *item["tags"]]
        }
        if wanted_topics and not any(
            wanted in value or value in wanted
            for wanted in wanted_topics
            for value in searchable_topics
        ):
            continue
        difficulty = item["difficulty"]
        if minimum is not None and (difficulty is None or int(difficulty) < int(minimum)):
            continue
        if maximum is not None and (difficulty is None or int(difficulty) > int(maximum)):
            continue
        records.append(item)

    buckets: dict[tuple[str, str, int], deque[dict[str, Any]]] = defaultdict(deque)
    for item in sorted(records, key=lambda row: row["problem_key"]):
        primary = next(
            (topic for topic in item["knowledge_topics"] if topic.casefold() in wanted_topics),
            item["knowledge_topics"][0] if item["knowledge_topics"] else "unclassified",
        )
        band = int(item["difficulty"] or 0) // 400
        buckets[(item["platform"], str(primary), band)].append(item)
    output: list[dict[str, Any]] = []
    ordered_keys = sorted(buckets)
    while len(output) < min(int(limit), MAX_GENERATION_CANDIDATES):
        progressed = False
        for key in ordered_keys:
            if buckets[key]:
                output.append(buckets[key].popleft())
                progressed = True
                if len(output) >= min(int(limit), MAX_GENERATION_CANDIDATES):
                    break
        if not progressed:
            break
    return output


def validate_generation_selection(
    value: Any,
    *,
    candidates: Sequence[Mapping[str, Any]],
    expected_count: int,
    expected_stages: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    root = _object(value, label="AI 选题结果", allowed={"stages"})
    raw_stages = root.get("stages")
    if not isinstance(raw_stages, list) or not raw_stages:
        raise AIPlanImportError("AI 选题结果必须包含非空 stages 数组")
    if expected_stages is not None and len(raw_stages) != len(expected_stages):
        raise AIPlanImportError("AI 选题结果改变了既定阶段数量")
    allowed = {str(item["problem_key"]) for item in candidates}
    selected: list[str] = []
    stages: list[dict[str, Any]] = []
    for index, raw_stage in enumerate(raw_stages, 1):
        stage = _object(raw_stage, label=f"stages[{index}]", allowed={"topic", "due_date", "problem_keys"})
        keys = stage.get("problem_keys")
        if not isinstance(keys, list) or not keys or any(not isinstance(key, str) for key in keys):
            raise AIPlanImportError(f"stages[{index}].problem_keys 必须是非空字符串数组")
        normalized = [str(key) for key in keys]
        if any(key not in allowed for key in normalized):
            raise AIPlanImportError("AI 选题结果包含越权或未知候选")
        selected.extend(normalized)
        stages.append({
            "topic": _text(stage.get("topic"), label="topic", required=True),
            "due_date": _date(stage.get("due_date"), label="due_date"),
            "problems": [
                {"problem_key": key, "level": "B", "note": ""}
                for key in normalized
            ],
        })
        if expected_stages is not None:
            expected = expected_stages[index - 1]
            if stages[-1]["topic"] != expected.get("topic") or stages[-1]["due_date"] != expected.get("due_date"):
                raise AIPlanImportError("AI 选题结果改变了既定阶段名称或日期")
    if len(selected) != len(set(selected)):
        raise AIPlanImportError("AI 选题结果包含重复候选")
    if len(selected) != int(expected_count):
        raise AIPlanImportError(
            f"AI 选题数量不符：需要 {int(expected_count)} 道，实际 {len(selected)} 道"
        )
    _validate_dates(stages)
    return {"stages": stages}


def validate_generated_problem_ids(value: Any) -> list[str]:
    """Validate one model round whose only authority is proposing public IDs."""

    root = _object(value, label="AI 选题结果", allowed={"problem_ids"})
    raw_ids = root.get("problem_ids")
    if not isinstance(raw_ids, list):
        raise AIPlanImportError("AI 选题结果的 problem_ids 必须是数组")
    if len(raw_ids) > MAX_GENERATION_CANDIDATES:
        raise AIPlanImportError(
            f"AI 单轮最多返回 {MAX_GENERATION_CANDIDATES} 个 problem_ids"
        )
    normalized: list[str] = []
    for index, value in enumerate(raw_ids, 1):
        if not isinstance(value, str):
            raise AIPlanImportError(f"problem_ids[{index}] 必须是字符串")
        problem_id = value.strip().upper()
        normalized.append(problem_id)
    return normalized


def filter_generated_problem_ids(
    problem_ids: Sequence[str],
    *,
    catalog: Mapping[str, Mapping[str, Any]],
    already_selected: Sequence[str],
    include_completed: bool,
    remaining_count: int,
) -> tuple[list[str], list[dict[str, str]]]:
    """Apply deterministic local eligibility gates to one model round.

    Returned accepted values are canonical ``platform:problem_id`` keys.  The
    model never receives the catalog or local state and therefore cannot grant
    eligibility to an unknown, active, accepted, or skipped problem.
    """

    if isinstance(remaining_count, bool) or not isinstance(remaining_count, int):
        raise AIPlanImportError("remaining_count 必须是整数")
    if remaining_count < 0:
        raise AIPlanImportError("remaining_count 不能为负数")
    by_problem_id = {
        str(item.get("problem_id") or key.split(":", 1)[-1]).upper(): (key, item)
        for key, item in catalog.items()
    }
    seen = set(str(key) for key in already_selected)
    accepted: list[str] = []
    rejected: list[dict[str, str]] = []
    for problem_id in problem_ids:
        if not _STRICT_GENERATED_ID_RE.fullmatch(str(problem_id)):
            rejected.append({"problem_id": str(problem_id), "reason": "invalid_problem_id"})
            continue
        entry = by_problem_id.get(str(problem_id).upper())
        if entry is None:
            rejected.append({"problem_id": str(problem_id), "reason": "not_in_local_catalog"})
            continue
        key, item = entry
        if key in seen:
            rejected.append({"problem_id": str(problem_id), "reason": "duplicate"})
            continue
        status = str(item.get("status") or "available")
        if status == "active":
            rejected.append({"problem_id": str(problem_id), "reason": "active"})
            continue
        if not include_completed and status in {"accepted", "skipped"}:
            rejected.append({"problem_id": str(problem_id), "reason": status})
            continue
        if len(accepted) >= remaining_count:
            rejected.append({"problem_id": str(problem_id), "reason": "over_requested_count"})
            continue
        accepted.append(key)
        seen.add(key)
    return accepted, rejected


def deterministic_generated_ir(
    problem_keys: Sequence[str], *, target_text: str
) -> dict[str, Any]:
    """Lower accepted recommendations into the single-stage generate draft."""

    return {
        "title": "AI 目标题单",
        "description": str(target_text),
        "stages": [
            {
                "topic": "目标训练",
                "due_date": None,
                "problems": [
                    {"problem_key": key, "level": "B", "note": ""}
                    for key in problem_keys
                ],
            }
        ],
    }


def fit_stages_to_task_count(
    stages: Sequence[Mapping[str, Any]], task_count: int
) -> list[dict[str, Any]]:
    """Deterministically merge excess intent stages so each can receive a task."""

    if isinstance(task_count, bool) or not isinstance(task_count, int) or task_count < 1:
        raise AIPlanImportError("task_count 必须是正整数")
    normalized = [dict(stage) for stage in stages]
    if len(normalized) <= task_count:
        return normalized
    kept = normalized[:task_count]
    merged = normalized[task_count - 1:]
    topics = list(dict.fromkeys(str(stage.get("topic") or "").strip() for stage in merged))
    kept[-1]["topic"] = " / ".join(topic for topic in topics if topic)
    kept[-1]["due_date"] = merged[-1].get("due_date")
    _validate_dates(kept)
    return kept


def make_plan_id(mode: str, text: str, controls: Mapping[str, Any]) -> str:
    material = json.dumps(
        {"mode": mode, "text": text, "controls": dict(controls)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return f"ai-plan-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:12]}"


def lower_plan(
    *,
    mode: str,
    text: str,
    controls: Mapping[str, Any],
    ir: Mapping[str, Any],
    catalog: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    stages = list(ir.get("stages") or [])
    _validate_dates(stages)
    dated = bool(stages and all(stage.get("due_date") for stage in stages))
    output_stages: list[dict[str, Any]] = []
    for stage_index, stage in enumerate(stages, 1):
        stage_key = f"stage-{stage_index:02d}"
        tasks: list[dict[str, Any]] = []
        for task_index, problem in enumerate(stage.get("problems") or [], 1):
            key = str(problem["problem_key"])
            platform, problem_id = key.split(":", 1)
            metadata = catalog.get(key, {})
            tasks.append(
                {
                    "task_key": f"{stage_key}-task-{task_index:03d}",
                    "platform": platform,
                    "problem_id": problem_id,
                    "url": problem_url(problem_id, platform),
                    "name": str(metadata.get("name") or problem_id),
                    "level": str(problem.get("level") or "B"),
                    "note": str(problem.get("note") or ""),
                    "tags": [],
                }
            )
        stage_value: dict[str, Any] = {
            "stage_key": stage_key,
            "topic": str(stage["topic"]),
            "kind": "practice",
            "tasks": tasks,
        }
        if dated:
            stage_value["due_date"] = str(stage["due_date"])
        output_stages.append(stage_value)
    return {
        "schema_version": 2,
        "plan_id": make_plan_id(mode, text, controls),
        "title": str(ir.get("title") or "AI 快速导入题单"),
        "description": str(ir.get("description") or ""),
        "schedule_mode": "dated" if dated else "progressive",
        "stages": output_stages,
    }


__all__ = [
    "AIPlanImportError",
    "DEFAULT_GENERATED_PROBLEMS",
    "MAX_GENERATED_PROBLEMS",
    "MAX_GENERATION_CANDIDATES",
    "MAX_GENERATION_ROUNDS",
    "MAX_STALLED_GENERATION_ROUNDS",
    "MAX_ORGANIZE_PROBLEMS",
    "build_generation_candidates",
    "catalog_index",
    "deterministic_generated_ir",
    "deterministic_organize_ir",
    "extract_problem_refs",
    "filter_generated_problem_ids",
    "fit_stages_to_task_count",
    "lower_plan",
    "make_plan_id",
    "validate_generation_intent",
    "validate_generation_selection",
    "validate_generated_problem_ids",
    "validate_organize_ir",
]
