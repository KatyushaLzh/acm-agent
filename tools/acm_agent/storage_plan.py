"""Recommendation and training-plan persistence."""

from __future__ import annotations
import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Mapping
from .storage_common import PlanRevisionConflict, _json, utc_now

class _PlanStorageMixin:
    def record_recommendations(
        self, mode: str, recommendations: Iterable[Mapping[str, Any]], *, generated_at: str | None = None
    ) -> None:
        stamp = generated_at or utc_now()
        for item in recommendations:
            self.connection.execute(
                """INSERT INTO recommendation_runs(generated_at,mode,slot,platform,
                                                     problem_id,score,breakdown_json)
                   VALUES(?,?,?,?,?,?,?)""",
                (
                    stamp, mode, item.get("slot"), item["platform"],
                    str(item["problem_id"]), float(item["score"]),
                    _json(item.get("breakdown") or {}),
                ),
            )

    def recommendation_runs(self, *, limit: int = 100) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM recommendation_runs ORDER BY generated_at DESC,id DESC LIMIT ?",
            (limit,),
        )

    def plans(self, *, enabled: bool | None = None) -> list[sqlite3.Row]:
        """Return plan registry rows without decoding their JSON documents."""
        if enabled is None:
            return self.query(
                "SELECT * FROM plans ORDER BY enabled DESC, updated_at DESC, plan_id"
            )
        return self.query(
            "SELECT * FROM plans WHERE enabled=? ORDER BY updated_at DESC, plan_id",
            (int(enabled),),
        )

    def plan(self, plan_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM plans WHERE plan_id=?", (plan_id,)
        ).fetchone()

    def plan_document(self, plan_id: str) -> dict[str, Any] | None:
        row = self.plan(plan_id)
        return json.loads(row["content_json"]) if row else None

    def plan_stage_rows(self, plan_id: str) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM plan_stages WHERE plan_id=? ORDER BY position", (plan_id,)
        )

    def plan_task_rows(self, plan_id: str | None = None) -> list[sqlite3.Row]:
        if plan_id is None:
            return self.query(
                """SELECT t.* FROM plan_tasks t JOIN plans p ON p.plan_id=t.plan_id
                   WHERE p.enabled=1
                   ORDER BY t.plan_id,t.stage_key,t.position"""
            )
        return self.query(
            """SELECT * FROM plan_tasks WHERE plan_id=?
               ORDER BY stage_key,is_replacement,position""",
            (plan_id,),
        )

    def _replace_normalized_plan_rows(
        self, plan_id: str, document: Mapping[str, Any]
    ) -> None:
        self.connection.execute("DELETE FROM plan_tasks WHERE plan_id=?", (plan_id,))
        self.connection.execute("DELETE FROM plan_stages WHERE plan_id=?", (plan_id,))
        for stage_position, stage in enumerate(document.get("stages", [])):
            self.connection.execute(
                """INSERT INTO plan_stages(plan_id,stage_key,position,topic,kind,
                                             due_date,unlock_at)
                   VALUES(?,?,?,?,?,?,?)""",
                (
                    plan_id,
                    stage["stage_key"],
                    stage_position,
                    stage.get("topic", ""),
                    stage.get("kind", stage.get("type", "practice")),
                    stage.get("due_date"),
                    stage.get("unlock_at"),
                ),
            )
            task_position = 0
            for task in stage.get("tasks", []):
                self.connection.execute(
                    """INSERT INTO plan_tasks(
                           plan_id,task_key,stage_key,position,platform,problem_id,url,
                           name,title,level,tags_json,is_replacement,replacement_json)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,0,NULL)""",
                    (
                        plan_id,
                        task["task_key"],
                        stage["stage_key"],
                        task_position,
                        task["platform"],
                        task["problem_id"],
                        task["url"],
                        task.get("name", task["problem_id"]),
                        task.get("title", ""),
                        task.get("level", task.get("difficulty", "B")),
                        _json(task.get("tags") or []),
                    ),
                )
                task_position += 1
            for replacement in stage.get("replacements", []):
                task = replacement["task"]
                metadata = {
                    "condition": replacement["condition"],
                    "replace_task_keys": replacement.get("replace_task_keys", []),
                    "replace_only_accepted": bool(
                        replacement.get("replace_only_accepted", False)
                    ),
                }
                self.connection.execute(
                    """INSERT INTO plan_tasks(
                           plan_id,task_key,stage_key,position,platform,problem_id,url,
                           name,title,level,tags_json,is_replacement,replacement_json)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,1,?)""",
                    (
                        plan_id,
                        task["task_key"],
                        stage["stage_key"],
                        task_position,
                        task["platform"],
                        task["problem_id"],
                        task["url"],
                        task.get("name", task["problem_id"]),
                        task.get("title", ""),
                        task.get("level", task.get("difficulty", "B")),
                        _json(task.get("tags") or []),
                        _json(metadata),
                    ),
                )
                task_position += 1

    def save_plan(
        self,
        document: Mapping[str, Any],
        *,
        enabled: bool = True,
        source: str = "managed",
        builtin_path: str | Path | None = None,
        managed_path: str | Path | None = None,
        expected_revision: int | None = None,
    ) -> int:
        """Atomically persist a canonical v2 plan and its normalized index.

        ``expected_revision`` implements optimistic concurrency.  For creation,
        callers may pass ``None`` or ``0``; updating an existing plan requires
        its exact current revision when the argument is provided.
        """
        plan_id = str(document["plan_id"])
        existing = self.plan(plan_id)
        actual = int(existing["revision"]) if existing else None
        if existing is None:
            if expected_revision not in (None, 0):
                raise PlanRevisionConflict(plan_id, expected_revision, actual)
            revision = 1
        else:
            if expected_revision is not None and int(expected_revision) != actual:
                raise PlanRevisionConflict(plan_id, expected_revision, actual)
            revision = actual + 1
        if source not in {"builtin", "managed"}:
            raise ValueError("plan source must be 'builtin' or 'managed'")
        now = utc_now()
        content_json = _json(document)
        with self.atomic():
            if existing is None:
                self.connection.execute(
                    """INSERT INTO plans(
                           plan_id,title,description,schedule_mode,enabled,source,
                           builtin_path,managed_path,revision,content_json,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        plan_id,
                        str(document.get("title", "")),
                        str(document.get("description", "")),
                        str(document["schedule_mode"]),
                        int(enabled),
                        source,
                        str(builtin_path) if builtin_path else None,
                        str(managed_path) if managed_path else None,
                        revision,
                        content_json,
                        now,
                        now,
                    ),
                )
            else:
                self.connection.execute(
                    """UPDATE plans SET title=?,description=?,schedule_mode=?,enabled=?,
                           source=?,builtin_path=?,managed_path=?,revision=?,content_json=?,
                           updated_at=? WHERE plan_id=?""",
                    (
                        str(document.get("title", "")),
                        str(document.get("description", "")),
                        str(document["schedule_mode"]),
                        int(enabled),
                        source,
                        str(builtin_path) if builtin_path else None,
                        str(managed_path) if managed_path else None,
                        revision,
                        content_json,
                        now,
                        plan_id,
                    ),
                )
            self._replace_normalized_plan_rows(plan_id, document)
            self.connection.execute(
                """INSERT INTO plan_revisions(plan_id,revision,content_json,enabled,created_at)
                   VALUES(?,?,?,?,?)""",
                (plan_id, revision, content_json, int(enabled), now),
            )
            self.connection.execute(
                """DELETE FROM plan_revisions
                   WHERE plan_id=? AND revision NOT IN (
                       SELECT revision FROM plan_revisions WHERE plan_id=?
                       ORDER BY revision DESC LIMIT 5
                   )""",
                (plan_id, plan_id),
            )
        return revision

    def set_plan_enabled(
        self, plan_id: str, enabled: bool, *, expected_revision: int | None = None
    ) -> int:
        row = self.plan(plan_id)
        if row is None:
            raise KeyError(f"plan {plan_id!r} not found")
        document = json.loads(row["content_json"])
        return self.save_plan(
            document,
            enabled=enabled,
            source=row["source"],
            builtin_path=row["builtin_path"],
            managed_path=row["managed_path"],
            expected_revision=expected_revision,
        )

    def plan_revisions(self, plan_id: str) -> list[sqlite3.Row]:
        return self.query(
            """SELECT plan_id,revision,enabled,created_at,content_json
               FROM plan_revisions WHERE plan_id=? ORDER BY revision DESC""",
            (plan_id,),
        )

    def plan_revision(self, plan_id: str, revision: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM plan_revisions WHERE plan_id=? AND revision=?",
            (plan_id, revision),
        ).fetchone()

    def delete_plan(self, plan_id: str) -> None:
        cursor = self.connection.execute("DELETE FROM plans WHERE plan_id=?", (plan_id,))
        if cursor.rowcount != 1:
            raise KeyError(f"plan {plan_id!r} not found")
