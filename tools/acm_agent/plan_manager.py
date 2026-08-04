"""Transactional multi-plan registry and managed-file operations."""

from __future__ import annotations

from copy import deepcopy
import json
import os
from pathlib import Path
import tempfile
from typing import Any, Callable, Iterable, Mapping, Sequence

from .plan import (
    PlanError,
    TrainingPlan,
    canonical_problem_key,
    convert_v1_to_v2,
    enrich_plan_names,
    load_plan_data,
    plan_task_records,
    plan_template,
    plan_to_dict,
    plan_platform_stats,
    readme_problem_names,
    validate_plan_data,
)
from .storage import Database, PlanRevisionConflict


RevisionConflict = PlanRevisionConflict


class DuplicatePlanError(PlanError):
    """Raised when an import would replace a plan without explicit consent."""


class BuiltinPlanError(PlanError):
    """Raised for unsupported destructive operations on an embedded plan."""


def _canonical_document(value: Mapping[str, Any] | str | bytes) -> dict[str, Any]:
    plan = load_plan_data(value)
    if plan.schema_version == 1:
        return convert_v1_to_v2(plan)
    return plan_to_dict(plan)


class PlanManager:
    """Manage editable plans stored under ``.acm/plans``.

    The repository's original plan is registered as ``source=builtin`` and is
    never rewritten.  Editing it creates a managed override with the same
    ``plan_id``; restoring the builtin removes only that override.
    """

    def __init__(
        self,
        root: str | Path,
        database: Database | None = None,
        *,
        builtin_plan: str | Path | None = None,
        bootstrap: bool = True,
    ):
        self.root = Path(root).resolve()
        self.state_dir = self.root / ".acm"
        self.plans_dir = self.state_dir / "plans"
        self.plans_dir.mkdir(parents=True, exist_ok=True)
        self.db = database or Database(self.state_dir / "state.db")
        self._owns_database = database is None
        self.builtin_plan = Path(builtin_plan).resolve() if builtin_plan else (
            self.root / "training" / "data-structures-30d" / "plan.json"
        )
        if bootstrap:
            self.bootstrap_builtin()

    def close(self) -> None:
        if self._owns_database:
            self.db.close()

    def __enter__(self) -> "PlanManager":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def bootstrap_builtin(self) -> dict[str, Any] | None:
        """Register the legacy repository plan as an immutable v2 source."""
        if not self.builtin_plan.is_file():
            return None
        document = _canonical_document(self.builtin_plan.read_text(encoding="utf-8-sig"))
        readme = self.builtin_plan.with_name("README.md")
        if readme.is_file():
            document = enrich_plan_names(
                document, readme_problem_names(readme.read_text(encoding="utf-8-sig"))
            )
        existing = self.db.plan(document["plan_id"])
        if existing is None:
            self.db.save_plan(
                document,
                source="builtin",
                enabled=True,
                builtin_path=self.builtin_plan,
            )
        elif existing["source"] == "builtin":
            current = _canonical_document(json.loads(existing["content_json"]))
            if readme.is_file():
                current = enrich_plan_names(
                    current, readme_problem_names(readme.read_text(encoding="utf-8-sig"))
                )
            if current != json.loads(existing["content_json"]):
                managed_path = existing["managed_path"]
                if managed_path:
                    self._atomic_write(Path(managed_path), self._serialize(current))
                self.db.save_plan(
                    current,
                    enabled=bool(existing["enabled"]),
                    source="builtin",
                    builtin_path=existing["builtin_path"] or self.builtin_plan,
                    managed_path=managed_path,
                    expected_revision=int(existing["revision"]),
                )
        return self.get_plan(document["plan_id"])

    @staticmethod
    def template() -> dict[str, Any]:
        return plan_template()

    def _summary(self, row: Any) -> dict[str, Any]:
        document = _canonical_document(json.loads(row["content_json"]))
        plan = load_plan_data(document)
        stats = plan_platform_stats(plan)
        tasks = [task for stage in document["stages"] for task in stage.get("tasks", [])]
        completion = self._completion_stats(tasks)
        return {
            "plan_id": row["plan_id"],
            "title": row["title"],
            "description": row["description"],
            "schedule_mode": row["schedule_mode"],
            "enabled": bool(row["enabled"]),
            "source": row["source"],
            "has_override": bool(row["managed_path"]),
            "revision": int(row["revision"]),
            "stage_count": len(document["stages"]),
            **stats,
            **completion,
            "updated_at": row["updated_at"],
        }

    def list_plans(self, *, enabled: bool | None = None) -> list[dict[str, Any]]:
        return [self._summary(row) for row in self.db.plans(enabled=enabled)]

    # Short alias is convenient for service/CLI integration.
    list = list_plans

    def get_plan(self, plan_id: str) -> dict[str, Any]:
        row = self.db.plan(plan_id)
        if row is None:
            raise KeyError(f"plan {plan_id!r} not found")
        document = _canonical_document(json.loads(row["content_json"]))
        tasks = [task for stage in document["stages"] for task in stage.get("tasks", [])]
        return {
            "plan": document,
            **plan_platform_stats(load_plan_data(document)),
            **self._completion_stats(tasks),
            "revision": int(row["revision"]),
            "enabled": bool(row["enabled"]),
            "source": row["source"],
            "has_override": bool(row["managed_path"]),
            "builtin_path": row["builtin_path"],
            "managed_path": row["managed_path"],
            "updated_at": row["updated_at"],
        }

    get = get_plan

    def task_statuses(
        self, document_or_plan_id: Mapping[str, Any] | str
    ) -> dict[str, dict[str, Any]]:
        """Return runtime state for every main and replacement task.

        The status map is deliberately separate from the persisted plan
        document so imports and exports stay deterministic and state-free.
        """
        if isinstance(document_or_plan_id, str):
            document = self.get_plan(document_or_plan_id)["plan"]
        else:
            document = _canonical_document(document_or_plan_id)

        tasks: list[Mapping[str, Any]] = []
        for stage in document.get("stages", []):
            tasks.extend(stage.get("tasks", []))
            tasks.extend(
                replacement["task"]
                for replacement in stage.get("replacements", [])
                if isinstance(replacement, Mapping)
                and isinstance(replacement.get("task"), Mapping)
            )

        cache: dict[tuple[str, str], dict[str, Any]] = {}
        statuses: dict[str, dict[str, Any]] = {}
        for task in tasks:
            platform = str(task["platform"]).strip().lower()
            display_id = str(task["problem_id"]).strip().upper()
            db_id = display_id
            if platform == "codeforces" and db_id.startswith("CF"):
                db_id = db_id[2:]
            cache_key = (platform, db_id)
            runtime = cache.get(cache_key)
            if runtime is None:
                runtime = self.db.problem_runtime_status(platform, db_id)
                cache[cache_key] = runtime
            task_key = str(task["task_key"])
            statuses[task_key] = {
                "task_key": task_key,
                "problem_key": canonical_problem_key(display_id, platform),
                "platform": platform,
                "problem_id": display_id,
                **runtime,
            }
        return statuses

    def preview(self, value: Mapping[str, Any] | str | bytes) -> dict[str, Any]:
        """Validate an import and report whether it would replace a plan."""
        result = validate_plan_data(value)
        if not result.ok:
            return result.to_dict()
        document = _canonical_document(value)
        existing = self.db.plan(document["plan_id"])
        payload = result.to_dict()
        payload.update(
            {
                "plan": document,
                **plan_platform_stats(load_plan_data(document)),
                "duplicate": existing is not None,
                "current_revision": int(existing["revision"]) if existing else None,
                "diff": self._diff_summary(
                    json.loads(existing["content_json"]) if existing else None,
                    document,
                ),
            }
        )
        return payload

    @staticmethod
    def _diff_summary(
        before: Mapping[str, Any] | None, after: Mapping[str, Any]
    ) -> dict[str, Any]:
        def counts(document: Mapping[str, Any] | None) -> tuple[int, int]:
            if not document:
                return 0, 0
            stages = document.get("stages", [])
            return len(stages), sum(len(stage.get("tasks", [])) for stage in stages)

        old_stages, old_tasks = counts(before)
        new_stages, new_tasks = counts(after)
        return {
            "title_changed": before is not None and before.get("title") != after.get("title"),
            "stages_before": old_stages,
            "stages_after": new_stages,
            "tasks_before": old_tasks,
            "tasks_after": new_tasks,
        }

    def _managed_path(self, plan_id: str) -> Path:
        # plan_id is schema-validated and therefore cannot escape this directory.
        return self.plans_dir / f"{plan_id}.json"

    @staticmethod
    def _serialize(document: Mapping[str, Any]) -> bytes:
        return (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")

    @staticmethod
    def _atomic_write(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.stem}-", suffix=".tmp", dir=path.parent
        )
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    def _save_managed(
        self,
        document: Mapping[str, Any],
        *,
        expected_revision: int | None,
        enabled: bool,
        source: str,
        builtin_path: str | Path | None,
        db_mutation: Callable[[], None] | None = None,
    ) -> int:
        plan_id = str(document["plan_id"])
        path = self._managed_path(plan_id)
        previous = path.read_bytes() if path.exists() else None
        self._atomic_write(path, self._serialize(document))
        try:
            with self.db.atomic():
                revision = self.db.save_plan(
                    document,
                    enabled=enabled,
                    source=source,
                    builtin_path=builtin_path,
                    managed_path=path,
                    expected_revision=expected_revision,
                )
                if db_mutation is not None:
                    db_mutation()
            return revision
        except Exception:
            if previous is None:
                path.unlink(missing_ok=True)
            else:
                self._atomic_write(path, previous)
            raise

    def import_plan(
        self,
        value: Mapping[str, Any] | str | bytes,
        *,
        confirm_replace: bool = False,
        expected_revision: int | None = None,
        enabled: bool = True,
    ) -> dict[str, Any]:
        document = _canonical_document(value)
        existing = self.db.plan(document["plan_id"])
        if existing is not None and not confirm_replace:
            raise DuplicatePlanError(
                f"plan {document['plan_id']!r} already exists; preview and confirm replacement"
            )
        if existing is not None and expected_revision is None:
            raise PlanError("expected_revision is required when replacing an existing plan")
        source = existing["source"] if existing else "managed"
        builtin_path = existing["builtin_path"] if existing else None
        revision = self._save_managed(
            document,
            expected_revision=expected_revision,
            enabled=bool(existing["enabled"]) if existing else enabled,
            source=source,
            builtin_path=builtin_path,
        )
        result = self.get_plan(document["plan_id"])
        result["revision"] = revision
        return result

    import_ = import_plan

    def edit_plan(
        self,
        plan_id: str,
        expected_revision: int,
        *,
        document: Mapping[str, Any] | None = None,
        operations: Mapping[str, Any] | Sequence[Mapping[str, Any]] | None = None,
        db_mutation: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        current = self.get_plan(plan_id)
        if document is not None and operations is not None:
            raise PlanError("provide either document or operations, not both")
        if document is None:
            if operations is None:
                raise PlanError("edit requires document or operations")
            candidate = deepcopy(current["plan"])
            operation_list = [operations] if isinstance(operations, Mapping) else list(operations)
            for operation in operation_list:
                self._apply_operation(candidate, operation)
        else:
            candidate = deepcopy(dict(document))
        candidate["plan_id"] = plan_id
        normalized = _canonical_document(candidate)
        revision = self._save_managed(
            normalized,
            expected_revision=expected_revision,
            enabled=current["enabled"],
            source=current["source"],
            builtin_path=current["builtin_path"],
            db_mutation=db_mutation,
        )
        result = self.get_plan(plan_id)
        result["revision"] = revision
        return result

    edit = edit_plan

    @staticmethod
    def _find_stage(document: Mapping[str, Any], stage_key: str) -> dict[str, Any]:
        for stage in document["stages"]:
            if stage.get("stage_key") == stage_key:
                return stage
        raise PlanError(f"stage {stage_key!r} not found")

    @staticmethod
    def _find_task(document: Mapping[str, Any], task_key: str) -> tuple[dict[str, Any], int]:
        for stage in document["stages"]:
            for index, task in enumerate(stage.get("tasks", [])):
                if task.get("task_key") == task_key:
                    return stage, index
        raise PlanError(f"task {task_key!r} not found")

    @staticmethod
    def _detach_replacement_target(stage: dict[str, Any], task_key: str) -> None:
        for replacement in stage.get("replacements", []):
            replacement["replace_task_keys"] = [
                key for key in replacement.get("replace_task_keys", []) if key != task_key
            ]

    @classmethod
    def _apply_operation(cls, document: dict[str, Any], operation: Mapping[str, Any]) -> None:
        action = str(operation.get("action", operation.get("op", ""))).strip().lower()
        if action in {"plan_update", "update_plan"}:
            forbidden = {"platform_target", "platform_ratio"} & set(
                operation.get("patch", {})
            )
            if forbidden:
                raise PlanError("platform ratios are derived from tasks and cannot be edited")
            allowed = {"title", "description", "schedule_mode"}
            document.update({key: value for key, value in operation.get("patch", {}).items() if key in allowed})
            return
        if action in {"stage_add", "add_stage"}:
            stage = deepcopy(dict(operation["stage"]))
            index = int(operation.get("index", len(document["stages"])))
            document["stages"].insert(max(0, min(index, len(document["stages"]))), stage)
            return
        if action in {"stage_update", "update_stage"}:
            stage = cls._find_stage(document, str(operation["stage_key"]))
            patch = dict(operation.get("patch", {}))
            patch.pop("stage_key", None)
            stage.update(patch)
            return
        if action in {"stage_delete", "delete_stage"}:
            stage_key = str(operation["stage_key"])
            before = len(document["stages"])
            document["stages"] = [
                stage for stage in document["stages"] if stage.get("stage_key") != stage_key
            ]
            if len(document["stages"]) == before:
                raise PlanError(f"stage {stage_key!r} not found")
            return
        if action in {"stage_move", "move_stage"}:
            stage = cls._find_stage(document, str(operation["stage_key"]))
            document["stages"].remove(stage)
            index = int(operation["index"])
            document["stages"].insert(max(0, min(index, len(document["stages"]))), stage)
            return
        if action in {"task_add", "add_task"}:
            stage = cls._find_stage(document, str(operation["stage_key"]))
            tasks = stage.setdefault("tasks", [])
            index = int(operation.get("index", len(tasks)))
            tasks.insert(max(0, min(index, len(tasks))), deepcopy(dict(operation["task"])))
            return
        if action in {"task_update", "update_task"}:
            _stage, index = cls._find_task(document, str(operation["task_key"]))
            patch = dict(operation.get("patch", {}))
            patch.pop("task_key", None)
            _stage["tasks"][index].update(patch)
            return
        if action in {"task_delete", "delete_task"}:
            stage, index = cls._find_task(document, str(operation["task_key"]))
            cls._detach_replacement_target(stage, str(operation["task_key"]))
            del stage["tasks"][index]
            return
        if action in {"task_move", "move_task"}:
            old_stage, old_index = cls._find_task(document, str(operation["task_key"]))
            cls._detach_replacement_target(old_stage, str(operation["task_key"]))
            task = old_stage["tasks"].pop(old_index)
            new_stage = cls._find_stage(document, str(operation["stage_key"]))
            tasks = new_stage.setdefault("tasks", [])
            index = int(operation.get("index", len(tasks)))
            tasks.insert(max(0, min(index, len(tasks))), task)
            return
        raise PlanError(f"unsupported edit action: {action!r}")

    def set_state(
        self, plan_id: str, enabled: bool, *, expected_revision: int
    ) -> dict[str, Any]:
        revision = self.db.set_plan_enabled(
            plan_id, enabled, expected_revision=expected_revision
        )
        result = self.get_plan(plan_id)
        result["revision"] = revision
        return result

    def revisions(self, plan_id: str) -> list[dict[str, Any]]:
        if self.db.plan(plan_id) is None:
            raise KeyError(f"plan {plan_id!r} not found")
        return [
            {
                "plan_id": row["plan_id"],
                "revision": int(row["revision"]),
                "enabled": bool(row["enabled"]),
                "created_at": row["created_at"],
                "plan": _canonical_document(json.loads(row["content_json"])),
            }
            for row in self.db.plan_revisions(plan_id)
        ]

    def restore(
        self, plan_id: str, revision: int, *, expected_revision: int
    ) -> dict[str, Any]:
        current = self.get_plan(plan_id)
        snapshot = self.db.plan_revision(plan_id, revision)
        if snapshot is None:
            raise KeyError(f"plan {plan_id!r} revision {revision} not found")
        document = json.loads(snapshot["content_json"])
        new_revision = self._save_managed(
            document,
            expected_revision=expected_revision,
            enabled=bool(snapshot["enabled"]),
            source=current["source"],
            builtin_path=current["builtin_path"],
        )
        result = self.get_plan(plan_id)
        result["revision"] = new_revision
        return result

    def restore_builtin(self, plan_id: str, *, expected_revision: int) -> dict[str, Any]:
        current = self.get_plan(plan_id)
        if current["source"] != "builtin" or not current["builtin_path"]:
            raise BuiltinPlanError(f"plan {plan_id!r} has no builtin source")
        if current["revision"] != expected_revision:
            raise RevisionConflict(plan_id, expected_revision, current["revision"])
        builtin_path = Path(current["builtin_path"])
        document = _canonical_document(builtin_path.read_text(encoding="utf-8-sig"))
        readme = builtin_path.with_name("README.md")
        if readme.is_file():
            document = enrich_plan_names(
                document, readme_problem_names(readme.read_text(encoding="utf-8-sig"))
            )
        managed_path = Path(current["managed_path"]) if current["managed_path"] else None
        revision = self.db.save_plan(
            document,
            enabled=current["enabled"],
            source="builtin",
            builtin_path=builtin_path,
            managed_path=None,
            expected_revision=expected_revision,
        )
        if managed_path:
            managed_path.unlink(missing_ok=True)
        result = self.get_plan(plan_id)
        result["revision"] = revision
        return result

    def delete_plan(self, plan_id: str, *, expected_revision: int) -> dict[str, Any]:
        current = self.get_plan(plan_id)
        if current["revision"] != expected_revision:
            raise RevisionConflict(plan_id, expected_revision, current["revision"])
        if current["source"] == "builtin":
            if current["has_override"]:
                result = self.restore_builtin(plan_id, expected_revision=expected_revision)
                result["action"] = "override_removed"
                return result
            result = self.set_state(plan_id, False, expected_revision=expected_revision)
            result["action"] = "builtin_disabled"
            return result
        managed_path = Path(current["managed_path"]) if current["managed_path"] else None
        previous = managed_path.read_bytes() if managed_path and managed_path.exists() else None
        if managed_path:
            managed_path.unlink(missing_ok=True)
        try:
            self.db.delete_plan(plan_id)
        except Exception:
            if managed_path and previous is not None:
                self._atomic_write(managed_path, previous)
            raise
        return {"plan_id": plan_id, "deleted": True, "history_preserved": True}

    delete = delete_plan

    def _accepted_problem_keys(self) -> set[str]:
        rows = self.db.query(
            """SELECT platform,problem_id FROM submissions
               WHERE verdict IN ('OK','AC','Accepted')
               UNION
               SELECT platform,problem_id FROM attempts
               WHERE UPPER(result) IN ('OK','AC','ACCEPTED')"""
        )
        result: set[str] = set()
        for row in rows:
            try:
                result.add(canonical_problem_key(str(row["problem_id"]), row["platform"]))
            except PlanError:
                # Platform snapshots may retain non-standard contest indices;
                # those are intentionally ineligible for plan matching.
                continue
        return result

    def _skipped_problem_keys(self) -> set[str]:
        result: set[str] = set()
        for row in self.db.problem_dispositions():
            if self.db.problem_status(row["platform"], row["problem_id"]) != "skipped":
                continue
            try:
                result.add(canonical_problem_key(str(row["problem_id"]), row["platform"]))
            except PlanError:
                continue
        return result

    def _completion_stats(self, tasks: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
        accepted = self._accepted_problem_keys()
        skipped = self._skipped_problem_keys() - accepted
        keys = [canonical_problem_key(task["problem_id"], task["platform"]) for task in tasks]
        accepted_count = sum(key in accepted for key in keys)
        skipped_count = sum(key in skipped for key in keys)
        completed = accepted_count + skipped_count
        return {
            "accepted_count": accepted_count,
            "skipped_count": skipped_count,
            "completed_count": completed,
            "progress": completed / len(keys) if keys else 0.0,
        }

    @staticmethod
    def active_stage_keys(plan: TrainingPlan, completed: set[str]) -> set[str]:
        """Resolve progressive unlocks without mutating completion state."""
        if plan.schedule_mode != "progressive":
            return {stage.stage_key for stage in plan.stages}
        active: set[str] = set()
        for stage in plan.stages:
            active.add(stage.stage_key)
            stage_tasks = [task.problem_key for task in stage.tasks]
            if stage_tasks and not all(key in completed for key in stage_tasks):
                break
        return active

    def recommendation_records(
        self, *, plan_ids: Iterable[str] | None = None
    ) -> list[dict[str, Any]]:
        selected = set(plan_ids) if plan_ids is not None else None
        accepted = self._accepted_problem_keys()
        skipped = self._skipped_problem_keys() - accepted
        records: list[dict[str, Any]] = []
        for row in self.db.plans(enabled=True):
            if selected is not None and row["plan_id"] not in selected:
                continue
            plan = load_plan_data(json.loads(row["content_json"]))
            records.extend(
                plan_task_records(
                    plan,
                    accepted=accepted,
                    active_stage_keys=self.active_stage_keys(plan, accepted | skipped),
                )
            )
        return records


__all__ = [
    "PlanManager", "RevisionConflict", "DuplicatePlanError", "BuiltinPlanError",
]
