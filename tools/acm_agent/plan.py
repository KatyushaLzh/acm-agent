"""Training-plan schemas, validation, migration, and consistency checks.

Schema v1 is retained as a read-only compatibility format for the original
30-day data-structures plan.  Plan management persists schema v2 documents;
v2 deliberately contains scheduling data only and never stores completion
state (platform submissions and recorded attempts remain authoritative).
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from datetime import date, timedelta
import json
from pathlib import Path
import re
from typing import Any, Iterable, Mapping
from urllib.parse import urlparse


PROBLEM_LINK_RE = re.compile(
    r"\[[^\]]*?(?P<id>CF\d+[A-Z]\d*|P\d+)[^\]]*\]\((?P<url>https?://[^)]+)\)",
    re.IGNORECASE,
)
MARKDOWN_PROBLEM_LINK_RE = re.compile(
    r"\[(?P<label>[^\]]+)\]\((?P<url>https?://[^)]+)\)", re.IGNORECASE
)
CF_PATH_RE = re.compile(r"/(?:problemset/problem|contest)/(\d+)/(?:problem/)?([A-Z]\d*)", re.I)
LUOGU_PATH_RE = re.compile(r"/problem/(P\d+)", re.I)
STABLE_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
VALID_LEVELS = {"A", "B", "C", "SIM", "FINAL"}
VALID_SCHEDULE_MODES = {"dated", "progressive"}


class PlanError(ValueError):
    """Raised when a machine-readable plan violates its schema or invariants."""


@dataclass(frozen=True)
class PlanTask:
    task_key: str
    problem_id: str
    platform: str
    url: str
    level: str
    name: str = ""
    title: str = ""
    note: str = ""
    tags: tuple[str, ...] = ()

    @property
    def problem_key(self) -> str:
        return canonical_problem_key(self.problem_id, self.platform)

    # Read-only compatibility aliases for callers that still consume v1 task
    # objects. Scheduling and progressive completion no longer use these.
    @property
    def required(self) -> bool:
        return True

    @property
    def due_date(self) -> None:
        return None

    @property
    def unlock_at(self) -> None:
        return None


@dataclass(frozen=True)
class Replacement:
    condition: str | Mapping[str, Any]
    task: PlanTask
    replace_task_keys: tuple[str, ...] = ()
    replace_only_accepted: bool = False


@dataclass(frozen=True)
class PlanStage:
    stage_key: str
    position: int
    topic: str
    kind: str
    tasks: tuple[PlanTask, ...] = ()
    replacements: tuple[Replacement, ...] = ()
    due_date: date | None = None
    unlock_at: date | None = None
    selection_condition: str = ""

    # Compatibility aliases used by the pre-v2 recommendation service.
    @property
    def day(self) -> int:
        return self.position + 1

    @property
    def scheduled_date(self) -> date:
        if self.due_date is None:
            raise PlanError(f"{self.stage_key}: stage has no due date")
        return self.due_date


# Old name remains public so existing imports continue to work.
PlanDay = PlanStage


@dataclass(frozen=True)
class TrainingPlan:
    schema_version: int
    plan_id: str
    title: str
    description: str
    schedule_mode: str
    stages: tuple[PlanStage, ...]
    start_date: date | None = None
    end_date: date | None = None

    @property
    def days(self) -> tuple[PlanStage, ...]:
        return self.stages

    def all_tasks(self, *, include_replacements: bool = False) -> Iterable[PlanTask]:
        for stage in self.stages:
            yield from stage.tasks
            if include_replacements:
                for replacement in stage.replacements:
                    yield replacement.task


@dataclass
class PlanCheckResult:
    ok: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    stats: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "errors": self.errors,
            "warnings": self.warnings,
            "stats": self.stats,
        }


def _parse_date(value: Any, field_name: str) -> date:
    if not isinstance(value, str):
        raise PlanError(f"{field_name} must be an ISO date string")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise PlanError(f"invalid {field_name}: {value!r}") from exc


def _optional_date(value: Any, field_name: str) -> date | None:
    return None if value in (None, "") else _parse_date(value, field_name)


def _stable_key(value: Any, field_name: str) -> str:
    key = str(value or "").strip()
    if not STABLE_KEY_RE.fullmatch(key):
        raise PlanError(
            f"{field_name} must start with an ASCII letter/digit and contain only "
            "letters, digits, '.', '_' or '-'"
        )
    return key


def canonical_problem_key(problem_id: str, platform: str | None = None) -> str:
    problem_id = str(problem_id).strip().upper()
    inferred = "codeforces" if problem_id.startswith("CF") else "luogu" if problem_id.startswith("P") else ""
    platform = (platform or inferred).strip().lower()
    if platform == "cf":
        platform = "codeforces"
    if platform == "codeforces" and re.fullmatch(r"\d+[A-Z]\d*", problem_id):
        problem_id = f"CF{problem_id}"
    if platform not in {"codeforces", "luogu"}:
        raise PlanError(f"unsupported platform for {problem_id!r}: {platform!r}")
    if platform == "codeforces" and not re.fullmatch(r"CF\d+[A-Z]\d*", problem_id):
        raise PlanError(f"invalid Codeforces id: {problem_id!r}")
    if platform == "luogu" and not re.fullmatch(r"P\d+", problem_id):
        raise PlanError(f"invalid Luogu id: {problem_id!r}")
    return f"{platform}:{problem_id}"


def problem_url(problem_id: str, platform: str) -> str:
    key = canonical_problem_key(problem_id, platform)
    platform, normalized_id = key.split(":", 1)
    if platform == "codeforces":
        match = re.fullmatch(r"CF(\d+)([A-Z]\d*)", normalized_id)
        assert match is not None
        return f"https://codeforces.com/problemset/problem/{match.group(1)}/{match.group(2)}"
    return f"https://www.luogu.com.cn/problem/{normalized_id}"


def problem_id_from_url(url: str) -> tuple[str, str]:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if host in {"codeforces.com", "www.codeforces.com"}:
        match = CF_PATH_RE.search(parsed.path)
        if match:
            return "codeforces", f"CF{match.group(1)}{match.group(2).upper()}"
    if host in {"luogu.com.cn", "www.luogu.com.cn"}:
        match = LUOGU_PATH_RE.search(parsed.path)
        if match:
            return "luogu", match.group(1).upper()
    raise PlanError(f"unsupported or malformed problem URL: {url!r}")


def _load_task(raw: Mapping[str, Any], *, default_level: str = "") -> PlanTask:
    if not isinstance(raw, Mapping):
        raise PlanError("each task must be an object")
    required = {"task_key", "problem_id", "platform"}
    missing = sorted(required - raw.keys())
    if missing:
        raise PlanError(f"task misses fields: {', '.join(missing)}")
    task_key = _stable_key(raw["task_key"], "task.task_key")
    platform = str(raw["platform"]).strip().lower()
    problem_id = str(raw["problem_id"]).strip().upper()
    canonical_problem_key(problem_id, platform)
    level = str(raw.get("level", raw.get("difficulty", default_level))).strip().upper()
    if not level:
        level = "B"
    if level not in VALID_LEVELS:
        raise PlanError(f"invalid level {level!r} in {task_key!r}")
    tags_value = raw.get("tags", [])
    if not isinstance(tags_value, list) or any(not isinstance(tag, str) for tag in tags_value):
        raise PlanError(f"{task_key}: tags must be an array of strings")
    tags = tuple(dict.fromkeys(tag.strip() for tag in tags_value if tag.strip()))
    url = str(raw.get("url") or problem_url(problem_id, platform))
    url_platform, url_id = problem_id_from_url(url)
    if (url_platform, url_id) != (platform, problem_id):
        raise PlanError(
            f"{task_key}: id/platform {platform}/{problem_id} "
            f"does not match URL {url_platform}/{url_id}"
        )
    name = str(raw.get("name", "")).strip()
    title = str(raw.get("title", "")).strip()
    if not name:
        name = title or problem_id
    return PlanTask(
        task_key=task_key,
        problem_id=problem_id,
        platform=platform,
        url=url,
        level=level,
        name=name,
        title=title,
        note=str(raw.get("note", "")),
        tags=tags,
    )


def _raw_stage_tasks(raw_stage: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    result: list[Mapping[str, Any]] = [
        task for task in raw_stage.get("tasks", []) if isinstance(task, Mapping)
    ]
    for replacement in raw_stage.get("replacements", []):
        if isinstance(replacement, Mapping) and isinstance(replacement.get("task"), Mapping):
            result.append(replacement["task"])
    return result


def _authoritative_stage_date(
    raw_stage: Mapping[str, Any], field_name: str, stage_key: str
) -> date | None:
    """Lift a legacy per-task date only when doing so is semantics-preserving."""
    explicit = _optional_date(raw_stage.get(field_name), f"{stage_key}.{field_name}")
    raw_values = [task.get(field_name) for task in _raw_stage_tasks(raw_stage)]
    present = [
        _parse_date(value, f"{stage_key}.task.{field_name}")
        for value in raw_values if value not in (None, "")
    ]
    if explicit is not None:
        if any(value != explicit for value in present):
            raise PlanError(
                f"{stage_key}: task {field_name} conflicts with authoritative stage {field_name}"
            )
        return explicit
    if not present:
        return None
    if len(present) != len(raw_values):
        raise PlanError(
            f"{stage_key}: mixed task {field_name} values cannot be safely lifted to the stage"
        )
    if len(set(present)) != 1:
        raise PlanError(
            f"{stage_key}: different task {field_name} values cannot be represented by one stage date"
        )
    return present[0]


def _normalize_condition(raw: Any, field_name: str) -> dict[str, Any]:
    if not isinstance(raw, Mapping):
        raise PlanError(f"{field_name} must be a structured AC condition object")
    condition_type = str(raw.get("type", "ac")).strip().lower()
    if condition_type != "ac":
        raise PlanError(f"{field_name}.type must be 'ac'")
    mode = str(raw.get("mode", "any")).strip().lower()
    if mode not in {"any", "all"}:
        raise PlanError(f"{field_name}.mode must be 'any' or 'all'")
    keys = raw.get("problem_keys", raw.get("problems", []))
    if not isinstance(keys, list) or not keys:
        raise PlanError(f"{field_name}.problem_keys must be a non-empty array")
    normalized: list[str] = []
    for value in keys:
        text = str(value).strip()
        if ":" in text:
            platform, problem_id = text.split(":", 1)
            normalized.append(canonical_problem_key(problem_id, platform))
        else:
            normalized.append(canonical_problem_key(text))
    return {"type": "ac", "mode": mode, "problem_keys": list(dict.fromkeys(normalized))}


def _load_v2_replacement(raw: Mapping[str, Any], stage_key: str) -> Replacement:
    if not isinstance(raw, Mapping):
        raise PlanError(f"{stage_key}: each replacement must be an object")
    condition = _normalize_condition(raw.get("condition"), f"{stage_key}.replacement.condition")
    keys = raw.get("replace_task_keys", [])
    if not isinstance(keys, list) or any(not isinstance(key, str) for key in keys):
        raise PlanError(f"{stage_key}: replace_task_keys must be an array of strings")
    return Replacement(
        condition=condition,
        task=_load_task(raw.get("task", {})),
        replace_task_keys=tuple(_stable_key(key, "replacement.replace_task_keys[]") for key in keys),
        replace_only_accepted=bool(raw.get("replace_only_accepted", False)),
    )


def _load_v1(raw: Mapping[str, Any]) -> TrainingPlan:
    days: list[PlanStage] = []
    seen_task_keys: set[str] = set()
    for index, raw_day in enumerate(raw.get("days", [])):
        if not isinstance(raw_day, Mapping):
            raise PlanError("each day must be an object")
        day_number = int(raw_day["day"])
        legacy_stage = dict(raw_day)
        legacy_stage["due_date"] = raw_day["date"]
        due_date = _authoritative_stage_date(legacy_stage, "due_date", f"D{day_number}")
        unlock_at = _authoritative_stage_date(legacy_stage, "unlock_at", f"D{day_number}")
        tasks = tuple(_load_task(item) for item in raw_day.get("tasks", []))
        replacements: list[Replacement] = []
        for item in raw_day.get("replacements", []):
            if not isinstance(item, Mapping) or not str(item.get("condition", "")).strip():
                raise PlanError(f"D{day_number}: replacement requires a condition")
            replacements.append(Replacement(str(item["condition"]).strip(), _load_task(item["task"])))
        for task in (*tasks, *(replacement.task for replacement in replacements)):
            if task.task_key in seen_task_keys:
                raise PlanError(f"duplicate task_key: {task.task_key}")
            seen_task_keys.add(task.task_key)
        days.append(
            PlanStage(
                stage_key=f"d{day_number:02d}",
                position=index,
                topic=str(raw_day["topic"]),
                kind=str(raw_day.get("kind", "practice")),
                tasks=tasks,
                replacements=tuple(replacements),
                selection_condition=str(raw_day.get("selection_condition", "")),
                due_date=due_date,
                unlock_at=unlock_at,
            )
        )
    plan = TrainingPlan(
        schema_version=1,
        plan_id=str(raw.get("plan_id", "data-structures-30d")),
        title=str(raw.get("title", "")),
        description=str(raw.get("description", "")),
        schedule_mode="dated",
        start_date=_parse_date(raw["start_date"], "start_date"),
        end_date=_parse_date(raw["end_date"], "end_date"),
        stages=tuple(days),
    )
    _validate_v1(plan)
    return plan


def _load_v2(raw: Mapping[str, Any]) -> TrainingPlan:
    plan_id = _stable_key(raw.get("plan_id"), "plan_id")
    schedule_mode = str(raw.get("schedule_mode", "")).strip().lower()
    if schedule_mode not in VALID_SCHEDULE_MODES:
        raise PlanError("schedule_mode must be 'dated' or 'progressive'")
    raw_stages = raw.get("stages")
    if not isinstance(raw_stages, list):
        raise PlanError("stages must be an array")
    stages: list[PlanStage] = []
    seen_stage_keys: set[str] = set()
    seen_task_keys: set[str] = set()
    for position, raw_stage in enumerate(raw_stages):
        if not isinstance(raw_stage, Mapping):
            raise PlanError("each stage must be an object")
        stage_key = _stable_key(raw_stage.get("stage_key"), "stage.stage_key")
        if stage_key in seen_stage_keys:
            raise PlanError(f"duplicate stage_key: {stage_key}")
        seen_stage_keys.add(stage_key)
        tasks = tuple(_load_task(item) for item in raw_stage.get("tasks", []))
        replacements = tuple(
            _load_v2_replacement(item, stage_key) for item in raw_stage.get("replacements", [])
        )
        for task in (*tasks, *(replacement.task for replacement in replacements)):
            if task.task_key in seen_task_keys:
                raise PlanError(f"duplicate task_key: {task.task_key}")
            seen_task_keys.add(task.task_key)
        normal_keys = {task.task_key for task in tasks}
        for replacement in replacements:
            missing = sorted(set(replacement.replace_task_keys) - normal_keys)
            if missing:
                raise PlanError(
                    f"{stage_key}: replacement references unknown task keys: {', '.join(missing)}"
                )
        due_date = _authoritative_stage_date(raw_stage, "due_date", stage_key)
        unlock_at = _authoritative_stage_date(raw_stage, "unlock_at", stage_key)
        if due_date and unlock_at and unlock_at > due_date:
            raise PlanError(f"{stage_key}: unlock_at must not be after due_date")
        if schedule_mode == "dated" and due_date is None:
            raise PlanError(f"{stage_key}: dated stages require due_date")
        stages.append(
            PlanStage(
                stage_key=stage_key,
                position=position,
                topic=str(raw_stage.get("topic", "")),
                kind=str(raw_stage.get("kind", raw_stage.get("type", "practice"))),
                tasks=tasks,
                replacements=replacements,
                due_date=due_date,
                unlock_at=unlock_at,
                selection_condition=str(raw_stage.get("selection_condition", "")),
            )
        )
    # Legacy target ratios are accepted but intentionally ignored. Platform
    # distribution is derived from the actual main tasks in every v2 output.
    target_raw = raw.get("platform_target", raw.get("platform_ratio"))
    if target_raw is not None and not isinstance(target_raw, Mapping):
        raise PlanError("legacy platform_target must be an object when present")
    plan = TrainingPlan(
        schema_version=2,
        plan_id=plan_id,
        title=str(raw.get("title", "")),
        description=str(raw.get("description", "")),
        schedule_mode=schedule_mode,
        stages=tuple(stages),
    )
    _validate_common(plan)
    return plan


def load_plan_data(raw: Mapping[str, Any] | str | bytes) -> TrainingPlan:
    """Load and validate an in-memory plan document."""
    if isinstance(raw, (str, bytes)):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise PlanError(
                f"invalid JSON at line {exc.lineno}, column {exc.colno}: {exc.msg}"
            ) from exc
    if not isinstance(raw, Mapping):
        raise PlanError("plan root must be an object")
    try:
        version = int(raw.get("schema_version", 0))
    except (TypeError, ValueError) as exc:
        raise PlanError("schema_version must be 1 or 2") from exc
    if version == 1:
        return _load_v1(raw)
    if version == 2:
        return _load_v2(raw)
    raise PlanError(f"unsupported schema_version: {version}")


def load_plan(path: str | Path) -> TrainingPlan:
    return load_plan_data(Path(path).read_text(encoding="utf-8-sig"))


def _validate_common(plan: TrainingPlan) -> None:
    if not plan.title.strip():
        raise PlanError("title must not be empty")
    if not plan.stages:
        raise PlanError("plan must contain at least one stage")
    previous_due: date | None = None
    if plan.schedule_mode == "dated":
        for stage in plan.stages:
            if stage.due_date is not None:
                if previous_due is not None and stage.due_date < previous_due:
                    raise PlanError("dated stage due dates must be non-decreasing")
                previous_due = stage.due_date


def _validate_v1(plan: TrainingPlan) -> None:
    if len(plan.stages) != 30:
        raise PlanError(f"expected 30 days, got {len(plan.stages)}")
    assert plan.start_date is not None and plan.end_date is not None
    if plan.end_date - plan.start_date != timedelta(days=29):
        raise PlanError("start_date/end_date must describe exactly 30 days")
    for offset, stage in enumerate(plan.stages):
        expected_date = plan.start_date + timedelta(days=offset)
        if stage.day != offset + 1 or stage.due_date != expected_date:
            raise PlanError(
                f"day sequence mismatch at index {offset}: D{stage.day} {stage.due_date}, "
                f"expected D{offset + 1} {expected_date}"
            )
        if stage.kind in {"virtual_contest", "contest"}:
            if stage.unlock_at != stage.due_date:
                raise PlanError(f"D{stage.day}: contest unlock_at must equal its scheduled date")
    _validate_common(plan)


def _task_to_dict(task: PlanTask) -> dict[str, Any]:
    result: dict[str, Any] = {
        "task_key": task.task_key,
        "platform": task.platform,
        "problem_id": task.problem_id,
        "url": task.url,
        "level": task.level,
        "tags": list(task.tags),
        "name": task.name or task.problem_id,
    }
    if task.title:
        result["title"] = task.title
    if task.note:
        result["note"] = task.note
    return result


def plan_to_dict(plan: TrainingPlan, *, force_v2: bool = False) -> dict[str, Any]:
    """Return a JSON-serializable canonical document."""
    if plan.schema_version == 1 and not force_v2:
        days: list[dict[str, Any]] = []
        for stage in plan.stages:
            day: dict[str, Any] = {
                "day": stage.day,
                "date": stage.scheduled_date.isoformat(),
                "topic": stage.topic,
                "kind": stage.kind,
                "tasks": [_task_to_dict(task) for task in stage.tasks],
            }
            if stage.unlock_at:
                day["unlock_at"] = stage.unlock_at.isoformat()
            if stage.selection_condition:
                day["selection_condition"] = stage.selection_condition
            if stage.replacements:
                day["replacements"] = [
                    {"condition": replacement.condition, "task": _task_to_dict(replacement.task)}
                    for replacement in stage.replacements
                ]
            days.append(day)
        assert plan.start_date is not None and plan.end_date is not None
        return {
            "schema_version": 1,
            "title": plan.title,
            "start_date": plan.start_date.isoformat(),
            "end_date": plan.end_date.isoformat(),
            "days": days,
        }
    result = {
        "schema_version": 2,
        "plan_id": plan.plan_id,
        "title": plan.title,
        "description": plan.description,
        "schedule_mode": plan.schedule_mode,
        "stages": [],
    }
    for stage in plan.stages:
        item: dict[str, Any] = {
            "stage_key": stage.stage_key,
            "topic": stage.topic,
            "kind": stage.kind,
            "tasks": [_task_to_dict(task) for task in stage.tasks],
        }
        if stage.due_date:
            item["due_date"] = stage.due_date.isoformat()
        if stage.unlock_at:
            item["unlock_at"] = stage.unlock_at.isoformat()
        if stage.selection_condition:
            item["selection_condition"] = stage.selection_condition
        if stage.replacements:
            structured = [
                replacement for replacement in stage.replacements
                if isinstance(replacement.condition, Mapping)
            ]
            if structured:
                item["replacements"] = [
                    {
                        "condition": dict(replacement.condition),
                        "replace_task_keys": list(replacement.replace_task_keys),
                        "replace_only_accepted": replacement.replace_only_accepted,
                        "task": _task_to_dict(replacement.task),
                    }
                    for replacement in structured
                ]
        result["stages"].append(item)
    return result


def dump_plan(plan: TrainingPlan) -> str:
    return json.dumps(plan_to_dict(plan), ensure_ascii=False, indent=2) + "\n"


def _legacy_replacement_to_v2(stage: PlanStage, replacement: Replacement) -> dict[str, Any]:
    text = str(replacement.condition)
    mentioned = re.findall(r"(?:CF\d+[A-Z]\d*|P\d+)", text, re.I)
    replace_only_accepted = False
    if mentioned:
        problem_keys = [canonical_problem_key(problem_id) for problem_id in mentioned]
        replace_keys = [
            task.task_key for task in stage.tasks if task.problem_key in set(problem_keys)
        ]
    elif "任一" in text:
        problem_keys = [task.problem_key for task in stage.tasks]
        replace_keys = [task.task_key for task in stage.tasks]
        replace_only_accepted = True
    else:
        raise PlanError(
            f"{stage.stage_key}: legacy replacement cannot be migrated to a structured AC condition"
        )
    return {
        "condition": {"type": "ac", "mode": "any", "problem_keys": problem_keys},
        "replace_task_keys": replace_keys,
        "replace_only_accepted": replace_only_accepted,
        "task": _task_to_dict(replacement.task),
    }


def convert_v1_to_v2(raw: Mapping[str, Any] | TrainingPlan) -> dict[str, Any]:
    """Convert a validated legacy plan to the canonical editable v2 format."""
    plan = raw if isinstance(raw, TrainingPlan) else load_plan_data(raw)
    if plan.schema_version == 2:
        return plan_to_dict(plan)
    result = plan_to_dict(plan, force_v2=True)
    result["plan_id"] = plan.plan_id
    for source_stage, target_stage in zip(plan.stages, result["stages"]):
        if source_stage.replacements:
            target_stage["replacements"] = [
                _legacy_replacement_to_v2(source_stage, replacement)
                for replacement in source_stage.replacements
            ]
    # Re-validate so conversion never produces a document the editor cannot save.
    return plan_to_dict(load_plan_data(result))


def validate_plan_data(raw: Mapping[str, Any] | str | bytes) -> PlanCheckResult:
    """Validate an upload and return errors with JSON line/column where available."""
    try:
        plan = load_plan_data(raw)
        if plan.schema_version == 1:
            plan = load_plan_data(convert_v1_to_v2(plan))
    except (KeyError, TypeError, ValueError, PlanError) as exc:
        return PlanCheckResult(False, [str(exc)])
    platform_stats = plan_platform_stats(plan)
    return PlanCheckResult(
        True,
        stats={
            "plan_id": plan.plan_id,
            "stages": len(plan.stages),
            "tasks": platform_stats["task_count"],
            "required_tasks": platform_stats["task_count"],
            "required_by_platform": platform_stats["platform_counts"],
            **platform_stats,
        },
    )


def plan_platform_stats(plan: TrainingPlan) -> dict[str, Any]:
    """Derive actual platform distribution from main tasks only.

    Conditional replacement candidates are deliberately excluded, matching the
    plan list's ``task_count`` and progress denominator.
    """
    tasks = list(plan.all_tasks())
    counts = Counter(task.platform for task in tasks)
    total = len(tasks)
    ratios = {platform: count / total for platform, count in counts.items()} if total else {}
    return {
        "task_count": total,
        "platform_counts": dict(sorted(counts.items())),
        "platform_ratio": dict(sorted(ratios.items())),
        "replacement_count": sum(len(stage.replacements) for stage in plan.stages),
        "platform_count_includes_replacements": False,
    }


def _readme_problem_links(markdown: str) -> Counter[tuple[str, str]]:
    result: Counter[tuple[str, str]] = Counter()
    for match in PROBLEM_LINK_RE.finditer(markdown):
        problem_id = match.group("id").upper()
        url = match.group("url")
        platform, url_id = problem_id_from_url(url)
        if url_id != problem_id:
            result[(f"MISMATCH:{problem_id}", url)] += 1
        else:
            result[(canonical_problem_key(problem_id, platform), url)] += 1
    return result


def readme_problem_names(markdown: str) -> dict[str, str]:
    """Extract canonical problem names from README link labels."""
    result: dict[str, str] = {}
    for match in MARKDOWN_PROBLEM_LINK_RE.finditer(markdown):
        try:
            platform, problem_id = problem_id_from_url(match.group("url"))
        except PlanError:
            continue
        label = match.group("label").strip()
        name = re.sub(
            rf"^{re.escape(problem_id)}(?:\s*[-—:：·])?\s*", "", label, flags=re.I
        ).strip()
        result.setdefault(canonical_problem_key(problem_id, platform), name or label or problem_id)
    return result


def enrich_plan_names(
    document: Mapping[str, Any], names: Mapping[str, str]
) -> dict[str, Any]:
    """Return a copy whose tasks have display names, using ids as fallback."""
    result = json.loads(json.dumps(document, ensure_ascii=False))
    for stage in result.get("stages", []):
        tasks = list(stage.get("tasks", []))
        tasks.extend(
            replacement["task"] for replacement in stage.get("replacements", [])
            if isinstance(replacement, Mapping) and isinstance(replacement.get("task"), Mapping)
        )
        for task in tasks:
            key = canonical_problem_key(task["problem_id"], task["platform"])
            current = str(task.get("name") or task.get("title") or "").strip()
            if not current or current.upper() == str(task["problem_id"]).upper():
                task["name"] = str(names.get(key) or task["problem_id"])
    return result


def check_plan(readme_path: str | Path, plan_path: str | Path) -> PlanCheckResult:
    errors: list[str] = []
    warnings: list[str] = []
    try:
        plan = load_plan(plan_path)
    except (OSError, json.JSONDecodeError, KeyError, TypeError, PlanError) as exc:
        return PlanCheckResult(False, [str(exc)])
    try:
        markdown = Path(readme_path).read_text(encoding="utf-8-sig")
    except OSError as exc:
        return PlanCheckResult(False, [str(exc)])
    readme_links = _readme_problem_links(markdown)
    plan_links: Counter[tuple[str, str]] = Counter(
        (task.problem_key, task.url) for task in plan.all_tasks(include_replacements=True)
    )
    for ref, count in sorted((readme_links - plan_links).items()):
        errors.append(f"README-only problem reference ({count}x): {ref[0]} {ref[1]}")
    for ref, count in sorted((plan_links - readme_links).items()):
        errors.append(f"plan-only problem reference ({count}x): {ref[0]} {ref[1]}")
    platform_stats = plan_platform_stats(plan)
    total = platform_stats["task_count"]
    counts = platform_stats["platform_counts"]
    ratios = platform_stats["platform_ratio"]
    stats = {
        "days": len(plan.stages),
        "task_occurrences": sum(1 for _ in plan.all_tasks()),
        "replacement_occurrences": sum(len(stage.replacements) for stage in plan.stages),
        "required_tasks": total,
        "required_by_platform": dict(sorted(counts.items())),
        "required_platform_ratio": dict(sorted(ratios.items())),
        "platform_counts": dict(sorted(counts.items())),
        "tasks_by_platform": dict(sorted(counts.items())),
        "platform_ratio": dict(sorted(ratios.items())),
        "platform_count_includes_replacements": False,
        "readme_problem_links": sum(readme_links.values()),
    }
    return PlanCheckResult(not errors, errors, warnings, stats)


def condition_matches(condition: Mapping[str, Any], accepted: set[str]) -> bool:
    """Evaluate a v2 replacement condition against canonical accepted keys."""
    normalized = _normalize_condition(condition, "condition")
    matches = [key in accepted for key in normalized["problem_keys"]]
    return all(matches) if normalized["mode"] == "all" else any(matches)


def plan_task_records(
    plan: TrainingPlan,
    *,
    accepted: set[str] | None = None,
    active_stage_keys: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Flatten a plan for persistence/recommendation without coupling to SQLite."""
    accepted = accepted or set()
    records: list[dict[str, Any]] = []
    for stage in plan.stages:
        if active_stage_keys is not None and stage.stage_key not in active_stage_keys:
            continue
        selected = list(stage.tasks)
        selected_source = {task.task_key: "plan" for task in selected}
        if plan.schema_version == 2:
            for replacement in stage.replacements:
                assert isinstance(replacement.condition, Mapping)
                if not condition_matches(replacement.condition, accepted):
                    continue
                remove_keys = set(replacement.replace_task_keys)
                if replacement.replace_only_accepted:
                    remove_keys = {
                        task.task_key for task in selected
                        if task.task_key in remove_keys and task.problem_key in accepted
                    }
                selected = [task for task in selected if task.task_key not in remove_keys]
                selected.append(replacement.task)
                selected_source[replacement.task.task_key] = "plan_replacement"
        for task in selected:
            due = stage.due_date
            unlock = stage.unlock_at
            records.append(
                {
                    "plan_id": plan.plan_id,
                    "stage_key": stage.stage_key,
                    "task_key": task.task_key,
                    "problem_key": task.problem_key,
                    "problem_id": task.problem_id,
                    "platform": task.platform,
                    "url": task.url,
                    "name": task.name or task.problem_id,
                    "title": task.title,
                    "level": task.level,
                    "tags": list(task.tags),
                    "topic": stage.topic,
                    "day": stage.day,
                    "stage_position": stage.position,
                    "due_date": due.isoformat() if due else None,
                    "unlock_at": unlock.isoformat() if unlock else None,
                    "source": selected_source.get(task.task_key, "plan"),
                }
            )
    return records


def plan_template() -> dict[str, Any]:
    """Return a small valid v2 document suitable for download/import."""
    return {
        "schema_version": 2,
        "plan_id": "my-plan",
        "title": "我的训练题单",
        "description": "",
        "schedule_mode": "progressive",
        "stages": [
            {
                "stage_key": "stage-1",
                "topic": "第一阶段",
                "kind": "practice",
                "tasks": [
                    {
                        "task_key": "stage-1-task-1",
                        "platform": "codeforces",
                        "problem_id": "CF1A",
                        "name": "Theatre Square",
                        "level": "A",
                        "tags": [],
                    }
                ],
            }
        ],
    }


__all__ = [
    "PlanError", "PlanTask", "Replacement", "PlanStage", "PlanDay",
    "TrainingPlan", "PlanCheckResult", "canonical_problem_key", "problem_url",
    "problem_id_from_url", "load_plan_data", "load_plan", "plan_to_dict",
    "dump_plan", "convert_v1_to_v2", "validate_plan_data", "check_plan",
    "condition_matches", "plan_task_records", "plan_template",
    "readme_problem_names", "enrich_plan_names", "plan_platform_stats",
]
