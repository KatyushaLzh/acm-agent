"""Training plan management and tag revision service methods."""

from __future__ import annotations

import json
from typing import Any, Mapping

from .plan import check_plan, validate_plan_data
from .plan_manager import PlanManager
from .platforms import preview_plan_task_tags
from .service_common import _db_problem_id, _display_problem_id
from .storage import Database
from .tag_policy import (
    meta_tag_reason,
    normalize_tags,
    split_meta_tags,
    tag_diff,
    tag_key,
)


class ServicePlanMixin:
    def plans(self) -> dict[str, Any]:
        with Database(self.paths.database) as db:
            manager = PlanManager(self.paths.root, db, builtin_plan=self.paths.plan)
            return {"ok": True, "plans": manager.list_plans()}

    def plan_detail(self, plan_id: str) -> dict[str, Any]:
        with Database(self.paths.database) as db:
            manager = PlanManager(self.paths.root, db, builtin_plan=self.paths.plan)
            detail = manager.get_plan(str(plan_id))
            task_effective_tags: dict[str, dict[str, Any]] = {}
            for task in self._plan_document_tasks(detail["plan"]):
                platform = str(task["platform"]).lower()
                problem_id = _db_problem_id(platform, str(task["problem_id"]))
                task_effective_tags[str(task["task_key"])] = {
                    "current_tags": normalize_tags(task.get("tags", [])),
                    "effective_tags": db.effective_problem_tags(platform, problem_id),
                }
            return {
                "ok": True,
                **detail,
                "task_statuses": manager.task_statuses(detail["plan"]),
                "task_effective_tags": task_effective_tags,
                "override_revision": db.tag_override_revision(),
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
        mode: str = "fill_missing",
    ) -> dict[str, Any]:
        """Build tag proposals; only the platform catalog may be refreshed."""
        mode = str(mode or "fill_missing").lower()
        if mode not in {"fill_missing", "cleanup"}:
            raise ValueError("mode 必须是 fill_missing 或 cleanup")
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
            if mode == "cleanup" and not refresh:
                cached_proposals: list[dict[str, Any]] = []
                for task in tasks:
                    platform = str(task["platform"]).lower()
                    problem_id = _db_problem_id(platform, str(task["problem_id"]))
                    row = db.connection.execute(
                        "SELECT tags_json FROM problems WHERE platform=? AND problem_id=?",
                        (platform, problem_id),
                    ).fetchone()
                    try:
                        cached_tags = normalize_tags(
                            json.loads(row["tags_json"] or "[]") if row else []
                        )
                    except json.JSONDecodeError:
                        cached_tags = []
                    cached_proposals.append(
                        {
                            "task_key": str(task["task_key"]),
                            "platform": platform,
                            "problem_id": _display_problem_id(platform, problem_id),
                            "name": str(task.get("name") or task.get("title") or task["problem_id"]),
                            "current_tags": normalize_tags(task.get("tags", [])),
                            "suggested_tags": cached_tags,
                            "source": "sqlite_catalog" if cached_tags else "unresolved",
                        }
                    )
                preview = {
                    "proposals": cached_proposals,
                    "coverage": {},
                    "errors": [],
                    "warnings": ["cleanup 使用本地平台标签快照；未请求远端刷新。"],
                }
            else:
                preview = preview_plan_task_tags(
                    db,
                    tasks,
                    codeforces_client=self._codeforces_client_factory(),
                    luogu_client=self._luogu_client_factory(),
                    overwrite=bool(overwrite or mode == "cleanup"),
                    refresh_codeforces=bool(refresh),
                )
            if mode == "cleanup":
                platform_by_task = {
                    str(item["task_key"]): item for item in preview["proposals"]
                }
                for item in preview["proposals"]:
                    raw_tags = normalize_tags(item.get("suggested_tags", []))
                    if raw_tags:
                        platform = str(item["platform"]).lower()
                        db.upsert_problem(
                            {
                                "platform": platform,
                                "problem_id": _db_problem_id(
                                    platform, str(item["problem_id"])
                                ),
                                "tags": raw_tags,
                            }
                        )
                proposals: list[dict[str, Any]] = []
                for task in tasks:
                    task_key = str(task["task_key"])
                    platform = str(task["platform"]).lower()
                    problem_id = _db_problem_id(platform, str(task["problem_id"]))
                    platform_item = platform_by_task.get(task_key, {})
                    raw_tags = normalize_tags(platform_item.get("suggested_tags", []))
                    if not raw_tags:
                        row = db.connection.execute(
                            "SELECT tags_json FROM problems WHERE platform=? AND problem_id=?",
                            (platform, problem_id),
                        ).fetchone()
                        if row:
                            try:
                                raw_tags = normalize_tags(json.loads(row["tags_json"] or "[]"))
                            except json.JSONDecodeError:
                                raw_tags = []
                    current_tags = normalize_tags(task.get("tags", []))
                    suggested_tags = db.effective_problem_tags(platform, problem_id)
                    added_tags, removed_tags = tag_diff(current_tags, suggested_tags)
                    _subject, ignored_meta = split_meta_tags([*raw_tags, *current_tags])
                    proposals.append(
                        {
                            "task_key": task_key,
                            "platform": platform,
                            "problem_id": _display_problem_id(platform, problem_id),
                            "name": str(task.get("name") or task.get("title") or task["problem_id"]),
                            "raw_tags": raw_tags,
                            "current_tags": current_tags,
                            "suggested_tags": suggested_tags,
                            "added_tags": added_tags,
                            "removed_tags": removed_tags,
                            "ignored_meta_tags": ignored_meta,
                            "source": platform_item.get("source", "effective_policy"),
                        }
                    )
                changed = sum(
                    bool(item["added_tags"] or item["removed_tags"])
                    for item in proposals
                )
                preview = {
                    "proposals": proposals,
                    "coverage": {
                        "eligible": len(tasks),
                        "suggested": sum(bool(item["suggested_tags"]) for item in proposals),
                        "unresolved": sum(not item["suggested_tags"] for item in proposals),
                        "skipped_nonempty": 0,
                        "changed": changed,
                        "added": sum(len(item["added_tags"]) for item in proposals),
                        "removed": sum(len(item["removed_tags"]) for item in proposals),
                        "ratio": (len(tasks) - changed) / len(tasks) if tasks else 1.0,
                    },
                    "errors": preview["errors"],
                    "warnings": preview["warnings"],
                }

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
                "override_revision": db.tag_override_revision(),
                "total_tasks": total_tasks,
                "overwrite": bool(overwrite),
                "mode": mode,
                **preview,
            }

    def plan_tags_apply(
        self,
        plan_id: str,
        expected_revision: int,
        proposals: Any,
        expected_override_revision: int | None = None,
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
            db.require_tag_override_revision(expected_override_revision)
            document = json.loads(json.dumps(current["plan"], ensure_ascii=False))
            task_by_key = {
                str(task["task_key"]): task for task in self._plan_document_tasks(document)
            }
            seen: set[str] = set()
            desired_by_problem: dict[tuple[str, str], list[str]] = {}
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
                if "tags" not in raw and "suggested_tags" not in raw:
                    raise ValueError(f"{task_key}: proposal 缺少 tags")
                tags = self._normalise_plan_tags(
                    raw["tags"] if "tags" in raw else raw["suggested_tags"]
                )
                platform = str(task["platform"]).lower()
                problem_id = _db_problem_id(platform, str(task["problem_id"]))
                problem_key = (platform, problem_id)
                previous_desired = desired_by_problem.get(problem_key)
                if previous_desired is not None and {
                    tag_key(tag) for tag in previous_desired
                } != {tag_key(tag) for tag in tags}:
                    raise ValueError(
                        f"{_display_problem_id(platform, problem_id)} 的全局标签建议不一致"
                    )
                desired_by_problem[problem_key] = tags
                if tags == normalize_tags(task.get("tags", [])):
                    skipped += 1
                    continue
                task["tags"] = tags
                updated += 1

            override_specs: dict[tuple[str, str], tuple[list[str], list[str]]] = {}
            override_changed = False
            for (platform, problem_id), desired in desired_by_problem.items():
                base_tags = db.problem_base_tags(platform, problem_id)
                subject_base, _ignored = split_meta_tags(base_tags)
                subject_by_key = {tag_key(tag): tag for tag in subject_base}
                desired_keys = {tag_key(tag) for tag in desired}
                additions = [
                    tag for tag in desired
                    if tag_key(tag) not in subject_by_key or meta_tag_reason(tag)
                ]
                suppressions = [
                    tag for tag in subject_base if tag_key(tag) not in desired_keys
                ]
                override_specs[(platform, problem_id)] = (additions, suppressions)
                wanted = {
                    **{tag_key(tag): (tag, "add") for tag in additions},
                    **{tag_key(tag): (tag, "suppress") for tag in suppressions},
                }
                existing = {
                    row["tag_key"]: (row["tag"], row["action"])
                    for row in db.problem_tag_overrides(platform, problem_id)
                }
                override_changed = override_changed or wanted != existing

            override_revision = db.tag_override_revision()

            def mutate_overrides() -> None:
                nonlocal override_revision
                changed = False
                db.require_tag_override_revision(expected_override_revision)
                for (platform, problem_id), (additions, suppressions) in override_specs.items():
                    db.upsert_problem(
                        {"platform": platform, "problem_id": problem_id}
                    )
                    changed = db.replace_problem_tag_overrides(
                        platform,
                        problem_id,
                        additions=additions,
                        suppressions=suppressions,
                        source="user",
                        reason="plan_tag_apply",
                    ) or changed
                if changed:
                    override_revision = db.bump_tag_override_revision(
                        expected_override_revision
                    )

            if updated or override_changed:
                result = manager.edit_plan(
                    str(plan_id),
                    int(expected_revision),
                    document=document,
                    db_mutation=mutate_overrides,
                )
            else:
                result = current
        return {
            "ok": True,
            "plan_id": str(plan_id),
            "updated": updated,
            "override_updated": override_changed,
            "override_revision": override_revision,
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


__all__ = ["ServicePlanMixin"]
