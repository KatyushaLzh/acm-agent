"""Core setup, synchronization, status, and recommendation service methods."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import json
from typing import Any, Mapping

from .config import load_config, save_config
from .deepseek import DeepSeekClient
from .plan_manager import PlanManager
from .platforms import freshness
from .service_common import _db_problem_id, _display_problem_id, _problem_key
from .storage import Database
from .tag_policy import effective_tags
from .workspace import parse_problem_ref, scan_local_solutions


class ServiceCoreMixin:
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


__all__ = ["ServiceCoreMixin"]
