"""SQLite persistence for the local ACM learning workflow.

The module deliberately keeps SQL and migration handling in one place.  Platform
clients fetch a complete logical update first and then use :meth:`Database.atomic`
to commit it, so a broken/partial network response never erases a good snapshot.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

from .tag_policy import effective_tags, normalize_tags, tag_key

SCHEMA_VERSION = 6


_UNSET = object()


class PlanRevisionConflict(RuntimeError):
    """Raised when optimistic plan revision checking rejects a stale write."""

    def __init__(self, plan_id: str, expected: int | None, actual: int | None):
        self.plan_id = plan_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"plan {plan_id!r} revision conflict: expected {expected}, current {actual}"
        )


class TagOverrideRevisionConflict(RuntimeError):
    """Raised when a cleanup preview targets stale global tag decisions."""

    def __init__(self, expected: int | None, actual: int):
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"tag override revision conflict: expected {expected}, current {actual}"
        )


class ProblemContextConflict(RuntimeError):
    """Raised when a problem statement edit targets stale cached content."""

    def __init__(self, expected: str | None, actual: str | None):
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"problem context conflict: expected hash {expected!r}, current {actual!r}"
        )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


_VERDICT_ALIASES = {
    "OK": "AC",
    "AC": "AC",
    "ACCEPTED": "AC",
    "WRONG_ANSWER": "WA",
    "WA": "WA",
    "TIME_LIMIT_EXCEEDED": "TLE",
    "TLE": "TLE",
    "RUNTIME_ERROR": "RE",
    "RE": "RE",
    "MEMORY_LIMIT_EXCEEDED": "MLE",
    "MLE": "MLE",
    "COMPILATION_ERROR": "CE",
    "COMPILE_ERROR": "CE",
    "CE": "CE",
    "ABANDONED": "ABANDONED",
}


def _normalize_verdict(value: Any) -> str | None:
    """Return a compact, stable judge-result label for UI/API consumers."""
    if value is None:
        return None
    normalized = re.sub(r"[^A-Z0-9]+", "_", str(value).strip().upper()).strip("_")
    if not normalized:
        return None
    return _VERDICT_ALIASES.get(normalized, normalized)


def _timestamp_key(value: Any) -> datetime:
    """Parse ISO timestamps for ordering, normalizing offsets to UTC.

    Snapshots created by older versions can contain missing or malformed dates;
    those remain usable and simply sort behind valid evidence.
    """
    if value is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return datetime.min.replace(tzinfo=timezone.utc)
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return stamp.astimezone(timezone.utc)
    except (TypeError, ValueError, OverflowError):
        return datetime.min.replace(tzinfo=timezone.utc)


MIGRATIONS: dict[int, str] = {
    1: """
    CREATE TABLE accounts (
        platform TEXT PRIMARY KEY,
        identifier TEXT NOT NULL,
        display_name TEXT,
        rating INTEGER,
        target_rating INTEGER,
        validated_at TEXT,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE problems (
        platform TEXT NOT NULL,
        problem_id TEXT NOT NULL,
        name TEXT,
        url TEXT,
        difficulty INTEGER,
        rating INTEGER,
        tags_json TEXT NOT NULL DEFAULT '[]',
        source_json TEXT NOT NULL DEFAULT '{}',
        updated_at TEXT NOT NULL,
        PRIMARY KEY (platform, problem_id)
    );

    CREATE TABLE submissions (
        platform TEXT NOT NULL,
        submission_id TEXT NOT NULL,
        problem_id TEXT NOT NULL,
        verdict TEXT,
        submitted_at TEXT,
        language TEXT,
        raw_json TEXT NOT NULL DEFAULT '{}',
        PRIMARY KEY (platform, submission_id),
        FOREIGN KEY (platform, problem_id)
            REFERENCES problems(platform, problem_id)
    );

    CREATE TABLE local_files (
        path TEXT PRIMARY KEY,
        platform TEXT,
        problem_id TEXT,
        discovered_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        FOREIGN KEY (platform, problem_id)
            REFERENCES problems(platform, problem_id)
    );

    CREATE TABLE attempts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        platform TEXT NOT NULL,
        problem_id TEXT NOT NULL,
        started_at TEXT NOT NULL,
        closed_at TEXT,
        result TEXT,
        minutes INTEGER,
        hint_level INTEGER NOT NULL DEFAULT 0 CHECK (hint_level BETWEEN 0 AND 4),
        failure_mode TEXT,
        notes TEXT,
        review_stage INTEGER NOT NULL DEFAULT 0,
        review_due TEXT,
        active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
        FOREIGN KEY (platform, problem_id)
            REFERENCES problems(platform, problem_id)
    );

    CREATE TABLE sync_state (
        platform TEXT PRIMARY KEY,
        status TEXT NOT NULL,
        last_attempt_at TEXT,
        last_success_at TEXT,
        error TEXT,
        cursor TEXT,
        metadata_json TEXT NOT NULL DEFAULT '{}'
    );

    CREATE TABLE recommendation_runs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        generated_at TEXT NOT NULL,
        mode TEXT NOT NULL,
        slot TEXT,
        platform TEXT NOT NULL,
        problem_id TEXT NOT NULL,
        score REAL NOT NULL,
        breakdown_json TEXT NOT NULL DEFAULT '{}',
        FOREIGN KEY (platform, problem_id)
            REFERENCES problems(platform, problem_id)
    );

    CREATE INDEX submissions_problem_idx
        ON submissions(platform, problem_id, submitted_at DESC);
    CREATE INDEX attempts_problem_idx
        ON attempts(platform, problem_id, started_at DESC);
    CREATE INDEX attempts_review_idx ON attempts(review_due, active);
    CREATE INDEX recommendation_recent_idx
        ON recommendation_runs(generated_at DESC, platform);
    """,
    2: """
    CREATE TABLE plans (
        plan_id TEXT PRIMARY KEY,
        title TEXT NOT NULL,
        description TEXT NOT NULL DEFAULT '',
        schedule_mode TEXT NOT NULL CHECK (schedule_mode IN ('dated', 'progressive')),
        enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
        source TEXT NOT NULL CHECK (source IN ('builtin', 'managed')),
        builtin_path TEXT,
        managed_path TEXT,
        revision INTEGER NOT NULL CHECK (revision >= 1),
        content_json TEXT NOT NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE TABLE plan_stages (
        plan_id TEXT NOT NULL,
        stage_key TEXT NOT NULL,
        position INTEGER NOT NULL CHECK (position >= 0),
        topic TEXT NOT NULL DEFAULT '',
        kind TEXT NOT NULL DEFAULT 'practice',
        due_date TEXT,
        unlock_at TEXT,
        PRIMARY KEY (plan_id, stage_key),
        UNIQUE (plan_id, position),
        FOREIGN KEY (plan_id) REFERENCES plans(plan_id) ON DELETE CASCADE
    );

    CREATE TABLE plan_tasks (
        plan_id TEXT NOT NULL,
        task_key TEXT NOT NULL,
        stage_key TEXT NOT NULL,
        position INTEGER NOT NULL CHECK (position >= 0),
        platform TEXT NOT NULL,
        problem_id TEXT NOT NULL,
        url TEXT NOT NULL,
        level TEXT NOT NULL,
        tags_json TEXT NOT NULL DEFAULT '[]',
        required INTEGER NOT NULL DEFAULT 1 CHECK (required IN (0, 1)),
        due_date TEXT,
        unlock_at TEXT,
        is_replacement INTEGER NOT NULL DEFAULT 0 CHECK (is_replacement IN (0, 1)),
        replacement_json TEXT,
        PRIMARY KEY (plan_id, task_key),
        FOREIGN KEY (plan_id, stage_key)
            REFERENCES plan_stages(plan_id, stage_key) ON DELETE CASCADE
    );

    CREATE TABLE plan_revisions (
        plan_id TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK (revision >= 1),
        content_json TEXT NOT NULL,
        enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
        created_at TEXT NOT NULL,
        PRIMARY KEY (plan_id, revision),
        FOREIGN KEY (plan_id) REFERENCES plans(plan_id) ON DELETE CASCADE
    );

    CREATE INDEX plans_enabled_idx ON plans(enabled, updated_at DESC);
    CREATE INDEX plan_tasks_problem_idx
        ON plan_tasks(platform, problem_id, plan_id, stage_key, position);
    CREATE INDEX plan_revisions_recent_idx
        ON plan_revisions(plan_id, revision DESC);
    """,
    3: """
    DROP INDEX plan_tasks_problem_idx;
    ALTER TABLE plan_tasks RENAME TO plan_tasks_v2;

    CREATE TABLE plan_tasks (
        plan_id TEXT NOT NULL,
        task_key TEXT NOT NULL,
        stage_key TEXT NOT NULL,
        position INTEGER NOT NULL CHECK (position >= 0),
        platform TEXT NOT NULL,
        problem_id TEXT NOT NULL,
        url TEXT NOT NULL,
        name TEXT NOT NULL DEFAULT '',
        title TEXT NOT NULL DEFAULT '',
        level TEXT NOT NULL,
        tags_json TEXT NOT NULL DEFAULT '[]',
        is_replacement INTEGER NOT NULL DEFAULT 0 CHECK (is_replacement IN (0, 1)),
        replacement_json TEXT,
        PRIMARY KEY (plan_id, task_key),
        FOREIGN KEY (plan_id, stage_key)
            REFERENCES plan_stages(plan_id, stage_key) ON DELETE CASCADE
    );

    INSERT INTO plan_tasks(
        plan_id,task_key,stage_key,position,platform,problem_id,url,name,title,
        level,tags_json,is_replacement,replacement_json
    )
    SELECT plan_id,task_key,stage_key,position,platform,problem_id,url,problem_id,'',
           level,tags_json,is_replacement,replacement_json
    FROM plan_tasks_v2;

    DROP TABLE plan_tasks_v2;
    CREATE INDEX plan_tasks_problem_idx
        ON plan_tasks(platform, problem_id, plan_id, stage_key, position);
    """,
    4: """
    CREATE TABLE problem_dispositions (
        platform TEXT NOT NULL,
        problem_id TEXT NOT NULL,
        disposition TEXT NOT NULL CHECK (disposition='skipped_mastered'),
        reason TEXT NOT NULL CHECK (reason='idea_clear_without_editorial'),
        notes TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL CHECK (source IN ('web','cli','agent')),
        context_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (platform, problem_id),
        FOREIGN KEY (platform, problem_id)
            REFERENCES problems(platform, problem_id) ON DELETE CASCADE
    );

    CREATE TABLE problem_disposition_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        platform TEXT NOT NULL,
        problem_id TEXT NOT NULL,
        action TEXT NOT NULL CHECK (action IN ('skip','unskip')),
        disposition TEXT NOT NULL CHECK (disposition='skipped_mastered'),
        reason TEXT NOT NULL CHECK (reason='idea_clear_without_editorial'),
        notes TEXT NOT NULL DEFAULT '',
        source TEXT NOT NULL CHECK (source IN ('web','cli','agent')),
        context_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        FOREIGN KEY (platform, problem_id)
            REFERENCES problems(platform, problem_id) ON DELETE CASCADE
    );

    CREATE INDEX problem_dispositions_updated_idx
        ON problem_dispositions(updated_at DESC, platform, problem_id);
    CREATE INDEX problem_disposition_events_problem_idx
        ON problem_disposition_events(platform, problem_id, created_at DESC, id DESC);
    """,
    5: """
    CREATE TABLE problem_tag_overrides (
        platform TEXT NOT NULL,
        problem_id TEXT NOT NULL,
        tag_key TEXT NOT NULL,
        tag TEXT NOT NULL,
        action TEXT NOT NULL CHECK (action IN ('add', 'suppress')),
        source TEXT NOT NULL,
        reason TEXT,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (platform, problem_id, tag_key),
        FOREIGN KEY (platform, problem_id)
            REFERENCES problems(platform, problem_id)
    );

    CREATE INDEX problem_tag_overrides_problem_idx
        ON problem_tag_overrides(platform, problem_id, action);

    CREATE TABLE tag_override_state (
        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
        revision INTEGER NOT NULL DEFAULT 0,
        updated_at TEXT NOT NULL
    );

    INSERT INTO tag_override_state(singleton, revision, updated_at)
        VALUES(1, 0, CURRENT_TIMESTAMP);

    CREATE TABLE attempt_tag_snapshots (
        attempt_id INTEGER PRIMARY KEY,
        tags_json TEXT NOT NULL DEFAULT '[]',
        source TEXT NOT NULL,
        captured_at TEXT NOT NULL,
        FOREIGN KEY (attempt_id) REFERENCES attempts(id) ON DELETE CASCADE
    );
    """,
    6: """
    CREATE TABLE problem_contexts (
        platform TEXT NOT NULL,
        problem_id TEXT NOT NULL,
        source TEXT NOT NULL CHECK (
            source IN ('codeforces_auto', 'luogu_auto', 'manual')
        ),
        content TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        source_url TEXT,
        fetched_at TEXT,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        PRIMARY KEY (platform, problem_id, source),
        FOREIGN KEY (platform, problem_id)
            REFERENCES problems(platform, problem_id) ON DELETE CASCADE
    );

    CREATE INDEX problem_contexts_resolve_idx
        ON problem_contexts(platform, problem_id, source, updated_at DESC);

    CREATE TABLE ai_conversations (
        id TEXT PRIMARY KEY,
        attempt_id INTEGER,
        platform TEXT NOT NULL,
        problem_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active'
            CHECK (status IN ('active', 'closed')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        closed_at TEXT,
        FOREIGN KEY (attempt_id) REFERENCES attempts(id) ON DELETE CASCADE,
        FOREIGN KEY (platform, problem_id)
            REFERENCES problems(platform, problem_id) ON DELETE CASCADE
    );

    CREATE UNIQUE INDEX ai_conversations_active_attempt_idx
        ON ai_conversations(attempt_id)
        WHERE attempt_id IS NOT NULL AND status='active';
    CREATE INDEX ai_conversations_problem_idx
        ON ai_conversations(platform, problem_id, updated_at DESC);

    CREATE TABLE ai_messages (
        id TEXT PRIMARY KEY,
        conversation_id TEXT NOT NULL,
        role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
        mode TEXT,
        hint_level INTEGER NOT NULL DEFAULT 0
            CHECK (hint_level BETWEEN 0 AND 4),
        content TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'pending'
            CHECK (status IN ('pending', 'streaming', 'complete', 'interrupted', 'error')),
        model TEXT,
        usage_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        completed_at TEXT,
        FOREIGN KEY (conversation_id)
            REFERENCES ai_conversations(id) ON DELETE CASCADE
    );

    CREATE INDEX ai_messages_conversation_idx
        ON ai_messages(conversation_id, created_at, id);

    CREATE TABLE ai_runs (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        model TEXT NOT NULL,
        conversation_id TEXT,
        message_id TEXT,
        request_summary_json TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL,
        finish_reason TEXT,
        usage_json TEXT NOT NULL DEFAULT '{}',
        error_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        completed_at TEXT,
        FOREIGN KEY (conversation_id)
            REFERENCES ai_conversations(id) ON DELETE SET NULL,
        FOREIGN KEY (message_id) REFERENCES ai_messages(id) ON DELETE SET NULL
    );

    CREATE INDEX ai_runs_recent_idx ON ai_runs(created_at DESC, id);
    CREATE INDEX ai_runs_conversation_idx
        ON ai_runs(conversation_id, created_at DESC);

    CREATE TABLE ai_patch_proposals (
        id TEXT PRIMARY KEY,
        run_id TEXT,
        conversation_id TEXT,
        attempt_id INTEGER,
        platform TEXT NOT NULL,
        problem_id TEXT NOT NULL,
        source_path TEXT NOT NULL,
        baseline_hash TEXT NOT NULL,
        candidate_code TEXT NOT NULL,
        diff_text TEXT NOT NULL,
        diagnosis TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT 'preview',
        applied_hash TEXT,
        backup_path TEXT,
        verify_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        applied_at TEXT,
        reverted_at TEXT,
        FOREIGN KEY (run_id) REFERENCES ai_runs(id) ON DELETE SET NULL,
        FOREIGN KEY (conversation_id)
            REFERENCES ai_conversations(id) ON DELETE SET NULL,
        FOREIGN KEY (attempt_id) REFERENCES attempts(id) ON DELETE SET NULL,
        FOREIGN KEY (platform, problem_id)
            REFERENCES problems(platform, problem_id) ON DELETE CASCADE
    );

    CREATE INDEX ai_patch_proposals_problem_idx
        ON ai_patch_proposals(platform, problem_id, created_at DESC);
    CREATE INDEX ai_patch_proposals_attempt_idx
        ON ai_patch_proposals(attempt_id, created_at DESC);
    """,
}


class Database:
    """Small repository-style wrapper around a versioned SQLite database."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Autocommit makes single repository calls durable.  ``atomic`` still
        # provides explicit all-or-nothing batches for platform snapshots.
        self.connection = sqlite3.connect(self.path, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        try:
            self._backup_v4_before_tag_migration()
            self.migrate()
        except Exception:
            self.connection.close()
            raise

    def _backup_v4_before_tag_migration(self) -> None:
        """Create one consistent SQLite backup before the v5 data migration."""

        current = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        if current != 4 or not self.path.exists():
            return
        backup_path = self.path.with_name(f"{self.path.name}.v4.bak")
        if backup_path.exists():
            return
        temporary = backup_path.with_name(f".{backup_path.name}.tmp")
        temporary.unlink(missing_ok=True)
        destination = sqlite3.connect(temporary)
        try:
            self.connection.backup(destination)
            destination.commit()
        finally:
            destination.close()
        try:
            os.replace(temporary, backup_path)
        finally:
            temporary.unlink(missing_ok=True)

    def _backup_v5_before_ai_migration(self) -> None:
        """Create one consistent adjacent backup immediately before schema v6."""

        current = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        if current != 5 or not self.path.exists():
            return
        backup_path = self.path.with_name(f"{self.path.name}.v5.bak")
        if backup_path.exists():
            return
        temporary = backup_path.with_name(f".{backup_path.name}.tmp")
        temporary.unlink(missing_ok=True)
        destination = sqlite3.connect(temporary)
        try:
            self.connection.backup(destination)
            destination.commit()
        finally:
            destination.close()
        try:
            os.replace(temporary, backup_path)
        finally:
            temporary.unlink(missing_ok=True)

    def __enter__(self) -> "Database":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        self.connection.close()

    def migrate(self) -> None:
        current = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"database schema {current} is newer than supported {SCHEMA_VERSION}"
            )
        if current == 5:
            self._backup_v5_before_ai_migration()
        for version in range(current + 1, SCHEMA_VERSION + 1):
            migration = MIGRATIONS.get(version)
            if migration is None:
                raise RuntimeError(f"missing database migration {version}")
            try:
                # ``executescript`` commits any pending transaction before it
                # runs. Put BEGIN in the script itself so DDL and user_version
                # remain one crash-safe migration under autocommit mode.
                self.connection.executescript("BEGIN IMMEDIATE;\n" + migration)
                if version == 5:
                    self._backfill_attempt_tag_snapshots_v5()
                self.connection.execute(f"PRAGMA user_version = {version}")
                self.connection.execute("COMMIT")
            except Exception:
                if self.connection.in_transaction:
                    self.connection.execute("ROLLBACK")
                raise

    def reconcile_interrupted_ai_state(self) -> None:
        """Close call state left in-flight by a previous service process."""

        stamp = utc_now()
        with self.atomic():
            self.connection.execute(
                """UPDATE ai_messages SET status='interrupted',completed_at=?
                   WHERE status IN ('pending','streaming')""",
                (stamp,),
            )
            self.connection.execute(
                """UPDATE ai_runs SET status='interrupted',completed_at=?
                   WHERE status IN ('pending','running')""",
                (stamp,),
            )

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

    @contextmanager
    def atomic(self) -> Iterator[sqlite3.Connection]:
        """Open a transaction, nesting safely if the caller already has one."""
        if self.connection.in_transaction:
            name = f"acm_nested_{id(self):x}"
            self.connection.execute(f"SAVEPOINT {name}")
            try:
                yield self.connection
            except Exception:
                self.connection.execute(f"ROLLBACK TO {name}")
                self.connection.execute(f"RELEASE {name}")
                raise
            else:
                self.connection.execute(f"RELEASE {name}")
            return
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            yield self.connection
        except Exception:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[sqlite3.Row]:
        return list(self.connection.execute(sql, params))

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

    def get_or_create_ai_conversation(
        self,
        conversation_id: str,
        attempt_id: int,
        platform: str,
        problem_id: str,
    ) -> tuple[sqlite3.Row, bool]:
        """Return the active conversation for an attempt or create it.

        ``conversation_id`` is generated by the caller so API/SSE code can
        publish a stable identifier before any model request begins.
        """

        attempt = self.connection.execute(
            "SELECT platform,problem_id,active FROM attempts WHERE id=?",
            (attempt_id,),
        ).fetchone()
        if attempt is None:
            raise KeyError(f"attempt {attempt_id} not found")
        platform = str(platform).strip().lower()
        problem_id = str(problem_id).strip()
        if attempt["platform"] != platform or attempt["problem_id"] != problem_id:
            raise ValueError("conversation problem does not match its attempt")
        if not int(attempt["active"]):
            raise ValueError("cannot create a conversation for a closed attempt")

        existing = self.active_ai_conversation(attempt_id)
        if existing is not None:
            return existing, False
        now = utc_now()
        try:
            self.connection.execute(
                """INSERT INTO ai_conversations(
                       id,attempt_id,platform,problem_id,status,created_at,updated_at)
                   VALUES(?,?,?,?,'active',?,?)""",
                (str(conversation_id), attempt_id, platform, problem_id, now, now),
            )
        except sqlite3.IntegrityError:
            # A concurrent creator may have won the partial unique index.
            existing = self.active_ai_conversation(attempt_id)
            if existing is None:
                raise
            return existing, False
        row = self.ai_conversation(str(conversation_id))
        assert row is not None
        return row, True

    def ai_conversation(self, conversation_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM ai_conversations WHERE id=?", (str(conversation_id),)
        ).fetchone()

    def active_ai_conversation(self, attempt_id: int) -> sqlite3.Row | None:
        return self.connection.execute(
            """SELECT * FROM ai_conversations
               WHERE attempt_id=? AND status='active' LIMIT 1""",
            (attempt_id,),
        ).fetchone()

    def close_ai_conversation(
        self, conversation_id: str, *, closed_at: str | None = None
    ) -> bool:
        stamp = closed_at or utc_now()
        cursor = self.connection.execute(
            """UPDATE ai_conversations
               SET status='closed',closed_at=?,updated_at=?
               WHERE id=? AND status='active'""",
            (stamp, stamp, str(conversation_id)),
        )
        return bool(cursor.rowcount)

    def create_ai_message(
        self,
        message_id: str,
        conversation_id: str,
        *,
        role: str,
        content: str = "",
        mode: str | None = None,
        hint_level: int = 0,
        status: str = "pending",
        model: str | None = None,
        usage: Mapping[str, Any] | None = None,
        created_at: str | None = None,
    ) -> sqlite3.Row:
        stamp = created_at or utc_now()
        self.connection.execute(
            """INSERT INTO ai_messages(
                   id,conversation_id,role,mode,hint_level,content,status,model,
                   usage_json,created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                str(message_id),
                str(conversation_id),
                role,
                mode,
                int(hint_level),
                content,
                status,
                model,
                _json(usage or {}),
                stamp,
            ),
        )
        self.connection.execute(
            "UPDATE ai_conversations SET updated_at=? WHERE id=?",
            (stamp, str(conversation_id)),
        )
        row = self.ai_message(str(message_id))
        assert row is not None
        return row

    def update_ai_message(
        self,
        message_id: str,
        *,
        content: str | object = _UNSET,
        status: str | object = _UNSET,
        model: str | None | object = _UNSET,
        usage: Mapping[str, Any] | object = _UNSET,
        completed_at: str | None | object = _UNSET,
    ) -> sqlite3.Row:
        assignments: list[str] = []
        values: list[Any] = []
        for column, value in (
            ("content", content),
            ("status", status),
            ("model", model),
            ("completed_at", completed_at),
        ):
            if value is not _UNSET:
                assignments.append(f"{column}=?")
                values.append(value)
        if usage is not _UNSET:
            assignments.append("usage_json=?")
            values.append(_json(usage))
        if not assignments:
            row = self.ai_message(str(message_id))
            if row is None:
                raise KeyError(f"AI message {message_id!r} not found")
            return row
        values.append(str(message_id))
        cursor = self.connection.execute(
            f"UPDATE ai_messages SET {','.join(assignments)} WHERE id=?", values
        )
        if cursor.rowcount != 1:
            raise KeyError(f"AI message {message_id!r} not found")
        row = self.ai_message(str(message_id))
        assert row is not None
        self.connection.execute(
            "UPDATE ai_conversations SET updated_at=? WHERE id=?",
            (utc_now(), row["conversation_id"]),
        )
        return row

    def ai_message(self, message_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM ai_messages WHERE id=?", (str(message_id),)
        ).fetchone()

    def ai_messages(
        self, conversation_id: str, *, limit: int | None = 24
    ) -> list[sqlite3.Row]:
        if limit is None:
            return self.query(
                """SELECT * FROM ai_messages WHERE conversation_id=?
                   ORDER BY created_at,rowid""",
                (str(conversation_id),),
            )
        return self.query(
            """SELECT * FROM ai_messages
               WHERE rowid IN (
                   SELECT rowid FROM ai_messages WHERE conversation_id=?
                   ORDER BY created_at DESC,rowid DESC LIMIT ?
               ) ORDER BY created_at,rowid""",
            (str(conversation_id), max(0, int(limit))),
        )

    def max_ai_hint_level(self, attempt_id: int) -> int:
        row = self.connection.execute(
            """SELECT COALESCE(MAX(m.hint_level),0) AS hint_level
               FROM ai_messages m
               JOIN ai_conversations c ON c.id=m.conversation_id
               WHERE c.attempt_id=? AND m.role='assistant'
                 AND m.status IN ('streaming','complete','interrupted')
                """,
            (attempt_id,),
        ).fetchone()
        return int(row["hint_level"] if row else 0)

    def create_ai_run(
        self,
        run_id: str,
        *,
        kind: str,
        model: str,
        request_summary: Mapping[str, Any] | None = None,
        status: str = "pending",
        conversation_id: str | None = None,
        message_id: str | None = None,
        created_at: str | None = None,
    ) -> sqlite3.Row:
        self.connection.execute(
            """INSERT INTO ai_runs(
                   id,kind,model,conversation_id,message_id,request_summary_json,
                   status,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (
                str(run_id),
                kind,
                model,
                conversation_id,
                message_id,
                _json(request_summary or {}),
                status,
                created_at or utc_now(),
            ),
        )
        row = self.ai_run(str(run_id))
        assert row is not None
        return row

    def update_ai_run(
        self,
        run_id: str,
        *,
        status: str | object = _UNSET,
        finish_reason: str | None | object = _UNSET,
        usage: Mapping[str, Any] | object = _UNSET,
        error: Mapping[str, Any] | object = _UNSET,
        completed_at: str | None | object = _UNSET,
    ) -> sqlite3.Row:
        assignments: list[str] = []
        values: list[Any] = []
        for column, value in (
            ("status", status),
            ("finish_reason", finish_reason),
            ("completed_at", completed_at),
        ):
            if value is not _UNSET:
                assignments.append(f"{column}=?")
                values.append(value)
        if usage is not _UNSET:
            assignments.append("usage_json=?")
            values.append(_json(usage))
        if error is not _UNSET:
            assignments.append("error_json=?")
            values.append(_json(error))
        if not assignments:
            row = self.ai_run(str(run_id))
            if row is None:
                raise KeyError(f"AI run {run_id!r} not found")
            return row
        values.append(str(run_id))
        cursor = self.connection.execute(
            f"UPDATE ai_runs SET {','.join(assignments)} WHERE id=?", values
        )
        if cursor.rowcount != 1:
            raise KeyError(f"AI run {run_id!r} not found")
        row = self.ai_run(str(run_id))
        assert row is not None
        return row

    def ai_run(self, run_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM ai_runs WHERE id=?", (str(run_id),)
        ).fetchone()

    def create_ai_patch_proposal(
        self,
        proposal_id: str,
        *,
        platform: str,
        problem_id: str,
        source_path: str | Path,
        baseline_hash: str,
        candidate_code: str,
        diff_text: str,
        diagnosis: str = "",
        run_id: str | None = None,
        conversation_id: str | None = None,
        attempt_id: int | None = None,
        created_at: str | None = None,
    ) -> sqlite3.Row:
        self.upsert_problem({"platform": platform, "problem_id": problem_id})
        stamp = created_at or utc_now()
        self.connection.execute(
            """INSERT INTO ai_patch_proposals(
                   id,run_id,conversation_id,attempt_id,platform,problem_id,
                   source_path,baseline_hash,candidate_code,diff_text,diagnosis,
                   status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,'preview',?,?)""",
            (
                str(proposal_id),
                run_id,
                conversation_id,
                attempt_id,
                platform,
                str(problem_id),
                str(source_path),
                baseline_hash,
                candidate_code,
                diff_text,
                diagnosis,
                stamp,
                stamp,
            ),
        )
        row = self.ai_patch_proposal(str(proposal_id))
        assert row is not None
        return row

    def ai_patch_proposal(self, proposal_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM ai_patch_proposals WHERE id=?", (str(proposal_id),)
        ).fetchone()

    def update_ai_patch_proposal(
        self,
        proposal_id: str,
        *,
        status: str | object = _UNSET,
        applied_hash: str | None | object = _UNSET,
        backup_path: str | Path | None | object = _UNSET,
        verify: Mapping[str, Any] | object = _UNSET,
        applied_at: str | None | object = _UNSET,
        reverted_at: str | None | object = _UNSET,
    ) -> sqlite3.Row:
        assignments = ["updated_at=?"]
        values: list[Any] = [utc_now()]
        for column, value in (
            ("status", status),
            ("applied_hash", applied_hash),
            ("backup_path", backup_path),
            ("applied_at", applied_at),
            ("reverted_at", reverted_at),
        ):
            if value is not _UNSET:
                assignments.append(f"{column}=?")
                values.append(
                    str(value)
                    if column == "backup_path" and value is not None
                    else value
                )
        if verify is not _UNSET:
            assignments.append("verify_json=?")
            values.append(_json(verify))
        values.append(str(proposal_id))
        cursor = self.connection.execute(
            f"UPDATE ai_patch_proposals SET {','.join(assignments)} WHERE id=?", values
        )
        if cursor.rowcount != 1:
            raise KeyError(f"AI patch proposal {proposal_id!r} not found")
        row = self.ai_patch_proposal(str(proposal_id))
        assert row is not None
        return row

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


def open_database(root: str | Path) -> Database:
    return Database(Path(root) / ".acm" / "state.db")


# A semantic alias for callers that prefer repository terminology.
Store = Database
