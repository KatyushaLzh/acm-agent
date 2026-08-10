"""Wall-clock budgeting for AI stress preparation.

One monotonic total deadline owns cumulative phase cutoffs.  Provider work has
an earlier hard stop, leaving non-borrowable local-validation and cleanup gates;
individual request and subprocess timeouts can only shorten those deadlines.

Phase cutoffs scale with the configured total, but per-request floors do not:
they describe what the provider physically needs to return a usable body.  A
small configured total can therefore leave a provider window too short for any
thinking request; ``supports_thinking_requests()`` reports that up front.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import threading
import time
from typing import Callable, Iterator


MIN_PREPARATION_TIMEOUT_SECONDS = 60
DEFAULT_PREPARATION_TIMEOUT_SECONDS = 600
MAX_PREPARATION_TIMEOUT_SECONDS = 1800

# Default ``request_cap_seconds`` for provider_timeout().  This is a preparation
# policy ceiling, not a socket ceiling: deepseek.MAX_TRANSPORT_REQUEST_SECONDS
# bounds the HTTP layer separately and is deliberately looser, because the
# policy caps below always clamp the value before it reaches that client.
MAX_PROVIDER_REQUEST_SECONDS = 180.0
MAX_NON_THINKING_REQUEST_SECONDS = 120.0
MAX_THINKING_REQUEST_SECONDS = 180.0
MAX_AUDIT_REQUEST_SECONDS = 50.0
MAX_PROVIDER_TOKENS_PER_PREPARATION = 100_000

# Per-request floors.  Unlike the phase schedule below these are absolute and
# are NOT scaled by ``PreparationBudget.scale``: they describe how long the
# provider physically needs to return a usable body, which does not shrink just
# because the operator configured a smaller total.  A configured total whose
# scaled provider window is under MIN_THINKING_REQUEST_SECONDS therefore cannot
# issue thinking requests at all -- ``hybrid`` degrades to non-thinking and
# ``full_thinking`` fails closed.  ``supports_thinking_requests()`` reports this
# up front and the exhaustion details explain it at the point of failure.
MIN_NON_THINKING_REQUEST_SECONDS = 8.0
MIN_THINKING_REQUEST_SECONDS = 30.0
MIN_AUDIT_REQUEST_SECONDS = 8.0

# Cumulative hard cutoffs for the default 600-second preparation.  Other
# configured totals scale the same schedule proportionally.  The provider must
# stop at 480 seconds; local validation owns the next 110 seconds and cleanup
# owns the final 10 seconds, neither of which provider work may borrow.
BASE_PHASE_DEADLINES: dict[str, float] = {
    "context": 20.0,
    "contract": 85.0,
    "initial_prepare": 300.0,
    "audit_initial": 345.0,
    "repair_1": 425.0,
    "provider": 480.0,
    "local_validation": 590.0,
    "total": 600.0,
}

# The cleanup window at the 600-second default, derived from the schedule so the
# two cannot drift.  It is a reporting constant only: every PreparationBudget
# recomputes its own ``cleanup_reserve_seconds`` as ``deadline - local_deadline``,
# which scales with the configured total (1s at 60s, 30s at 1800s).
PREPARATION_CLEANUP_RESERVE_SECONDS = (
    BASE_PHASE_DEADLINES["total"] - BASE_PHASE_DEADLINES["local_validation"]
)

# These numbers describe nominal phase durations at the 600-second default.
# They remain reporting hints; BASE_PHASE_DEADLINES owns enforcement.
BASE_SOFT_BUDGETS: dict[str, float] = {
    "context": 20.0,
    "contract": 65.0,
    "initial_prepare": 215.0,
    "audit_initial": 45.0,
    "generator_repair_1": 60.0,
    "repair_review_1": 20.0,
    "generator_repair_2": 40.0,
    "final_review": 15.0,
    "local_validation": 110.0,
    "cleanup": 10.0,
}

SOFT_STAGE_ALIASES: dict[str, str] = {
    "check_isolation": "context",
    "isolation_context": "context",
    "extract_contract": "contract",
    "prepare_helpers": "initial_prepare",
    "audit_helpers": "audit_initial",
    "repair_helpers": "generator_repair_1",
}

_LOCAL_STAGE_MARKERS = (
    "apply",
    "compile",
    "create_stress_run",
    "local_validation",
    "persist",
    "preflight",
    "stage_helpers",
)


def _reasoning_tokens(value: object) -> int | float:
    if isinstance(value, dict):
        direct = value.get("reasoning_tokens")
        if isinstance(direct, (int, float)) and not isinstance(direct, bool):
            return direct
        total: int | float = 0
        for key, item in value.items():
            if isinstance(item, dict):
                total += _reasoning_tokens(item)
        return total
    return 0


class PreparationBudgetExhausted(RuntimeError):
    code = "stress_prepare_budget_exhausted"

    def __init__(
        self,
        budget: "PreparationBudget",
        stage: str,
        *,
        details: dict[str, object] | None = None,
    ) -> None:
        elapsed = budget.elapsed()
        super().__init__(
            f"AI 对拍准备超过 {budget.timeout_seconds} 秒上限"
            f"（最后阶段：{stage}，已耗时 {elapsed:.1f} 秒）"
        )
        self.details = {
            "configured_timeout_seconds": budget.timeout_seconds,
            "elapsed_seconds": round(elapsed, 3),
            "remaining_seconds": round(
                budget.remaining(include_cleanup_reserve=True), 3
            ),
            "last_stage": stage,
            **budget.context(),
            **dict(details or {}),
        }
        self.usage = dict(budget.snapshot().get("provider_usage") or {})


class PreparationTokenBudgetExhausted(PreparationBudgetExhausted):
    code = "stress_prepare_token_budget_exhausted"

    def __init__(self, budget: "PreparationBudget", stage: str) -> None:
        used = budget.provider_tokens_used()
        limit = MAX_PROVIDER_TOKENS_PER_PREPARATION
        # Distinguish the two fail-closed boundaries.  add_usage() fires only on a
        # real overrun (``> limit``): a call that lands exactly on the allowance
        # spent what it was given and its result stays usable.  provider_timeout()
        # fires on ``>= limit`` because starting another request with zero
        # allowance left could only overrun.  Both end preparation; the message
        # says which one was hit instead of claiming an overrun that never
        # happened.
        reason = (
            f"已用尽单次 {limit} provider tokens 硬上限，无法再发起请求"
            if used <= limit
            else f"超过单次 {limit} provider tokens 硬上限"
        )
        RuntimeError.__init__(
            self,
            f"AI 对拍准备{reason}"
            f"（最后阶段：{stage}，累计 {used:g} tokens）"
        )
        self.details = {
            "provider_token_limit": MAX_PROVIDER_TOKENS_PER_PREPARATION,
            "provider_tokens_used": used,
            "last_stage": stage,
            **budget.context(),
        }
        self.usage = dict(budget.snapshot().get("provider_usage") or {})


@dataclass(slots=True)
class PreparationBudget:
    timeout_seconds: int = DEFAULT_PREPARATION_TIMEOUT_SECONDS
    clock: Callable[[], float] = time.monotonic
    # Derived in __post_init__ from the scaled phase schedule.  It is not a
    # constructor argument: an accepted-then-overwritten value would silently
    # mislead callers into believing they had widened the cleanup window.
    cleanup_reserve_seconds: float = field(init=False)
    started_at: float = field(init=False)
    deadline: float = field(init=False)
    work_deadline: float = field(init=False)
    provider_deadline: float = field(init=False)
    local_deadline: float = field(init=False)
    deadline_at: str = field(init=False)
    last_stage: str = field(default="queued", init=False)
    _stage_timings: dict[str, float] = field(default_factory=dict, init=False)
    _usage: dict[str, object] = field(default_factory=dict, init=False)
    _context: dict[str, object] = field(default_factory=dict, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _current_stage: str = field(default="queued", init=False)
    _current_stage_started: float = field(init=False)
    _phase_deadlines: dict[str, float] = field(default_factory=dict, init=False)
    _paused_at: float | None = field(default=None, init=False)
    _paused_total: float = field(default=0.0, init=False)

    def __post_init__(self) -> None:
        value = self.timeout_seconds
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError("preparation_timeout_seconds 必须是整数")
        if not MIN_PREPARATION_TIMEOUT_SECONDS <= value <= MAX_PREPARATION_TIMEOUT_SECONDS:
            raise ValueError("preparation_timeout_seconds 必须在 60 到 1800 秒之间")
        self.started_at = float(self.clock())
        self.deadline = self.started_at + float(value)
        self.deadline_at = (
            datetime.now(timezone.utc) + timedelta(seconds=value)
        ).isoformat(timespec="seconds")
        self._phase_deadlines = {
            phase: self.started_at + seconds * self.scale
            for phase, seconds in BASE_PHASE_DEADLINES.items()
        }
        self.provider_deadline = self._phase_deadlines["provider"]
        self.local_deadline = self._phase_deadlines["local_validation"]
        self.cleanup_reserve_seconds = max(0.0, self.deadline - self.local_deadline)
        # Provider work is the initial mode.  Local stages explicitly switch
        # this compatibility deadline to local_deadline via require().
        self.work_deadline = self.provider_deadline
        self._current_stage_started = self.started_at

    @property
    def scale(self) -> float:
        return float(self.timeout_seconds) / DEFAULT_PREPARATION_TIMEOUT_SECONDS

    def thinking_window_seconds(self) -> float:
        """Return the whole scaled provider window in seconds."""

        return max(0.0, self.provider_deadline - self.started_at)

    def supports_thinking_requests(self) -> bool:
        """Report whether this configured total can ever issue a thinking request.

        Per-request floors are absolute while phase cutoffs scale, so a small
        configured total can leave a provider window shorter than one thinking
        request needs.  Callers use this to explain the limit up front instead of
        surfacing it as a mid-preparation timeout.
        """

        return self.thinking_window_seconds() >= MIN_THINKING_REQUEST_SECONDS

    def phase_deadline_offsets(self) -> dict[str, float]:
        """Return scaled cumulative phase cutoffs in seconds from start."""

        return {
            phase: round(deadline - self.started_at, 6)
            for phase, deadline in self._phase_deadlines.items()
        }

    def phase_deadline(self, phase: str) -> float:
        """Return one named phase's absolute monotonic cutoff."""

        try:
            return self._phase_deadlines[str(phase)]
        except KeyError as exc:
            raise ValueError(f"unknown preparation phase: {phase}") from exc

    def phase_remaining(self, phase: str) -> float:
        return max(0.0, self.phase_deadline(phase) - float(self.clock()))

    def _generation_attempt(self) -> int:
        with self._lock:
            value = self._context.get("generation_attempt", 0)
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return 0

    def _phase_for_stage(self, stage: str, soft_stage: str | None = None) -> str:
        selected = f"{stage} {soft_stage or ''}".casefold()
        if "cleanup" in selected:
            return "total"
        if any(marker in selected for marker in _LOCAL_STAGE_MARKERS):
            return "local_validation"
        if "isolation" in selected or "context" in selected:
            return "context"
        if "contract" in selected:
            return "contract"
        attempt = self._generation_attempt()
        if "repair_2" in selected or "final_review" in selected:
            return "provider"
        is_repair = "repair" in selected or attempt > 0
        if "audit" in selected or "review" in selected:
            if attempt >= 2:
                return "provider"
            if is_repair:
                return "repair_1"
            return "audit_initial"
        if is_repair:
            return "provider" if attempt >= 2 else "repair_1"
        return "initial_prepare"

    def deadline_for_stage(self, stage: str, *, soft_stage: str | None = None) -> float:
        """Return the absolute monotonic hard cutoff for a preparation stage."""

        return self._phase_deadlines[self._phase_for_stage(stage, soft_stage)]

    def remaining_for_stage(self, stage: str, *, soft_stage: str | None = None) -> float:
        return max(0.0, self.deadline_for_stage(stage, soft_stage=soft_stage) - float(self.clock()))

    def elapsed(self) -> float:
        with self._lock:
            paused = self._paused_total
        return max(0.0, float(self.clock()) - self.started_at - paused)

    @property
    def paused(self) -> bool:
        with self._lock:
            return self._paused_at is not None

    def pause(self) -> None:
        """Exclude interactive user-wait time from every budget deadline.

        User think time must not consume the preparation window: the caller
        pauses before blocking on user input and resumes immediately after,
        shifting all absolute phase cutoffs by the paused duration.
        """

        with self._lock:
            if self._paused_at is None:
                self._paused_at = float(self.clock())

    def resume(self) -> None:
        with self._lock:
            if self._paused_at is not None:
                delta = max(0.0, float(self.clock()) - self._paused_at)
                self._paused_at = None
                self._paused_total += delta
                if delta > 0.0:
                    self.deadline += delta
                    self.work_deadline += delta
                    self.provider_deadline += delta
                    self.local_deadline += delta
                    for phase in self._phase_deadlines:
                        self._phase_deadlines[phase] += delta

    def paused_total_seconds(self) -> float:
        with self._lock:
            return round(self._paused_total, 3)

    def remaining(self, *, include_cleanup_reserve: bool = False) -> float:
        target = self.deadline if include_cleanup_reserve else self.work_deadline
        return max(0.0, target - float(self.clock()))

    def soft_budget(self, stage: str) -> float:
        canonical = SOFT_STAGE_ALIASES.get(stage, stage)
        return BASE_SOFT_BUDGETS.get(canonical, 0.0) * self.scale

    def scaled_reserve(self, base_seconds: float) -> float:
        """Scale a 600-second legacy reserve for the configured attempt window."""

        return max(0.0, float(base_seconds)) * self.scale

    def available_after_reserve(
        self,
        reserve_seconds: float,
        *,
        scaled: bool = True,
    ) -> float:
        """Return usable time without double-counting legacy gate reserves.

        Callers still pass their former downstream reserve for compatibility.
        Provider/local/cleanup ownership is now encoded in absolute phase
        deadlines, so subtracting the legacy value again would prematurely
        starve repair and audit work.
        """

        _ = reserve_seconds, scaled
        return self.remaining()

    def set_context(self, **values: object) -> None:
        with self._lock:
            for key, value in values.items():
                if value is None:
                    self._context.pop(str(key), None)
                else:
                    self._context[str(key)] = value

    def context(self) -> dict[str, object]:
        with self._lock:
            return dict(self._context)

    def mark_stage(self, stage: str) -> None:
        now = float(self.clock())
        selected = str(stage or "working")
        with self._lock:
            if selected != self._current_stage:
                elapsed = max(0.0, now - self._current_stage_started)
                self._stage_timings[self._current_stage] = round(
                    self._stage_timings.get(self._current_stage, 0.0) + elapsed,
                    6,
                )
                self._current_stage = selected
                self._current_stage_started = now
            self.last_stage = selected

    def add_usage(self, usage: dict[str, object] | object) -> None:
        if not isinstance(usage, dict):
            try:
                usage = dict(usage)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return
        with self._lock:
            for key, value in usage.items():
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    current = self._usage.get(str(key), 0)
                    if isinstance(current, (int, float)):
                        self._usage[str(key)] = current + value
            total = self._usage.get("total_tokens", 0)
            # Strictly greater: a response that lands exactly on the allowance has
            # not overrun it, so its result stays usable.  The pre-call check in
            # provider_timeout() uses ``>=`` and stops the next request.  Token
            # cost is not knowable before a call, so one overshoot is unavoidable
            # -- the cap bounds it rather than pretending to predict it.
            exceeded = (
                isinstance(total, (int, float))
                and not isinstance(total, bool)
                and total > MAX_PROVIDER_TOKENS_PER_PREPARATION
            )
            stage = self.last_stage
        if exceeded:
            raise PreparationTokenBudgetExhausted(self, stage)

    def provider_tokens_used(self) -> int | float:
        with self._lock:
            value = self._usage.get("total_tokens", 0)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            return value
        return 0

    def record_span(self, stage: str, duration: float) -> None:
        """Record an independently measured span, including concurrent work."""

        with self._lock:
            self._stage_timings[str(stage)] = round(
                self._stage_timings.get(str(stage), 0.0)
                + max(0.0, float(duration)),
                6,
            )

    def require(self, stage: str, *, include_cleanup_reserve: bool = False) -> float:
        with self._lock:
            self.last_stage = stage
        phase = self._phase_for_stage(stage)
        if include_cleanup_reserve or phase == "total":
            target = self.deadline
        else:
            target = self._phase_deadlines[phase]
            if phase == "local_validation":
                # Existing runtime callers pass work_deadline into subprocess
                # and preflight helpers after require(); expose the local gate
                # only once provider work has ended.
                self.work_deadline = self.local_deadline
        remaining = max(0.0, target - float(self.clock()))
        if remaining <= 0.0:
            raise PreparationBudgetExhausted(self, stage)
        return remaining

    def provider_timeout(
        self,
        stage: str,
        *,
        soft_stage: str | None = None,
        reserve_seconds: float = 0.0,
        reserve_is_scaled: bool = True,
        minimum_seconds: float = 0.1,
        request_cap_seconds: float = MAX_PROVIDER_REQUEST_SECONDS,
    ) -> float:
        """Return a timeout bounded by the stage and provider hard cutoffs.

        ``reserve_seconds`` remains accepted for API compatibility.  The
        cumulative phase schedule now owns downstream reservations, so callers
        cannot accidentally double-reserve or borrow local-validation time.
        """

        with self._lock:
            self.last_stage = stage
            total = self._usage.get("total_tokens", 0)
        if (
            isinstance(total, (int, float))
            and not isinstance(total, bool)
            and total >= MAX_PROVIDER_TOKENS_PER_PREPARATION
        ):
            raise PreparationTokenBudgetExhausted(self, stage)
        phase_deadline = min(
            self.provider_deadline,
            self.deadline_for_stage(stage, soft_stage=soft_stage),
        )
        available = max(0.0, phase_deadline - float(self.clock()))
        selected = f"{stage} {soft_stage or ''}".casefold()
        audit = "audit" in selected or "review" in selected
        thinking = float(minimum_seconds) >= MIN_THINKING_REQUEST_SECONDS
        policy_minimum = (
            MIN_AUDIT_REQUEST_SECONDS
            if audit
            else MIN_THINKING_REQUEST_SECONDS
            if thinking
            else MIN_NON_THINKING_REQUEST_SECONDS
        )
        policy_cap = (
            MAX_AUDIT_REQUEST_SECONDS
            if audit
            else MAX_THINKING_REQUEST_SECONDS
            if thinking
            else MAX_NON_THINKING_REQUEST_SECONDS
        )
        minimum = max(policy_minimum, float(minimum_seconds))
        if available < minimum:
            reserve = (
                self.scaled_reserve(reserve_seconds)
                if reserve_is_scaled
                else max(0.0, float(reserve_seconds))
            )
            details: dict[str, object] = {
                "available_seconds": round(available, 3),
                "reserved_gate_seconds": round(reserve, 3),
                "minimum_request_seconds": round(minimum, 3),
                "phase_deadline_seconds": round(
                    phase_deadline - self.started_at, 3
                ),
                "thinking_request": thinking,
            }
            if thinking and not self.supports_thinking_requests():
                # Per-request floors are absolute while phase cutoffs scale, so
                # this is a configuration limit rather than a slow provider.
                details["thinking_window_seconds"] = round(
                    self.thinking_window_seconds(), 3
                )
                details["thinking_supported"] = False
                details["hint"] = (
                    f"配置的准备预算 {self.timeout_seconds} 秒过小："
                    f"provider 窗口只有 {self.thinking_window_seconds():.1f} 秒，"
                    f"不足单次 thinking 请求所需的 {MIN_THINKING_REQUEST_SECONDS:g} 秒。"
                    "请调大准备预算，或改用 fast / hybrid 生成模式。"
                )
            raise PreparationBudgetExhausted(self, stage, details=details)
        return min(float(request_cap_seconds), policy_cap, available)

    def subprocess_timeout(self, stage: str, requested: float) -> float:
        return max(0.1, min(float(requested), self.require(stage)))

    @contextmanager
    def measure(self, stage: str) -> Iterator[None]:
        self.require(stage)
        started = float(self.clock())
        try:
            yield
        finally:
            duration = max(0.0, float(self.clock()) - started)
            with self._lock:
                self._stage_timings[stage] = round(
                    self._stage_timings.get(stage, 0.0) + duration,
                    6,
                )

    def progress(self, stage: str, *, soft_stage: str | None = None) -> dict[str, object]:
        remaining = self.remaining(include_cleanup_reserve=True)
        with self._lock:
            stage_elapsed = max(0.0, float(self.clock()) - self._current_stage_started)
            reasoning_tokens = _reasoning_tokens(self._usage)
        return {
            "configured_timeout_seconds": self.timeout_seconds,
            "elapsed_seconds": round(self.elapsed(), 1),
            "remaining_seconds": round(remaining, 1),
            "usable_seconds": round(self.remaining(), 1),
            "usable_remaining_seconds": round(self.remaining(), 1),
            "reserved_validation_seconds": round(
                max(0.0, self.local_deadline - self.provider_deadline), 1
            ),
            "reserved_cleanup_seconds": round(self.cleanup_reserve_seconds, 1),
            "provider_deadline_seconds": round(
                self.provider_deadline - self.started_at, 1
            ),
            "local_deadline_seconds": round(
                self.local_deadline - self.started_at, 1
            ),
            "stage": stage,
            "stage_elapsed_seconds": round(stage_elapsed, 1),
            "soft_budget_seconds": round(self.soft_budget(soft_stage or stage), 1),
            "deadline_at": self.deadline_at,
            "reasoning_tokens": reasoning_tokens,
            "provider_token_limit": MAX_PROVIDER_TOKENS_PER_PREPARATION,
            "provider_tokens_used": self.provider_tokens_used(),
            "paused_total_seconds": self.paused_total_seconds(),
            **self.context(),
        }

    def snapshot(self) -> dict[str, object]:
        now = float(self.clock())
        with self._lock:
            timings = dict(self._stage_timings)
            timings[self._current_stage] = round(
                timings.get(self._current_stage, 0.0)
                + max(0.0, now - self._current_stage_started),
                6,
            )
            last_stage = self.last_stage
            usage = dict(self._usage)
            context = dict(self._context)
        return {
            "configured_timeout_seconds": self.timeout_seconds,
            "elapsed_seconds": round(self.elapsed(), 3),
            "remaining_seconds": round(self.remaining(include_cleanup_reserve=True), 3),
            "cleanup_reserve_seconds": self.cleanup_reserve_seconds,
            "reserved_validation_seconds": round(
                max(0.0, self.local_deadline - self.provider_deadline), 3
            ),
            "provider_deadline_seconds": round(
                self.provider_deadline - self.started_at, 3
            ),
            "local_deadline_seconds": round(
                self.local_deadline - self.started_at, 3
            ),
            "phase_deadlines": self.phase_deadline_offsets(),
            "last_stage": last_stage,
            "deadline_at": self.deadline_at,
            "stage_timings": timings,
            "soft_budgets": {
                key: round(value * self.scale, 3)
                for key, value in BASE_SOFT_BUDGETS.items()
            },
            "provider_usage": usage,
            "provider_token_limit": MAX_PROVIDER_TOKENS_PER_PREPARATION,
            "provider_tokens_used": (
                usage.get("total_tokens", 0)
                if isinstance(usage.get("total_tokens", 0), (int, float))
                and not isinstance(usage.get("total_tokens", 0), bool)
                else 0
            ),
            "reasoning_tokens": _reasoning_tokens(usage),
            "paused_total_seconds": self.paused_total_seconds(),
            **context,
        }


__all__ = [
    "BASE_PHASE_DEADLINES",
    "BASE_SOFT_BUDGETS",
    "DEFAULT_PREPARATION_TIMEOUT_SECONDS",
    "MAX_AUDIT_REQUEST_SECONDS",
    "MAX_NON_THINKING_REQUEST_SECONDS",
    "MAX_PROVIDER_REQUEST_SECONDS",
    "MAX_PROVIDER_TOKENS_PER_PREPARATION",
    "MAX_PREPARATION_TIMEOUT_SECONDS",
    "MAX_THINKING_REQUEST_SECONDS",
    "MIN_AUDIT_REQUEST_SECONDS",
    "MIN_NON_THINKING_REQUEST_SECONDS",
    "MIN_PREPARATION_TIMEOUT_SECONDS",
    "MIN_THINKING_REQUEST_SECONDS",
    "SOFT_STAGE_ALIASES",
    "PreparationBudget",
    "PreparationBudgetExhausted",
    "PreparationTokenBudgetExhausted",
]
