"""Application service shared by the CLI and the local web interface."""

from __future__ import annotations

from collections import Counter
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any, Callable, Mapping

from .config import DEFAULT_CONFIG, Paths, load_config, save_config
from .plan import check_plan, load_plan, plan_task_records, validate_plan_data
from .plan_manager import PlanManager
from .platforms import (
    CodeforcesClient,
    LuoguClient,
    freshness,
    preview_plan_task_tags,
    sync_codeforces,
    sync_luogu,
)
from .recommend import compute_weakness, recommend
from .storage import Database
from .verify import verify_problem
from .workspace import ProblemRef, parse_problem_ref, scan_local_solutions, start_problem


RESULTS = ("AC", "WA", "TLE", "RE", "MLE", "ABANDONED")
FAILURE_MODES = (
    "none",
    "selection",
    "modeling",
    "invariant",
    "implementation",
    "complexity",
    "edge_case",
    "editorial",
)


def _db_problem_id(platform: str, problem_id: str) -> str:
    problem_id = problem_id.upper()
    if platform == "codeforces" and problem_id.startswith("CF"):
        return problem_id[2:]
    return problem_id


def _display_problem_id(platform: str, problem_id: str) -> str:
    problem_id = str(problem_id).upper()
    if platform == "codeforces" and not problem_id.startswith("CF"):
        return f"CF{problem_id}"
    return problem_id


def _problem_key(platform: str, problem_id: str) -> str:
    return f"{platform}:{_display_problem_id(platform, problem_id)}"


