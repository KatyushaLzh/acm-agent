"""Versioned SQLite schema for the local ACM learning workflow.

Split out of :mod:`storage` so the repository logic stays readable: this
module is pure DDL data with no behaviour.  ``SCHEMA_VERSION`` lives here
because it must equal ``max(MIGRATIONS)`` -- keeping the two together makes
that invariant local and checkable.

``storage`` re-exports both names, so importers keep using
``from .storage import MIGRATIONS, SCHEMA_VERSION``.  Tests that patch a
single migration in place mutate this same dict object.
"""

from __future__ import annotations

SCHEMA_VERSION = 16


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
    13: """
    CREATE TABLE stress_artifacts_v13 (
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
                'cnblogs', 'csdn', 'local_existing', 'user_specified'
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

    INSERT INTO stress_artifacts_v13(
        id,bundle_id,ai_run_id,kind,source_code,source_hash,target_path,
        baseline_hash,source_kind,source_url,source_title,source_license,
        source_content_hash,status,validation_json,metadata_json,created_at,updated_at)
    SELECT
        id,bundle_id,ai_run_id,kind,source_code,source_hash,target_path,
        baseline_hash,source_kind,source_url,source_title,source_license,
        source_content_hash,status,validation_json,metadata_json,created_at,updated_at
    FROM stress_artifacts;

    DROP TABLE stress_artifacts;
    ALTER TABLE stress_artifacts_v13 RENAME TO stress_artifacts;

    CREATE INDEX stress_artifacts_bundle_idx
        ON stress_artifacts(bundle_id, kind);
    CREATE INDEX stress_artifacts_ai_run_idx
        ON stress_artifacts(ai_run_id, created_at DESC);

    CREATE TABLE stress_artifact_candidates_v13 (
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
                'cnblogs', 'csdn', 'local_existing', 'user_specified'
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

    INSERT INTO stress_artifact_candidates_v13(
        id,generation_key,producer_ai_run_id,platform,problem_id,role,
        source_code,source_hash,source_kind,provenance_json,
        generation_identity_json,usage_json,status,created_at)
    SELECT
        id,generation_key,producer_ai_run_id,platform,problem_id,role,
        source_code,source_hash,source_kind,provenance_json,
        generation_identity_json,usage_json,status,created_at
    FROM stress_artifact_candidates;

    DROP TABLE stress_artifact_candidates;
    ALTER TABLE stress_artifact_candidates_v13 RENAME TO stress_artifact_candidates;

    CREATE INDEX stress_artifact_candidates_generation_idx
        ON stress_artifact_candidates(
            generation_key, status, created_at DESC, id
        );
    CREATE INDEX stress_artifact_candidates_problem_role_idx
        ON stress_artifact_candidates(
            platform, problem_id, role, created_at DESC, id
        );
    """,
    14: """
    CREATE TABLE stress_artifacts_v14 (
        id TEXT PRIMARY KEY,
        bundle_id TEXT NOT NULL,
        ai_run_id TEXT,
        kind TEXT NOT NULL CHECK (kind IN (
            'generator', 'brute', 'reference',
            'reference_primary', 'reference_secondary'
        )),
        source_code TEXT NOT NULL,
        source_hash TEXT NOT NULL,
        target_path TEXT NOT NULL,
        baseline_hash TEXT,
        source_kind TEXT NOT NULL CHECK (
            source_kind IN (
                'ai_generated', 'codeforces_editorial', 'luogu_solution',
                'cnblogs', 'csdn', 'local_existing', 'user_specified'
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

    INSERT INTO stress_artifacts_v14
    SELECT * FROM stress_artifacts;
    DROP TABLE stress_artifacts;
    ALTER TABLE stress_artifacts_v14 RENAME TO stress_artifacts;
    CREATE INDEX stress_artifacts_bundle_idx
        ON stress_artifacts(bundle_id, kind);
    CREATE INDEX stress_artifacts_ai_run_idx
        ON stress_artifacts(ai_run_id, created_at DESC);

    CREATE TABLE stress_artifact_candidates_v14 (
        id TEXT PRIMARY KEY,
        generation_key TEXT NOT NULL,
        producer_ai_run_id TEXT,
        platform TEXT NOT NULL,
        problem_id TEXT NOT NULL,
        role TEXT NOT NULL CHECK (role IN (
            'generator', 'validator', 'brute', 'reference',
            'reference_primary', 'reference_secondary'
        )),
        source_code TEXT NOT NULL,
        source_hash TEXT NOT NULL,
        source_kind TEXT NOT NULL CHECK (
            source_kind IN (
                'ai_generated', 'codeforces_editorial', 'luogu_solution',
                'cnblogs', 'csdn', 'local_existing', 'user_specified'
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

    INSERT INTO stress_artifact_candidates_v14
    SELECT * FROM stress_artifact_candidates;
    DROP TABLE stress_artifact_candidates;
    ALTER TABLE stress_artifact_candidates_v14 RENAME TO stress_artifact_candidates;
    CREATE INDEX stress_artifact_candidates_generation_idx
        ON stress_artifact_candidates(
            generation_key, status, created_at DESC, id
        );
    CREATE INDEX stress_artifact_candidates_problem_role_idx
        ON stress_artifact_candidates(
            platform, problem_id, role, created_at DESC, id
        );

    CREATE TABLE stress_bundle_certifications_v14 (
        certification_key TEXT PRIMARY KEY,
        platform TEXT NOT NULL,
        problem_id TEXT NOT NULL,
        oracle_protocol TEXT NOT NULL CHECK (
            oracle_protocol IN ('legacy_trio', 'dual_reference_v1')
        ),
        generator_candidate_id TEXT NOT NULL,
        brute_candidate_id TEXT,
        reference_candidate_id TEXT,
        reference_primary_candidate_id TEXT,
        reference_secondary_candidate_id TEXT,
        certification_identity_json TEXT NOT NULL,
        scope_json TEXT NOT NULL DEFAULT '{}',
        preflight_json TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL CHECK (status IN ('valid', 'invalidated')),
        created_at TEXT NOT NULL,
        last_used_at TEXT,
        CHECK (
            (oracle_protocol='legacy_trio'
             AND brute_candidate_id IS NOT NULL
             AND reference_candidate_id IS NOT NULL
             AND reference_primary_candidate_id IS NULL
             AND reference_secondary_candidate_id IS NULL)
            OR
            (oracle_protocol='dual_reference_v1'
             AND brute_candidate_id IS NULL
             AND reference_candidate_id IS NULL
             AND reference_primary_candidate_id IS NOT NULL
             AND reference_secondary_candidate_id IS NOT NULL)
        ),
        FOREIGN KEY (platform, problem_id)
            REFERENCES problems(platform, problem_id) ON DELETE CASCADE,
        FOREIGN KEY (generator_candidate_id)
            REFERENCES stress_artifact_candidates(id) ON DELETE RESTRICT,
        FOREIGN KEY (brute_candidate_id)
            REFERENCES stress_artifact_candidates(id) ON DELETE RESTRICT,
        FOREIGN KEY (reference_candidate_id)
            REFERENCES stress_artifact_candidates(id) ON DELETE RESTRICT,
        FOREIGN KEY (reference_primary_candidate_id)
            REFERENCES stress_artifact_candidates(id) ON DELETE RESTRICT,
        FOREIGN KEY (reference_secondary_candidate_id)
            REFERENCES stress_artifact_candidates(id) ON DELETE RESTRICT
    );

    INSERT INTO stress_bundle_certifications_v14(
        certification_key,platform,problem_id,oracle_protocol,
        generator_candidate_id,brute_candidate_id,reference_candidate_id,
        certification_identity_json,scope_json,preflight_json,status,
        created_at,last_used_at)
    SELECT certification_key,platform,problem_id,'legacy_trio',
           generator_candidate_id,brute_candidate_id,reference_candidate_id,
           certification_identity_json,scope_json,preflight_json,status,
           created_at,last_used_at
      FROM stress_bundle_certifications;
    DROP TABLE stress_bundle_certifications;
    ALTER TABLE stress_bundle_certifications_v14
        RENAME TO stress_bundle_certifications;
    CREATE INDEX stress_bundle_certifications_problem_idx
        ON stress_bundle_certifications(
            platform, problem_id, oracle_protocol, status,
            created_at DESC, certification_key
        );
    CREATE INDEX stress_bundle_certifications_legacy_candidates_idx
        ON stress_bundle_certifications(
            generator_candidate_id, brute_candidate_id, reference_candidate_id
        ) WHERE oracle_protocol='legacy_trio';
    CREATE INDEX stress_bundle_certifications_dual_candidates_idx
        ON stress_bundle_certifications(
            generator_candidate_id, reference_primary_candidate_id,
            reference_secondary_candidate_id
        ) WHERE oracle_protocol='dual_reference_v1';
    """,
    15: """
    CREATE TABLE stress_artifacts_v15 (
        id TEXT PRIMARY KEY,
        bundle_id TEXT NOT NULL,
        ai_run_id TEXT,
        kind TEXT NOT NULL CHECK (kind IN (
            'generator', 'validator', 'brute', 'reference',
            'reference_primary', 'reference_secondary'
        )),
        source_code TEXT NOT NULL,
        source_hash TEXT NOT NULL,
        target_path TEXT NOT NULL,
        baseline_hash TEXT,
        source_kind TEXT NOT NULL CHECK (
            source_kind IN (
                'ai_generated', 'codeforces_editorial', 'luogu_solution',
                'cnblogs', 'csdn', 'local_existing', 'user_specified'
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

    INSERT INTO stress_artifacts_v15
    SELECT * FROM stress_artifacts;
    DROP TABLE stress_artifacts;
    ALTER TABLE stress_artifacts_v15 RENAME TO stress_artifacts;
    CREATE INDEX stress_artifacts_bundle_idx
        ON stress_artifacts(bundle_id, kind);
    CREATE INDEX stress_artifacts_ai_run_idx
        ON stress_artifacts(ai_run_id, created_at DESC);
    """,
    16: """
    CREATE TABLE stress_artifacts_v16 (
        id TEXT PRIMARY KEY,
        bundle_id TEXT NOT NULL,
        ai_run_id TEXT,
        kind TEXT NOT NULL CHECK (kind IN (
            'generator', 'validator', 'brute', 'reference',
            'reference_primary', 'reference_secondary'
        )),
        source_code TEXT NOT NULL,
        source_hash TEXT NOT NULL,
        target_path TEXT NOT NULL,
        baseline_hash TEXT,
        source_kind TEXT NOT NULL CHECK (
            source_kind IN (
                'ai_generated', 'ai_recipe_composed',
                'codeforces_editorial', 'luogu_solution',
                'cnblogs', 'csdn', 'local_existing', 'user_specified'
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

    INSERT INTO stress_artifacts_v16
    SELECT * FROM stress_artifacts;
    DROP TABLE stress_artifacts;
    ALTER TABLE stress_artifacts_v16 RENAME TO stress_artifacts;
    CREATE INDEX stress_artifacts_bundle_idx
        ON stress_artifacts(bundle_id, kind);
    CREATE INDEX stress_artifacts_ai_run_idx
        ON stress_artifacts(ai_run_id, created_at DESC);

    CREATE TABLE stress_artifact_candidates_v16 (
        id TEXT PRIMARY KEY,
        generation_key TEXT NOT NULL,
        producer_ai_run_id TEXT,
        platform TEXT NOT NULL,
        problem_id TEXT NOT NULL,
        role TEXT NOT NULL CHECK (role IN (
            'generator', 'validator', 'brute', 'reference',
            'reference_primary', 'reference_secondary'
        )),
        source_code TEXT NOT NULL,
        source_hash TEXT NOT NULL,
        source_kind TEXT NOT NULL CHECK (
            source_kind IN (
                'ai_generated', 'ai_recipe_composed',
                'codeforces_editorial', 'luogu_solution',
                'cnblogs', 'csdn', 'local_existing', 'user_specified'
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

    INSERT INTO stress_artifact_candidates_v16
    SELECT * FROM stress_artifact_candidates;
    DROP TABLE stress_artifact_candidates;
    ALTER TABLE stress_artifact_candidates_v16 RENAME TO stress_artifact_candidates;
    CREATE INDEX stress_artifact_candidates_generation_idx
        ON stress_artifact_candidates(
            generation_key, status, created_at DESC, id
        );
    CREATE INDEX stress_artifact_candidates_problem_role_idx
        ON stress_artifact_candidates(
            platform, problem_id, role, created_at DESC, id
        );
    """,
}
