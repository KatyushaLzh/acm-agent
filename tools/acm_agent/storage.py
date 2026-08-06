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

SCHEMA_VERSION = 12


_UNSET = object()


def _process_is_alive(pid: int | None) -> bool:
    if not pid or int(pid) <= 0:
        return False
    if int(pid) == os.getpid():
        return True
    if os.name == "nt":
        try:
            import ctypes

            handle = ctypes.windll.kernel32.OpenProcess(0x100000, False, int(pid))
            if not handle:
                return False
            try:
                code = ctypes.c_ulong()
                return bool(ctypes.windll.kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
            finally:
                ctypes.windll.kernel32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


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


class MarkdownSummaryTargetRevisionConflict(RuntimeError):
    """Raised when a saved Markdown target is edited from a stale revision."""

    def __init__(self, target_id: str, expected: int | None, actual: int | None):
        self.target_id = target_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"markdown summary target {target_id!r} revision conflict: "
            f"expected {expected}, current {actual}"
        )


class MarkdownSummaryProposalRevisionConflict(RuntimeError):
    """Raised when an archive action targets a stale proposal preview."""

    def __init__(self, proposal_id: str, expected: int | None, actual: int | None):
        self.proposal_id = proposal_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"markdown summary proposal {proposal_id!r} revision conflict: "
            f"expected {expected}, current {actual}"
        )


class StressArtifactBundleRevisionConflict(RuntimeError):
    """Raised when a helper bundle is updated from a stale revision."""

    def __init__(self, bundle_id: str, expected: int | None, actual: int | None):
        self.bundle_id = bundle_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"stress artifact bundle {bundle_id!r} revision conflict: "
            f"expected {expected}, current {actual}"
        )


class StressRunRevisionConflict(RuntimeError):
    """Raised when a stress runner writes state from a stale revision."""

    def __init__(self, run_id: str, expected: int | None, actual: int | None):
        self.run_id = run_id
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"stress run {run_id!r} revision conflict: "
            f"expected {expected}, current {actual}"
        )


class StressSetupSlotConflict(RuntimeError):
    """Raised when another AI stress preparation already owns the global slot."""

    code = "stress_setup_active"

    def __init__(self, run_id: str, active_run_id: str | None):
        self.run_id = str(run_id)
        self.active_run_id = (
            str(active_run_id) if active_run_id is not None else None
        )
        self.details = {"active_run_id": self.active_run_id}
        active = self.active_run_id or "unknown"
        super().__init__(
            f"stress setup {self.run_id!r} conflicts with active run {active!r}"
        )


class StressPreparationCacheConflict(RuntimeError):
    """Raised when a content-addressed cache key maps to different payloads."""

    code = "stress_preparation_cache_conflict"

    def __init__(self, cache_key: str):
        self.cache_key = str(cache_key)
        self.details = {"cache_key": self.cache_key}
        super().__init__(
            f"stress preparation cache key {self.cache_key!r} already has "
            "a different payload"
        )


class StressArtifactCandidateConflict(RuntimeError):
    """Raised when an immutable candidate id maps to different content."""

    code = "stress_artifact_candidate_conflict"

    def __init__(self, candidate_id: str):
        self.candidate_id = str(candidate_id)
        self.details = {"candidate_id": self.candidate_id}
        super().__init__(
            f"stress artifact candidate {self.candidate_id!r} already has "
            "different immutable content"
        )


class StressArtifactProofConflict(RuntimeError):
    """Raised when an immutable proof key maps to different content."""

    code = "stress_artifact_proof_conflict"

    def __init__(self, proof_key: str):
        self.proof_key = str(proof_key)
        self.details = {"proof_key": self.proof_key}
        super().__init__(
            f"stress artifact proof {self.proof_key!r} already has "
            "different immutable content"
        )


class StressBundleCertificationConflict(RuntimeError):
    """Raised when an immutable certification key maps to different content."""

    code = "stress_bundle_certification_conflict"

    def __init__(self, certification_key: str):
        self.certification_key = str(certification_key)
        self.details = {"certification_key": self.certification_key}
        super().__init__(
            f"stress bundle certification {self.certification_key!r} already has "
            "different immutable content"
        )


class StressCacheAliasRevisionConflict(RuntimeError):
    """Raised when publishing a stress cache alias from a stale revision."""

    code = "stress_cache_alias_revision_conflict"

    def __init__(self, alias_key: str, expected: int | None, actual: int | None):
        self.alias_key = str(alias_key)
        self.expected = expected
        self.actual = actual
        self.details = {
            "alias_key": self.alias_key,
            "expected_revision": expected,
            "actual_revision": actual,
        }
        super().__init__(
            f"stress cache alias {self.alias_key!r} revision conflict: "
            f"expected {expected}, current {actual}"
        )


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _json_object(value: Any) -> dict[str, Any]:
    """Return a mutable JSON object, treating legacy/corrupt values as empty."""

    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    if value is None:
        return {}
    try:
        decoded = json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    if not isinstance(decoded, dict):
        return {}
    return {str(key): item for key, item in decoded.items()}