class AcmService:
    """Structured application API for all stateful ACM workflows.

    Network and compiler dependencies are injectable so HTTP and unit tests do
    not need to monkey-patch implementation modules or launch subprocess CLIs.
    """

    def __init__(
        self,
        root: str | Path,
        *,
        codeforces_client_factory: Callable[[], Any] = CodeforcesClient,
        luogu_client_factory: Callable[[], Any] = LuoguClient,
        sync_codeforces_fn: Callable[..., Any] = sync_codeforces,
        sync_luogu_fn: Callable[..., Any] = sync_luogu,
        verify_fn: Callable[..., Any] = verify_problem,
    ) -> None:
        self.paths = Paths.for_root(Path(root))
        self._codeforces_client_factory = codeforces_client_factory
        self._luogu_client_factory = luogu_client_factory
        self._sync_codeforces = sync_codeforces_fn
        self._sync_luogu = sync_luogu_fn
        self._verify = verify_fn

    @property
    def configured(self) -> bool:
        if not self.paths.config.is_file():
            return False
        config = load_config(self.paths)
        return bool(
            str(config["accounts"]["codeforces"].get("handle") or "").strip()
            and str(config["accounts"]["luogu"].get("uid") or "").strip()
        )

    def setup(
        self,
        codeforces: str,
        luogu: str | int,
        *,
        target_rating: int | None = None,
        skip_validate: bool = False,
    ) -> dict[str, Any]:
        handle = str(codeforces).strip()
        uid = str(luogu).strip()
        if not handle or not uid or not uid.isdigit():
            raise ValueError("Codeforces handle 和洛谷数字 UID 均为必填")
        if target_rating is not None:
            target_rating = int(target_rating)

        cf_user: Mapping[str, Any] | None = None
        luogu_user: Mapping[str, Any] | None = None
        if not skip_validate:
            cf_user = self._codeforces_client_factory().user_info(handle)
            luogu_user = self._luogu_client_factory().user_info(uid)

        config = json.loads(json.dumps(DEFAULT_CONFIG))
        config["accounts"]["codeforces"]["handle"] = handle
        config["accounts"]["luogu"]["uid"] = uid
        config["recommendation"]["target_cf_rating"] = target_rating
        save_config(self.paths, config)
        with Database(self.paths.database) as db:
            stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
            db.upsert_account(
                "codeforces",
                handle,
                display_name=str((cf_user or {}).get("handle") or handle),
                rating=(cf_user or {}).get("rating"),
                target_rating=target_rating,
                validated_at=None if skip_validate else stamp,
            )
            # COALESCE in upsert_account intentionally preserves a previous
            # target when None, but setup is also the settings update API and
            # must therefore allow the user to clear it.
            db.connection.execute(
                "UPDATE accounts SET target_rating=? WHERE platform='codeforces'",
                (target_rating,),
            )
            db.upsert_account(
                "luogu",
                uid,
                display_name=str((luogu_user or {}).get("name") or uid),
                validated_at=None if skip_validate else stamp,
            )
            PlanManager(self.paths.root, db, builtin_plan=self.paths.plan)
            imported = self._import_local_files(db)
        return {
            "ok": True,
            "validated": not skip_validate,
            "accounts": {"codeforces": handle, "luogu": uid},
            "target_cf_rating": target_rating,
            "local_files_imported": imported,
            "config": str(self.paths.config),
        }

    def sync(self, platform: str = "all", *, force: bool = False) -> dict[str, Any]:
        if platform not in {"all", "codeforces", "luogu"}:
            raise ValueError("platform 必须是 all、codeforces 或 luogu")
        config = load_config(self.paths)
        selected = [platform] if platform != "all" else ["codeforces", "luogu"]
        results: list[dict[str, Any]] = []
        with Database(self.paths.database) as db:
            self._import_local_files(db)
            for selected_platform in selected:
                if selected_platform == "codeforces":
                    handle = str(config["accounts"]["codeforces"].get("handle") or "")
                    if not handle:
                        raise ValueError("未配置 Codeforces handle，请先运行 acm init")
                    result = self._sync_codeforces(
                        db, handle, refresh_catalog=True if force else None
                    )
                else:
                    uid = str(config["accounts"]["luogu"].get("uid") or "")
                    if not uid:
                        raise ValueError("未配置洛谷 UID，请先运行 acm init")
                    cf_account = db.account("codeforces")
                    baseline = int(cf_account["rating"] or 1600) if cf_account else 1600
                    equivalents = {
                        1: 800, 2: 1000, 3: 1300, 4: 1600,
                        5: 1900, 6: 2200, 7: 2500,
                    }
                    target_difficulties = {
                        min(equivalents, key=lambda level: abs(equivalents[level] - target))
                        for target in (baseline - 150, baseline + 50, baseline + 250)
                    }
                    result = self._sync_luogu(
                        db,
                        uid,
                        refresh_catalog=True if force else None,
                        candidate_queries=[
                            {"page": 1, "difficulty": difficulty}
                            for difficulty in sorted(target_difficulties)
                        ],
                    )
                row = result.as_dict()
                row["freshness"] = freshness(db, selected_platform)
                results.append(row)
        return {
            "ok": all(item["status"] in {"fresh", "partial"} for item in results),
            "results": results,
        }

    def status(self) -> dict[str, Any]:
        config = load_config(self.paths)
        with Database(self.paths.database) as db:
            self._import_local_files(db)
            counts: Counter[str] = Counter()
            accepted_by_platform: Counter[str] = Counter()
            accepted = 0
            for row in db.problems():
                state = db.problem_status(row["platform"], row["problem_id"])
                counts[state] += 1
                accepted += state == "accepted"
                if state == "accepted":
                    accepted_by_platform[row["platform"]] += 1
            accounts: dict[str, Any] = {}
            sources: dict[str, Any] = {}
            for platform in ("codeforces", "luogu"):
                account = db.account(platform)
                state = db.sync_state(platform)
                accounts[platform] = dict(account) if account else None
                sources[platform] = self._source_detail(db, platform, state)
            due = self._review_due_rows(db)
            return {
                "ok": True,
                "accounts": accounts,
                "sources": sources,
                "status_counts": dict(sorted(counts.items())),
                "accepted": accepted,
                "accepted_by_platform": {
                    platform: accepted_by_platform[platform]
                    for platform in ("codeforces", "luogu")
                },
                "local_files": len(db.local_files()),
                "review_due": len(due),
            }

    def bootstrap(self, *, recent_limit: int = 20) -> dict[str, Any]:
        """Return all dashboard startup data without requiring configuration."""
        if not self.configured:
            return {
                "ok": True,
                "configured": False,
                "accounts": {"codeforces": None, "luogu": None},
                "sources": {},
                "status_counts": {},
                "accepted": 0,
                "accepted_by_platform": {"codeforces": 0, "luogu": 0},
                "local_files": 0,
                "active_sessions": [],
                "recent_sessions": [],
                "review_due": [],
            }
        status = self.status()
        with Database(self.paths.database) as db:
            attempts = db.attempts()
            active = [self._session_dict(db, row) for row in attempts if row["active"]]
            recent = [
                self._session_dict(db, row)
                for row in attempts if not row["active"]
            ][:max(0, int(recent_limit))]
            due = self._review_due_rows(db)
        return {
            **status,
            "configured": True,
            "active_sessions": active,
            "recent_sessions": recent,
            "review_due": due,
        }

    def recommendations(
        self,
        *,
        count: int = 3,
        mode: str = "mixed",
        source_mode: str = "balanced",
        plan_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        if count < 1:
            raise ValueError("count 必须至少为 1")
        if mode not in {"mixed", "new", "review"}:
            raise ValueError("mode 必须是 mixed、new 或 review")
        if source_mode not in {"balanced", "catalog_only", "plan_only"}:
            raise ValueError("source_mode 必须是 balanced、catalog_only 或 plan_only")
        if plan_ids is not None:
            if not isinstance(plan_ids, list) or not all(isinstance(item, str) for item in plan_ids):
                raise ValueError("plan_ids 必须是题单 ID 字符串数组")
            plan_ids = list(dict.fromkeys(item.strip() for item in plan_ids if item.strip()))
        config = load_config(self.paths)
        with Database(self.paths.database) as db:
            self._import_local_files(db)
            problems = db.problems()
            accepted: set[str] = set()
            skipped: set[str] = set()
            for row in problems:
                display_id = _display_problem_id(row["platform"], row["problem_id"])
                try:
                    parse_problem_ref(display_id)
                except ValueError:
                    continue
                if db.problem_status(row["platform"], row["problem_id"]) == "accepted":
                    accepted.add(_problem_key(row["platform"], row["problem_id"]))
                elif db.problem_status(row["platform"], row["problem_id"]) == "skipped":
                    skipped.add(_problem_key(row["platform"], row["problem_id"]))
            due_by_key = self._review_due_by_key(db)
            problem_by_id = {(row["platform"], row["problem_id"]): row for row in problems}
            catalog: list[dict[str, Any]] = []
            for row in problems:
                display_id = _display_problem_id(row["platform"], row["problem_id"])
                try:
                    parse_problem_ref(display_id)
                except ValueError:
                    continue
                item = dict(row)
                item["problem_id"] = display_id
                item["problem_key"] = _problem_key(row["platform"], row["problem_id"])
                item["status"] = db.problem_status(row["platform"], row["problem_id"])
                item["review_due"] = due_by_key.get(item["problem_key"])
                item["source"] = "platform_catalog"
                catalog.append(item)
            plan_manager = PlanManager(self.paths.root, db, builtin_plan=self.paths.plan)
            plan_records = plan_manager.recommendation_records(plan_ids=plan_ids)
            for item in plan_records:
                db_id = _db_problem_id(item["platform"], item["problem_id"])
                metadata = problem_by_id.get((item["platform"], db_id))
                item["status"] = (
                    "accepted" if item["problem_key"] in accepted
                    else db.problem_status(item["platform"], db_id)
                )
                item["review_due"] = due_by_key.get(item["problem_key"])
                platform_tags = json.loads(metadata["tags_json"] or "[]") if metadata else []
                item["tags"] = list(dict.fromkeys([item["topic"], *platform_tags]))
                if metadata:
                    item["name"] = metadata["name"] or ""
                    item["rating"] = metadata["rating"]
                    item["difficulty"] = metadata["difficulty"]

            account = db.account("codeforces")
            rating = account["rating"] if account else None
            target_rating = account["target_rating"] if account else None
            if target_rating is None:
                target_rating = config.get("recommendation", {}).get("target_cf_rating")
            recent_ac = [
                {
                    "problem_id": row["problem_id"],
                    "rating": row["rating"],
                    "verdict": row["verdict"],
                    "timestamp": row["submitted_at"],
                }
                for row in db.query(
                    """SELECT s.problem_id,s.verdict,s.submitted_at,p.rating
                       FROM submissions s JOIN problems p USING(platform,problem_id)
                       WHERE s.platform='codeforces' AND s.verdict='OK'
                         AND p.rating IS NOT NULL
                       ORDER BY s.submitted_at DESC"""
                )
            ]
            history: list[dict[str, Any]] = []
            for row in reversed(db.recommendation_runs(limit=30)):
                item = dict(row)
                try:
                    stored_breakdown = json.loads(item.get("breakdown_json") or "{}")
                except json.JSONDecodeError:
                    stored_breakdown = {}
                item["plan_sources"] = stored_breakdown.get("_plan_sources", [])
                history.append(item)
            output = [
                item.to_dict()
                for item in recommend(
                    catalog,
                    plan_tasks=plan_records,
                    accepted_keys=accepted,
                    skipped_keys=skipped,
                    attempts=self._attempt_rows_with_tags(db),
                    recommendation_history=history,
                    count=count,
                    mode=mode,
                    source_mode=source_mode,
                    plan_ids=plan_ids,
                    cf_rating=rating,
                    target_cf_rating=target_rating,
                    recent_cf_accepted=recent_ac,
                )
            ]
            for item in output:
                db_id = _db_problem_id(item["platform"], item["problem_id"])
                db.upsert_problem({
                    "platform": item["platform"],
                    "problem_id": db_id,
                    "name": item.get("title"),
                    "url": item.get("url"),
                })
            db.record_recommendations(
                mode,
                [
                    {
                        **item,
                        "problem_id": _db_problem_id(item["platform"], item["problem_id"]),
                        "breakdown": {
                            **item["breakdown"],
                            "_plan_sources": item.get("plan_sources", []),
                        },
                    }
                    for item in output
                ],
            )
            sources = {platform: freshness(db, platform) for platform in ("codeforces", "luogu")}
            source_details = {
                platform: self._source_detail(db, platform, db.sync_state(platform))
                for platform in ("codeforces", "luogu")
            }
        warnings: list[str] = []
        if source_mode == "plan_only":
            basis = "plan_only"
        elif not any(item["last_success_at"] for item in source_details.values()):
            basis = "plan_only" if any(item.get("plan_sources") for item in output) else "unavailable"
            warnings.append("没有可用的平台成功快照；平台题库候选不可用。")
        elif any(value != "fresh" for value in sources.values()):
            basis = "cached"
            warnings.append("部分平台不是 fresh；推荐使用最后成功快照与本地记录。")
        else:
            basis = "synced"
        if source_mode == "balanced" and len(output) < count:
            warnings.append("平台题库候选不足；为保持题单占比上限，本次没有用更多题单题目补位。")
        return {
            "ok": True,
            "mode": mode,
            "source_mode": source_mode,
            "plan_ids": plan_ids,
            "recommendation_basis": basis,
            "sources": source_details,
            "warnings": warnings,
            "recommendations": output,
        }

    def start(self, problem: str, *, with_stress: bool = False) -> dict[str, Any]:
        load_config(self.paths)
        ref = parse_problem_ref(problem)
        with Database(self.paths.database) as db:
            db_id = _db_problem_id(ref.platform, ref.problem_id)
            if db.problem_status(ref.platform, db_id) == "skipped":
                raise ValueError(f"{ref.problem_id} 已被 Skip；请先 unskip 再开始")
        result = start_problem(self.paths.root, problem, with_stress=with_stress)
        with Database(self.paths.database) as db:
            problem_id = _db_problem_id(result.problem.platform, result.problem.problem_id)
            db.upsert_problem({"platform": result.problem.platform, "problem_id": problem_id})
            db.upsert_local_file(result.source, result.problem.platform, problem_id)
            attempt_id = self._find_or_start_attempt(db, result.problem)
        return {**result.to_dict(), "attempt_id": attempt_id, "ok": True}

    def verify(
        self,
        problem: str | None = None,
        *,
        debug: bool = False,
        exact: bool = False,
        timeout: float = 2.0,
        stress_iterations: int = 100,
        seed: int | None = None,
    ) -> dict[str, Any]:
        load_config(self.paths)
        selected = problem
        if not selected:
            with Database(self.paths.database) as db:
                active = [row for row in db.attempts() if row["active"]]
            if not active:
                raise ValueError("未指定题号，且没有 active session")
            selected = _display_problem_id(active[0]["platform"], active[0]["problem_id"])
        result = self._verify(
            self.paths.root,
            selected,
            exact=exact,
            debug=debug,
            timeout=float(timeout),
            stress_iterations=int(stress_iterations),
            seed=seed,
        )
        payload = result.to_dict()
        payload["ok"] = result.passed
        return payload

    def problem_skip(
        self,
        problem: str,
        *,
        reason: str = "idea_clear_without_editorial",
        note: str = "",
        notes: str | None = None,
        source: str = "agent",
        context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        ref = parse_problem_ref(problem)
        problem_id = _db_problem_id(ref.platform, ref.problem_id)
        reason = str(reason or "idea_clear_without_editorial").strip()
        if reason != "idea_clear_without_editorial":
            raise ValueError("Skip reason 必须是 idea_clear_without_editorial")
        note = str(note if notes is None else notes).strip()
        source = str(source or "agent").strip().lower()
        if source not in {"web", "cli", "agent"}:
            raise ValueError("source 必须是 web、cli 或 agent")
        if context is not None and not isinstance(context, Mapping):
            raise ValueError("context 必须是 JSON 对象")
        with Database(self.paths.database) as db:
            db.upsert_problem({"platform": ref.platform, "problem_id": problem_id})
            if db.problem_status(ref.platform, problem_id) == "accepted":
                raise ValueError(f"{ref.problem_id} 已 AC，不能 Skip")
            active = db.connection.execute(
                """SELECT 1 FROM attempts
                   WHERE platform=? AND problem_id=? AND active=1 LIMIT 1""",
                (ref.platform, problem_id),
            ).fetchone()
            if active:
                raise ValueError(f"{ref.problem_id} 存在 active session，不能 Skip")
            db.skip_problem(
                ref.platform,
                problem_id,
                notes=note,
                source=source,
                context=context,
            )
        return {
            "ok": True,
            "problem": ref.problem_id,
            "platform": ref.platform,
            "problem_id": ref.problem_id,
            "status": "skipped",
            "disposition": "skipped_mastered",
            "reason": reason,
            "note": note,
            "notes": note,
        }

    def problem_unskip(
        self,
        problem: str,
        *,
        reason: str = "idea_clear_without_editorial",
        note: str = "",
        notes: str | None = None,
        source: str = "agent",
        context: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        ref = parse_problem_ref(problem)
        problem_id = _db_problem_id(ref.platform, ref.problem_id)
        if str(reason or "idea_clear_without_editorial") != "idea_clear_without_editorial":
            raise ValueError("Skip reason 必须是 idea_clear_without_editorial")
        source = str(source or "agent").strip().lower()
        if source not in {"web", "cli", "agent"}:
            raise ValueError("source 必须是 web、cli 或 agent")
        if context is not None and not isinstance(context, Mapping):
            raise ValueError("context 必须是 JSON 对象")
        with Database(self.paths.database) as db:
            note = str(note if notes is None else notes).strip()
            removed = db.unskip_problem(
                ref.platform,
                problem_id,
                notes=note,
                source=source,
                context=context,
            )
            status = db.problem_status(ref.platform, problem_id)
        return {
            "ok": True,
            "problem": ref.problem_id,
            "platform": ref.platform,
            "problem_id": ref.problem_id,
            "unskipped": removed,
            "status": status,
            "disposition": None,
            "note": note,
            "notes": note,
        }

    def skipped_problems(self) -> dict[str, Any]:
        with Database(self.paths.database) as db:
            rows: list[dict[str, Any]] = []
            for disposition in db.problem_dispositions():
                platform = disposition["platform"]
                problem_id = disposition["problem_id"]
                if db.problem_status(platform, problem_id) != "skipped":
                    continue
                problem = db.connection.execute(
                    "SELECT name,url FROM problems WHERE platform=? AND problem_id=?",
                    (platform, problem_id),
                ).fetchone()
                display_id = _display_problem_id(platform, problem_id)
                rows.append(
                    {
                        "platform": platform,
                        "problem_id": display_id,
                        "problem_key": _problem_key(platform, problem_id),
                        "name": problem["name"] if problem else None,
                        "url": problem["url"] if problem else None,
                        "reason": disposition["reason"],
                        "notes": disposition["notes"],
                        "note": disposition["notes"],
                        "source": disposition["source"],
                        "context": json.loads(disposition["context_json"] or "{}"),
                        "disposition": disposition["disposition"],
                        "created_at": disposition["created_at"],
                        "updated_at": disposition["updated_at"],
                        "status": "skipped",
                    }
                )
        return {"ok": True, "problems": rows, "count": len(rows)}

    def close(
        self,
        problem: str,
        *,
        result: str,
        minutes: int | None,
        hint_level: int,
        failure: str | None = None,
        notes: str | None = None,
    ) -> dict[str, Any]:
        load_config(self.paths)
        ref = parse_problem_ref(problem)
        normalized_result = str(result).upper()
        if normalized_result not in RESULTS:
            raise ValueError(f"result 必须是 {', '.join(RESULTS)}")
        if minutes is not None and int(minutes) < 0:
            raise ValueError("minutes 不能为负数")
        hint = int(hint_level)
        if not 0 <= hint <= 4:
            raise ValueError("hint-level 必须在 0..4")
        if failure and failure not in FAILURE_MODES:
            raise ValueError(f"failure 必须是 {', '.join(FAILURE_MODES)}")

        problem_id = _db_problem_id(ref.platform, ref.problem_id)
        today = date.today()
        with Database(self.paths.database) as db:
            attempt_id = self._find_or_start_attempt(db, ref)
            previous = [
                row for row in db.attempts(ref.platform, problem_id)
                if row["id"] != attempt_id
            ]
            previous_wa = sum(str(row["result"] or "").upper() == "WA" for row in previous)
            previous_abandoned = any(
                str(row["result"] or "").upper() == "ABANDONED" for row in previous
            )
            previous_stage = max((int(row["review_stage"] or 0) for row in previous), default=0)
            previous_due = next(
                (row["review_due"][:10] for row in previous if row["review_due"]), None
            )
            qualifies = normalized_result == "AC" and (
                hint >= 2
                or previous_wa >= 2
                or previous_abandoned
                or (failure or "") in {"selection", "modeling", "invariant", "editorial"}
                or previous_stage > 0
            )
            if qualifies:
                next_stage = previous_stage + 1
                review_stage = min(next_stage, 3)
                delay = {1: 7, 2: 30, 3: 90}.get(next_stage)
                review_due = (today + timedelta(days=delay)).isoformat() if delay else None
            elif previous_stage > 0 and normalized_result != "AC":
                review_stage = previous_stage
                review_due = previous_due or today.isoformat()
            else:
                review_stage = 0
                review_due = None
            db.close_attempt(
                attempt_id,
                result=normalized_result,
                minutes=minutes,
                hint_level=hint,
                failure_mode=None if failure == "none" else failure,
                notes=notes,
                review_stage=review_stage,
                review_due=review_due,
            )
            state = db.problem_status(ref.platform, problem_id)

        candidate = {
            "problem_key": _problem_key(ref.platform, problem_id),
            "problem_id": ref.problem_id,
            "platform": ref.platform,
            "result": normalized_result,
            "minutes": minutes,
            "hint_level": hint,
            "failure_mode": None if failure == "none" else failure,
            "notes": notes,
            "review_stage": review_stage,
            "review_due": review_due,
            "archive_candidate": bool(
                normalized_result == "AC" and (hint >= 2 or failure not in {None, "none"})
            ),
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }
        self.paths.reports.mkdir(parents=True, exist_ok=True)
        report = self.paths.reports / f"archive-candidate-{attempt_id}.json"
        report.write_text(
            json.dumps(candidate, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return {
            "ok": True,
            "attempt_id": attempt_id,
            "status": state,
            "review_due": review_due,
            "archive_candidate": str(report),
            "close": candidate,
        }

    def weekly_review(self) -> dict[str, Any]:
        load_config(self.paths)
        cutoff = datetime.now(timezone.utc) - timedelta(days=7)
        with Database(self.paths.database) as db:
            attempts = [
                dict(row) for row in db.attempts()
                if row["closed_at"] and datetime.fromisoformat(row["closed_at"]) >= cutoff
            ]
            due = self._review_due_rows(db)
            weakness = compute_weakness(self._attempt_rows_with_tags(db))
        results = Counter(str(row["result"] or "unknown") for row in attempts)
        failures = Counter(str(row["failure_mode"] or "none") for row in attempts)
        average_hint = (
            round(sum(int(row["hint_level"] or 0) for row in attempts) / len(attempts), 2)
            if attempts else 0
        )
        payload: dict[str, Any] = {
            "ok": True,
            "window": {"from": cutoff.date().isoformat(), "to": date.today().isoformat()},
            "sessions": len(attempts),
            "results": dict(results),
            "failure_modes": dict(failures),
            "average_hint_level": average_hint,
            "weak_topics": weakness,
            "review_due": due,
        }
        self.paths.reports.mkdir(parents=True, exist_ok=True)
        report = self.paths.reports / f"week-{date.today().isoformat()}.md"
        lines = [
            f"# ACM 周复盘（截至 {date.today().isoformat()}）",
            "",
            f"- 完成 session：{len(attempts)}",
            f"- 平均提示等级：{average_hint}",
            f"- 到期复做：{len(due)}",
            "",
            "## 结果",
            "",
            *(f"- {key}: {value}" for key, value in sorted(results.items())),
            "",
            "## 薄弱专题",
            "",
            *(
                f"- {key}: {value}"
                for key, value in sorted(weakness.items(), key=lambda item: (-item[1], item[0]))
            ),
        ]
        report.write_text("\n".join(lines) + "\n", encoding="utf-8")
        payload["report"] = str(report)
        return payload

    def plans(self) -> dict[str, Any]:
        with Database(self.paths.database) as db:
            manager = PlanManager(self.paths.root, db, builtin_plan=self.paths.plan)
            return {"ok": True, "plans": manager.list_plans()}

    def plan_detail(self, plan_id: str) -> dict[str, Any]:
        with Database(self.paths.database) as db:
            manager = PlanManager(self.paths.root, db, builtin_plan=self.paths.plan)
            detail = manager.get_plan(str(plan_id))
            return {
                "ok": True,
                **detail,
                "task_statuses": manager.task_statuses(detail["plan"]),
            }

    def plan_template(self) -> dict[str, Any]:
        return {"ok": True, "plan": PlanManager.template()}

    @staticmethod
    def _plan_input(*, content: Any = None, plan: Any = None) -> Any:
        value = plan if plan is not None else content
        if value is None:
            raise ValueError("缺少题单 JSON 内容")
        if not isinstance(value, (str, bytes, Mapping)):
            raise ValueError("题单内容必须是 JSON 对象或 JSON 文本")
        return value

    def plan_preview(self, *, content: Any = None, plan: Any = None) -> dict[str, Any]:
        value = self._plan_input(content=content, plan=plan)
        with Database(self.paths.database) as db:
            manager = PlanManager(self.paths.root, db, builtin_plan=self.paths.plan)
            return {"ok": True, **manager.preview(value)}

    def plan_import(
        self,
        *,
        content: Any = None,
        plan: Any = None,
        replace: bool = False,
        confirm_replace: bool = False,
        expected_revision: int | None = None,
        enabled: bool = True,
    ) -> dict[str, Any]:
        value = self._plan_input(content=content, plan=plan)
        with Database(self.paths.database) as db:
            manager = PlanManager(self.paths.root, db, builtin_plan=self.paths.plan)
            result = manager.import_plan(
                value,
                confirm_replace=bool(replace or confirm_replace),
                expected_revision=expected_revision,
                enabled=bool(enabled),
            )
            return {"ok": True, "plan_id": result["plan"]["plan_id"], **result}

    def plan_edit(
        self,
        plan_id: str,
        expected_revision: int,
        *,
        plan: Mapping[str, Any] | None = None,
        document: Mapping[str, Any] | None = None,
        operation: Mapping[str, Any] | None = None,
        operations: Any = None,
    ) -> dict[str, Any]:
        if plan is not None and document is not None:
            raise ValueError("plan 与 document 不能同时提供")
        if operation is not None and operations is not None:
            raise ValueError("operation 与 operations 不能同时提供")
        with Database(self.paths.database) as db:
            manager = PlanManager(self.paths.root, db, builtin_plan=self.paths.plan)
            result = manager.edit_plan(
                str(plan_id),
                int(expected_revision),
                document=document if document is not None else plan,
                operations=operations if operations is not None else operation,
            )
            return {"ok": True, "plan_id": str(plan_id), **result}

    def plan_state(
        self, plan_id: str, enabled: bool, expected_revision: int
    ) -> dict[str, Any]:
        with Database(self.paths.database) as db:
            manager = PlanManager(self.paths.root, db, builtin_plan=self.paths.plan)
            result = manager.set_state(
                str(plan_id), bool(enabled), expected_revision=int(expected_revision)
            )
            return {"ok": True, "plan_id": str(plan_id), **result}

    def plan_delete(self, plan_id: str, expected_revision: int) -> dict[str, Any]:
        with Database(self.paths.database) as db:
            manager = PlanManager(self.paths.root, db, builtin_plan=self.paths.plan)
            return {
                "ok": True,
                **manager.delete_plan(str(plan_id), expected_revision=int(expected_revision)),
            }

    def plan_revisions(self, plan_id: str) -> dict[str, Any]:
        with Database(self.paths.database) as db:
            manager = PlanManager(self.paths.root, db, builtin_plan=self.paths.plan)
            return {
                "ok": True,
                "plan_id": str(plan_id),
                "revisions": manager.revisions(str(plan_id)),
            }

    def plan_restore(
        self,
        plan_id: str,
        expected_revision: int,
        *,
        revision: int | None = None,
        restore_builtin: bool = False,
    ) -> dict[str, Any]:
        with Database(self.paths.database) as db:
            manager = PlanManager(self.paths.root, db, builtin_plan=self.paths.plan)
            if restore_builtin:
                result = manager.restore_builtin(
                    str(plan_id), expected_revision=int(expected_revision)
                )
            else:
                if revision is None:
                    raise ValueError("恢复历史版本时必须提供 revision")
                result = manager.restore(
                    str(plan_id), int(revision), expected_revision=int(expected_revision)
                )
            return {"ok": True, "plan_id": str(plan_id), **result}

    @staticmethod
    def _plan_document_tasks(document: Mapping[str, Any]) -> list[dict[str, Any]]:
        tasks: list[dict[str, Any]] = []
        for stage in document.get("stages", []):
            tasks.extend(stage.get("tasks", []))
            tasks.extend(
                replacement["task"]
                for replacement in stage.get("replacements", [])
                if isinstance(replacement, Mapping)
                and isinstance(replacement.get("task"), dict)
            )
        return tasks

    @staticmethod
    def _normalise_plan_tags(value: Any) -> list[str]:
        if not isinstance(value, list) or any(not isinstance(tag, str) for tag in value):
            raise ValueError("suggested_tags 必须是字符串数组")
        result: list[str] = []
        seen: set[str] = set()
        for raw in value:
            tag = " ".join(raw.split())
            folded = tag.casefold()
            if tag and folded not in seen:
                seen.add(folded)
                result.append(tag)
        return result

    def plan_tags_preview(
        self,
        plan_id: str,
        *,
        expected_revision: int | None = None,
        overwrite: bool = False,
        refresh: bool = True,
    ) -> dict[str, Any]:
        """Build tag proposals; only the platform catalog may be refreshed."""
        with Database(self.paths.database) as db:
            manager = PlanManager(self.paths.root, db, builtin_plan=self.paths.plan)
            current = manager.get_plan(str(plan_id))
            if (
                expected_revision is not None
                and int(expected_revision) != int(current["revision"])
            ):
                from .plan_manager import RevisionConflict

                raise RevisionConflict(
                    str(plan_id), int(expected_revision), int(current["revision"])
                )
            tasks = self._plan_document_tasks(current["plan"])
            preview = preview_plan_task_tags(
                db,
                tasks,
                codeforces_client=self._codeforces_client_factory(),
                luogu_client=self._luogu_client_factory(),
                overwrite=bool(overwrite),
                refresh_codeforces=bool(refresh),
            )
        total_tasks = len(tasks)
        tagged_before = sum(bool(task.get("tags")) for task in tasks)
        newly_tagged = sum(
            bool(proposal.get("suggested_tags")) and not proposal.get("current_tags")
            for proposal in preview["proposals"]
        )
        projected = min(total_tasks, tagged_before + newly_tagged)
        preview["coverage"].update(
            {
                "total_tasks": total_tasks,
                "tagged_before": tagged_before,
                "before": tagged_before / total_tasks if total_tasks else 1.0,
                "current": tagged_before / total_tasks if total_tasks else 1.0,
                "tagged_after": projected,
                "after": projected / total_tasks if total_tasks else 1.0,
                "projected": projected / total_tasks if total_tasks else 1.0,
            }
        )
        return {
            "ok": True,
            "plan_id": str(plan_id),
            "base_revision": current["revision"],
            "total_tasks": total_tasks,
            "overwrite": bool(overwrite),
            **preview,
        }

    def plan_tags_apply(
        self,
        plan_id: str,
        expected_revision: int,
        proposals: Any,
    ) -> dict[str, Any]:
        if not isinstance(proposals, list):
            raise ValueError("proposals 必须是数组")
        with Database(self.paths.database) as db:
            manager = PlanManager(self.paths.root, db, builtin_plan=self.paths.plan)
            current = manager.get_plan(str(plan_id))
            if int(expected_revision) != int(current["revision"]):
                from .plan_manager import RevisionConflict

                raise RevisionConflict(
                    str(plan_id), int(expected_revision), int(current["revision"])
                )
            document = json.loads(json.dumps(current["plan"], ensure_ascii=False))
            task_by_key = {
                str(task["task_key"]): task for task in self._plan_document_tasks(document)
            }
            seen: set[str] = set()
            updated = 0
            skipped = 0
            for raw in proposals:
                if not isinstance(raw, Mapping):
                    raise ValueError("每个 proposal 必须是对象")
                task_key = str(raw.get("task_key") or "")
                if not task_key or task_key in seen:
                    raise ValueError(f"proposal task_key 缺失或重复: {task_key!r}")
                seen.add(task_key)
                task = task_by_key.get(task_key)
                if task is None:
                    raise ValueError(f"题单中不存在 task_key: {task_key}")
                if raw.get("platform") not in (None, task["platform"]):
                    raise ValueError(f"{task_key}: platform 与当前题单不一致")
                if raw.get("problem_id") not in (
                    None,
                    task["problem_id"],
                    _display_problem_id(task["platform"], task["problem_id"]),
                ):
                    raise ValueError(f"{task_key}: problem_id 与当前题单不一致")
                tags = self._normalise_plan_tags(
                    raw.get("suggested_tags", raw.get("tags", []))
                )
                if not tags or tags == task.get("tags", []):
                    skipped += 1
                    continue
                task["tags"] = tags
                updated += 1
            if updated:
                result = manager.edit_plan(
                    str(plan_id), int(expected_revision), document=document
                )
            else:
                result = current
        return {
            "ok": True,
            "plan_id": str(plan_id),
            "updated": updated,
            "skipped": skipped,
            **result,
        }

    def plan_check(self, plan_id: str | None = None) -> dict[str, Any]:
        with Database(self.paths.database) as db:
            manager = PlanManager(self.paths.root, db, builtin_plan=self.paths.plan)
            selected = [manager.get_plan(plan_id)] if plan_id else [
                manager.get_plan(row["plan_id"]) for row in manager.list_plans()
            ]
        results = [
            {
                "plan_id": item["plan"]["plan_id"],
                **validate_plan_data(item["plan"]).to_dict(),
            }
            for item in selected
        ]
        errors = [
            f"{item['plan_id']}: {error}"
            for item in results
            for error in item.get("errors", [])
        ]
        warnings = [
            f"{item['plan_id']}: {warning}"
            for item in results
            for warning in item.get("warnings", [])
        ]
        legacy = None
        if not plan_id and self.paths.plan.is_file() and self.paths.plan_readme.is_file():
            legacy = check_plan(self.paths.plan_readme, self.paths.plan).to_dict()
            errors.extend(legacy.get("errors", []))
            warnings.extend(legacy.get("warnings", []))
        return {
            "ok": not errors,
            "errors": errors,
            "warnings": warnings,
            "plans": results,
            "stats": (legacy or {}).get("stats", {}),
        }

    def _import_local_files(self, db: Database) -> int:
        solutions = scan_local_solutions(self.paths.root)
        for solution in solutions:
            platform = solution.problem.platform
            problem_id = _db_problem_id(platform, solution.problem.problem_id)
            db.upsert_problem({"platform": platform, "problem_id": problem_id})
            db.upsert_local_file(solution.path, platform, problem_id)
        return len(solutions)

    @staticmethod
    def _source_detail(db: Database, platform: str, state: Any) -> dict[str, Any]:
        return {
            "freshness": freshness(db, platform),
            "last_success_at": state["last_success_at"] if state else None,
            "last_attempt_status": state["status"] if state else "never",
            "error": state["error"] if state else None,
        }

    @staticmethod
    def _attempt_rows_with_tags(db: Database) -> list[dict[str, Any]]:
        problem_tags = {
            (row["platform"], row["problem_id"]): json.loads(row["tags_json"] or "[]")
            for row in db.problems()
        }
        return [
            {**dict(row), "tags": problem_tags.get((row["platform"], row["problem_id"]), [])}
            for row in db.attempts()
        ]

    @staticmethod
    def _review_due_by_key(db: Database) -> dict[str, str]:
        result: dict[str, str] = {}
        seen: set[str] = set()
        for row in db.attempts():
            key = _problem_key(row["platform"], row["problem_id"])
            if key in seen:
                continue
            seen.add(key)
            if row["review_due"]:
                result[key] = row["review_due"][:10]
        return result

    def _review_due_rows(self, db: Database) -> list[dict[str, Any]]:
        today = date.today().isoformat()
        return [
            {
                "problem_id": key.split(":", 1)[1],
                "platform": key.split(":", 1)[0],
                "review_due": due_date,
            }
            for key, due_date in sorted(self._review_due_by_key(db).items())
            if due_date <= today
        ]

    @staticmethod
    def _session_dict(db: Database, row: Any) -> dict[str, Any]:
        payload = dict(row)
        payload["attempt_id"] = int(row["id"])
        payload["problem_id"] = _display_problem_id(row["platform"], row["problem_id"])
        local = db.query(
            """SELECT path FROM local_files
               WHERE platform=? AND problem_id=? ORDER BY updated_at DESC LIMIT 1""",
            (row["platform"], row["problem_id"]),
        )
        payload["source"] = local[0]["path"] if local else None
        return payload

    @staticmethod
    def _find_or_start_attempt(db: Database, ref: ProblemRef) -> int:
        problem_id = _db_problem_id(ref.platform, ref.problem_id)
        active = [row for row in db.attempts(ref.platform, problem_id) if row["active"]]
        return int(active[0]["id"]) if active else db.start_attempt(ref.platform, problem_id)

    def _resolve_plan_records(self, db: Database, accepted: set[str]) -> list[dict[str, Any]]:
        plan = load_plan(self.paths.plan)
        records = plan_task_records(plan)
        for day in plan.days:
            for replacement in day.replacements:
                chosen = False
                if "D26" in replacement.condition:
                    chosen = "codeforces:CF1797D" in accepted
                    if chosen:
                        records = [
                            row for row in records
                            if not (row["day"] == day.day and row["problem_id"] == "CF1797D")
                        ]
                elif "任一" in replacement.condition:
                    original_keys = {task.problem_key for task in day.tasks}
                    chosen = bool(original_keys & accepted)
                    if chosen:
                        records = [
                            row for row in records
                            if not (row["day"] == day.day and row["problem_key"] in accepted)
                        ]
                if chosen:
                    task = replacement.task
                    unlock = task.unlock_at or day.unlock_at
                    records.append({
                        "task_key": task.task_key,
                        "problem_key": task.problem_key,
                        "problem_id": task.problem_id,
                        "platform": task.platform,
                        "url": task.url,
                        "level": task.level,
                        "required": task.required,
                        "topic": day.topic,
                        "day": day.day,
                        "due_date": day.scheduled_date.isoformat(),
                        "unlock_at": unlock.isoformat() if unlock else None,
                        "source": "plan_replacement",
                    })
        return records


__all__ = ["AcmService", "RESULTS", "FAILURE_MODES"]
