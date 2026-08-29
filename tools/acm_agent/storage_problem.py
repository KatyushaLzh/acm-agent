"""Problem, account, sync, attempt, and disposition persistence."""

from __future__ import annotations
import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from .storage_common import (
    ProblemContextConflict,
    TagOverrideRevisionConflict,
    _UNSET,
    _json,
    _normalize_verdict,
    _sha256_text,
    _timestamp_key,
    utc_now,
)
from .tag_policy import effective_tags, normalize_tags, tag_key

class _ProblemStorageMixin:
    def _backfill_attempt_tag_snapshots_v5(self) -> None:
        """Freeze the best current effective tags for pre-v5 closed attempts."""

        has_attempts = self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='attempts'"
        ).fetchone()
        if not has_attempts:
            return
        rows = self.query("SELECT id,platform,problem_id,closed_at FROM attempts WHERE closed_at IS NOT NULL")
        for row in rows:
            tags = self.effective_problem_tags(row["platform"], row["problem_id"])
            self.connection.execute(
                """INSERT OR IGNORE INTO attempt_tag_snapshots(
                       attempt_id,tags_json,source,captured_at)
                   VALUES(?,?,?,?)""",
                (row["id"], _json(tags), "migration_v5", utc_now()),
            )

    def upsert_account(
        self,
        platform: str,
        identifier: str,
        *,
        display_name: str | None = None,
        rating: int | None = None,
        target_rating: int | None = None,
        validated_at: str | None = None,
    ) -> None:
        now = utc_now()
        self.connection.execute(
            """
            INSERT INTO accounts(platform, identifier, display_name, rating,
                                 target_rating, validated_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(platform) DO UPDATE SET
                identifier=excluded.identifier,
                display_name=COALESCE(excluded.display_name, accounts.display_name),
                rating=COALESCE(excluded.rating, accounts.rating),
                target_rating=COALESCE(excluded.target_rating, accounts.target_rating),
                validated_at=COALESCE(excluded.validated_at, accounts.validated_at),
                updated_at=excluded.updated_at
            """,
            (platform, identifier, display_name, rating, target_rating, validated_at, now),
        )

    def account(self, platform: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM accounts WHERE platform=?", (platform,)
        ).fetchone()

    def upsert_problem(self, problem: Mapping[str, Any]) -> None:
        self.connection.execute(
            """
            INSERT INTO problems(platform, problem_id, name, url, difficulty,
                                 rating, tags_json, source_json, updated_at)
            VALUES(:platform, :problem_id, :name, :url, :difficulty,
                   :rating, :tags_json, :source_json, :updated_at)
            ON CONFLICT(platform, problem_id) DO UPDATE SET
                name=COALESCE(excluded.name, problems.name),
                url=COALESCE(excluded.url, problems.url),
                difficulty=COALESCE(excluded.difficulty, problems.difficulty),
                rating=COALESCE(excluded.rating, problems.rating),
                tags_json=CASE WHEN excluded.tags_json='[]'
                               THEN problems.tags_json ELSE excluded.tags_json END,
                source_json=CASE WHEN excluded.source_json='{}'
                                 THEN problems.source_json ELSE excluded.source_json END,
                updated_at=excluded.updated_at
            """,
            {
                "platform": problem["platform"],
                "problem_id": str(problem["problem_id"]),
                "name": problem.get("name"),
                "url": problem.get("url"),
                "difficulty": problem.get("difficulty"),
                "rating": problem.get("rating"),
                "tags_json": _json(problem.get("tags") or []),
                "source_json": _json(problem.get("source") or {}),
                "updated_at": problem.get("updated_at") or utc_now(),
            },
        )

    def upsert_problems(self, problems: Iterable[Mapping[str, Any]]) -> None:
        for problem in problems:
            self.upsert_problem(problem)

    def upsert_submission(self, submission: Mapping[str, Any]) -> None:
        self.connection.execute(
            """
            INSERT INTO submissions(platform, submission_id, problem_id, verdict,
                                    submitted_at, language, raw_json)
            VALUES(:platform, :submission_id, :problem_id, :verdict,
                   :submitted_at, :language, :raw_json)
            ON CONFLICT(platform, submission_id) DO UPDATE SET
                problem_id=excluded.problem_id,
                verdict=excluded.verdict,
                submitted_at=excluded.submitted_at,
                language=excluded.language,
                raw_json=excluded.raw_json
            """,
            {
                "platform": submission["platform"],
                "submission_id": str(submission["submission_id"]),
                "problem_id": str(submission["problem_id"]),
                "verdict": submission.get("verdict"),
                "submitted_at": submission.get("submitted_at"),
                "language": submission.get("language"),
                "raw_json": _json(submission.get("raw") or {}),
            },
        )

    def known_submission_ids(self, platform: str) -> set[str]:
        return {
            str(row[0])
            for row in self.connection.execute(
                "SELECT submission_id FROM submissions WHERE platform=?", (platform,)
            )
        }

    def record_sync_attempt(
        self,
        platform: str,
        *,
        status: str,
        error: str | None = None,
        cursor: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        success: bool = False,
        attempted_at: str | None = None,
    ) -> None:
        now = attempted_at or utc_now()
        old = self.connection.execute(
            "SELECT * FROM sync_state WHERE platform=?", (platform,)
        ).fetchone()
        last_success = now if success else (old["last_success_at"] if old else None)
        if metadata is None and old:
            metadata_json = old["metadata_json"]
        else:
            metadata_json = _json(metadata or {})
        self.connection.execute(
            """
            INSERT INTO sync_state(platform, status, last_attempt_at, last_success_at,
                                   error, cursor, metadata_json)
            VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(platform) DO UPDATE SET
                status=excluded.status,
                last_attempt_at=excluded.last_attempt_at,
                last_success_at=excluded.last_success_at,
                error=excluded.error,
                cursor=COALESCE(excluded.cursor, sync_state.cursor),
                metadata_json=excluded.metadata_json
            """,
            (platform, status, now, last_success, error, cursor, metadata_json),
        )

    def sync_state(self, platform: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM sync_state WHERE platform=?", (platform,)
        ).fetchone()

    def sync_states(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM sync_state ORDER BY platform")

    def problems(self, platform: str | None = None) -> list[sqlite3.Row]:
        if platform is None:
            return self.query("SELECT * FROM problems ORDER BY platform, problem_id")
        return self.query(
            "SELECT * FROM problems WHERE platform=? ORDER BY problem_id", (platform,)
        )

    def save_problem_context(
        self,
        platform: str,
        problem_id: str,
        content: str,
        *,
        source: str,
        source_url: str | None = None,
        fetched_at: str | None = None,
        metadata: Mapping[str, Any] | None = None,
        expected_hash: str | None | object = _UNSET,
    ) -> sqlite3.Row:
        """Save one statement source without letting auto data replace manual data.

        Automatic and manual statements occupy separate rows.  Resolution via
        :meth:`problem_context` always prefers the manual row, while refreshes
        may continue updating the platform cache underneath it.
        """

        platform = str(platform).strip().lower()
        problem_id = str(problem_id).strip()
        source = str(source).strip().lower()
        allowed = {"manual", "codeforces_auto", "luogu_auto"}
        if source not in allowed:
            raise ValueError(f"unsupported problem context source {source!r}")
        if source != "manual" and source != f"{platform}_auto":
            raise ValueError(f"context source {source!r} does not match {platform!r}")
        if not isinstance(content, str) or not content.strip():
            raise ValueError("problem context content must be non-empty text")

        current = self.problem_context(platform, problem_id)
        actual_hash = str(current["content_hash"]) if current else None
        if expected_hash is not _UNSET and expected_hash != actual_hash:
            raise ProblemContextConflict(
                None if expected_hash is None else str(expected_hash), actual_hash
            )

        self.upsert_problem({"platform": platform, "problem_id": problem_id})
        now = utc_now()
        content_hash = _sha256_text(content)
        self.connection.execute(
            """INSERT INTO problem_contexts(
                   platform,problem_id,source,content,content_hash,source_url,
                   fetched_at,metadata_json,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(platform,problem_id,source) DO UPDATE SET
                   content=excluded.content,
                   content_hash=excluded.content_hash,
                   source_url=excluded.source_url,
                   fetched_at=excluded.fetched_at,
                   metadata_json=excluded.metadata_json,
                   updated_at=excluded.updated_at""",
            (
                platform,
                problem_id,
                source,
                content,
                content_hash,
                source_url,
                fetched_at or (now if source != "manual" else None),
                _json(metadata or {}),
                now,
                now,
            ),
        )
        row = self.connection.execute(
            """SELECT * FROM problem_contexts
               WHERE platform=? AND problem_id=? AND source=?""",
            (platform, problem_id, source),
        ).fetchone()
        assert row is not None
        return row

    def problem_context(
        self, platform: str, problem_id: str
    ) -> sqlite3.Row | None:
        """Return the effective statement, preferring a manual override."""

        return self.connection.execute(
            """SELECT * FROM problem_contexts
               WHERE platform=? AND problem_id=?
               ORDER BY CASE source WHEN 'manual' THEN 0 ELSE 1 END,
                        updated_at DESC
               LIMIT 1""",
            (str(platform).strip().lower(), str(problem_id).strip()),
        ).fetchone()

    def problem_context_rows(
        self, platform: str, problem_id: str
    ) -> list[sqlite3.Row]:
        return self.query(
            """SELECT * FROM problem_contexts
               WHERE platform=? AND problem_id=?
               ORDER BY CASE source WHEN 'manual' THEN 0 ELSE 1 END,
                        updated_at DESC""",
            (str(platform).strip().lower(), str(problem_id).strip()),
        )

    def delete_manual_problem_context(
        self,
        platform: str,
        problem_id: str,
        *,
        expected_hash: str | None | object = _UNSET,
    ) -> bool:
        """Remove a manual override, revealing the latest automatic context."""

        row = self.connection.execute(
            """SELECT content_hash FROM problem_contexts
               WHERE platform=? AND problem_id=? AND source='manual'""",
            (str(platform).strip().lower(), str(problem_id).strip()),
        ).fetchone()
        actual_hash = str(row["content_hash"]) if row else None
        if expected_hash is not _UNSET and expected_hash != actual_hash:
            raise ProblemContextConflict(
                None if expected_hash is None else str(expected_hash), actual_hash
            )
        cursor = self.connection.execute(
            """DELETE FROM problem_contexts
               WHERE platform=? AND problem_id=? AND source='manual'""",
            (str(platform).strip().lower(), str(problem_id).strip()),
        )
        return bool(cursor.rowcount)

    @staticmethod
    def _problem_id_variants(platform: str, problem_id: str) -> tuple[str, ...]:
        value = str(problem_id).upper()
        if platform == "codeforces":
            bare = value[2:] if value.startswith("CF") else value
            return bare, f"CF{bare}"
        return (value,)

    def problem_base_tags(self, platform: str, problem_id: str) -> list[str]:
        """Merge raw platform tags with every current plan occurrence."""

        variants = self._problem_id_variants(platform, problem_id)
        row = self.connection.execute(
            "SELECT tags_json FROM problems WHERE platform=? AND problem_id=?",
            (platform, variants[0]),
        ).fetchone()
        values: list[str] = []
        if row:
            try:
                values.extend(json.loads(row["tags_json"] or "[]"))
            except (TypeError, json.JSONDecodeError):
                pass
        placeholders = ",".join("?" for _ in variants)
        plan_rows = self.query(
            f"SELECT tags_json FROM plan_tasks WHERE platform=? AND UPPER(problem_id) IN ({placeholders})",
            (platform, *variants),
        )
        for plan_row in plan_rows:
            try:
                values.extend(json.loads(plan_row["tags_json"] or "[]"))
            except (TypeError, json.JSONDecodeError):
                continue
        return normalize_tags(values)

    def problem_tag_overrides(self, platform: str, problem_id: str) -> list[sqlite3.Row]:
        variants = self._problem_id_variants(platform, problem_id)
        return self.query(
            """SELECT * FROM problem_tag_overrides
               WHERE platform=? AND problem_id=?
               ORDER BY updated_at,tag_key""",
            (platform, variants[0]),
        )

    def effective_problem_tags(self, platform: str, problem_id: str) -> list[str]:
        return effective_tags(
            self.problem_base_tags(platform, problem_id),
            self.problem_tag_overrides(platform, problem_id),
        )

    def tag_override_revision(self) -> int:
        row = self.connection.execute(
            "SELECT revision FROM tag_override_state WHERE singleton=1"
        ).fetchone()
        return int(row["revision"] if row else 0)

    def require_tag_override_revision(self, expected: int | None) -> int:
        actual = self.tag_override_revision()
        if expected is not None and int(expected) != actual:
            raise TagOverrideRevisionConflict(int(expected), actual)
        return actual

    def replace_problem_tag_overrides(
        self,
        platform: str,
        problem_id: str,
        *,
        additions: Sequence[str] = (),
        suppressions: Sequence[str] = (),
        source: str = "user",
        reason: str | None = None,
    ) -> bool:
        """Replace every explicit decision for one problem without bumping revision."""

        variants = self._problem_id_variants(platform, problem_id)
        normalized_add = normalize_tags(list(additions))
        normalized_suppress = normalize_tags(list(suppressions))
        desired = {
            tag_key(tag): (tag, "add") for tag in normalized_add
        }
        desired.update(
            {tag_key(tag): (tag, "suppress") for tag in normalized_suppress}
        )
        before = {
            row["tag_key"]: (row["tag"], row["action"])
            for row in self.problem_tag_overrides(platform, variants[0])
        }
        if before == desired:
            return False
        self.connection.execute(
            "DELETE FROM problem_tag_overrides WHERE platform=? AND problem_id=?",
            (platform, variants[0]),
        )
        now = utc_now()
        for key, (tag, action) in desired.items():
            self.connection.execute(
                """INSERT INTO problem_tag_overrides(
                       platform,problem_id,tag_key,tag,action,source,reason,updated_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (platform, variants[0], key, tag, action, source, reason, now),
            )
        return True

    def bump_tag_override_revision(self, expected: int | None) -> int:
        actual = self.require_tag_override_revision(expected)
        revision = actual + 1
        self.connection.execute(
            """UPDATE tag_override_state SET revision=?,updated_at=?
               WHERE singleton=1 AND revision=?""",
            (revision, utc_now(), actual),
        )
        return revision

    def submissions(
        self, platform: str | None = None, problem_id: str | None = None
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        params: list[Any] = []
        if platform is not None:
            clauses.append("platform=?")
            params.append(platform)
        if problem_id is not None:
            clauses.append("problem_id=?")
            params.append(problem_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return self.query(
            f"SELECT * FROM submissions{where} ORDER BY submitted_at DESC, submission_id DESC",
            params,
        )

    def upsert_local_file(
        self, path: str | Path, platform: str | None, problem_id: str | None
    ) -> None:
        now = utc_now()
        self.connection.execute(
            """INSERT INTO local_files(path,platform,problem_id,discovered_at,updated_at)
               VALUES(?,?,?,?,?)
               ON CONFLICT(path) DO UPDATE SET platform=excluded.platform,
                 problem_id=excluded.problem_id,updated_at=excluded.updated_at""",
            (str(path), platform, problem_id, now, now),
        )

    def local_files(self) -> list[sqlite3.Row]:
        return self.query("SELECT * FROM local_files ORDER BY path")

    def start_attempt(
        self,
        platform: str,
        problem_id: str,
        *,
        started_at: str | None = None,
    ) -> int:
        self.upsert_problem({"platform": platform, "problem_id": problem_id})
        cursor = self.connection.execute(
            """INSERT INTO attempts(platform,problem_id,started_at,active)
               VALUES(?,?,?,1)""",
            (platform, problem_id, started_at or utc_now()),
        )
        return int(cursor.lastrowid)

    def close_attempt(
        self,
        attempt_id: int,
        *,
        result: str,
        minutes: int | None = None,
        hint_level: int = 0,
        failure_mode: str | None = None,
        notes: str | None = None,
        review_stage: int = 0,
        review_due: str | None = None,
        closed_at: str | None = None,
    ) -> None:
        cursor = self.connection.execute(
            """UPDATE attempts SET closed_at=?,result=?,minutes=?,hint_level=?,
                   failure_mode=?,notes=?,review_stage=?,review_due=?,active=0
               WHERE id=? AND active=1""",
            (
                closed_at or utc_now(), result, minutes, hint_level, failure_mode,
                notes, review_stage, review_due, attempt_id,
            ),
        )
        if cursor.rowcount != 1:
            raise KeyError(f"active attempt {attempt_id} not found")

    def save_attempt_tag_snapshot(
        self,
        attempt_id: int,
        tags: Sequence[str],
        *,
        source: str = "close",
        captured_at: str | None = None,
    ) -> None:
        self.connection.execute(
            """INSERT INTO attempt_tag_snapshots(attempt_id,tags_json,source,captured_at)
               VALUES(?,?,?,?)
               ON CONFLICT(attempt_id) DO UPDATE SET
                 tags_json=excluded.tags_json,
                 source=excluded.source,
                 captured_at=excluded.captured_at""",
            (attempt_id, _json(normalize_tags(list(tags))), source, captured_at or utc_now()),
        )

    def attempt_tag_snapshot(self, attempt_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM attempt_tag_snapshots WHERE attempt_id=?", (attempt_id,)
        ).fetchone()

    def attempts(
        self, platform: str | None = None, problem_id: str | None = None
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        params: list[Any] = []
        if platform is not None:
            clauses.append("platform=?")
            params.append(platform)
        if problem_id is not None:
            clauses.append("problem_id=?")
            params.append(problem_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return self.query(
            f"SELECT * FROM attempts{where} ORDER BY started_at DESC, id DESC", params
        )

    def review_queue_entries(self) -> list[sqlite3.Row]:
        """Return every scheduled review in deterministic due-date order."""

        return self.query(
            """SELECT queue.*,problem.name,problem.name AS title,problem.url,
                      problem.difficulty,problem.rating,problem.tags_json
                 FROM review_queue AS queue
                 LEFT JOIN problems AS problem
                   ON problem.platform=queue.platform
                  AND problem.problem_id=queue.problem_id
                ORDER BY queue.review_due,queue.platform,queue.problem_id"""
        )

    def review_queue_entry(
        self, platform: str, problem_id: str
    ) -> sqlite3.Row | None:
        return self.connection.execute(
            """SELECT * FROM review_queue
               WHERE platform=? AND problem_id=?""",
            (str(platform).strip().lower(), str(problem_id).strip()),
        ).fetchone()

    def upsert_review_queue(
        self,
        platform: str,
        problem_id: str,
        *,
        review_due: str,
        queue_type: str,
        review_stage: int = 0,
        created_at: str | None = None,
        updated_at: str | None = None,
    ) -> sqlite3.Row:
        """Create or replace one review schedule without changing its reset cutoff."""

        platform = str(platform).strip().lower()
        problem_id = str(problem_id).strip()
        now = updated_at or utc_now()
        self.connection.execute(
            """INSERT INTO review_queue(
                   platform,problem_id,review_due,queue_type,review_stage,
                   created_at,updated_at)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(platform,problem_id) DO UPDATE SET
                   review_due=excluded.review_due,
                   queue_type=excluded.queue_type,
                   review_stage=excluded.review_stage,
                   updated_at=excluded.updated_at""",
            (
                platform,
                problem_id,
                str(review_due),
                str(queue_type),
                int(review_stage),
                created_at or now,
                now,
            ),
        )
        row = self.review_queue_entry(platform, problem_id)
        assert row is not None
        return row

    def review_reset_at(self, platform: str, problem_id: str) -> str | None:
        row = self.connection.execute(
            """SELECT reset_at FROM review_queue_resets
               WHERE platform=? AND problem_id=?""",
            (str(platform).strip().lower(), str(problem_id).strip()),
        ).fetchone()
        return str(row["reset_at"]) if row else None

    def review_reset_attempt_id(self, platform: str, problem_id: str) -> int:
        row = self.connection.execute(
            """SELECT reset_attempt_id FROM review_queue_resets
               WHERE platform=? AND problem_id=?""",
            (str(platform).strip().lower(), str(problem_id).strip()),
        ).fetchone()
        return int(row["reset_attempt_id"]) if row else 0

    def set_review_reset(
        self,
        platform: str,
        problem_id: str,
        *,
        reset_at: str | None = None,
        reset_attempt_id: int | None = None,
    ) -> str:
        platform = str(platform).strip().lower()
        problem_id = str(problem_id).strip()
        value = reset_at or utc_now()
        if reset_attempt_id is None:
            row = self.connection.execute(
                """SELECT COALESCE(MAX(id),0) AS latest_id FROM attempts
                   WHERE platform=? AND problem_id=?""",
                (platform, problem_id),
            ).fetchone()
            reset_attempt_id = int(row["latest_id"])
        self.connection.execute(
            """INSERT INTO review_queue_resets(
                   platform,problem_id,reset_at,reset_attempt_id)
               VALUES(?,?,?,?)
               ON CONFLICT(platform,problem_id) DO UPDATE SET
                   reset_at=excluded.reset_at,
                   reset_attempt_id=excluded.reset_attempt_id""",
            (platform, problem_id, value, int(reset_attempt_id)),
        )
        return value

    def remove_review_queue(
        self,
        platform: str,
        problem_id: str,
        *,
        reset_at: str | None = None,
        reset_attempt_id: int | None = None,
    ) -> bool:
        """Remove one live schedule and atomically advance its evidence cutoff."""

        platform = str(platform).strip().lower()
        problem_id = str(problem_id).strip()
        with self.atomic():
            cursor = self.connection.execute(
                "DELETE FROM review_queue WHERE platform=? AND problem_id=?",
                (platform, problem_id),
            )
            if cursor.rowcount:
                self.set_review_reset(
                    platform,
                    problem_id,
                    reset_at=reset_at,
                    reset_attempt_id=reset_attempt_id,
                )
        return bool(cursor.rowcount)

    def clear_review_queue(self, *, reset_at: str | None = None) -> int:
        """Clear all schedules and atomically reset their evidence baselines."""

        value = reset_at or utc_now()
        with self.atomic():
            entries = self.query("SELECT platform,problem_id FROM review_queue")
            for entry in entries:
                self.set_review_reset(
                    entry["platform"], entry["problem_id"], reset_at=value
                )
            self.connection.execute("DELETE FROM review_queue")
        return len(entries)

    def skip_problem(
        self,
        platform: str,
        problem_id: str,
        *,
        notes: str = "",
        source: str = "agent",
        context: Mapping[str, Any] | None = None,
    ) -> None:
        self.upsert_problem({"platform": platform, "problem_id": problem_id})
        now = utc_now()
        with self.atomic():
            self.connection.execute(
                """INSERT INTO problem_dispositions(
                       platform,problem_id,disposition,reason,notes,source,context_json,
                       created_at,updated_at)
                   VALUES(?,?,'skipped_mastered','idea_clear_without_editorial',?,?,?,?,?)
                   ON CONFLICT(platform,problem_id) DO UPDATE SET
                       disposition='skipped_mastered',reason='idea_clear_without_editorial',
                       notes=excluded.notes,source=excluded.source,
                       context_json=excluded.context_json,
                       updated_at=excluded.updated_at""",
                (platform, problem_id, notes, source, _json(context or {}), now, now),
            )
            self.connection.execute(
                """INSERT INTO problem_disposition_events(
                       platform,problem_id,action,disposition,reason,notes,source,
                       context_json,created_at)
                   VALUES(?,?,'skip','skipped_mastered','idea_clear_without_editorial',?,?,?,?)""",
                (platform, problem_id, notes, source, _json(context or {}), now),
            )

    def unskip_problem(
        self,
        platform: str,
        problem_id: str,
        *,
        notes: str = "",
        source: str = "agent",
        context: Mapping[str, Any] | None = None,
    ) -> bool:
        now = utc_now()
        with self.atomic():
            cursor = self.connection.execute(
                "DELETE FROM problem_dispositions WHERE platform=? AND problem_id=?",
                (platform, problem_id),
            )
            if cursor.rowcount:
                self.connection.execute(
                    """INSERT INTO problem_disposition_events(
                           platform,problem_id,action,disposition,reason,notes,source,
                           context_json,created_at)
                       VALUES(?,?,'unskip','skipped_mastered','idea_clear_without_editorial',?,?,?,?)""",
                    (platform, problem_id, notes, source, _json(context or {}), now),
                )
        return bool(cursor.rowcount)

    def problem_disposition(self, platform: str, problem_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM problem_dispositions WHERE platform=? AND problem_id=?",
            (platform, problem_id),
        ).fetchone()

    def problem_dispositions(self) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM problem_dispositions ORDER BY updated_at DESC,platform,problem_id"
        )

    def problem_disposition_events(
        self, platform: str | None = None, problem_id: str | None = None
    ) -> list[sqlite3.Row]:
        if platform is None:
            return self.query(
                "SELECT * FROM problem_disposition_events ORDER BY created_at DESC,id DESC"
            )
        return self.query(
            """SELECT * FROM problem_disposition_events
               WHERE platform=? AND (? IS NULL OR problem_id=?)
               ORDER BY created_at DESC,id DESC""",
            (platform, problem_id, problem_id),
        )

    def problem_runtime_status(self, platform: str, problem_id: str) -> dict[str, Any]:
        """Aggregate judge and local workflow evidence for one problem.

        Platform AC is permanent and authoritative.  In the absence of AC,
        Codeforces verdicts and locally closed attempts compete by actual
        timestamp (not ISO string ordering); Luogu's anonymous snapshot only
        contributes AC evidence.
        """
        platform = str(platform).strip().lower()
        problem_id = str(problem_id).strip()
        submissions = self.submissions(platform, problem_id)
        attempts = self.attempts(platform, problem_id)
        problem = self.connection.execute(
            "SELECT updated_at FROM problems WHERE platform=? AND problem_id=?",
            (platform, problem_id),
        ).fetchone()
        fallback_updated_at = problem["updated_at"] if problem else None

        platform_ac = [
            row for row in submissions if _normalize_verdict(row["verdict"]) == "AC"
        ]
        local_ac = [
            row
            for row in attempts
            if not row["active"] and _normalize_verdict(row["result"]) == "AC"
        ]
        if platform_ac or local_ac:
            # Prefer authoritative platform evidence whenever it exists.  A
            # missing platform timestamp falls back to the problem snapshot.
            if platform_ac:
                evidence = max(
                    platform_ac,
                    key=lambda row: _timestamp_key(row["submitted_at"]),
                )
                updated_at = evidence["submitted_at"] or fallback_updated_at
                source = "platform"
            else:
                evidence = max(local_ac, key=lambda row: _timestamp_key(row["closed_at"]))
                updated_at = evidence["closed_at"] or evidence["started_at"]
                source = "local"
            return {
                "judge_result": "AC",
                "workflow_status": "accepted",
                "skipped": False,
                "evidence_source": source,
                "updated_at": updated_at,
            }

        status = self.problem_status(platform, problem_id)
        if platform == "luogu" and status == "attempted":
            # Anonymous Luogu snapshots cannot establish a failed attempt.
            # Rebuild workflow state from trusted local evidence only.
            if attempts:
                status = "attempted"
            else:
                local = self.connection.execute(
                    "SELECT 1 FROM local_files WHERE platform=? AND problem_id=? LIMIT 1",
                    (platform, problem_id),
                ).fetchone()
                status = "local_only" if local else ("not_started" if problem else "unknown")
        skipped = status == "skipped"

        # A Luogu public sync has no reliable negative evidence, so only local
        # closed attempts are considered for its non-AC judge result.
        verdict_evidence: list[tuple[datetime, int, str, str | None, str]] = []
        if platform == "codeforces":
            for row in submissions:
                verdict = _normalize_verdict(row["verdict"])
                if verdict and verdict != "AC":
                    verdict_evidence.append(
                        (
                            _timestamp_key(row["submitted_at"]),
                            1,
                            verdict,
                            row["submitted_at"],
                            "platform",
                        )
                    )
        for row in attempts:
            if row["active"]:
                continue
            verdict = _normalize_verdict(row["result"])
            if verdict and verdict != "AC":
                stamp = row["closed_at"] or row["started_at"]
                verdict_evidence.append(
                    (_timestamp_key(stamp), 0, verdict, stamp, "local")
                )

        selected = max(verdict_evidence, default=None)
        active = next((row for row in attempts if row["active"]), None)
        if skipped:
            workflow_status = "skipped"
        elif active is not None:
            workflow_status = "active"
        elif status in {"attempted", "local_only", "not_started", "unknown"}:
            workflow_status = status
        else:
            workflow_status = "unknown"

        if selected is not None:
            _, _, judge_result, updated_at, source = selected
        else:
            judge_result = None
            updated_at = None
            source = "none"
            if active is not None:
                updated_at = active["started_at"]
                source = "local"
            elif skipped:
                disposition = self.problem_disposition(platform, problem_id)
                if disposition:
                    updated_at = disposition["updated_at"]
                    source = "local"
            elif status == "local_only":
                local = self.connection.execute(
                    """SELECT updated_at FROM local_files
                       WHERE platform=? AND problem_id=?
                       ORDER BY updated_at DESC LIMIT 1""",
                    (platform, problem_id),
                ).fetchone()
                if local:
                    updated_at = local["updated_at"]
                    source = "local"

        return {
            "judge_result": judge_result,
            "workflow_status": workflow_status,
            "skipped": skipped,
            "evidence_source": source,
            "updated_at": updated_at,
        }

    def problem_status(self, platform: str, problem_id: str) -> str:
        accepted = self.connection.execute(
            """SELECT 1 FROM submissions
               WHERE platform=? AND problem_id=? AND verdict IN ('OK', 'AC', 'Accepted')
               LIMIT 1""",
            (platform, problem_id),
        ).fetchone()
        if accepted:
            return "accepted"
        manually_accepted = self.connection.execute(
            """SELECT 1 FROM attempts
               WHERE platform=? AND problem_id=? AND UPPER(result) IN ('OK','AC','ACCEPTED')
               LIMIT 1""",
            (platform, problem_id),
        ).fetchone()
        if manually_accepted:
            return "accepted"
        skipped = self.problem_disposition(platform, problem_id)
        if skipped:
            return "skipped"
        attempted = self.connection.execute(
            """SELECT 1 FROM submissions WHERE platform=? AND problem_id=?
               UNION ALL
               SELECT 1 FROM attempts WHERE platform=? AND problem_id=? LIMIT 1""",
            (platform, problem_id, platform, problem_id),
        ).fetchone()
        if attempted:
            return "attempted"
        local = self.connection.execute(
            "SELECT 1 FROM local_files WHERE platform=? AND problem_id=? LIMIT 1",
            (platform, problem_id),
        ).fetchone()
        if local:
            return "local_only"
        exists = self.connection.execute(
            "SELECT 1 FROM problems WHERE platform=? AND problem_id=?",
            (platform, problem_id),
        ).fetchone()
        return "not_started" if exists else "unknown"
