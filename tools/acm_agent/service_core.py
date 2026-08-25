"""Core setup, synchronization, status, and recommendation service methods."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import inspect
import json
from typing import Any, Callable, Mapping

from .config import load_config, save_config
from .deepseek import DeepSeekClient
from .plan_manager import PlanManager
from .platforms import freshness
from .recommend import LUOGU_CF_EQUIVALENT, recommendation_difficulty_targets
from .service_common import _db_problem_id, _display_problem_id, _problem_key
from .storage import Database
from .tag_policy import effective_tags
from .workspace import parse_problem_ref, scan_local_solutions


class ServiceCoreMixin:
    @staticmethod
    def _accepts_keyword(function: Callable[..., Any], name: str) -> bool:
        side_effect = getattr(function, "side_effect", None)
        if callable(side_effect):
            function = side_effect
        try:
            parameters = inspect.signature(function).parameters.values()
        except (TypeError, ValueError):
            return False
        return any(
            parameter.kind == inspect.Parameter.VAR_KEYWORD
            or parameter.name == name
            for parameter in parameters
        )

    @property
    def configured(self) -> bool:
        if not self.paths.config.is_file():
            return False
        config = load_config(self.paths)
        return bool(
            str(config["accounts"]["codeforces"].get("handle") or "").strip()
            and str(config["accounts"]["luogu"].get("uid") or "").strip()
        )

    def _deepseek_client(self, *, timeout: float | None = None) -> Any:
        if self._deepseek_client_factory is DeepSeekClient:
            return DeepSeekClient(
                api_key=self._deepseek_api_key,
                timeout=(60.0 if timeout is None else float(timeout)),
            )
        return self._deepseek_client_factory()

    @staticmethod
    def _recent_solved_difficulty_profile(
        db: Database, *, limit_per_platform: int = 50
    ) -> dict[str, Any]:
        """Collect up to 50 latest distinct solved problems from each platform."""

        problem_rows = {
            (str(row["platform"]), str(row["problem_id"])): row
            for row in db.problems()
        }
        evidence: dict[tuple[str, str], tuple[str | None, bool]] = {}
        for row in db.query(
            """SELECT platform,problem_id,submitted_at AS solved_at
               FROM submissions
               WHERE (platform='codeforces' AND UPPER(verdict)='OK')
                  OR (platform='luogu' AND UPPER(verdict)='AC')
               UNION ALL
               SELECT platform,problem_id,closed_at AS solved_at
               FROM attempts
               WHERE UPPER(result)='AC' AND closed_at IS NOT NULL"""
        ):
            platform = str(row["platform"] or "").lower()
            if platform not in {"codeforces", "luogu"}:
                continue
            key = (platform, str(row["problem_id"]))
            solved_at = str(row["solved_at"] or "") or None
            current = evidence.get(key)
            if current is None or str(solved_at or "") > str(current[0] or ""):
                evidence[key] = (solved_at, solved_at is not None)

        values: list[int] = []
        platforms: dict[str, dict[str, Any]] = {}
        for platform in ("codeforces", "luogu"):
            rows: list[tuple[tuple[Any, ...], int | None, bool]] = []
            for (row_platform, problem_id), (solved_at, timestamped) in evidence.items():
                if row_platform != platform:
                    continue
                metadata = problem_rows.get((platform, problem_id))
                if metadata is None:
                    continue
                fallback_at = str(metadata["updated_at"] or "")
                equivalent = (
                    int(metadata["rating"])
                    if platform == "codeforces" and metadata["rating"] is not None
                    else LUOGU_CF_EQUIVALENT.get(int(metadata["difficulty"]))
                    if platform == "luogu" and metadata["difficulty"] is not None
                    else None
                )
                rows.append(
                    (
                        (1 if timestamped else 0, solved_at or fallback_at, problem_id),
                        equivalent,
                        timestamped,
                    )
                )
            selected = sorted(rows, key=lambda item: item[0], reverse=True)[
                : max(0, int(limit_per_platform))
            ]
            known = [int(item[1]) for item in selected if item[1] is not None]
            values.extend(known)
            platforms[platform] = {
                "selected": len(selected),
                "with_equivalent_difficulty": len(known),
                "with_acceptance_time": sum(bool(item[2]) for item in selected),
                "average": round(sum(known) / len(known)) if known else None,
            }
        return {
            "limit_per_platform": int(limit_per_platform),
            "values": values,
            "sample_count": len(values),
            "average": round(sum(values) / len(values)) if values else None,
            "platforms": platforms,
        }

    def setup(
        self,
        codeforces: str,
        luogu: str | int,
        *,
        target_rating: int | None = None,
        skip_validate: bool = False,
        defer_sync: bool = False,
        _progress_callback: Callable[[Mapping[str, Any]], None] | None = None,
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

        config = load_config(self.paths, required=False)
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
        initial_sync: dict[str, Any] | None = None
        tag_enrichment: dict[str, Any] | None = None
        if not skip_validate and not defer_sync:
            try:
                # Initialization is the first point where both platform
                # identities are known.  Populate the complete CF catalog,
                # Luogu AC set, and every currently missing Luogu tag now so
                # later recommendations never need to perform metadata I/O.
                initial_sync = self.sync(
                    "all",
                    full_catalog=True,
                    import_local_files=False,
                    _progress_callback=_progress_callback,
                    _validated_cf_user=cf_user,
                )
                luogu_result = next(
                    (
                        item
                        for item in initial_sync.get("results", [])
                        if item.get("platform") == "luogu"
                    ),
                    None,
                )
                if luogu_result:
                    raw_enrichment = luogu_result.get("tag_enrichment")
                    if isinstance(raw_enrichment, Mapping):
                        tag_enrichment = dict(raw_enrichment)
            except Exception as exc:
                # Account initialization is durable even when public platform
                # data is temporarily unavailable; surface the failed eager
                # sync so the user can retry it without re-entering IDs.
                initial_sync = {
                    "ok": False,
                    "results": [],
                    "error": {
                        "code": "initial_sync_failed",
                        "message": " ".join(str(exc).split())[:300]
                        or exc.__class__.__name__,
                    },
                }
        return {
            "ok": True,
            "validated": not skip_validate,
            "accounts": {"codeforces": handle, "luogu": uid},
            "target_cf_rating": target_rating,
            "local_files_imported": imported,
            "config": str(self.paths.config),
            "initial_sync": initial_sync,
            "initial_sync_deferred": bool(not skip_validate and defer_sync),
            "tag_enrichment": tag_enrichment,
        }

    def sync(
        self,
        platform: str = "all",
        *,
        force: bool = False,
        full_catalog: bool = False,
        import_local_files: bool = True,
        _progress_callback: Callable[[Mapping[str, Any]], None] | None = None,
        _validated_cf_user: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        if platform not in {"all", "codeforces", "luogu"}:
            raise ValueError("platform 必须是 all、codeforces 或 luogu")
        config = load_config(self.paths)
        selected = [platform] if platform != "all" else ["codeforces", "luogu"]
        results: list[dict[str, Any]] = []
        started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

        def progress(values: Mapping[str, Any]) -> None:
            if _progress_callback is None:
                return
            payload = {
                "phase": str(values.get("phase") or "sync"),
                "platform": str(values.get("platform") or platform),
                "step": int(values.get("step") or 0),
                "total": int(values.get("total") or len(selected)),
                "completed": int(values.get("completed") or 0),
                "failed": int(values.get("failed") or 0),
                "message": str(values.get("message") or "正在同步平台数据"),
                "started_at": started_at,
                "last_activity_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "usable": bool(values.get("usable", False)),
            }
            _progress_callback(payload)

        progress(
            {
                "phase": "preparing",
                "platform": platform,
                "step": 0,
                "total": len(selected),
                "completed": 0,
                "failed": 0,
                "message": "正在准备同步",
                "usable": False,
            }
        )
        with Database(self.paths.database) as db:
            if import_local_files:
                self._import_local_files(db)
            for platform_index, selected_platform in enumerate(selected, start=1):
                progress(
                    {
                        "phase": "platform",
                        "platform": selected_platform,
                        "step": platform_index,
                        "total": len(selected),
                        "completed": platform_index - 1,
                        "failed": sum(item["status"] == "failed" for item in results),
                        "message": f"正在同步 {selected_platform}",
                        "usable": bool(results),
                    }
                )
                if selected_platform == "codeforces":
                    handle = str(config["accounts"]["codeforces"].get("handle") or "")
                    if not handle:
                        raise ValueError("未配置 Codeforces handle，请先运行 acm init")
                    kwargs: dict[str, Any] = {
                        "refresh_catalog": True if force else None,
                    }
                    if _progress_callback is not None and self._accepts_keyword(
                        self._sync_codeforces, "progress_callback"
                    ):
                        kwargs["progress_callback"] = progress
                    if (
                        isinstance(_validated_cf_user, Mapping)
                        and self._accepts_keyword(self._sync_codeforces, "validated_user")
                    ):
                        kwargs["validated_user"] = _validated_cf_user
                    result = self._sync_codeforces(db, handle, **kwargs)
                else:
                    uid = str(config["accounts"]["luogu"].get("uid") or "")
                    if not uid:
                        raise ValueError("未配置洛谷 UID，请先运行 acm init")
                    if full_catalog:
                        kwargs = {
                            "refresh_catalog": True if force else None,
                            "full_catalog": True,
                        }
                        if _progress_callback is not None and self._accepts_keyword(
                            self._sync_luogu, "progress_callback"
                        ):
                            kwargs["progress_callback"] = progress
                        result = self._sync_luogu(db, uid, **kwargs)
                    else:
                        cf_account = db.account("codeforces")
                        solved_profile = self._recent_solved_difficulty_profile(db)
                        difficulty_targets = recommendation_difficulty_targets(
                            cf_account["rating"] if cf_account else None,
                            solved_profile["values"],
                            target_rating=(
                                cf_account["target_rating"]
                                if cf_account and cf_account["target_rating"] is not None
                                else config.get("recommendation", {}).get("target_cf_rating")
                            ),
                        )
                        equivalents = {
                            1: 800, 2: 1000, 3: 1300, 4: 1600,
                            5: 1900, 6: 2200, 7: 2500,
                        }
                        target_difficulties = {
                            min(equivalents, key=lambda level: abs(equivalents[level] - target))
                            for target in difficulty_targets.values()
                        }
                        kwargs = {
                            "refresh_catalog": True if force else None,
                            "candidate_queries": [
                                {"page": 1, "difficulty": difficulty}
                                for difficulty in sorted(target_difficulties)
                            ],
                        }
                        if _progress_callback is not None and self._accepts_keyword(
                            self._sync_luogu, "progress_callback"
                        ):
                            kwargs["progress_callback"] = progress
                        result = self._sync_luogu(db, uid, **kwargs)
                row = result.as_dict()
                row["freshness"] = freshness(db, selected_platform)
                results.append(row)
                progress(
                    {
                        "phase": "platform_complete",
                        "platform": selected_platform,
                        "step": platform_index,
                        "total": len(selected),
                        "completed": platform_index,
                        "failed": sum(item["status"] == "failed" for item in results),
                        "message": f"{selected_platform} 同步结果：{row['status']}",
                        "usable": any(item["status"] in {"fresh", "partial"} for item in results),
                    }
                )
        statuses = [item["status"] for item in results]
        aggregate_status = (
            "fresh"
            if statuses and all(status == "fresh" for status in statuses)
            else "failed"
            if statuses and all(status == "failed" for status in statuses)
            else "partial"
        )
        progress(
            {
                "phase": "complete",
                "platform": platform,
                "step": len(selected),
                "total": len(selected),
                "completed": len(results),
                "failed": sum(status == "failed" for status in statuses),
                "message": f"同步完成：{aggregate_status}",
                "usable": any(status in {"fresh", "partial"} for status in statuses),
            }
        )
        return {
            "ok": all(item["status"] in {"fresh", "partial"} for item in results),
            "status": aggregate_status,
            "results": results,
        }

    def status(self) -> dict[str, Any]:
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

    @staticmethod
    def _catalog_eligible(row: Mapping[str, Any]) -> bool:
        """Only canonical CF/Luogu rows may enter the recommendation catalog.

        Legacy rows with nonstandard ids (for example ``9201`` stored on the
        codeforces platform) and custom-id rows must stay out: neither can be
        normalized to a plan-style problem key.
        """
        platform = str(row["platform"] or "").lower()
        if platform not in {"codeforces", "luogu"}:
            return False
        display_id = _display_problem_id(platform, str(row["problem_id"] or ""))
        try:
            return parse_problem_ref(display_id).platform == platform
        except ValueError:
            return False

    def recommendations(
        self,
        *,
        count: int = 3,
        mode: str = "mixed",
        source_mode: str = "balanced",
        plan_ids: list[str] | None = None,
        _record: bool = True,
        _return_pool: bool = False,
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
                if not self._catalog_eligible(row):
                    continue
                if db.problem_status(row["platform"], row["problem_id"]) == "accepted":
                    accepted.add(_problem_key(row["platform"], row["problem_id"]))
                elif db.problem_status(row["platform"], row["problem_id"]) == "skipped":
                    skipped.add(_problem_key(row["platform"], row["problem_id"]))
            due_by_key = self._review_due_by_key(db)
            problem_by_id = {(row["platform"], row["problem_id"]): row for row in problems}
            catalog: list[dict[str, Any]] = []
            for row in problems:
                if not self._catalog_eligible(row):
                    continue
                display_id = _display_problem_id(row["platform"], row["problem_id"])
                item = dict(row)
                item["problem_id"] = display_id
                item["problem_key"] = _problem_key(row["platform"], row["problem_id"])
                item["status"] = db.problem_status(row["platform"], row["problem_id"])
                item["review_due"] = due_by_key.get(item["problem_key"])
                item["source"] = "platform_catalog"
                item["tags"] = db.effective_problem_tags(
                    row["platform"], row["problem_id"]
                )
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
                base_tags = db.problem_base_tags(item["platform"], db_id)
                item["tags"] = effective_tags(
                    [*base_tags, item.get("topic", "")],
                    db.problem_tag_overrides(item["platform"], db_id),
                )
                if metadata:
                    item["name"] = metadata["name"] or ""
                    item["rating"] = metadata["rating"]
                    item["difficulty"] = metadata["difficulty"]

            account = db.account("codeforces")
            rating = account["rating"] if account else None
            target_rating = account["target_rating"] if account else None
            if target_rating is None:
                target_rating = config.get("recommendation", {}).get("target_cf_rating")
            solved_profile = self._recent_solved_difficulty_profile(db)
            difficulty_targets = recommendation_difficulty_targets(
                rating,
                solved_profile["values"],
                target_rating=target_rating,
            )
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
                for item in self._recommend(
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
                    recent_solved_equivalents=solved_profile["values"],
                    return_pool=_return_pool,
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
            if _record:
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
        return {
            "ok": True,
            "mode": mode,
            "source_mode": source_mode,
            "plan_ids": plan_ids,
            "recommendation_basis": basis,
            "sources": source_details,
            "warnings": warnings,
            "difficulty_profile": {
                "targets": {
                    "current_plus_100": difficulty_targets["recovery"],
                    "recent_solved_average": difficulty_targets["main"],
                    "target_rating": difficulty_targets["stretch"],
                },
                "recent_solved": {
                    key: value
                    for key, value in solved_profile.items()
                    if key != "values"
                },
            },
            "recommendations": output,
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
        detail = {
            "freshness": freshness(db, platform),
            "last_success_at": state["last_success_at"] if state else None,
            "last_attempt_status": state["status"] if state else "never",
            "error": state["error"] if state else None,
        }
        if platform == "luogu":
            tagless_count = 0
            if state:
                try:
                    metadata = json.loads(state["metadata_json"] or "{}")
                except (json.JSONDecodeError, KeyError, TypeError):
                    metadata = {}
                tagless = metadata.get("tag_enrichment_tagless")
                if isinstance(tagless, (Mapping, list)):
                    tagless_count = len(tagless)
            detail["tagless"] = tagless_count
        return detail


__all__ = ["ServiceCoreMixin"]
