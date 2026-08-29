"""Deterministic, explainable problem recommendation primitives."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
import re
import json
from statistics import median
from typing import Any, Iterable, Mapping, Sequence

from .plan import PlanError, canonical_problem_key


LUOGU_CF_EQUIVALENT = {1: 800, 2: 1000, 3: 1300, 4: 1600, 5: 1900, 6: 2200, 7: 2500}
# Legacy public constant retained for compatibility. Difficulty scoring now
# uses three explicit targets instead of offsets from one baseline.
SLOT_OFFSETS = {"recovery": -150, "main": 50, "stretch": 250}
DIFFICULTY_BANDS = {
    "recovery": "current_plus_100",
    "main": "recent_solved_average",
    "stretch": "target_rating",
}
DIFFICULTY_BAND_LABELS = {
    "recovery": "当前 rating + 100",
    "main": "近期已解决均值",
    "stretch": "目标 rating",
}
ACCEPTED_RESULTS = {"AC", "ACCEPTED", "OK"}
FAILED_RESULTS = {"WA", "WRONG_ANSWER", "TLE", "RE", "RUNTIME_ERROR", "MLE", "ABANDONED"}
SOURCE_MODES = {"balanced", "catalog_only", "plan_only"}


def _as_date(value: Any) -> date | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value)[:10])


def _as_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, datetime.min.time(), tzinfo=timezone.utc)
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _get(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, Mapping):
        return item.get(name, default)
    return getattr(item, name, default)


def _normalise_key(value: str) -> str:
    text = str(value).strip()
    if ":" in text:
        platform, problem_id = text.split(":", 1)
        return canonical_problem_key(problem_id, platform)
    return canonical_problem_key(text)


@dataclass(frozen=True)
class PlanSource:
    """One plan occurrence of a problem, retained when occurrences are merged."""

    plan_id: str = ""
    plan_title: str = ""
    stage_key: str = ""
    stage_order: int | None = None
    task_key: str = ""
    due_date: date | None = None
    unlock_at: date | None = None
    plan_level: str = ""

    @classmethod
    def from_value(cls, value: PlanSource | Mapping[str, Any] | Any) -> PlanSource:
        if isinstance(value, cls):
            return value
        stage_order = _get(value, "stage_order", _get(value, "plan_day", _get(value, "day")))
        return cls(
            plan_id=str(_get(value, "plan_id", "")),
            plan_title=str(_get(value, "plan_title", _get(value, "title", ""))),
            stage_key=str(_get(value, "stage_key", "")),
            stage_order=int(stage_order) if stage_order not in (None, "") else None,
            task_key=str(_get(value, "task_key", "")),
            due_date=_as_date(_get(value, "due_date")),
            unlock_at=_as_date(_get(value, "unlock_at")),
            plan_level=str(_get(value, "plan_level", _get(value, "level", ""))).upper(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "plan_id": self.plan_id,
            "plan_title": self.plan_title,
            "stage_key": self.stage_key,
            "stage_order": self.stage_order,
            "task_key": self.task_key,
            "due_date": self.due_date.isoformat() if self.due_date else None,
            "unlock_at": self.unlock_at.isoformat() if self.unlock_at else None,
            "plan_level": self.plan_level,
        }


@dataclass(frozen=True)
class Candidate:
    problem_key: str
    problem_id: str
    platform: str
    title: str = ""
    url: str = ""
    rating: int | None = None
    difficulty: int | None = None
    tags: tuple[str, ...] = ()
    status: str = "unknown"
    source: str = "catalog"
    task_key: str = ""
    plan_day: int | None = None
    plan_level: str = ""
    due_date: date | None = None
    unlock_at: date | None = None
    review_due: date | None = None
    plan_id: str = ""
    plan_title: str = ""
    stage_key: str = ""
    stage_order: int | None = None
    plan_sources: tuple[PlanSource, ...] = ()

    @property
    def equivalent_rating(self) -> int | None:
        if self.rating is not None:
            return self.rating
        if self.platform == "luogu" and self.difficulty is not None:
            return LUOGU_CF_EQUIVALENT.get(self.difficulty)
        return None

    @classmethod
    def from_value(cls, value: Candidate | Mapping[str, Any] | Any) -> Candidate:
        if isinstance(value, cls):
            return value
        problem_id = str(_get(value, "problem_id", _get(value, "id", ""))).upper()
        platform = str(_get(value, "platform", "")).lower()
        key = str(_get(value, "problem_key", ""))
        if not key:
            key = canonical_problem_key(problem_id, platform)
        else:
            key = _normalise_key(key)
        rating = _get(value, "rating")
        difficulty = _get(value, "difficulty")
        tags = _get(value, "tags", None)
        if tags is None:
            raw_tags = _get(value, "tags_json", "[]")
            try:
                tags = json.loads(raw_tags) if isinstance(raw_tags, str) else raw_tags
            except json.JSONDecodeError:
                tags = ()
        raw_plan_sources = _get(value, "plan_sources", ()) or ()
        if isinstance(raw_plan_sources, str):
            try:
                raw_plan_sources = json.loads(raw_plan_sources)
            except json.JSONDecodeError:
                raw_plan_sources = ()
        stage_order = _get(value, "stage_order", _get(value, "plan_day", _get(value, "day")))
        return cls(
            problem_key=key,
            problem_id=problem_id or key.split(":", 1)[-1].upper(),
            platform=platform or key.split(":", 1)[0],
            title=str(_get(value, "title", _get(value, "name", ""))),
            url=str(_get(value, "url", "")),
            rating=int(rating) if rating not in (None, "") else None,
            difficulty=int(difficulty) if difficulty not in (None, "") else None,
            tags=tuple(str(tag) for tag in (tags or ())),
            status=str(_get(value, "status", "unknown")).lower(),
            source=str(_get(value, "source", "catalog")),
            task_key=str(_get(value, "task_key", "")),
            plan_day=int(_get(value, "plan_day", _get(value, "day")))
            if _get(value, "plan_day", _get(value, "day")) not in (None, "")
            else None,
            plan_level=str(_get(value, "plan_level", _get(value, "level", ""))).upper(),
            due_date=_as_date(_get(value, "due_date")),
            unlock_at=_as_date(_get(value, "unlock_at")),
            review_due=_as_date(_get(value, "review_due")),
            plan_id=str(_get(value, "plan_id", "")),
            plan_title=str(_get(value, "plan_title", "")),
            stage_key=str(_get(value, "stage_key", "")),
            stage_order=int(stage_order) if stage_order not in (None, "") else None,
            plan_sources=tuple(PlanSource.from_value(item) for item in raw_plan_sources),
        )

    def as_plan_source(self) -> PlanSource:
        return PlanSource(
            plan_id=self.plan_id,
            plan_title=self.plan_title,
            stage_key=self.stage_key,
            stage_order=self.stage_order if self.stage_order is not None else self.plan_day,
            task_key=self.task_key,
            due_date=self.due_date,
            unlock_at=self.unlock_at,
            plan_level=self.plan_level,
        )


@dataclass(frozen=True)
class Recommendation:
    slot: str
    problem_key: str
    problem_id: str
    platform: str
    title: str
    url: str
    equivalent_rating: int | None
    difficulty_target: int
    difficulty_band: str
    score: float
    breakdown: Mapping[str, float]
    reasons: tuple[str, ...] = ()
    task_key: str = ""
    tags: tuple[str, ...] = ()
    source: str = "catalog"
    plan_sources: tuple[PlanSource, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "slot": self.slot,
            "problem_key": self.problem_key,
            "problem_id": self.problem_id,
            "platform": self.platform,
            "title": self.title,
            "url": self.url,
            "equivalent_rating": self.equivalent_rating,
            "difficulty_target": self.difficulty_target,
            "difficulty_band": self.difficulty_band,
            "score": self.score,
            "breakdown": dict(self.breakdown),
            "reasons": list(self.reasons),
            "task_key": self.task_key,
            "tags": list(self.tags),
            "source": self.source,
            "plan_sources": [source.to_dict() for source in self.plan_sources],
        }


def estimate_cf_baseline(
    profile_rating: int | None,
    recent_cf_accepted: Iterable[int | Mapping[str, Any] | Any],
    *,
    target_rating: int | None = None,
    default: int = 1600,
) -> int:
    """Prefer the configured target, then profile rating and recent AC median."""
    if target_rating is not None and int(target_rating) > 0:
        return int(target_rating)
    if profile_rating is not None and int(profile_rating) > 0:
        return int(profile_rating)
    records = list(recent_cf_accepted)
    if records and not isinstance(records[0], int):
        records.sort(
            key=lambda item: _as_datetime(
                _get(item, "accepted_at", _get(item, "created_at", _get(item, "timestamp")))
            )
            or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
    ratings: list[int] = []
    seen: set[str] = set()
    for index, item in enumerate(records):
        if isinstance(item, int):
            rating, key = item, f"index:{index}"
        else:
            result = str(_get(item, "result", _get(item, "verdict", "AC"))).upper()
            if result not in ACCEPTED_RESULTS:
                continue
            rating = _get(item, "rating")
            key = str(_get(item, "problem_key", _get(item, "problem_id", f"index:{index}")))
        if rating in (None, "") or key in seen:
            continue
        seen.add(key)
        ratings.append(int(rating))
        if len(ratings) == 30:
            break
    return int(median(ratings)) if ratings else default


def luogu_equivalent(difficulty: int | None) -> int | None:
    return LUOGU_CF_EQUIVALENT.get(int(difficulty)) if difficulty is not None else None


def _difficulty_distance(equivalent_rating: int | None, target: int) -> float:
    if equivalent_rating is None:
        return float("inf")
    return float(abs(int(equivalent_rating) - int(target)))


def recommendation_difficulty_targets(
    current_cf_rating: int | None,
    recent_solved_equivalents: Iterable[int],
    *,
    target_rating: int | None = None,
    default: int = 1600,
) -> dict[str, int]:
    """Return the three equally represented recommendation difficulty targets."""

    recent: list[int] = []
    for value in recent_solved_equivalents:
        try:
            converted = int(value)
        except (TypeError, ValueError):
            continue
        if converted > 0:
            recent.append(converted)
    recent_average = round(sum(recent) / len(recent)) if recent else None
    current_base = (
        int(current_cf_rating)
        if current_cf_rating is not None and int(current_cf_rating) > 0
        else recent_average
        if recent_average is not None
        else int(target_rating)
        if target_rating is not None and int(target_rating) > 0
        else int(default)
    )
    current_plus_100 = current_base + 100
    return {
        "recovery": current_plus_100,
        "main": recent_average if recent_average is not None else current_plus_100,
        "stretch": (
            int(target_rating)
            if target_rating is not None and int(target_rating) > 0
            else current_plus_100
        ),
    }


def compute_weakness(
    attempts: Iterable[Mapping[str, Any] | Any],
    *,
    now: date | datetime | None = None,
    window_days: int = 90,
) -> dict[str, float]:
    """Return result/failure-based per-tag weakness from recent attempts.

    Hint level is retained as attempt metadata but does not affect weakness.
    """
    today = (now.date() if isinstance(now, datetime) else now) or date.today()
    cutoff = today - timedelta(days=window_days)
    score: defaultdict[str, float] = defaultdict(float)
    for attempt in attempts:
        attempted = _as_date(
            _get(attempt, "closed_at", _get(attempt, "attempted_at", _get(attempt, "date")))
        )
        if attempted is not None and attempted < cutoff:
            continue
        tags = tuple(str(tag) for tag in (_get(attempt, "tags", ()) or ()))
        result = str(_get(attempt, "result", "")).upper()
        failure_mode = str(_get(attempt, "failure_mode", "")).lower()
        delta = 0.0
        if result == "ABANDONED":
            delta += 3.0
        elif result in FAILED_RESULTS:
            delta += 2.0
        if failure_mode in {"selection", "modeling", "invariant"}:
            delta += 1.0
        if result in ACCEPTED_RESULTS:
            delta -= 1.0
        for tag in tags:
            score[tag] += delta
    return {tag: round(max(0.0, value), 3) for tag, value in sorted(score.items())}


def _normalise_accepted(values: Iterable[str]) -> set[str]:
    accepted: set[str] = set()
    for value in values:
        accepted.add(_normalise_key(str(value)))
    return accepted


def _history_fields(history: Sequence[Mapping[str, Any] | Any]) -> tuple[list[str], list[str]]:
    keys: list[str] = []
    platforms: list[str] = []
    for item in history[-30:]:
        if isinstance(item, str):
            try:
                key = _normalise_key(item)
            except PlanError:
                key = item
            platform = key.split(":", 1)[0] if ":" in key else ""
        else:
            key = str(_get(item, "problem_key", ""))
            if not key and _get(item, "platform") and _get(item, "problem_id"):
                key = f"{_get(item, 'platform')}:{_get(item, 'problem_id')}"
            try:
                key = _normalise_key(key) if key else ""
            except PlanError:
                pass
            platform = str(_get(item, "platform", "")).lower() or (
                key.split(":", 1)[0] if ":" in key else ""
            )
        keys.append(key)
        platforms.append(platform)
    return keys, platforms


def _history_plan_ids(item: Mapping[str, Any] | Any) -> set[str]:
    result: set[str] = set()
    direct = _get(item, "plan_id", "")
    if direct:
        result.add(str(direct))
    raw_ids = _get(item, "plan_ids", ()) or ()
    if isinstance(raw_ids, str):
        try:
            raw_ids = json.loads(raw_ids)
        except json.JSONDecodeError:
            raw_ids = (raw_ids,)
    result.update(str(plan_id) for plan_id in raw_ids if str(plan_id))
    raw_sources = _get(item, "plan_sources", ()) or ()
    if isinstance(raw_sources, str):
        try:
            raw_sources = json.loads(raw_sources)
        except json.JSONDecodeError:
            raw_sources = ()
    for source in raw_sources:
        plan_id = _get(source, "plan_id", "")
        if plan_id:
            result.add(str(plan_id))
    return result


def plan_fairness_order(
    plan_ids: Iterable[str],
    recommendation_history: Sequence[Mapping[str, Any] | Any] = (),
) -> tuple[str, ...]:
    """Order plans from least recently represented to most recently represented.

    History must be chronological (oldest to newest), matching ``recommend``'s
    existing history contract. Unseen plans win first; total appearances and the
    plan id provide deterministic tie-breaks.
    """
    unique_ids = sorted({str(plan_id) for plan_id in plan_ids if str(plan_id)})
    counts: Counter[str] = Counter()
    last_seen = {plan_id: -1 for plan_id in unique_ids}
    for index, item in enumerate(recommendation_history):
        for plan_id in _history_plan_ids(item):
            if plan_id in last_seen:
                counts[plan_id] += 1
                last_seen[plan_id] = index
    return tuple(sorted(unique_ids, key=lambda plan_id: (last_seen[plan_id], counts[plan_id], plan_id)))


def _plan_source_sort_key(source: PlanSource) -> tuple[Any, ...]:
    return (
        source.due_date or date.max,
        source.stage_order if source.stage_order is not None else 10**9,
        source.plan_id,
        source.stage_key,
        source.task_key,
    )


def _candidate_plan_sources(candidate: Candidate) -> tuple[PlanSource, ...]:
    sources = list(candidate.plan_sources)
    if candidate.source == "plan" and not sources:
        sources.append(candidate.as_plan_source())
    deduplicated = {
        (
            source.plan_id,
            source.plan_title,
            source.stage_key,
            source.stage_order,
            source.task_key,
            source.due_date,
            source.unlock_at,
            source.plan_level,
        ): source
        for source in sources
    }
    return tuple(sorted(deduplicated.values(), key=_plan_source_sort_key))


def _merge_candidates(values: Sequence[Candidate]) -> Candidate:
    """Merge the catalog metadata and every plan occurrence for one problem."""
    ordered = sorted(
        values,
        key=lambda candidate: (
            candidate.source == "plan",
            candidate.problem_id,
            candidate.task_key,
            candidate.title,
        ),
    )
    base = ordered[0]
    plan_sources = tuple(
        sorted(
            {
                (
                    source.plan_id,
                    source.plan_title,
                    source.stage_key,
                    source.stage_order,
                    source.task_key,
                    source.due_date,
                    source.unlock_at,
                    source.plan_level,
                ): source
                for candidate in ordered
                for source in _candidate_plan_sources(candidate)
            }.values(),
            key=_plan_source_sort_key,
        )
    )
    primary_source = plan_sources[0] if plan_sources else None

    def first_text(name: str) -> str:
        return next((str(_get(item, name)) for item in ordered if _get(item, name)), "")

    def first_number(name: str) -> int | None:
        value = next((_get(item, name) for item in ordered if _get(item, name) is not None), None)
        return int(value) if value is not None else None

    due_dates = [source.due_date for source in plan_sources if source.due_date is not None]
    if not due_dates:
        due_dates = [item.due_date for item in ordered if item.due_date is not None]
    review_dates = [item.review_due for item in ordered if item.review_due is not None]
    tags = tuple(sorted({tag for item in ordered for tag in item.tags}))
    return replace(
        base,
        problem_id=first_text("problem_id") or base.problem_id,
        title=first_text("title"),
        url=first_text("url"),
        rating=first_number("rating"),
        difficulty=first_number("difficulty"),
        tags=tags,
        status="accepted" if any(item.status == "accepted" for item in ordered) else base.status,
        source="plan" if plan_sources else "catalog",
        task_key=primary_source.task_key if primary_source else first_text("task_key"),
        plan_day=primary_source.stage_order if primary_source else base.plan_day,
        plan_level=primary_source.plan_level if primary_source else base.plan_level,
        due_date=min(due_dates) if due_dates else None,
        unlock_at=None,
        review_due=min(review_dates) if review_dates else None,
        plan_id=primary_source.plan_id if primary_source else "",
        plan_title=primary_source.plan_title if primary_source else "",
        stage_key=primary_source.stage_key if primary_source else "",
        stage_order=primary_source.stage_order if primary_source else None,
        plan_sources=plan_sources,
    )


def _plan_tie_key(candidate: Candidate, fairness_rank: Mapping[str, int]) -> tuple[Any, ...]:
    if not candidate.plan_sources:
        return (10**9, 10**9, "")
    stage_order = min(
        (source.stage_order for source in candidate.plan_sources if source.stage_order is not None),
        default=10**9,
    )
    plan_ids = sorted({source.plan_id for source in candidate.plan_sources if source.plan_id})
    fair_rank = min((fairness_rank.get(plan_id, 10**9) for plan_id in plan_ids), default=10**9)
    return stage_order, fair_rank, ",".join(plan_ids)


def recommendation_slots(count: int) -> list[str]:
    """Cycle the three bands so every complete group is split 1/3 each."""

    if count <= 0:
        return []
    bases = ("recovery", "main", "stretch")
    slots: list[str] = []
    for index in range(count):
        base = bases[index % len(bases)]
        group = index // len(bases) + 1
        slots.append(base if group == 1 else f"{base}-{group}")
    return slots


def _stable_problem_sort_key(candidate: Candidate) -> tuple[int, int, str]:
    if candidate.platform == "codeforces":
        match = re.fullmatch(r"(?:CF)?(\d+)([A-Z]\d*)", candidate.problem_id)
        if match:
            return 0, int(match.group(1)), match.group(2)
    if candidate.platform == "luogu" and candidate.problem_id.startswith("P"):
        return 1, int(candidate.problem_id[1:]), ""
    return 2, 0, candidate.problem_id


def _score_candidate(
    candidate: Candidate,
    *,
    slot: str,
    today: date,
    difficulty_targets: Mapping[str, int],
    weakness: Mapping[str, float],
    recent_keys: Sequence[str],
    platform_counts: Counter[str],
) -> tuple[float, dict[str, float], tuple[str, ...]]:
    reasons: list[str] = []
    # Plan membership, schedule and A/B/C level are provenance only.  They may
    # filter candidates through source_mode/unlock_at, but never improve the
    # ranking of an otherwise identical problem.
    plan_score = 0.0

    review_score = 0.0
    if candidate.review_due is not None and candidate.review_due <= today:
        days = (today - candidate.review_due).days
        review_score = 1000.0 + min(days, 30) * 4.0
        reasons.append("到期复做" if days == 0 else f"复做逾期 {days} 天")

    target_slot = slot.split("-", 1)[0]
    target = int(difficulty_targets.get(target_slot, difficulty_targets["main"]))
    equivalent = candidate.equivalent_rating
    difficulty_score = 0.0 if equivalent is None else max(0.0, 180.0 - abs(equivalent - target) * 0.6)
    if equivalent is not None:
        reasons.append(
            f"等价难度 {equivalent}，"
            f"{DIFFICULTY_BAND_LABELS.get(target_slot, target_slot)} 目标 {target}"
        )

    tag_values = [weakness.get(tag, 0.0) for tag in candidate.tags]
    weakness_score = min(120.0, sum(tag_values) / len(tag_values) * 20.0) if tag_values else 0.0
    if weakness_score > 0:
        strongest = max(candidate.tags, key=lambda tag: weakness.get(tag, 0.0))
        reasons.append(f"薄弱专题：{strongest}")

    target_share = 0.6 if candidate.platform == "codeforces" else 0.4
    history_total = sum(platform_counts.values())
    actual_share = platform_counts[candidate.platform] / history_total if history_total else 0.0
    platform_score = (target_share - actual_share) * 100.0
    if platform_score > 0:
        reasons.append(f"补足 {candidate.platform} 滚动比例")

    recent_score = 0.0
    if candidate.problem_key in recent_keys:
        distance = len(recent_keys) - 1 - max(
            index for index, key in enumerate(recent_keys) if key == candidate.problem_key
        )
        recent_score = -max(40.0, 240.0 - distance * 20.0)
        reasons.append("近期已经推荐过")

    breakdown = {
        "plan_urgency": round(plan_score, 3),
        "review_due": round(review_score, 3),
        "difficulty_fit": round(difficulty_score, 3),
        "weakness": round(weakness_score, 3),
        "platform_balance": round(platform_score, 3),
        "recent_repeat": round(recent_score, 3),
    }
    return round(sum(breakdown.values()), 3), breakdown, tuple(reasons)


def recommend(
    candidates: Iterable[Candidate | Mapping[str, Any] | Any],
    *,
    plan_tasks: Iterable[Candidate | Mapping[str, Any] | Any] = (),
    source_mode: str = "balanced",
    plan_ids: Iterable[str] | None = None,
    accepted_keys: Iterable[str] = (),
    skipped_keys: Iterable[str] = (),
    attempts: Iterable[Mapping[str, Any] | Any] = (),
    recommendation_history: Sequence[Mapping[str, Any] | Any] = (),
    now: date | datetime | None = None,
    count: int = 3,
    mode: str = "mixed",
    cf_rating: int | None = None,
    target_cf_rating: int | None = None,
    recent_cf_accepted: Iterable[int | Mapping[str, Any] | Any] = (),
    recent_solved_equivalents: Iterable[int] = (),
    return_pool: bool = False,
) -> list[Recommendation]:
    """Recommend problems with an explicit score breakdown and stable tie-breaks.

    ``mode='new'`` excludes every accepted problem; ``mode='review'`` includes
    only accepted problems whose review date is due; ``mixed`` combines those
    two pools. ``source_mode`` controls catalog/plan participation independently
    of that acceptance mode. In balanced mode catalog and plan rows form one
    ordinary union: plan provenance never changes scoring or tie-breaking.
    Future plan tasks are always hidden until ``unlock_at``.
    """
    if mode not in {"mixed", "new", "review"}:
        raise ValueError("mode must be one of: mixed, new, review")
    if source_mode not in SOURCE_MODES:
        raise ValueError("source_mode must be one of: balanced, catalog_only, plan_only")
    today = (now.date() if isinstance(now, datetime) else now) or date.today()
    accepted = _normalise_accepted(accepted_keys)
    skipped = _normalise_accepted(skipped_keys)
    selected_plan_ids = None if plan_ids is None else {str(plan_id) for plan_id in plan_ids}
    prepared: list[Candidate] = []
    raw_values = [(raw, False) for raw in candidates]
    raw_values.extend((raw, True) for raw in plan_tasks)
    for raw, forced_plan in raw_values:
        candidate = Candidate.from_value(raw)
        is_plan_record = forced_plan or candidate.source == "plan"
        if forced_plan:
            candidate = replace(candidate, source="plan")

        if is_plan_record:
            if source_mode == "catalog_only":
                continue
            sources = _candidate_plan_sources(candidate)
            sources = tuple(
                source
                for source in sources
                if (selected_plan_ids is None or source.plan_id in selected_plan_ids)
                and (source.unlock_at is None or source.unlock_at <= today)
            )
            if not sources:
                continue
            candidate = replace(candidate, source="plan", plan_sources=sources, unlock_at=None)
        else:
            if source_mode == "plan_only":
                continue
            if candidate.unlock_at is not None and candidate.unlock_at > today:
                continue
            # A pre-merged catalog row may carry plan occurrences. Keep only
            # currently eligible selected plans, or strip them in catalog-only.
            sources = () if source_mode == "catalog_only" else tuple(
                source
                for source in _candidate_plan_sources(candidate)
                if (selected_plan_ids is None or source.plan_id in selected_plan_ids)
                and (source.unlock_at is None or source.unlock_at <= today)
            )
            candidate = replace(candidate, plan_sources=sources)
        prepared.append(candidate)

    grouped: defaultdict[str, list[Candidate]] = defaultdict(list)
    for candidate in prepared:
        grouped[candidate.problem_key].append(candidate)

    normalised: list[Candidate] = []
    for problem_key in sorted(grouped):
        candidate = _merge_candidates(grouped[problem_key])
        if candidate.problem_key in skipped or candidate.status == "skipped":
            continue
        is_accepted = candidate.problem_key in accepted or candidate.status == "accepted"
        is_due_review = candidate.review_due is not None and candidate.review_due <= today
        if mode == "new" and is_accepted:
            continue
        if mode == "review" and not (is_accepted and is_due_review):
            continue
        if mode == "mixed" and is_accepted and not is_due_review:
            continue
        normalised.append(candidate)

    recent_equivalents = list(recent_solved_equivalents)
    if not recent_equivalents:
        for item in recent_cf_accepted:
            rating = item if isinstance(item, int) else _get(item, "rating")
            if rating not in (None, ""):
                recent_equivalents.append(int(rating))
    difficulty_targets = recommendation_difficulty_targets(
        cf_rating,
        recent_equivalents,
        target_rating=target_cf_rating,
    )
    weakness = compute_weakness(attempts, now=today)
    recent_keys, recent_platforms = _history_fields(recommendation_history)
    platform_counts = Counter(recent_platforms)
    if return_pool:
        scored_pool: list[tuple[Any, ...]] = []
        for candidate in normalised:
            score, breakdown, reasons = _score_candidate(
                candidate,
                slot="main",
                today=today,
                difficulty_targets=difficulty_targets,
                weakness=weakness,
                recent_keys=recent_keys,
                platform_counts=platform_counts,
            )
            scored_pool.append(
                (
                    _difficulty_distance(
                        candidate.equivalent_rating, difficulty_targets["main"]
                    ),
                    -score,
                    _stable_problem_sort_key(candidate),
                    candidate.problem_key,
                    Recommendation(
                        slot="main",
                        problem_key=candidate.problem_key,
                        problem_id=candidate.problem_id,
                        platform=candidate.platform,
                        title=candidate.title,
                        url=candidate.url,
                        equivalent_rating=candidate.equivalent_rating,
                        difficulty_target=difficulty_targets["main"],
                        difficulty_band=DIFFICULTY_BANDS["main"],
                        score=score,
                        breakdown=breakdown,
                        reasons=reasons,
                        task_key=candidate.task_key,
                        tags=candidate.tags,
                        source=candidate.source,
                        plan_sources=candidate.plan_sources,
                    ),
                )
            )
        return [row[-1] for row in sorted(scored_pool)]

    selected: list[Recommendation] = []
    used: set[str] = set()
    for slot in recommendation_slots(count):
        scored: list[tuple[Any, ...]] = []
        for candidate in normalised:
            if candidate.problem_key in used:
                continue
            score, breakdown, reasons = _score_candidate(
                candidate,
                slot=slot,
                today=today,
                difficulty_targets=difficulty_targets,
                weakness=weakness,
                recent_keys=recent_keys,
                platform_counts=platform_counts,
            )
            scored.append(
                (
                    _difficulty_distance(
                        candidate.equivalent_rating,
                        difficulty_targets[slot.split("-", 1)[0]],
                    ),
                    -score,
                    _stable_problem_sort_key(candidate),
                    candidate.problem_key,
                    candidate,
                    breakdown,
                    reasons,
                )
            )
        if not scored:
            break
        _, _, _, _, candidate, breakdown, reasons = min(scored)
        score = round(sum(breakdown.values()), 3)
        base_slot = slot.split("-", 1)[0]
        selected.append(
            Recommendation(
                slot=slot,
                problem_key=candidate.problem_key,
                problem_id=candidate.problem_id,
                platform=candidate.platform,
                title=candidate.title,
                url=candidate.url,
                equivalent_rating=candidate.equivalent_rating,
                difficulty_target=difficulty_targets[base_slot],
                difficulty_band=DIFFICULTY_BANDS[base_slot],
                score=score,
                breakdown=breakdown,
                reasons=reasons,
                task_key=candidate.task_key,
                tags=candidate.tags,
                source=candidate.source,
                plan_sources=candidate.plan_sources,
            )
        )
        used.add(candidate.problem_key)
        platform_counts[candidate.platform] += 1
    return selected


recommend_problems = recommend
