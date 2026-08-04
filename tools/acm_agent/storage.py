"""SQLite persistence for the local ACM learning workflow.

The module deliberately keeps SQL and migration handling in one place.  Platform
clients fetch a complete logical update first and then use :meth:`Database.atomic`
to commit it, so a broken/partial network response never erases a good snapshot.
"""

from __future__ import annotations

import json
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence


SCHEMA_VERSION = 4


class PlanRevisionConflict(RuntimeError):
    """Raised when optimistic plan revision checking rejects a stale write."""

    def __init__(self, plan_id: str, expected: int | None, actual: int | None):
        self.plan_id = plan_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"plan {plan_id!r} revision conflict: expected {expected}, current {actual}"
        )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


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
        self.migrate()

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
        for version in range(current + 1, SCHEMA_VERSION + 1):
            migration = MIGRATIONS.get(version)
            if migration is None:
                raise RuntimeError(f"missing database migration {version}")
            with self.connection:
                self.connection.executescript(migration)
                self.connection.execute(f"PRAGMA user_version = {version}")

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