def _merge_json_objects(
    current: Mapping[str, Any] | str | None,
    updates: Mapping[str, Any],
) -> dict[str, Any]:
    """Recursively merge object fields while replacing arrays and scalars."""

    merged = _json_object(current)
    for key, value in updates.items():
        name = str(key)
        previous = merged.get(name)
        if isinstance(previous, Mapping) and isinstance(value, Mapping):
            merged[name] = _merge_json_objects(previous, value)
        else:
            merged[name] = value
    return merged


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
    7: """
    ALTER TABLE ai_conversations ADD COLUMN closed_reason TEXT
        CHECK (closed_reason IN ('user_cleared', 'attempt_closed', 'legacy'));
    ALTER TABLE ai_conversations ADD COLUMN superseded_by TEXT;

    UPDATE ai_conversations SET closed_reason='legacy'
        WHERE status='closed' AND closed_reason IS NULL;

    CREATE INDEX ai_conversations_attempt_summary_idx
        ON ai_conversations(attempt_id, closed_reason, updated_at DESC);

    CREATE TABLE markdown_summary_targets (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        path TEXT NOT NULL COLLATE NOCASE UNIQUE,
        preset TEXT NOT NULL,
        schema_json TEXT NOT NULL,
        schema_hash TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
        enabled INTEGER NOT NULL DEFAULT 1 CHECK (enabled IN (0, 1)),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE INDEX markdown_summary_targets_enabled_idx
        ON markdown_summary_targets(enabled, updated_at DESC, id);

    CREATE TABLE markdown_summary_proposals (
        id TEXT PRIMARY KEY,
        attempt_id INTEGER NOT NULL,
        run_id TEXT,
        target_id TEXT,
        target_revision INTEGER,
        target_path TEXT NOT NULL,
        target_existed INTEGER NOT NULL CHECK (target_existed IN (0, 1)),
        baseline_hash TEXT,
        schema_json TEXT NOT NULL,
        schema_hash TEXT NOT NULL,
        entry_json TEXT NOT NULL,
        entry_markdown TEXT NOT NULL DEFAULT '',
        candidate_bytes BLOB NOT NULL,
        candidate_hash TEXT NOT NULL,
        diff_text TEXT NOT NULL DEFAULT '',
        confidence REAL,
        warnings_json TEXT NOT NULL DEFAULT '[]',
        duplicate_json TEXT NOT NULL DEFAULT '{}',
        rationale TEXT NOT NULL DEFAULT '',
        revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
        status TEXT NOT NULL DEFAULT 'preview' CHECK (
            status IN (
                'preview', 'applying', 'applied', 'conflict', 'failed',
                'reverting', 'reverted'
            )
        ),
        backup_path TEXT,
        applied_hash TEXT,
        error_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        applied_at TEXT,
        reverted_at TEXT,
        FOREIGN KEY (attempt_id) REFERENCES attempts(id) ON DELETE CASCADE,
        FOREIGN KEY (run_id) REFERENCES ai_runs(id) ON DELETE SET NULL,
        FOREIGN KEY (target_id)
            REFERENCES markdown_summary_targets(id) ON DELETE SET NULL
    );

    CREATE INDEX markdown_summary_proposals_attempt_idx
        ON markdown_summary_proposals(attempt_id, created_at DESC, id);
    CREATE INDEX markdown_summary_proposals_target_idx
        ON markdown_summary_proposals(target_id, created_at DESC, id);
    CREATE INDEX markdown_summary_proposals_status_idx
        ON markdown_summary_proposals(status, updated_at DESC, id);
    """,
    8: """
    CREATE TABLE stress_artifact_bundles (
        id TEXT PRIMARY KEY,
        attempt_id INTEGER,
        platform TEXT NOT NULL,
        problem_id TEXT NOT NULL,
        contract_json TEXT NOT NULL DEFAULT '{}',
        baseline_manifest_json TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'staging' CHECK (
            status IN (
                'staging', 'ready', 'applying', 'applied', 'conflict',
                'failed', 'reverting', 'reverted'
            )
        ),
        backup_path TEXT,
        revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
        error_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        applied_at TEXT,
        reverted_at TEXT,
        FOREIGN KEY (attempt_id) REFERENCES attempts(id) ON DELETE SET NULL,
        FOREIGN KEY (platform, problem_id)
            REFERENCES problems(platform, problem_id) ON DELETE CASCADE
    );

    CREATE INDEX stress_artifact_bundles_problem_idx
        ON stress_artifact_bundles(platform, problem_id, created_at DESC, id);
    CREATE INDEX stress_artifact_bundles_status_idx
        ON stress_artifact_bundles(status, updated_at DESC, id);

    CREATE TABLE stress_artifacts (
        id TEXT PRIMARY KEY,
        bundle_id TEXT NOT NULL,
        ai_run_id TEXT,
        kind TEXT NOT NULL CHECK (kind IN ('generator', 'brute', 'reference')),
        source_code TEXT NOT NULL,
        source_hash TEXT NOT NULL,
        target_path TEXT NOT NULL,
        baseline_hash TEXT,
        source_kind TEXT NOT NULL CHECK (
            source_kind IN (
                'ai_generated', 'codeforces_editorial', 'luogu_solution',
                'cnblogs', 'csdn', 'local_existing'
            )
        ),
        source_url TEXT,
        source_title TEXT,
        source_license TEXT,
        source_content_hash TEXT,
        status TEXT NOT NULL DEFAULT 'staged' CHECK (
            status IN (
                'staged', 'compiled', 'validated', 'applied', 'failed',
                'reverted'
            )
        ),
        validation_json TEXT NOT NULL DEFAULT '{}',
        metadata_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (bundle_id, kind),
        FOREIGN KEY (bundle_id)
            REFERENCES stress_artifact_bundles(id) ON DELETE CASCADE,
        FOREIGN KEY (ai_run_id) REFERENCES ai_runs(id) ON DELETE SET NULL
    );

    CREATE INDEX stress_artifacts_bundle_idx
        ON stress_artifacts(bundle_id, kind);
    CREATE INDEX stress_artifacts_ai_run_idx
        ON stress_artifacts(ai_run_id, created_at DESC);

    CREATE TABLE stress_runs (
        id TEXT PRIMARY KEY,
        bundle_id TEXT NOT NULL,
        attempt_id INTEGER,
        platform TEXT NOT NULL,
        problem_id TEXT NOT NULL,
        user_source_path TEXT NOT NULL,
        user_source_hash TEXT NOT NULL,
        owner_pid INTEGER NOT NULL,
        config_json TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'pending' CHECK (
            status IN (
                'pending', 'preparing', 'running', 'stop_requested',
                'stopped', 'mismatch', 'oracle_conflict', 'fault',
                'interrupted', 'completed'
            )
        ),
        phase TEXT NOT NULL DEFAULT 'preparing',
        start_seed INTEGER NOT NULL DEFAULT 0 CHECK (start_seed >= 0),
        current_seed INTEGER NOT NULL DEFAULT 0 CHECK (current_seed >= 0),
        next_seed INTEGER NOT NULL DEFAULT 0 CHECK (next_seed >= 0),
        small_count INTEGER NOT NULL DEFAULT 0 CHECK (small_count >= 0),
        medium_count INTEGER NOT NULL DEFAULT 0 CHECK (medium_count >= 0),
        total_count INTEGER NOT NULL DEFAULT 0 CHECK (total_count >= 0),
        mismatch_seed INTEGER,
        failure_path TEXT,
        stop_reason TEXT,
        error_json TEXT NOT NULL DEFAULT '{}',
        revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        started_at TEXT,
        completed_at TEXT,
        FOREIGN KEY (bundle_id)
            REFERENCES stress_artifact_bundles(id) ON DELETE RESTRICT,
        FOREIGN KEY (attempt_id) REFERENCES attempts(id) ON DELETE SET NULL,
        FOREIGN KEY (platform, problem_id)
            REFERENCES problems(platform, problem_id) ON DELETE CASCADE
    );

    CREATE INDEX stress_runs_problem_idx
        ON stress_runs(platform, problem_id, created_at DESC, id);
    CREATE INDEX stress_runs_status_idx
        ON stress_runs(status, updated_at DESC, id);
    CREATE UNIQUE INDEX stress_runs_single_active_idx
        ON stress_runs((1))
        WHERE status IN ('pending', 'preparing', 'running', 'stop_requested');
    """,
    9: """
    ALTER TABLE stress_runs ADD COLUMN large_count INTEGER NOT NULL DEFAULT 0
        CHECK (large_count >= 0);
    """,
    10: """
    CREATE TABLE stress_runs_v10 (
        id TEXT PRIMARY KEY,
        bundle_id TEXT NOT NULL,
        attempt_id INTEGER,
        platform TEXT NOT NULL,
        problem_id TEXT NOT NULL,
        user_source_path TEXT NOT NULL,
        user_source_hash TEXT NOT NULL,
        owner_pid INTEGER NOT NULL,
        config_json TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'pending' CHECK (
            status IN (
                'pending', 'preparing', 'running', 'stop_requested',
                'stopped', 'mismatch', 'oracle_conflict', 'fault',
                'interrupted', 'completed'
            )
        ),
        phase TEXT NOT NULL DEFAULT 'preparing',
        start_seed INTEGER NOT NULL DEFAULT 0 CHECK (start_seed >= 0),
        current_seed INTEGER NOT NULL DEFAULT 0 CHECK (current_seed >= 0),
        next_seed INTEGER NOT NULL DEFAULT 0 CHECK (next_seed >= 0),
        small_count INTEGER NOT NULL DEFAULT 0 CHECK (small_count >= 0),
        large_count INTEGER NOT NULL DEFAULT 0 CHECK (large_count >= 0),
        total_count INTEGER NOT NULL DEFAULT 0 CHECK (total_count >= 0),
        mismatch_seed INTEGER,
        failure_path TEXT,
        stop_reason TEXT,
        error_json TEXT NOT NULL DEFAULT '{}',
        revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        started_at TEXT,
        completed_at TEXT,
        FOREIGN KEY (bundle_id)
            REFERENCES stress_artifact_bundles(id) ON DELETE RESTRICT,
        FOREIGN KEY (attempt_id) REFERENCES attempts(id) ON DELETE SET NULL,
        FOREIGN KEY (platform, problem_id)
            REFERENCES problems(platform, problem_id) ON DELETE CASCADE
    );

    INSERT INTO stress_runs_v10(
        id,bundle_id,attempt_id,platform,problem_id,user_source_path,
        user_source_hash,owner_pid,config_json,status,phase,start_seed,
        current_seed,next_seed,small_count,large_count,total_count,
        mismatch_seed,failure_path,stop_reason,error_json,revision,
        created_at,updated_at,started_at,completed_at)
    SELECT
        id,bundle_id,attempt_id,platform,problem_id,user_source_path,
        user_source_hash,owner_pid,config_json,status,phase,start_seed,
        current_seed,next_seed,small_count,large_count,total_count,
        mismatch_seed,failure_path,stop_reason,error_json,revision,
        created_at,updated_at,started_at,completed_at
    FROM stress_runs;

    DROP TABLE stress_runs;
    ALTER TABLE stress_runs_v10 RENAME TO stress_runs;

    CREATE INDEX stress_runs_problem_idx
        ON stress_runs(platform, problem_id, created_at DESC, id);
    CREATE INDEX stress_runs_status_idx
        ON stress_runs(status, updated_at DESC, id);
    CREATE UNIQUE INDEX stress_runs_single_active_idx
        ON stress_runs((1))
        WHERE status IN ('pending', 'preparing', 'running', 'stop_requested');
    """,
    11: """
    ALTER TABLE ai_runs ADD COLUMN preparation_meta_json TEXT NOT NULL
        DEFAULT '{}';

    CREATE TABLE stress_preparation_cache (
        cache_key TEXT PRIMARY KEY,
        payload_json TEXT NOT NULL,
        metadata_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    ALTER TABLE stress_artifact_bundles
        ADD COLUMN preparation_cache_key TEXT
        REFERENCES stress_preparation_cache(cache_key) ON DELETE SET NULL;
    ALTER TABLE stress_artifact_bundles
        ADD COLUMN preparation_meta_json TEXT NOT NULL DEFAULT '{}';

    CREATE INDEX stress_artifact_bundles_preparation_cache_idx
        ON stress_artifact_bundles(
            preparation_cache_key, created_at DESC, id
        ) WHERE preparation_cache_key IS NOT NULL;

    UPDATE ai_runs
       SET status='interrupted',
           finish_reason=COALESCE(finish_reason, 'schema_v11_slot_recovery'),
           completed_at=COALESCE(completed_at, created_at)
     WHERE kind='stress_setup' AND status='running'
       AND rowid NOT IN (
           SELECT rowid FROM ai_runs
            WHERE kind='stress_setup' AND status='running'
            ORDER BY created_at DESC, rowid DESC LIMIT 1
       );

    CREATE UNIQUE INDEX ai_runs_single_running_stress_setup_idx
        ON ai_runs((1))
        WHERE kind='stress_setup' AND status='running';
    """,
    12: """
    CREATE TABLE problem_samples (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        platform TEXT NOT NULL,
        problem_id TEXT NOT NULL,
        sample_key TEXT NOT NULL,
        input_data BLOB NOT NULL,
        expected_output BLOB NOT NULL,
        content_hash TEXT NOT NULL,
        source TEXT NOT NULL DEFAULT 'problem_context',
        metadata_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        UNIQUE (platform, problem_id, sample_key),
        UNIQUE (platform, problem_id, content_hash),
        FOREIGN KEY (platform, problem_id)
            REFERENCES problems(platform, problem_id) ON DELETE CASCADE
    );

    CREATE INDEX problem_samples_problem_idx
        ON problem_samples(platform, problem_id, id);

    CREATE TABLE stress_artifact_candidates (
        id TEXT PRIMARY KEY,
        generation_key TEXT NOT NULL,
        producer_ai_run_id TEXT,
        platform TEXT NOT NULL,
        problem_id TEXT NOT NULL,
        role TEXT NOT NULL CHECK (
            role IN ('generator', 'validator', 'brute', 'reference')
        ),
        source_code TEXT NOT NULL,
        source_hash TEXT NOT NULL,
        source_kind TEXT NOT NULL CHECK (
            source_kind IN (
                'ai_generated', 'codeforces_editorial', 'luogu_solution',
                'cnblogs', 'csdn', 'local_existing'
            )
        ),
        provenance_json TEXT NOT NULL DEFAULT '{}',
        generation_identity_json TEXT NOT NULL,
        usage_json TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'generated' CHECK (
            status IN ('generated', 'rejected', 'superseded')
        ),
        created_at TEXT NOT NULL,
        UNIQUE (generation_key, source_hash),
        FOREIGN KEY (producer_ai_run_id) REFERENCES ai_runs(id) ON DELETE SET NULL,
        FOREIGN KEY (platform, problem_id)
            REFERENCES problems(platform, problem_id) ON DELETE CASCADE
    );

    CREATE INDEX stress_artifact_candidates_generation_idx
        ON stress_artifact_candidates(
            generation_key, status, created_at DESC, id
        );
    CREATE INDEX stress_artifact_candidates_problem_role_idx
        ON stress_artifact_candidates(
            platform, problem_id, role, created_at DESC, id
        );

    CREATE TABLE stress_artifact_proofs (
        proof_key TEXT PRIMARY KEY,
        candidate_id TEXT NOT NULL,
        proof_kind TEXT NOT NULL,
        certification_identity_json TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('passed', 'failed')),
        result_json TEXT NOT NULL DEFAULT '{}',
        executable_path TEXT,
        executable_hash TEXT,
        created_at TEXT NOT NULL,
        UNIQUE (candidate_id, proof_kind, certification_identity_json),
        FOREIGN KEY (candidate_id)
            REFERENCES stress_artifact_candidates(id) ON DELETE CASCADE
    );

    CREATE INDEX stress_artifact_proofs_candidate_idx
        ON stress_artifact_proofs(candidate_id, status, proof_kind, created_at DESC);

    CREATE TABLE stress_bundle_certifications (
        certification_key TEXT PRIMARY KEY,
        platform TEXT NOT NULL,
        problem_id TEXT NOT NULL,
        generator_candidate_id TEXT NOT NULL,
        brute_candidate_id TEXT NOT NULL,
        reference_candidate_id TEXT NOT NULL,
        certification_identity_json TEXT NOT NULL,
        scope_json TEXT NOT NULL DEFAULT '{}',
        preflight_json TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL CHECK (status IN ('valid', 'invalidated')),
        created_at TEXT NOT NULL,
        last_used_at TEXT,
        FOREIGN KEY (platform, problem_id)
            REFERENCES problems(platform, problem_id) ON DELETE CASCADE,
        FOREIGN KEY (generator_candidate_id)
            REFERENCES stress_artifact_candidates(id) ON DELETE RESTRICT,
        FOREIGN KEY (brute_candidate_id)
            REFERENCES stress_artifact_candidates(id) ON DELETE RESTRICT,
        FOREIGN KEY (reference_candidate_id)
            REFERENCES stress_artifact_candidates(id) ON DELETE RESTRICT
    );

    CREATE INDEX stress_bundle_certifications_problem_idx
        ON stress_bundle_certifications(
            platform, problem_id, status, created_at DESC, certification_key
        );
    CREATE INDEX stress_bundle_certifications_candidates_idx
        ON stress_bundle_certifications(
            generator_candidate_id, brute_candidate_id, reference_candidate_id
        );

    CREATE TABLE stress_cache_aliases (
        alias_key TEXT PRIMARY KEY,
        alias_kind TEXT NOT NULL CHECK (
            alias_kind IN ('contract', 'blueprint', 'artifact', 'certification')
        ),
        target_id TEXT NOT NULL,
        revision INTEGER NOT NULL DEFAULT 1 CHECK (revision >= 1),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE INDEX stress_cache_aliases_target_idx
        ON stress_cache_aliases(alias_kind, target_id);

    ALTER TABLE stress_artifact_bundles
        ADD COLUMN certification_key TEXT
        REFERENCES stress_bundle_certifications(certification_key) ON DELETE SET NULL;

    CREATE INDEX stress_artifact_bundles_certification_idx
        ON stress_artifact_bundles(
            certification_key, created_at DESC, id
        ) WHERE certification_key IS NOT NULL;
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

    def _backup_v6_before_summary_migration(self) -> None:
        """Create one consistent adjacent backup immediately before schema v7."""

        current = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        if current != 6 or not self.path.exists():
            return
        backup_path = self.path.with_name(f"{self.path.name}.v6.bak")
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

    def _backup_v7_before_stress_migration(self) -> None:
        """Create one consistent adjacent backup immediately before schema v8."""

        current = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        if current != 7 or not self.path.exists():
            return
        backup_path = self.path.with_name(f"{self.path.name}.v7.bak")
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

    def _backup_v8_before_large_profile_migration(self) -> None:
        """Create one consistent adjacent backup immediately before schema v9."""

        current = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        if current != 8 or not self.path.exists():
            return
        backup_path = self.path.with_name(f"{self.path.name}.v8.bak")
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

    def _backup_v9_before_medium_removal_migration(self) -> None:
        """Create one consistent adjacent backup immediately before schema v10."""

        current = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        if current != 9 or not self.path.exists():
            return
        backup_path = self.path.with_name(f"{self.path.name}.v9.bak")
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

    def _backup_v10_before_preparation_cache_migration(self) -> None:
        """Create one consistent adjacent backup immediately before schema v11."""

        current = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        if current != 10 or not self.path.exists():
            return
        backup_path = self.path.with_name(f"{self.path.name}.v10.bak")
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

    def _backup_v11_before_proof_cache_migration(self) -> None:
        """Create one consistent adjacent backup immediately before schema v12."""

        current = int(self.connection.execute("PRAGMA user_version").fetchone()[0])
        if current != 11 or not self.path.exists():
            return
        backup_path = self.path.with_name(f"{self.path.name}.v11.bak")
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
        if current == 6:
            self._backup_v6_before_summary_migration()
        if current == 7:
            self._backup_v7_before_stress_migration()
        if current == 8:
            self._backup_v8_before_large_profile_migration()
        if current == 9:
            self._backup_v9_before_medium_removal_migration()
        if current == 10:
            self._backup_v10_before_preparation_cache_migration()
        if current == 11:
            self._backup_v11_before_proof_cache_migration()
        for version in range(current + 1, SCHEMA_VERSION + 1):
            if version == 7 and current != 0:
                # A database may enter this process at v5 and reach v6 in the
                # preceding loop iteration.  Take the v6 snapshot immediately
                # before applying v7 in either case.
                self._backup_v6_before_summary_migration()
            if version == 8 and current != 0:
                # As above, a v5/v6 database can reach v7 inside this loop.
                # Snapshot that exact v7 state before adding stress storage.
                self._backup_v7_before_stress_migration()
            if version == 9 and current != 0:
                # Databases starting before v8 reach the complete legacy stress
                # schema in this loop. Preserve that exact state before adding
                # the v2 large-profile counter.
                self._backup_v8_before_large_profile_migration()
            if version == 10 and current != 0:
                # Preserve the complete v9 state before atomically rebuilding
                # stress_runs without the retired medium counter.
                self._backup_v9_before_medium_removal_migration()
            if version == 11 and current != 0:
                # Preserve the complete v10 stress state before adding the
                # preparation cache and the global setup ownership constraint.
                self._backup_v10_before_preparation_cache_migration()
            if version == 12 and current != 0:
                # Preserve the complete v11 preparation cache before adding
                # immutable role candidates, proofs, certifications, aliases,
                # and persisted problem samples.
                self._backup_v11_before_proof_cache_migration()
            migration = MIGRATIONS.get(version)
            if migration is None:
                raise RuntimeError(f"missing database migration {version}")
            problems_table_exists = self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='problems'"
            ).fetchone()
            incomplete_legacy_v10 = version == 10 and problems_table_exists is None
            if incomplete_legacy_v10:
                # Some pre-plan test/preview databases only carried a version
                # marker and unrelated tables.  SQLite resolves every foreign
                # key target while rebuilding stress_runs, so temporarily
                # disable enforcement for this already-incomplete legacy shape.
                # Normal ACM databases retain enforcement throughout v10.
                self.connection.execute("PRAGMA foreign_keys = OFF")
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
            finally:
                if incomplete_legacy_v10:
                    self.connection.execute("PRAGMA foreign_keys = ON")

    def reconcile_interrupted_ai_state(self) -> None:
        """Close AI and stress state left in-flight by a previous process."""

        stamp = utc_now()
        with self.atomic():
            self.connection.execute(
                """UPDATE ai_messages SET status='interrupted',completed_at=?
                   WHERE status IN ('pending','streaming')""",
                (stamp,),
            )
            self.connection.execute(
                """UPDATE ai_runs SET status='interrupted',completed_at=?
                   WHERE status IN ('pending','running')
                     AND kind<>'stress_setup'""",
                (stamp,),
            )
            setup_rows = self.query(
                """SELECT id,preparation_meta_json FROM ai_runs
                   WHERE kind='stress_setup' AND status='running'"""
            )
            for row in setup_rows:
                try:
                    metadata = json.loads(str(row["preparation_meta_json"] or "{}"))
                except (TypeError, ValueError, json.JSONDecodeError):
                    metadata = {}
                owner_pid = metadata.get("owner_pid") if isinstance(metadata, Mapping) else None
                if _process_is_alive(owner_pid):
                    continue
                self.connection.execute(
                    """UPDATE ai_runs SET status='interrupted',completed_at=?
                       WHERE id=? AND status='running'""",
                    (stamp, str(row["id"])),
                )
            self._reconcile_interrupted_stress_state(stamp)

    def reconcile_interrupted_stress_state(self) -> int:
        """Pause active stress runs so restart requires an explicit resume."""

        with self.atomic():
            return self._reconcile_interrupted_stress_state(utc_now())

    def _reconcile_interrupted_stress_state(self, stamp: str) -> int:
        rows = self.query(
            """SELECT id,owner_pid FROM stress_runs
               WHERE status IN ('pending','preparing','running','stop_requested')"""
        )
        stale_ids = [str(row["id"]) for row in rows if not _process_is_alive(row["owner_pid"])]
        for run_id in stale_ids:
            self.connection.execute(
                """UPDATE stress_runs
                   SET status='interrupted',stop_reason='service_restart',
                       updated_at=?,completed_at=?,revision=revision+1
                   WHERE id=?""",
                (stamp, stamp, run_id),
            )
        return len(stale_ids)

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
        self,
        conversation_id: str,
        *,
        closed_at: str | None = None,
        closed_reason: str = "attempt_closed",
        superseded_by: str | None = None,
    ) -> bool:
        if closed_reason not in {"user_cleared", "attempt_closed", "legacy"}:
            raise ValueError("invalid AI conversation closed reason")
        stamp = closed_at or utc_now()
        cursor = self.connection.execute(
            """UPDATE ai_conversations
               SET status='closed',closed_at=?,updated_at=?,closed_reason=?,superseded_by=?
               WHERE id=? AND status='active'""",
            (
                stamp,
                stamp,
                closed_reason,
                str(superseded_by) if superseded_by is not None else None,
                str(conversation_id),
            ),
        )
        return bool(cursor.rowcount)

    def link_ai_conversation_replacement(
        self, conversation_id: str, superseded_by: str
    ) -> sqlite3.Row:
        """Link an archived conversation after its replacement has been created."""

        cursor = self.connection.execute(
            """UPDATE ai_conversations SET superseded_by=?,updated_at=?
               WHERE id=? AND status='closed' AND closed_reason='user_cleared'""",
            (str(superseded_by), utc_now(), str(conversation_id)),
        )
        if cursor.rowcount != 1:
            raise KeyError(f"cleared AI conversation {conversation_id!r} not found")
        row = self.ai_conversation(str(conversation_id))
        assert row is not None
        return row

    def latest_summary_ai_conversation(self, attempt_id: int) -> sqlite3.Row | None:
        """Return the latest conversation eligible for Markdown summarisation.

        A user-cleared conversation remains available for hint/audit queries but
        is deliberately excluded from outbound summary context.
        """

        return self.connection.execute(
            """SELECT * FROM ai_conversations
               WHERE attempt_id=? AND COALESCE(closed_reason,'')!='user_cleared'
               ORDER BY updated_at DESC,rowid DESC LIMIT 1""",
            (int(attempt_id),),
        ).fetchone()

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
        preparation_meta: Mapping[str, Any] | None = None,
        created_at: str | None = None,
    ) -> sqlite3.Row:
        try:
            self.connection.execute(
                """INSERT INTO ai_runs(
                       id,kind,model,conversation_id,message_id,
                       request_summary_json,status,preparation_meta_json,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    str(run_id),
                    kind,
                    model,
                    conversation_id,
                    message_id,
                    _json(request_summary or {}),
                    status,
                    _json(preparation_meta or {}),
                    created_at or utc_now(),
                ),
            )
        except sqlite3.IntegrityError as exc:
            constraint = getattr(exc, "sqlite_errorcode", None)
            if (
                str(kind) == "stress_setup"
                and str(status) == "running"
                and constraint
                in {
                    sqlite3.SQLITE_CONSTRAINT_PRIMARYKEY,
                    sqlite3.SQLITE_CONSTRAINT_UNIQUE,
                }
            ):
                active = self.active_stress_setup_run()
                if active is not None:
                    raise StressSetupSlotConflict(
                        str(run_id), str(active["id"])
                    ) from None
            raise
        row = self.ai_run(str(run_id))
        assert row is not None
        return row

    def active_stress_setup_run(self) -> sqlite3.Row | None:
        """Return the single SQLite-owned stress setup slot, if any."""

        return self.connection.execute(
            """SELECT * FROM ai_runs
               WHERE kind='stress_setup' AND status='running'
               ORDER BY created_at DESC,rowid DESC LIMIT 1"""
        ).fetchone()

    def acquire_stress_setup_slot(
        self,
        run_id: str,
        *,
        model: str,
        request_summary: Mapping[str, Any] | None = None,
        preparation_meta: Mapping[str, Any] | None = None,
        conversation_id: str | None = None,
        message_id: str | None = None,
        created_at: str | None = None,
    ) -> sqlite3.Row:
        """Atomically acquire the global preparation slot before external work."""

        return self.create_ai_run(
            str(run_id),
            kind="stress_setup",
            model=str(model),
            request_summary=request_summary,
            status="running",
            conversation_id=conversation_id,
            message_id=message_id,
            preparation_meta=preparation_meta,
            created_at=created_at,
        )

    def update_ai_run(
        self,
        run_id: str,
        *,
        status: str | object = _UNSET,
        finish_reason: str | None | object = _UNSET,
        usage: Mapping[str, Any] | object = _UNSET,
        error: Mapping[str, Any] | object = _UNSET,
        preparation_meta: Mapping[str, Any] | object = _UNSET,
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
        if preparation_meta is not _UNSET and not isinstance(
            preparation_meta, Mapping
        ):
            raise TypeError("preparation_meta must be a mapping")
        if not assignments:
            if preparation_meta is not _UNSET:
                return self.merge_ai_run_preparation_meta(
                    str(run_id), preparation_meta
                )
            row = self.ai_run(str(run_id))
            if row is None:
                raise KeyError(f"AI run {run_id!r} not found")
            return row
        with self.atomic():
            if preparation_meta is not _UNSET:
                current = self.ai_run(str(run_id))
                if current is None:
                    raise KeyError(f"AI run {run_id!r} not found")
                assignments.append("preparation_meta_json=?")
                values.append(
                    _json(
                        _merge_json_objects(
                            current["preparation_meta_json"], preparation_meta
                        )
                    )
                )
            values.append(str(run_id))
            cursor = self.connection.execute(
                f"UPDATE ai_runs SET {','.join(assignments)} WHERE id=?", values
            )
            if cursor.rowcount != 1:
                raise KeyError(f"AI run {run_id!r} not found")
        row = self.ai_run(str(run_id))
        assert row is not None
        return row

    def merge_ai_run_preparation_meta(
        self, run_id: str, updates: Mapping[str, Any]
    ) -> sqlite3.Row:
        """Deep-merge durable preparation timings/cache/failure metadata."""

        if not isinstance(updates, Mapping):
            raise TypeError("updates must be a mapping")
        with self.atomic():
            row = self.ai_run(str(run_id))
            if row is None:
                raise KeyError(f"AI run {run_id!r} not found")
            merged = _merge_json_objects(row["preparation_meta_json"], updates)
            self.connection.execute(
                "UPDATE ai_runs SET preparation_meta_json=? WHERE id=?",
                (_json(merged), str(run_id)),
            )
        updated = self.ai_run(str(run_id))
        assert updated is not None
        return updated

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

    def create_markdown_summary_target(
        self,
        target_id: str,
        *,
        name: str,
        path: str | Path,
        preset: str,
        schema: Mapping[str, Any],
        schema_hash: str | None = None,
        enabled: bool = True,
        created_at: str | None = None,
    ) -> sqlite3.Row:
        """Register one exact local Markdown file and its declared schema."""

        stamp = created_at or utc_now()
        schema_json = _json(schema)
        self.connection.execute(
            """INSERT INTO markdown_summary_targets(
                   id,name,path,preset,schema_json,schema_hash,revision,enabled,
                   created_at,updated_at)
               VALUES(?,?,?,?,?,?,1,?,?,?)""",
            (
                str(target_id),
                str(name),
                str(path),
                str(preset),
                schema_json,
                schema_hash or _sha256_text(schema_json),
                int(bool(enabled)),
                stamp,
                stamp,
            ),
        )
        row = self.markdown_summary_target(str(target_id))
        assert row is not None
        return row

    def markdown_summary_target(self, target_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM markdown_summary_targets WHERE id=?", (str(target_id),)
        ).fetchone()

    def markdown_summary_target_by_path(self, path: str | Path) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM markdown_summary_targets WHERE path=?", (str(path),)
        ).fetchone()

    def markdown_summary_targets(
        self, *, enabled: bool | None = None
    ) -> list[sqlite3.Row]:
        if enabled is None:
            return self.query(
                """SELECT * FROM markdown_summary_targets
                   ORDER BY enabled DESC,updated_at DESC,id"""
            )
        return self.query(
            """SELECT * FROM markdown_summary_targets WHERE enabled=?
               ORDER BY updated_at DESC,id""",
            (int(bool(enabled)),),
        )

    def update_markdown_summary_target(
        self,
        target_id: str,
        *,
        expected_revision: int | None = None,
        name: str | object = _UNSET,
        path: str | Path | object = _UNSET,
        preset: str | object = _UNSET,
        schema: Mapping[str, Any] | object = _UNSET,
        schema_hash: str | object = _UNSET,
        enabled: bool | object = _UNSET,
    ) -> sqlite3.Row:
        row = self.markdown_summary_target(str(target_id))
        if row is None:
            raise KeyError(f"Markdown summary target {target_id!r} not found")
        actual = int(row["revision"])
        if expected_revision is not None and int(expected_revision) != actual:
            raise MarkdownSummaryTargetRevisionConflict(
                str(target_id), expected_revision, actual
            )

        assignments = ["updated_at=?", "revision=revision+1"]
        values: list[Any] = [utc_now()]
        for column, value in (("name", name), ("preset", preset)):
            if value is not _UNSET:
                assignments.append(f"{column}=?")
                values.append(str(value))
        if path is not _UNSET:
            assignments.append("path=?")
            values.append(str(path))
        if schema is not _UNSET:
            encoded = _json(schema)
            assignments.append("schema_json=?")
            values.append(encoded)
            if schema_hash is _UNSET:
                schema_hash = _sha256_text(encoded)
        if schema_hash is not _UNSET:
            assignments.append("schema_hash=?")
            values.append(str(schema_hash))
        if enabled is not _UNSET:
            assignments.append("enabled=?")
            values.append(int(bool(enabled)))
        values.extend((str(target_id), actual))
        cursor = self.connection.execute(
            f"""UPDATE markdown_summary_targets SET {','.join(assignments)}
                WHERE id=? AND revision=?""",
            values,
        )
        if cursor.rowcount != 1:
            current = self.markdown_summary_target(str(target_id))
            raise MarkdownSummaryTargetRevisionConflict(
                str(target_id), actual, int(current["revision"]) if current else None
            )
        updated = self.markdown_summary_target(str(target_id))
        assert updated is not None
        return updated

    def delete_markdown_summary_target(
        self, target_id: str, *, expected_revision: int | None = None
    ) -> None:
        row = self.markdown_summary_target(str(target_id))
        if row is None:
            raise KeyError(f"Markdown summary target {target_id!r} not found")
        actual = int(row["revision"])
        if expected_revision is not None and int(expected_revision) != actual:
            raise MarkdownSummaryTargetRevisionConflict(
                str(target_id), expected_revision, actual
            )
        cursor = self.connection.execute(
            "DELETE FROM markdown_summary_targets WHERE id=? AND revision=?",
            (str(target_id), actual),
        )
        if cursor.rowcount != 1:
            current = self.markdown_summary_target(str(target_id))
            raise MarkdownSummaryTargetRevisionConflict(
                str(target_id), actual, int(current["revision"]) if current else None
            )

    def create_markdown_summary_proposal(
        self,
        proposal_id: str,
        *,
        attempt_id: int,
        target_path: str | Path,
        target_existed: bool,
        schema: Mapping[str, Any],
        entry: Mapping[str, Any],
        candidate_bytes: bytes | bytearray | memoryview | str,
        diff_text: str,
        run_id: str | None = None,
        target_id: str | None = None,
        target_revision: int | None = None,
        baseline_hash: str | None = None,
        schema_hash: str | None = None,
        entry_markdown: str = "",
        candidate_hash: str | None = None,
        confidence: float | None = None,
        warnings: Sequence[Any] | Mapping[str, Any] | None = None,
        duplicate: Mapping[str, Any] | None = None,
        rationale: str = "",
        created_at: str | None = None,
    ) -> sqlite3.Row:
        stamp = created_at or utc_now()
        schema_json = _json(schema)
        payload = (
            candidate_bytes.encode("utf-8")
            if isinstance(candidate_bytes, str)
            else bytes(candidate_bytes)
        )
        self.connection.execute(
            """INSERT INTO markdown_summary_proposals(
                   id,attempt_id,run_id,target_id,target_revision,target_path,
                   target_existed,baseline_hash,schema_json,schema_hash,entry_json,
                   entry_markdown,candidate_bytes,candidate_hash,diff_text,confidence,
                   warnings_json,duplicate_json,rationale,revision,status,
                   created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,'preview',?,?)""",
            (
                str(proposal_id),
                int(attempt_id),
                run_id,
                target_id,
                target_revision,
                str(target_path),
                int(bool(target_existed)),
                baseline_hash,
                schema_json,
                schema_hash or _sha256_text(schema_json),
                _json(entry),
                str(entry_markdown),
                sqlite3.Binary(payload),
                candidate_hash or hashlib.sha256(payload).hexdigest(),
                str(diff_text),
                confidence,
                _json(warnings or []),
                _json(duplicate or {}),
                str(rationale),
                stamp,
                stamp,
            ),
        )
        row = self.markdown_summary_proposal(str(proposal_id))
        assert row is not None
        return row

    def markdown_summary_proposal(self, proposal_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM markdown_summary_proposals WHERE id=?",
            (str(proposal_id),),
        ).fetchone()

    def markdown_summary_proposals(
        self,
        *,
        attempt_id: int | None = None,
        target_id: str | None = None,
        status: str | None = None,
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        values: list[Any] = []
        if attempt_id is not None:
            clauses.append("attempt_id=?")
            values.append(int(attempt_id))
        if target_id is not None:
            clauses.append("target_id=?")
            values.append(str(target_id))
        if status is not None:
            clauses.append("status=?")
            values.append(str(status))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return self.query(
            f"""SELECT * FROM markdown_summary_proposals{where}
                ORDER BY created_at DESC,rowid DESC""",
            values,
        )

    def update_markdown_summary_proposal(
        self,
        proposal_id: str,
        *,
        expected_revision: int | None = None,
        target_id: str | None | object = _UNSET,
        target_revision: int | None | object = _UNSET,
        target_path: str | Path | object = _UNSET,
        target_existed: bool | object = _UNSET,
        baseline_hash: str | None | object = _UNSET,
        schema: Mapping[str, Any] | object = _UNSET,
        schema_hash: str | object = _UNSET,
        entry: Mapping[str, Any] | object = _UNSET,
        entry_markdown: str | object = _UNSET,
        candidate_bytes: bytes | bytearray | memoryview | str | object = _UNSET,
        candidate_hash: str | object = _UNSET,
        diff_text: str | object = _UNSET,
        confidence: float | None | object = _UNSET,
        warnings: Sequence[Any] | Mapping[str, Any] | object = _UNSET,
        duplicate: Mapping[str, Any] | object = _UNSET,
        rationale: str | object = _UNSET,
        status: str | object = _UNSET,
        backup_path: str | Path | None | object = _UNSET,
        applied_hash: str | None | object = _UNSET,
        error: Mapping[str, Any] | object = _UNSET,
        applied_at: str | None | object = _UNSET,
        reverted_at: str | None | object = _UNSET,
    ) -> sqlite3.Row:
        row = self.markdown_summary_proposal(str(proposal_id))
        if row is None:
            raise KeyError(f"Markdown summary proposal {proposal_id!r} not found")
        actual = int(row["revision"])
        if expected_revision is not None and int(expected_revision) != actual:
            raise MarkdownSummaryProposalRevisionConflict(
                str(proposal_id), expected_revision, actual
            )

        assignments = ["updated_at=?", "revision=revision+1"]
        values: list[Any] = [utc_now()]
        for column, value in (
            ("target_id", target_id),
            ("target_revision", target_revision),
            ("baseline_hash", baseline_hash),
            ("confidence", confidence),
            ("applied_hash", applied_hash),
            ("applied_at", applied_at),
            ("reverted_at", reverted_at),
        ):
            if value is not _UNSET:
                assignments.append(f"{column}=?")
                values.append(value)
        for column, value in (
            ("target_path", target_path),
            ("entry_markdown", entry_markdown),
            ("diff_text", diff_text),
            ("rationale", rationale),
            ("status", status),
        ):
            if value is not _UNSET:
                assignments.append(f"{column}=?")
                values.append(str(value))
        if target_existed is not _UNSET:
            assignments.append("target_existed=?")
            values.append(int(bool(target_existed)))
        if schema is not _UNSET:
            encoded = _json(schema)
            assignments.append("schema_json=?")
            values.append(encoded)
            if schema_hash is _UNSET:
                schema_hash = _sha256_text(encoded)
        if schema_hash is not _UNSET:
            assignments.append("schema_hash=?")
            values.append(str(schema_hash))
        if entry is not _UNSET:
            assignments.append("entry_json=?")
            values.append(_json(entry))
        if candidate_bytes is not _UNSET:
            payload = (
                candidate_bytes.encode("utf-8")
                if isinstance(candidate_bytes, str)
                else bytes(candidate_bytes)
            )
            assignments.append("candidate_bytes=?")
            values.append(sqlite3.Binary(payload))
            if candidate_hash is _UNSET:
                candidate_hash = hashlib.sha256(payload).hexdigest()
        if candidate_hash is not _UNSET:
            assignments.append("candidate_hash=?")
            values.append(str(candidate_hash))
        if warnings is not _UNSET:
            assignments.append("warnings_json=?")
            values.append(_json(warnings))
        if duplicate is not _UNSET:
            assignments.append("duplicate_json=?")
            values.append(_json(duplicate))
        if backup_path is not _UNSET:
            assignments.append("backup_path=?")
            values.append(str(backup_path) if backup_path is not None else None)
        if error is not _UNSET:
            assignments.append("error_json=?")
            values.append(_json(error))
        values.extend((str(proposal_id), actual))
        cursor = self.connection.execute(
            f"""UPDATE markdown_summary_proposals SET {','.join(assignments)}
                WHERE id=? AND revision=?""",
            values,
        )
        if cursor.rowcount != 1:
            current = self.markdown_summary_proposal(str(proposal_id))
            raise MarkdownSummaryProposalRevisionConflict(
                str(proposal_id), actual, int(current["revision"]) if current else None
            )
        updated = self.markdown_summary_proposal(str(proposal_id))
        assert updated is not None
        return updated

    def stress_preparation_cache(
        self, cache_key: str
    ) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM stress_preparation_cache WHERE cache_key=?",
            (str(cache_key),),
        ).fetchone()

    def save_stress_preparation_cache(
        self,
        cache_key: str,
        *,
        payload: Mapping[str, Any],
        metadata: Mapping[str, Any] | None = None,
        created_at: str | None = None,
    ) -> sqlite3.Row:
        """Save immutable prepared content and merge mutable cache metadata."""

        key = str(cache_key).strip()
        if not key:
            raise ValueError("stress preparation cache key must not be empty")
        if not isinstance(payload, Mapping):
            raise TypeError("payload must be a mapping")
        if metadata is not None and not isinstance(metadata, Mapping):
            raise TypeError("metadata must be a mapping")
        payload_json = _json(payload)
        stamp = created_at or utc_now()
        with self.atomic():
            current = self.stress_preparation_cache(key)
            if current is None:
                self.connection.execute(
                    """INSERT INTO stress_preparation_cache(
                           cache_key,payload_json,metadata_json,created_at,updated_at)
                       VALUES(?,?,?,?,?)""",
                    (key, payload_json, _json(metadata or {}), stamp, stamp),
                )
            else:
                try:
                    same_payload = json.loads(str(current["payload_json"])) == json.loads(
                        payload_json
                    )
                except (TypeError, ValueError, json.JSONDecodeError):
                    same_payload = str(current["payload_json"]) == payload_json
                if not same_payload:
                    raise StressPreparationCacheConflict(key)
                if metadata:
                    merged = _merge_json_objects(current["metadata_json"], metadata)
                    self.connection.execute(
                        """UPDATE stress_preparation_cache
                           SET metadata_json=?,updated_at=? WHERE cache_key=?""",
                        (_json(merged), utc_now(), key),
                    )
        row = self.stress_preparation_cache(key)
        assert row is not None
        return row

    def merge_stress_preparation_cache_metadata(
        self, cache_key: str, updates: Mapping[str, Any]
    ) -> sqlite3.Row:
        """Deep-merge access/validation metadata without replacing cache content."""

        if not isinstance(updates, Mapping):
            raise TypeError("updates must be a mapping")
        key = str(cache_key)
        with self.atomic():
            row = self.stress_preparation_cache(key)
            if row is None:
                raise KeyError(f"Stress preparation cache {key!r} not found")
            merged = _merge_json_objects(row["metadata_json"], updates)
            self.connection.execute(
                """UPDATE stress_preparation_cache
                   SET metadata_json=?,updated_at=? WHERE cache_key=?""",
                (_json(merged), utc_now(), key),
            )
        updated = self.stress_preparation_cache(key)
        assert updated is not None
        return updated

    def upsert_problem_sample(
        self,
        platform: str,
        problem_id: str,
        sample_key: str,
        *,
        input_data: bytes | bytearray | memoryview | str,
        expected_output: bytes | bytearray | memoryview | str,
        source: str = "problem_context",
        metadata: Mapping[str, Any] | None = None,
    ) -> sqlite3.Row:
        """Insert or refresh one named sample while deduplicating equal content."""

        selected_platform = str(platform).strip()
        selected_problem = str(problem_id).strip()
        selected_key = str(sample_key).strip()
        selected_source = str(source).strip() or "problem_context"
        if not selected_platform or not selected_problem or not selected_key:
            raise ValueError("platform, problem_id, and sample_key must not be empty")
        if metadata is not None and not isinstance(metadata, Mapping):
            raise TypeError("metadata must be a mapping")

        def as_bytes(value: bytes | bytearray | memoryview | str) -> bytes:
            if isinstance(value, str):
                return value.encode("utf-8")
            if isinstance(value, (bytes, bytearray, memoryview)):
                return bytes(value)
            raise TypeError("sample input and output must be bytes or strings")

        input_bytes = as_bytes(input_data)
        output_bytes = as_bytes(expected_output)
        digest = hashlib.sha256()
        digest.update(len(input_bytes).to_bytes(8, "big"))
        digest.update(input_bytes)
        digest.update(len(output_bytes).to_bytes(8, "big"))
        digest.update(output_bytes)
        content_hash = digest.hexdigest()
        stamp = utc_now()
        with self.atomic():
            by_key = self.connection.execute(
                """SELECT * FROM problem_samples
                   WHERE platform=? AND problem_id=? AND sample_key=?""",
                (selected_platform, selected_problem, selected_key),
            ).fetchone()
            by_content = self.connection.execute(
                """SELECT * FROM problem_samples
                   WHERE platform=? AND problem_id=? AND content_hash=?""",
                (selected_platform, selected_problem, content_hash),
            ).fetchone()
            if by_content is not None and (
                by_key is None or int(by_content["id"]) != int(by_key["id"])
            ):
                if metadata:
                    merged = _merge_json_objects(by_content["metadata_json"], metadata)
                    self.connection.execute(
                        """UPDATE problem_samples
                           SET metadata_json=?,updated_at=? WHERE id=?""",
                        (_json(merged), stamp, int(by_content["id"])),
                    )
                row_id = int(by_content["id"])
            elif by_key is None:
                cursor = self.connection.execute(
                    """INSERT INTO problem_samples(
                           platform,problem_id,sample_key,input_data,expected_output,
                           content_hash,source,metadata_json,created_at,updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?)""",
                    (
                        selected_platform,
                        selected_problem,
                        selected_key,
                        sqlite3.Binary(input_bytes),
                        sqlite3.Binary(output_bytes),
                        content_hash,
                        selected_source,
                        _json(metadata or {}),
                        stamp,
                        stamp,
                    ),
                )
                row_id = int(cursor.lastrowid)
            else:
                merged = _merge_json_objects(by_key["metadata_json"], metadata or {})
                self.connection.execute(
                    """UPDATE problem_samples
                       SET input_data=?,expected_output=?,content_hash=?,source=?,
                           metadata_json=?,updated_at=? WHERE id=?""",
                    (
                        sqlite3.Binary(input_bytes),
                        sqlite3.Binary(output_bytes),
                        content_hash,
                        selected_source,
                        _json(merged),
                        stamp,
                        int(by_key["id"]),
                    ),
                )
                row_id = int(by_key["id"])
        row = self.connection.execute(
            "SELECT * FROM problem_samples WHERE id=?", (row_id,)
        ).fetchone()
        assert row is not None
        return row

    def problem_samples(
        self, platform: str, problem_id: str
    ) -> list[sqlite3.Row]:
        return self.query(
            """SELECT * FROM problem_samples
               WHERE platform=? AND problem_id=? ORDER BY id""",
            (str(platform), str(problem_id)),
        )

    def replace_problem_samples(
        self,
        platform: str,
        problem_id: str,
        samples: Sequence[Mapping[str, Any]],
        *,
        source: str = "problem_context",
        metadata: Mapping[str, Any] | None = None,
    ) -> list[sqlite3.Row]:
        """Atomically replace the structured samples owned by one source.

        Context refreshes must not leave an obsolete third sample behind when
        the new statement contains only two.  Other sources are preserved so
        future importers can coexist with the statement parser.
        """

        selected_platform = str(platform).strip()
        selected_problem = str(problem_id).strip()
        selected_source = str(source).strip() or "problem_context"
        if not selected_platform or not selected_problem:
            raise ValueError("platform and problem_id must not be empty")
        normalized = list(samples)
        with self.atomic():
            self.connection.execute(
                """DELETE FROM problem_samples
                   WHERE platform=? AND problem_id=? AND source=?""",
                (selected_platform, selected_problem, selected_source),
            )
            for ordinal, sample in enumerate(normalized, 1):
                if not isinstance(sample, Mapping):
                    raise TypeError("each sample must be a mapping")
                self.upsert_problem_sample(
                    selected_platform,
                    selected_problem,
                    str(sample.get("name") or f"sample{ordinal}"),
                    input_data=sample.get("input", b""),
                    expected_output=sample.get("output", b""),
                    source=selected_source,
                    metadata=metadata,
                )
        return self.problem_samples(selected_platform, selected_problem)

    def stress_artifact_candidate(
        self, candidate_id: str
    ) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM stress_artifact_candidates WHERE id=?",
            (str(candidate_id),),
        ).fetchone()

    def stress_artifact_candidates(
        self,
        *,
        generation_key: str | None = None,
        platform: str | None = None,
        problem_id: str | None = None,
        role: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        values: list[Any] = []
        for column, value in (
            ("generation_key", generation_key),
            ("platform", platform),
            ("problem_id", problem_id),
            ("role", role),
            ("status", status),
        ):
            if value is not None:
                clauses.append(f"{column}=?")
                values.append(str(value))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(max(1, int(limit)))
        return self.query(
            f"""SELECT * FROM stress_artifact_candidates{where}
                ORDER BY created_at DESC,rowid DESC LIMIT ?""",
            values,
        )

    def save_stress_artifact_candidate(
        self,
        candidate_id: str,
        *,
        generation_key: str,
        platform: str,
        problem_id: str,
        role: str,
        source_code: str,
        source_kind: str,
        generation_identity: Mapping[str, Any],
        producer_ai_run_id: str | None = None,
        provenance: Mapping[str, Any] | None = None,
        usage: Mapping[str, Any] | None = None,
        status: str = "generated",
        created_at: str | None = None,
    ) -> sqlite3.Row:
        """Save immutable generated/source content; identical retries are idempotent."""

        selected_id = str(candidate_id).strip()
        selected_generation = str(generation_key).strip()
        if not selected_id or not selected_generation:
            raise ValueError("candidate_id and generation_key must not be empty")
        for name, value in (
            ("generation_identity", generation_identity),
            ("provenance", provenance or {}),
            ("usage", usage or {}),
        ):
            if not isinstance(value, Mapping):
                raise TypeError(f"{name} must be a mapping")
        source = str(source_code)
        source_hash = _sha256_text(source)
        immutable = {
            "generation_key": selected_generation,
            "producer_ai_run_id": (
                str(producer_ai_run_id) if producer_ai_run_id is not None else None
            ),
            "platform": str(platform),
            "problem_id": str(problem_id),
            "role": str(role),
            "source_code": source,
            "source_hash": source_hash,
            "source_kind": str(source_kind),
            "provenance_json": _json(provenance or {}),
            "generation_identity_json": _json(generation_identity),
            "usage_json": _json(usage or {}),
            "status": str(status),
        }

        # Candidate identity is content-addressed.  Billing/provenance fields
        # describe the first observation and may legitimately differ when an
        # identical candidate is rediscovered in another cold run; they must
        # not turn an idempotent content hit into a conflict.
        identity_fields = (
            "generation_key",
            "platform",
            "problem_id",
            "role",
            "source_code",
            "source_hash",
            "source_kind",
            "generation_identity_json",
        )

        def matches(row: sqlite3.Row) -> bool:
            return all(row[key] == immutable[key] for key in identity_fields)

        with self.atomic():
            current = self.stress_artifact_candidate(selected_id)
            if current is not None:
                if not matches(current):
                    raise StressArtifactCandidateConflict(selected_id)
                return current
            duplicate = self.connection.execute(
                """SELECT * FROM stress_artifact_candidates
                   WHERE generation_key=? AND source_hash=?""",
                (selected_generation, source_hash),
            ).fetchone()
            if duplicate is not None:
                if not matches(duplicate):
                    raise StressArtifactCandidateConflict(str(duplicate["id"]))
                return duplicate
            stamp = created_at or utc_now()
            self.connection.execute(
                """INSERT INTO stress_artifact_candidates(
                       id,generation_key,producer_ai_run_id,platform,problem_id,
                       role,source_code,source_hash,source_kind,provenance_json,
                       generation_identity_json,usage_json,status,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    selected_id,
                    immutable["generation_key"],
                    immutable["producer_ai_run_id"],
                    immutable["platform"],
                    immutable["problem_id"],
                    immutable["role"],
                    immutable["source_code"],
                    immutable["source_hash"],
                    immutable["source_kind"],
                    immutable["provenance_json"],
                    immutable["generation_identity_json"],
                    immutable["usage_json"],
                    immutable["status"],
                    stamp,
                ),
            )
        row = self.stress_artifact_candidate(selected_id)
        assert row is not None
        return row

    def stress_artifact_proof(self, proof_key: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM stress_artifact_proofs WHERE proof_key=?",
            (str(proof_key),),
        ).fetchone()

    def stress_artifact_proofs(
        self,
        *,
        candidate_id: str | None = None,
        proof_kind: str | None = None,
        status: str | None = None,
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        values: list[Any] = []
        for column, value in (
            ("candidate_id", candidate_id),
            ("proof_kind", proof_kind),
            ("status", status),
        ):
            if value is not None:
                clauses.append(f"{column}=?")
                values.append(str(value))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        return self.query(
            f"""SELECT * FROM stress_artifact_proofs{where}
                ORDER BY created_at DESC,rowid DESC""",
            values,
        )

    def save_stress_artifact_proof(
        self,
        proof_key: str,
        *,
        candidate_id: str,
        proof_kind: str,
        certification_identity: Mapping[str, Any],
        status: str,
        result: Mapping[str, Any] | None = None,
        executable_path: str | Path | None = None,
        executable_hash: str | None = None,
        created_at: str | None = None,
    ) -> sqlite3.Row:
        selected_key = str(proof_key).strip()
        if not selected_key:
            raise ValueError("proof_key must not be empty")
        if not isinstance(certification_identity, Mapping):
            raise TypeError("certification_identity must be a mapping")
        if result is not None and not isinstance(result, Mapping):
            raise TypeError("result must be a mapping")
        immutable = {
            "candidate_id": str(candidate_id),
            "proof_kind": str(proof_kind),
            "certification_identity_json": _json(certification_identity),
            "status": str(status),
            "result_json": _json(result or {}),
            "executable_path": (
                str(executable_path) if executable_path is not None else None
            ),
            "executable_hash": (
                str(executable_hash) if executable_hash is not None else None
            ),
        }

        def matches(row: sqlite3.Row) -> bool:
            return all(row[key] == value for key, value in immutable.items())

        with self.atomic():
            current = self.stress_artifact_proof(selected_key)
            if current is not None:
                if not matches(current):
                    raise StressArtifactProofConflict(selected_key)
                return current
            duplicate = self.connection.execute(
                """SELECT * FROM stress_artifact_proofs
                   WHERE candidate_id=? AND proof_kind=?
                     AND certification_identity_json=?""",
                (
                    immutable["candidate_id"],
                    immutable["proof_kind"],
                    immutable["certification_identity_json"],
                ),
            ).fetchone()
            if duplicate is not None:
                if not matches(duplicate):
                    raise StressArtifactProofConflict(str(duplicate["proof_key"]))
                return duplicate
            self.connection.execute(
                """INSERT INTO stress_artifact_proofs(
                       proof_key,candidate_id,proof_kind,
                       certification_identity_json,status,result_json,
                       executable_path,executable_hash,created_at)
                   VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    selected_key,
                    immutable["candidate_id"],
                    immutable["proof_kind"],
                    immutable["certification_identity_json"],
                    immutable["status"],
                    immutable["result_json"],
                    immutable["executable_path"],
                    immutable["executable_hash"],
                    created_at or utc_now(),
                ),
            )
        row = self.stress_artifact_proof(selected_key)
        assert row is not None
        return row

    def stress_bundle_certification(
        self, certification_key: str
    ) -> sqlite3.Row | None:
        return self.connection.execute(
            """SELECT * FROM stress_bundle_certifications
               WHERE certification_key=?""",
            (str(certification_key),),
        ).fetchone()

    def stress_bundle_certifications(
        self,
        *,
        platform: str | None = None,
        problem_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        values: list[Any] = []
        for column, value in (
            ("platform", platform),
            ("problem_id", problem_id),
            ("status", status),
        ):
            if value is not None:
                clauses.append(f"{column}=?")
                values.append(str(value))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(max(1, int(limit)))
        return self.query(
            f"""SELECT * FROM stress_bundle_certifications{where}
                ORDER BY created_at DESC,rowid DESC LIMIT ?""",
            values,
        )

    def save_stress_bundle_certification(
        self,
        certification_key: str,
        *,
        platform: str,
        problem_id: str,
        generator_candidate_id: str,
        brute_candidate_id: str,
        reference_candidate_id: str,
        certification_identity: Mapping[str, Any],
        scope: Mapping[str, Any] | None = None,
        preflight: Mapping[str, Any] | None = None,
        status: str = "valid",
        created_at: str | None = None,
        last_used_at: str | None = None,
    ) -> sqlite3.Row:
        selected_key = str(certification_key).strip()
        if not selected_key:
            raise ValueError("certification_key must not be empty")
        for name, value in (
            ("certification_identity", certification_identity),
            ("scope", scope or {}),
            ("preflight", preflight or {}),
        ):
            if not isinstance(value, Mapping):
                raise TypeError(f"{name} must be a mapping")
        immutable = {
            "platform": str(platform),
            "problem_id": str(problem_id),
            "generator_candidate_id": str(generator_candidate_id),
            "brute_candidate_id": str(brute_candidate_id),
            "reference_candidate_id": str(reference_candidate_id),
            "certification_identity_json": _json(certification_identity),
            "scope_json": _json(scope or {}),
            "preflight_json": _json(preflight or {}),
            "status": str(status),
            "last_used_at": last_used_at,
        }

        def matches(row: sqlite3.Row) -> bool:
            return all(row[key] == value for key, value in immutable.items())

        with self.atomic():
            current = self.stress_bundle_certification(selected_key)
            if current is not None:
                if not matches(current):
                    raise StressBundleCertificationConflict(selected_key)
                return current
            self.connection.execute(
                """INSERT INTO stress_bundle_certifications(
                       certification_key,platform,problem_id,
                       generator_candidate_id,brute_candidate_id,
                       reference_candidate_id,certification_identity_json,
                       scope_json,preflight_json,status,created_at,last_used_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    selected_key,
                    immutable["platform"],
                    immutable["problem_id"],
                    immutable["generator_candidate_id"],
                    immutable["brute_candidate_id"],
                    immutable["reference_candidate_id"],
                    immutable["certification_identity_json"],
                    immutable["scope_json"],
                    immutable["preflight_json"],
                    immutable["status"],
                    created_at or utc_now(),
                    immutable["last_used_at"],
                ),
            )
        row = self.stress_bundle_certification(selected_key)
        assert row is not None
        return row

    def stress_cache_alias(self, alias_key: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM stress_cache_aliases WHERE alias_key=?",
            (str(alias_key),),
        ).fetchone()

    def publish_stress_cache_alias(
        self,
        alias_key: str,
        *,
        alias_kind: str,
        target_id: str,
        expected_revision: int | None = None,
    ) -> sqlite3.Row:
        """Create or CAS-update the mutable pointer to immutable cache content."""

        selected_key = str(alias_key).strip()
        selected_target = str(target_id).strip()
        if not selected_key or not selected_target:
            raise ValueError("alias_key and target_id must not be empty")
        with self.atomic():
            current = self.stress_cache_alias(selected_key)
            if current is None:
                if expected_revision not in {None, 0}:
                    raise StressCacheAliasRevisionConflict(
                        selected_key, expected_revision, None
                    )
                stamp = utc_now()
                self.connection.execute(
                    """INSERT INTO stress_cache_aliases(
                           alias_key,alias_kind,target_id,revision,created_at,updated_at)
                       VALUES(?,?,?,1,?,?)""",
                    (selected_key, str(alias_kind), selected_target, stamp, stamp),
                )
            else:
                actual = int(current["revision"])
                if expected_revision is None or int(expected_revision) != actual:
                    raise StressCacheAliasRevisionConflict(
                        selected_key, expected_revision, actual
                    )
                cursor = self.connection.execute(
                    """UPDATE stress_cache_aliases
                       SET alias_kind=?,target_id=?,revision=revision+1,updated_at=?
                       WHERE alias_key=? AND revision=?""",
                    (
                        str(alias_kind),
                        selected_target,
                        utc_now(),
                        selected_key,
                        actual,
                    ),
                )
                if cursor.rowcount != 1:
                    latest = self.stress_cache_alias(selected_key)
                    raise StressCacheAliasRevisionConflict(
                        selected_key,
                        actual,
                        int(latest["revision"]) if latest is not None else None,
                    )
        row = self.stress_cache_alias(selected_key)
        assert row is not None
        return row

    def create_stress_artifact_bundle(
        self,
        bundle_id: str,
        *,
        platform: str,
        problem_id: str,
        attempt_id: int | None = None,
        contract: Mapping[str, Any] | None = None,
        baseline_manifest: Mapping[str, Any] | None = None,
        preparation_cache_key: str | None = None,
        certification_key: str | None = None,
        preparation_meta: Mapping[str, Any] | None = None,
        status: str = "staging",
    ) -> sqlite3.Row:
        stamp = utc_now()
        self.connection.execute(
            """INSERT INTO stress_artifact_bundles(
                   id,attempt_id,platform,problem_id,contract_json,
                   baseline_manifest_json,preparation_cache_key,
                   certification_key,preparation_meta_json,status,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                str(bundle_id),
                attempt_id,
                str(platform),
                str(problem_id),
                _json(contract or {}),
                _json(baseline_manifest or {}),
                (
                    str(preparation_cache_key)
                    if preparation_cache_key is not None
                    else None
                ),
                str(certification_key) if certification_key is not None else None,
                _json(preparation_meta or {}),
                str(status),
                stamp,
                stamp,
            ),
        )
        row = self.stress_artifact_bundle(str(bundle_id))
        assert row is not None
        return row

    def stress_artifact_bundle(self, bundle_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM stress_artifact_bundles WHERE id=?", (str(bundle_id),)
        ).fetchone()

    def stress_artifact_bundle_for_cache_key(
        self,
        preparation_cache_key: str,
        *,
        platform: str | None = None,
        problem_id: str | None = None,
    ) -> sqlite3.Row | None:
        clauses = ["preparation_cache_key=?"]
        values: list[Any] = [str(preparation_cache_key)]
        if platform is not None:
            clauses.append("platform=?")
            values.append(str(platform))
        if problem_id is not None:
            clauses.append("problem_id=?")
            values.append(str(problem_id))
        return self.connection.execute(
            f"""SELECT * FROM stress_artifact_bundles
                WHERE {' AND '.join(clauses)}
                ORDER BY created_at DESC,rowid DESC LIMIT 1""",
            values,
        ).fetchone()

    def stress_artifact_bundles(
        self,
        *,
        platform: str | None = None,
        problem_id: str | None = None,
        status: str | None = None,
        preparation_cache_key: str | None = None,
        limit: int = 100,
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        values: list[Any] = []
        if platform is not None:
            clauses.append("platform=?")
            values.append(str(platform))
        if problem_id is not None:
            clauses.append("problem_id=?")
            values.append(str(problem_id))
        if status is not None:
            clauses.append("status=?")
            values.append(str(status))
        if preparation_cache_key is not None:
            clauses.append("preparation_cache_key=?")
            values.append(str(preparation_cache_key))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(max(1, int(limit)))
        return self.query(
            f"""SELECT * FROM stress_artifact_bundles{where}
                ORDER BY created_at DESC,rowid DESC LIMIT ?""",
            values,
        )

    def update_stress_artifact_bundle(
        self,
        bundle_id: str,
        *,
        expected_revision: int | None = None,
        contract: Mapping[str, Any] | object = _UNSET,
        baseline_manifest: Mapping[str, Any] | object = _UNSET,
        preparation_cache_key: str | None | object = _UNSET,
        certification_key: str | None | object = _UNSET,
        preparation_meta: Mapping[str, Any] | object = _UNSET,
        status: str | object = _UNSET,
        backup_path: str | Path | None | object = _UNSET,
        error: Mapping[str, Any] | object = _UNSET,
        applied_at: str | None | object = _UNSET,
        reverted_at: str | None | object = _UNSET,
    ) -> sqlite3.Row:
        row = self.stress_artifact_bundle(str(bundle_id))
        if row is None:
            raise KeyError(f"Stress artifact bundle {bundle_id!r} not found")
        actual = int(row["revision"])
        if expected_revision is not None and int(expected_revision) != actual:
            raise StressArtifactBundleRevisionConflict(
                str(bundle_id), expected_revision, actual
            )
        assignments = ["updated_at=?", "revision=revision+1"]
        values: list[Any] = [utc_now()]
        for column, value in (
            ("status", status),
            ("applied_at", applied_at),
            ("reverted_at", reverted_at),
        ):
            if value is not _UNSET:
                assignments.append(f"{column}=?")
                values.append(value)
        if backup_path is not _UNSET:
            assignments.append("backup_path=?")
            values.append(str(backup_path) if backup_path is not None else None)
        if contract is not _UNSET:
            assignments.append("contract_json=?")
            values.append(_json(contract))
        if baseline_manifest is not _UNSET:
            assignments.append("baseline_manifest_json=?")
            values.append(_json(baseline_manifest))
        if preparation_cache_key is not _UNSET:
            assignments.append("preparation_cache_key=?")
            values.append(
                str(preparation_cache_key)
                if preparation_cache_key is not None
                else None
            )
        if certification_key is not _UNSET:
            assignments.append("certification_key=?")
            values.append(
                str(certification_key) if certification_key is not None else None
            )
        if preparation_meta is not _UNSET:
            if not isinstance(preparation_meta, Mapping):
                raise TypeError("preparation_meta must be a mapping")
            assignments.append("preparation_meta_json=?")
            values.append(
                _json(
                    _merge_json_objects(
                        row["preparation_meta_json"], preparation_meta
                    )
                )
            )
        if error is not _UNSET:
            assignments.append("error_json=?")
            values.append(_json(error))
        values.extend((str(bundle_id), actual))
        cursor = self.connection.execute(
            f"""UPDATE stress_artifact_bundles SET {','.join(assignments)}
                WHERE id=? AND revision=?""",
            values,
        )
        if cursor.rowcount != 1:
            current = self.stress_artifact_bundle(str(bundle_id))
            raise StressArtifactBundleRevisionConflict(
                str(bundle_id), actual, int(current["revision"]) if current else None
            )
        updated = self.stress_artifact_bundle(str(bundle_id))
        assert updated is not None
        return updated

    def save_stress_artifact(
        self,
        artifact_id: str,
        *,
        bundle_id: str,
        kind: str,
        source_code: str,
        target_path: str | Path,
        source_kind: str,
        ai_run_id: str | None = None,
        baseline_hash: str | None = None,
        source_url: str | None = None,
        source_title: str | None = None,
        source_license: str | None = None,
        source_content_hash: str | None = None,
        status: str = "staged",
        validation: Mapping[str, Any] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> sqlite3.Row:
        stamp = utc_now()
        current = self.stress_artifact_for_kind(str(bundle_id), str(kind))
        validation_payload = (
            _merge_json_objects(current["validation_json"], validation or {})
            if current is not None
            else dict(validation or {})
        )
        metadata_payload = (
            _merge_json_objects(current["metadata_json"], metadata or {})
            if current is not None
            else dict(metadata or {})
        )
        self.connection.execute(
            """INSERT INTO stress_artifacts(
                   id,bundle_id,ai_run_id,kind,source_code,source_hash,target_path,
                   baseline_hash,source_kind,source_url,source_title,source_license,
                   source_content_hash,status,validation_json,metadata_json,
                   created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(bundle_id,kind) DO UPDATE SET
                   ai_run_id=excluded.ai_run_id,
                   source_code=excluded.source_code,
                   source_hash=excluded.source_hash,
                   target_path=excluded.target_path,
                   baseline_hash=excluded.baseline_hash,
                   source_kind=excluded.source_kind,
                   source_url=excluded.source_url,
                   source_title=excluded.source_title,
                   source_license=excluded.source_license,
                   source_content_hash=excluded.source_content_hash,
                   status=excluded.status,
                   validation_json=excluded.validation_json,
                   metadata_json=excluded.metadata_json,
                   updated_at=excluded.updated_at""",
            (
                str(artifact_id),
                str(bundle_id),
                str(ai_run_id) if ai_run_id is not None else None,
                str(kind),
                str(source_code),
                _sha256_text(str(source_code)),
                str(target_path),
                baseline_hash,
                str(source_kind),
                source_url,
                source_title,
                source_license,
                source_content_hash,
                str(status),
                _json(validation_payload),
                _json(metadata_payload),
                stamp,
                stamp,
            ),
        )
        row = self.stress_artifact_for_kind(str(bundle_id), str(kind))
        assert row is not None
        return row

    def stress_artifact(self, artifact_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM stress_artifacts WHERE id=?", (str(artifact_id),)
        ).fetchone()

    def stress_artifact_for_kind(
        self, bundle_id: str, kind: str
    ) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM stress_artifacts WHERE bundle_id=? AND kind=?",
            (str(bundle_id), str(kind)),
        ).fetchone()

    def stress_artifacts(self, bundle_id: str) -> list[sqlite3.Row]:
        return self.query(
            """SELECT * FROM stress_artifacts WHERE bundle_id=?
               ORDER BY CASE kind
                   WHEN 'generator' THEN 0 WHEN 'brute' THEN 1 ELSE 2 END""",
            (str(bundle_id),),
        )

    def update_stress_artifact(
        self,
        artifact_id: str,
        *,
        source_code: str | object = _UNSET,
        status: str | object = _UNSET,
        validation: Mapping[str, Any] | object = _UNSET,
        metadata: Mapping[str, Any] | object = _UNSET,
    ) -> sqlite3.Row:
        current = self.stress_artifact(str(artifact_id))
        if current is None:
            raise KeyError(f"Stress artifact {artifact_id!r} not found")
        assignments = ["updated_at=?"]
        values: list[Any] = [utc_now()]
        if source_code is not _UNSET:
            assignments.extend(("source_code=?", "source_hash=?"))
            values.extend((str(source_code), _sha256_text(str(source_code))))
        if status is not _UNSET:
            assignments.append("status=?")
            values.append(str(status))
        if validation is not _UNSET:
            if not isinstance(validation, Mapping):
                raise TypeError("validation must be a mapping")
            assignments.append("validation_json=?")
            values.append(
                _json(_merge_json_objects(current["validation_json"], validation))
            )
        if metadata is not _UNSET:
            if not isinstance(metadata, Mapping):
                raise TypeError("metadata must be a mapping")
            assignments.append("metadata_json=?")
            values.append(
                _json(_merge_json_objects(current["metadata_json"], metadata))
            )
        values.append(str(artifact_id))
        cursor = self.connection.execute(
            f"UPDATE stress_artifacts SET {','.join(assignments)} WHERE id=?", values
        )
        if cursor.rowcount != 1:
            raise KeyError(f"Stress artifact {artifact_id!r} not found")
        row = self.stress_artifact(str(artifact_id))
        assert row is not None
        return row

    def create_stress_run(
        self,
        run_id: str,
        *,
        bundle_id: str,
        platform: str,
        problem_id: str,
        user_source_path: str | Path,
        user_source_hash: str,
        attempt_id: int | None = None,
        config: Mapping[str, Any] | None = None,
        status: str = "pending",
        phase: str = "preparing",
        start_seed: int = 0,
        large_count: int = 0,
        owner_pid: int | None = None,
    ) -> sqlite3.Row:
        seed = int(start_seed)
        stamp = utc_now()
        self.connection.execute(
            """INSERT INTO stress_runs(
                   id,bundle_id,attempt_id,platform,problem_id,user_source_path,
                   user_source_hash,owner_pid,config_json,status,phase,start_seed,current_seed,
                   next_seed,large_count,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                str(run_id),
                str(bundle_id),
                attempt_id,
                str(platform),
                str(problem_id),
                str(user_source_path),
                str(user_source_hash),
                int(owner_pid if owner_pid is not None else os.getpid()),
                _json(config or {}),
                str(status),
                str(phase),
                seed,
                seed,
                seed,
                int(large_count),
                stamp,
                stamp,
            ),
        )
        row = self.stress_run(str(run_id))
        assert row is not None
        return row

    def stress_run(self, run_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM stress_runs WHERE id=?", (str(run_id),)
        ).fetchone()

    def active_stress_run(self) -> sqlite3.Row | None:
        return self.connection.execute(
            """SELECT * FROM stress_runs
               WHERE status IN ('pending','preparing','running','stop_requested')
               ORDER BY created_at DESC,rowid DESC LIMIT 1"""
        ).fetchone()

    def stress_runs(
        self,
        *,
        platform: str | None = None,
        problem_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        values: list[Any] = []
        if platform is not None:
            clauses.append("platform=?")
            values.append(str(platform))
        if problem_id is not None:
            clauses.append("problem_id=?")
            values.append(str(problem_id))
        if status is not None:
            clauses.append("status=?")
            values.append(str(status))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        values.append(max(1, int(limit)))
        return self.query(
            f"""SELECT * FROM stress_runs{where}
                ORDER BY created_at DESC,rowid DESC LIMIT ?""",
            values,
        )

    def update_stress_run(
        self,
        run_id: str,
        *,
        expected_revision: int | None = None,
        config: Mapping[str, Any] | object = _UNSET,
        status: str | object = _UNSET,
        phase: str | object = _UNSET,
        current_seed: int | object = _UNSET,
        next_seed: int | object = _UNSET,
        small_count: int | object = _UNSET,
        large_count: int | object = _UNSET,
        total_count: int | object = _UNSET,
        mismatch_seed: int | None | object = _UNSET,
        failure_path: str | Path | None | object = _UNSET,
        stop_reason: str | None | object = _UNSET,
        error: Mapping[str, Any] | object = _UNSET,
        started_at: str | None | object = _UNSET,
        completed_at: str | None | object = _UNSET,
        owner_pid: int | object = _UNSET,
        user_source_hash: str | object = _UNSET,
    ) -> sqlite3.Row:
        row = self.stress_run(str(run_id))
        if row is None:
            raise KeyError(f"Stress run {run_id!r} not found")
        actual = int(row["revision"])
        if expected_revision is not None and int(expected_revision) != actual:
            raise StressRunRevisionConflict(str(run_id), expected_revision, actual)
        assignments = ["updated_at=?", "revision=revision+1"]
        values: list[Any] = [utc_now()]
        for column, value in (
            ("status", status),
            ("phase", phase),
            ("mismatch_seed", mismatch_seed),
            ("stop_reason", stop_reason),
            ("started_at", started_at),
            ("completed_at", completed_at),
        ):
            if value is not _UNSET:
                assignments.append(f"{column}=?")
                values.append(value)
        for column, value in (
            ("current_seed", current_seed),
            ("next_seed", next_seed),
            ("small_count", small_count),
            ("large_count", large_count),
            ("total_count", total_count),
            ("owner_pid", owner_pid),
        ):
            if value is not _UNSET:
                assignments.append(f"{column}=?")
                values.append(int(value))
        if config is not _UNSET:
            assignments.append("config_json=?")
            values.append(_json(config))
        if user_source_hash is not _UNSET:
            assignments.append("user_source_hash=?")
            values.append(str(user_source_hash))
        if failure_path is not _UNSET:
            assignments.append("failure_path=?")
            values.append(str(failure_path) if failure_path is not None else None)
        if error is not _UNSET:
            assignments.append("error_json=?")
            values.append(_json(error))
        values.extend((str(run_id), actual))
        cursor = self.connection.execute(
            f"""UPDATE stress_runs SET {','.join(assignments)}
                WHERE id=? AND revision=?""",
            values,
        )
        if cursor.rowcount != 1:
            current = self.stress_run(str(run_id))
            raise StressRunRevisionConflict(
                str(run_id), actual, int(current["revision"]) if current else None
            )
        updated = self.stress_run(str(run_id))
        assert updated is not None
        return updated

    def request_stress_run_stop(self, run_id: str) -> sqlite3.Row:
        row = self.stress_run(str(run_id))
        if row is None:
            raise KeyError(f"Stress run {run_id!r} not found")
        if row["status"] not in {"pending", "preparing", "running"}:
            return row
        return self.update_stress_run(
            str(run_id),
            expected_revision=int(row["revision"]),
            status="stop_requested",
            stop_reason="user_requested",
        )

    def request_stress_run_finish(self, run_id: str) -> sqlite3.Row:
        """Permanently finish a run, stopping its process tree first if active."""
        row = self.stress_run(str(run_id))
        if row is None:
            raise KeyError(f"Stress run {run_id!r} not found")
        status = str(row["status"])
        if status in {"pending", "preparing", "running", "stop_requested"}:
            return self.update_stress_run(
                str(run_id),
                expected_revision=int(row["revision"]),
                status="stop_requested",
                stop_reason="user_finished",
            )
        if status in {"stopped", "interrupted"}:
            return self.update_stress_run(
                str(run_id),
                expected_revision=int(row["revision"]),
                status="completed",
                phase="complete",
                stop_reason="user_finished",
                completed_at=utc_now(),
            )
        return row

    def resume_stress_run(
        self,
        run_id: str,
        *,
        user_source_hash: str | None = None,
        rate_base_total: int | None = None,
    ) -> sqlite3.Row:
        row = self.stress_run(str(run_id))
        if row is None:
            raise KeyError(f"Stress run {run_id!r} not found")
        if row["status"] not in {
            "interrupted",
            "stopped",
            "mismatch",
            "oracle_conflict",
            "fault",
        }:
            raise ValueError(
                f"Stress run {run_id!r} cannot resume from {row['status']!r}"
            )
        try:
            config = json.loads(str(row["config_json"] or "{}"))
        except (TypeError, json.JSONDecodeError):
            config = {}
        if not isinstance(config, dict):
            config = {}
        config["rate_base_total"] = int(
            row["total_count"] if rate_base_total is None else rate_base_total
        )
        return self.update_stress_run(
            str(run_id),
            expected_revision=int(row["revision"]),
            config=config,
            status="pending",
            phase="preparing",
            current_seed=int(row["next_seed"]),
            owner_pid=os.getpid(),
            user_source_hash=(
                str(user_source_hash)
                if user_source_hash is not None
                else str(row["user_source_hash"])
            ),
            mismatch_seed=None,
            failure_path=None,
            error={},
            stop_reason=None,
            started_at=None,
            completed_at=None,
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
