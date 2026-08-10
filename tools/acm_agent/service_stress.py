"""Persistent AI stress preparation and execution service methods."""

from __future__ import annotations

from datetime import datetime, timezone
import os
from pathlib import Path
from typing import Any, Callable, Mapping
from uuid import uuid4

from .ai_context import validate_managed_cpp
from .config import (
    STRESS_CACHE_MODES,
    STRESS_GENERATION_MODES,
    load_config,
    validate_stress_generation_mode,
    validate_stress_prepare_timeout_seconds,
)
from .deepseek import validate_model, validate_reasoning_effort
from .service_common import (
    PUBLIC_AI_SETTING_KEYS,
    STRESS_AI_REQUEST_TIMEOUT_SECONDS,
    _db_problem_id,
    _display_problem_id,
    _problem_key,
)
from .storage import Database, StressSetupSlotConflict
from .stress_budget import PreparationBudget
from .stress_runtime import StressRuntimeError, normalize_stress_failure
from .stress_sources import source_order_for_platform
from .workspace import find_solution, parse_problem_ref


class ServiceStressMixin:
    def ai_stress_status(self) -> dict[str, Any]:
        status = self._stress.status()
        ai = self.ai_status()
        status["api_key_detected"] = bool(ai["api_key_detected"])
        status["allowed_models"] = ai["allowed_models"]
        status["cache_modes"] = list(STRESS_CACHE_MODES)
        status["settings"] = {
            key: ai["settings"][key]
            for key in PUBLIC_AI_SETTING_KEYS
            if key.startswith("validation_")
        }
        config = load_config(self.paths)
        status["settings"]["stress_prepare_timeout_seconds"] = int(
            config["ai"]["stress_prepare_timeout_seconds"]
        )
        status["settings"]["stress_generation_mode"] = str(
            config["ai"]["stress_generation_mode"]
        )
        status["generation_modes"] = list(STRESS_GENERATION_MODES)
        with Database(self.paths.database) as db:
            active_setup = db.active_stress_setup_run()
        status["active_setup"] = (
            {"id": str(active_setup["id"]), "created_at": active_setup["created_at"]}
            if active_setup is not None
            else None
        )
        return status

    def ai_stress_start(
        self,
        problem: str | None = None,
        *,
        generate_generator: bool = True,
        prepare_reference_primary: bool = True,
        prepare_reference_secondary: bool = True,
        generate_brute: bool | None = None,
        prepare_reference: bool | None = None,
        large_profile: bool = True,
        # An independent validator is opt-in.  Certification without it relies
        # on the generator manifest plus the small-case oracle agreement, which
        # is the documented default; callers that want input certification pass
        # include_validator=True explicitly.
        include_validator: bool = False,
        allow_validator_degradation: bool = True,
        unvalidated_large: bool = False,
        minimal_verification: bool = False,
        preparation_timeout_seconds: int | None = None,
        force_regenerate: bool = False,
        cache_mode: str | None = None,
        generation_mode: str | None = None,
        model: str | None = None,
        seed: int | None = None,
        timeout: float = 2.0,
        reference_secondary_timeout: float | None = None,
        brute_timeout: float | None = None,
        compare: str = "token",
        progress_callback: Callable[[str, str, int, int], None] | None = None,
        reference_primary_file: Path | str | None = None,
        reference_secondary_file: Path | str | None = None,
        # Deprecated compatibility aliases.  New callers must use the two
        # explicit reference roles; aliases are rejected when ambiguous.
        brute_file: Path | str | None = None,
        reference_file: Path | str | None = None,
        generator_file: Path | str | None = None,
    ) -> dict[str, Any]:
        deprecations: list[str] = []
        if generate_brute is not None:
            prepare_reference_secondary = bool(generate_brute)
            deprecations.append("generate_brute")
        if prepare_reference is not None:
            prepare_reference_primary = bool(prepare_reference)
            deprecations.append("prepare_reference")
        if reference_primary_file is not None and reference_file is not None:
            raise ValueError(
                "reference_primary_file 与已弃用的 reference_file 不能同时提供"
            )
        if reference_secondary_file is not None and brute_file is not None:
            raise ValueError(
                "reference_secondary_file 与已弃用的 brute_file 不能同时提供"
            )
        if reference_primary_file is None and reference_file is not None:
            reference_primary_file = reference_file
            deprecations.append("reference_file")
        if reference_secondary_file is None and brute_file is not None:
            reference_secondary_file = brute_file
            deprecations.append("brute_file")
        if reference_secondary_timeout is not None and brute_timeout is not None:
            raise ValueError(
                "reference_secondary_timeout 与已弃用的 brute_timeout 不能同时提供"
            )
        if reference_secondary_timeout is None:
            reference_secondary_timeout = 5.0 if brute_timeout is None else brute_timeout
        if brute_timeout is not None:
            deprecations.append("brute_timeout")
        config = load_config(self.paths)
        selected_prepare_timeout = validate_stress_prepare_timeout_seconds(
            config["ai"]["stress_prepare_timeout_seconds"]
            if preparation_timeout_seconds is None
            else preparation_timeout_seconds
        )
        selected_generation_mode = validate_stress_generation_mode(
            config["ai"]["stress_generation_mode"]
            if generation_mode is None
            else generation_mode
        )
        budget = PreparationBudget(selected_prepare_timeout)
        if not isinstance(force_regenerate, bool):
            raise ValueError("force_regenerate 必须是布尔值")
        selected_cache_mode = (
            "cold" if force_regenerate else "reuse"
        ) if cache_mode is None else str(cache_mode).strip().casefold()
        if selected_cache_mode not in STRESS_CACHE_MODES:
            raise ValueError(
                "cache_mode 必须是 reuse、refresh_helpers 或 cold"
            )
        if force_regenerate and selected_cache_mode != "cold":
            raise ValueError("force_regenerate=true 只能与 cache_mode=cold 一起使用")
        selected_problem = problem
        if not selected_problem:
            with Database(self.paths.database) as db:
                active = [row for row in db.attempts() if row["active"]]
            if not active:
                raise ValueError("未指定题号，且没有 active session")
            selected_problem = _display_problem_id(active[0]["platform"], active[0]["problem_id"])
        ref = parse_problem_ref(str(selected_problem))
        source = validate_managed_cpp(self.paths.root, find_solution(self.paths.root, ref))
        if compare not in {"token", "exact"}:
            raise ValueError("compare 必须是 token 或 exact")
        if not 0.1 <= float(timeout) <= 60 or not 0.1 <= float(reference_secondary_timeout) <= 60:
            raise ValueError("timeout 必须在 0.1 到 60 秒之间")
        context = self.problem_context(ref.problem_id)
        if not str(context.get("content") or "").strip():
            context = self.problem_context_fetch(ref.problem_id)
        if not str(context.get("content") or "").strip():
            raise ValueError("题面抓取失败；请先保存人工题面")
        selected_model = validate_model(model or str(config["ai"]["validation_model"]))
        settings = {
            "model": selected_model,
            "thinking": False,
            "reasoning_effort": validate_reasoning_effort(
                str(config["ai"]["validation_reasoning_effort"])
            ),
        }
        problem_db_id = _db_problem_id(ref.platform, ref.problem_id)
        attempt_id: int | None = None
        title = ""
        with Database(self.paths.database) as db:
            active_attempt = db.connection.execute(
                """SELECT id FROM attempts
                   WHERE platform=? AND problem_id=? AND active=1
                   ORDER BY id DESC LIMIT 1""",
                (ref.platform, problem_db_id),
            ).fetchone()
            if active_attempt is not None:
                attempt_id = int(active_attempt["id"])
            problem_row = db.connection.execute(
                "SELECT name FROM problems WHERE platform=? AND problem_id=?",
                (ref.platform, problem_db_id),
            ).fetchone()
            if problem_row is not None:
                title = str(problem_row["name"] or "")
        run_id = str(uuid4())
        try:
            with Database(self.paths.database) as db:
                db.acquire_stress_setup_slot(
                    run_id,
                    model=selected_model,
                    request_summary={
                        "problem_key": _problem_key(ref.platform, problem_db_id),
                        "statement_bytes": len(str(context["content"]).encode("utf-8")),
                        "generator": bool(generate_generator),
                        "reference_primary": bool(prepare_reference_primary),
                        "reference_secondary": bool(prepare_reference_secondary),
                        "validator": bool(include_validator),
                        "validator_strict": bool(
                            include_validator
                            and not allow_validator_degradation
                            and not minimal_verification
                        ),
                        "minimal_verification": bool(minimal_verification),
                        "oracle_protocol": "dual_reference_v1",
                        "large": bool(large_profile),
                        "profile_version": 2,
                        "contains_user_source": False,
                        "source_order": list(
                            source_order_for_platform(ref.platform)
                        ),
                        "preparation_timeout_seconds": selected_prepare_timeout,
                        "force_regenerate": bool(force_regenerate),
                        "cache_mode": selected_cache_mode,
                        "generation_mode": selected_generation_mode,
                    },
                    preparation_meta={
                        "configured_timeout_seconds": selected_prepare_timeout,
                        "cache_result": "pending",
                        "generation_mode": selected_generation_mode,
                        "cache_mode": selected_cache_mode,
                        "owner_pid": os.getpid(),
                    },
                )
        except StressSetupSlotConflict as exc:
            raise StressRuntimeError(
                "stress_setup_active",
                f"已有 AI 对拍准备正在运行：{exc.active_run_id}",
                details={"active_run_id": exc.active_run_id},
            ) from None
        try:
            result = self._stress.start(
                client=self._deepseek_client(
                    timeout=min(
                        STRESS_AI_REQUEST_TIMEOUT_SECONDS,
                        max(0.1, budget.remaining()),
                    )
                ),
                platform=ref.platform,
                problem_id=ref.problem_id,
                title=title,
                statement=str(context["content"]),
                primary_source=source,
                attempt_id=attempt_id,
                ai_run_id=run_id,
                model_settings=settings,
                compare=compare,
                seed=seed,
                include_generator=bool(generate_generator),
                include_reference_primary=bool(prepare_reference_primary),
                include_reference_secondary=bool(prepare_reference_secondary),
                include_validator=bool(include_validator),
                include_large=bool(large_profile),
                allow_validator_degradation=bool(allow_validator_degradation),
                unvalidated_large=bool(unvalidated_large),
                minimal_verification=bool(minimal_verification),
                preparation_timeout_seconds=selected_prepare_timeout,
                force_regenerate=bool(force_regenerate),
                cache_mode=selected_cache_mode,
                generation_mode=selected_generation_mode,
                preparation_budget=budget,
                timeout=float(timeout),
                reference_secondary_timeout=float(reference_secondary_timeout),
                progress_callback=progress_callback,
                reference_primary_file=reference_primary_file,
                reference_secondary_file=reference_secondary_file,
                generator_file=generator_file,
            )
        except Exception as exc:
            strict_validator_requested = bool(
                include_validator
                and not allow_validator_degradation
                and not minimal_verification
            )
            budget_snapshot = budget.snapshot()
            failure = normalize_stress_failure(
                exc,
                phase="preparation",
                stage=str(budget_snapshot.get("last_stage") or "setup"),
            )
            with Database(self.paths.database) as db:
                db.update_ai_run(
                    run_id,
                    status="failed",
                    error=failure,
                    usage=dict(getattr(exc, "usage", {}) or {}),
                    preparation_meta={
                        **budget_snapshot,
                        "cache_result": "failed",
                        "generation_mode": selected_generation_mode,
                        "cache_mode": selected_cache_mode,
                        "provider_usage_before_failure": dict(
                            getattr(exc, "usage", {}) or {}
                        ),
                        "helpers_unchanged": True,
                        "run_created": False,
                    },
                    completed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                )
            details = dict(failure)
            if strict_validator_requested:
                details["validator_strict_certification_failed"] = True
            raise StressRuntimeError(
                str(failure["code"]),
                str(failure["message"]),
                details=details,
                usage=dict(getattr(exc, "usage", {}) or {}),
            ) from exc
        with Database(self.paths.database) as db:
            db.update_ai_run(
                run_id,
                status="complete",
                usage=result.get("usage") or {},
                preparation_meta={
                    **dict(result.get("preparation") or budget.snapshot()),
                    "generation_mode": selected_generation_mode,
                    "cache_mode": selected_cache_mode,
                },
                completed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
            )
        result["ai_run_id"] = run_id
        result["generation_mode"] = selected_generation_mode
        result["cache_mode"] = selected_cache_mode
        result["deprecations"] = deprecations
        run_row = result.get("run")
        if isinstance(run_row, Mapping):
            result["unvalidated"] = bool(run_row.get("unvalidated", False))
            result["degraded_reason"] = str(
                run_row.get("degraded_reason") or ""
            )
            result["validator_requested"] = bool(
                run_row.get("validator_requested", False)
            )
            result["validator_certified"] = bool(
                run_row.get("validator_certified", False)
            )
        return result

    def stress_runs(self, problem: str | None = None) -> dict[str, Any]:
        if problem:
            ref = parse_problem_ref(problem)
            problem_id = _db_problem_id(ref.platform, ref.problem_id)
        else:
            problem_id = None
        return {"ok": True, "runs": self._stress.runs(problem_id=problem_id)}

    def stress_run(self, run_id: str) -> dict[str, Any]:
        return {"ok": True, "run": self._stress.run(str(run_id))}

    def stress_stop(self, run_id: str) -> dict[str, Any]:
        return self._stress.stop(str(run_id))

    def stress_resume(self, run_id: str) -> dict[str, Any]:
        return self._stress.resume(str(run_id))

    def stress_finish(self, run_id: str) -> dict[str, Any]:
        return self._stress.finish(str(run_id))

    def stress_bundle(self, bundle_id: str) -> dict[str, Any]:
        return {"ok": True, "bundle": self._stress.bundle(str(bundle_id))}

    def stress_bundle_revert(self, bundle_id: str) -> dict[str, Any]:
        return self._stress.revert_bundle(str(bundle_id))

    def shutdown(self) -> None:
        self._stress.shutdown()


__all__ = ["ServiceStressMixin"]
