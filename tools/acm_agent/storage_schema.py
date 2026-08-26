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

SCHEMA_VERSION = 24


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
    8: "",
    9: "",
    10: "",
    11: "",
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
    """,
    13: "",
    14: "",
    15: "",
    16: "",
    17: """
    DROP INDEX IF EXISTS ai_runs_single_running_stress_setup_idx;

    DROP TABLE IF EXISTS stress_bundle_certifications;
    DROP TABLE IF EXISTS stress_artifact_proofs;
    DROP TABLE IF EXISTS stress_cache_aliases;
    DROP TABLE IF EXISTS stress_artifact_candidates;
    DROP TABLE IF EXISTS stress_runs;
    DROP TABLE IF EXISTS stress_artifacts;
    DROP TABLE IF EXISTS stress_artifact_bundles;
    DROP TABLE IF EXISTS stress_preparation_cache;

    CREATE TABLE ai_runs_v17 (
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

    INSERT INTO ai_runs_v17(
        id,kind,model,conversation_id,message_id,request_summary_json,
        status,finish_reason,usage_json,error_json,created_at,completed_at
    )
    SELECT
        id,kind,model,conversation_id,message_id,request_summary_json,
        status,finish_reason,usage_json,error_json,created_at,completed_at
      FROM ai_runs
     WHERE kind <> 'stress_setup';

    DROP TABLE ai_runs;
    ALTER TABLE ai_runs_v17 RENAME TO ai_runs;

    CREATE INDEX ai_runs_recent_idx ON ai_runs(created_at DESC, id);
    CREATE INDEX ai_runs_conversation_idx
        ON ai_runs(conversation_id, created_at DESC);
    """,
    18: """
    ALTER TABLE ai_runs ADD COLUMN telemetry_json TEXT NOT NULL DEFAULT '{}';
    ALTER TABLE ai_runs ADD COLUMN estimated_cost_json TEXT NOT NULL DEFAULT '{}';
    """,
    19: """
    ALTER TABLE ai_runs ADD COLUMN provider_id TEXT;
    ALTER TABLE ai_runs ADD COLUMN profile_id TEXT;
    ALTER TABLE ai_runs ADD COLUMN requested_model TEXT;
    ALTER TABLE ai_runs ADD COLUMN resolved_model TEXT;
    ALTER TABLE ai_runs ADD COLUMN provider_origin TEXT;
    ALTER TABLE ai_runs ADD COLUMN credential_slot_id TEXT;
    ALTER TABLE ai_runs ADD COLUMN fallback_json TEXT;
    ALTER TABLE ai_runs ADD COLUMN cache_status TEXT;
    ALTER TABLE ai_runs ADD COLUMN cache_read_tokens INTEGER;
    ALTER TABLE ai_runs ADD COLUMN cache_write_tokens INTEGER;

    UPDATE ai_runs
       SET provider_id='deepseek',
           requested_model=model,
           provider_origin='https://api.deepseek.com',
           credential_slot_id='deepseek',
           profile_id=CASE
             WHEN kind='recommendation' THEN 'recommendation'
             WHEN kind='coaching' THEN 'coaching'
             WHEN kind='patch' THEN 'patch'
             WHEN kind='markdown_summary' THEN 'summary'
             WHEN kind='plan_import' AND json_valid(request_summary_json)
               THEN CASE json_extract(request_summary_json,'$.mode')
                 WHEN 'organize' THEN 'plan_organize'
                 WHEN 'generate' THEN 'plan_generate'
                 ELSE NULL END
             ELSE NULL END,
           cache_read_tokens=CASE WHEN json_valid(usage_json) THEN
             COALESCE(
               json_extract(usage_json,'$.cache_read_tokens'),
               json_extract(usage_json,'$.prompt_cache_hit_tokens'),
               json_extract(usage_json,'$.prompt_tokens_details.cached_tokens')
             ) ELSE NULL END,
           cache_write_tokens=CASE WHEN json_valid(usage_json) THEN
              json_extract(usage_json,'$.cache_write_tokens') ELSE NULL END;
    """,
    20: """
    ALTER TABLE ai_runs ADD COLUMN requested_reasoning_strength TEXT
        CHECK (
            requested_reasoning_strength IS NULL OR
            requested_reasoning_strength IN ('auto', 'low', 'medium', 'high')
        );
    ALTER TABLE ai_runs ADD COLUMN resolved_reasoning_strength TEXT
        CHECK (
            resolved_reasoning_strength IS NULL OR
            resolved_reasoning_strength IN ('auto', 'low', 'medium', 'high')
        );

    ALTER TABLE ai_conversations ADD COLUMN provider_id TEXT;
    ALTER TABLE ai_conversations ADD COLUMN model TEXT;
    ALTER TABLE ai_conversations ADD COLUMN reasoning_strength TEXT
        CHECK (
            reasoning_strength IS NULL OR
            reasoning_strength IN ('auto', 'low', 'medium', 'high')
        );
    ALTER TABLE ai_conversations ADD COLUMN provider_definition_hash TEXT;
    """,
    21: """
    ALTER TABLE ai_runs ADD COLUMN resolved_provider_id TEXT;
    ALTER TABLE ai_runs ADD COLUMN governance_json TEXT NOT NULL DEFAULT '{}';

    ALTER TABLE ai_conversations ADD COLUMN resolved_model TEXT;
    ALTER TABLE ai_conversations ADD COLUMN cache_session_key TEXT;
    ALTER TABLE ai_conversations ADD COLUMN route_policy_hash TEXT;

    CREATE TABLE ai_run_legs (
        run_id TEXT NOT NULL,
        ordinal INTEGER NOT NULL CHECK (ordinal >= 0),
        route_kind TEXT NOT NULL CHECK (route_kind IN ('primary','retry','fallback','legacy')),
        provider_id TEXT,
        profile_id TEXT,
        requested_model TEXT,
        resolved_model TEXT,
        reasoning_strength TEXT,
        status TEXT NOT NULL,
        error_code TEXT,
        provider_requests INTEGER,
        usage_json TEXT NOT NULL DEFAULT '{}',
        cache_status TEXT,
        cache_read_tokens INTEGER,
        cache_write_tokens INTEGER,
        estimated_cost_json TEXT NOT NULL DEFAULT '{}',
        PRIMARY KEY (run_id, ordinal),
        FOREIGN KEY (run_id) REFERENCES ai_runs(id) ON DELETE CASCADE
    );

    CREATE INDEX ai_run_legs_route_idx
        ON ai_run_legs(provider_id, profile_id, requested_model);

    INSERT INTO ai_run_legs(
        run_id,ordinal,route_kind,provider_id,profile_id,requested_model,
        resolved_model,reasoning_strength,status,provider_requests,usage_json,
        cache_status,cache_read_tokens,cache_write_tokens,estimated_cost_json
    )
    SELECT id,0,'legacy',provider_id,profile_id,requested_model,resolved_model,
           resolved_reasoning_strength,status,
           CASE WHEN json_valid(usage_json) THEN json_extract(usage_json,'$.provider_requests') END,
           usage_json,cache_status,cache_read_tokens,cache_write_tokens,estimated_cost_json
      FROM ai_runs;

    CREATE TABLE ai_run_cost_estimates (
        run_id TEXT NOT NULL,
        catalog_version TEXT NOT NULL,
        catalog_sha256 TEXT NOT NULL,
        basis TEXT NOT NULL CHECK (basis IN ('at_run_time','repriced')),
        status TEXT NOT NULL CHECK (status IN ('known','partial','unknown')),
        currency TEXT,
        amount_decimal TEXT,
        cache_savings_decimal TEXT,
        estimate_json TEXT NOT NULL,
        computed_at TEXT NOT NULL,
        PRIMARY KEY (run_id, catalog_version, catalog_sha256),
        FOREIGN KEY (run_id) REFERENCES ai_runs(id) ON DELETE CASCADE
    );

    CREATE INDEX ai_run_cost_estimates_version_idx
        ON ai_run_cost_estimates(catalog_version, computed_at DESC);
    """,
    22: """
    ALTER TABLE ai_runs ADD COLUMN local_cache_status TEXT CHECK (
        local_cache_status IS NULL OR local_cache_status IN (
            'bypass','miss','hit','coalesced','refresh'
        )
    );
    ALTER TABLE ai_runs ADD COLUMN local_cache_key TEXT;
    ALTER TABLE ai_runs ADD COLUMN cache_source_run_id TEXT;
    ALTER TABLE ai_runs ADD COLUMN cache_validation_json TEXT NOT NULL DEFAULT '{}';

    CREATE INDEX ai_runs_local_cache_idx
        ON ai_runs(profile_id, local_cache_status, created_at DESC);

    CREATE TABLE ai_cache_entries (
        cache_key TEXT PRIMARY KEY CHECK (
            length(cache_key)=64 AND cache_key NOT GLOB '*[^0-9a-f]*'
        ),
        profile_id TEXT NOT NULL CHECK (
            profile_id IN ('recommendation','plan_organize','summary')
        ),
        manifest_hash TEXT NOT NULL CHECK (
            length(manifest_hash)=64 AND manifest_hash NOT GLOB '*[^0-9a-f]*'
        ),
        artifact_json TEXT NOT NULL CHECK (json_valid(artifact_json)),
        artifact_hash TEXT NOT NULL CHECK (
            length(artifact_hash)=64 AND artifact_hash NOT GLOB '*[^0-9a-f]*'
        ),
        proof_json TEXT NOT NULL CHECK (json_valid(proof_json)),
        proof_hash TEXT NOT NULL CHECK (
            length(proof_hash)=64 AND proof_hash NOT GLOB '*[^0-9a-f]*'
        ),
        size_bytes INTEGER NOT NULL CHECK (size_bytes > 0),
        source_run_id TEXT,
        created_at TEXT NOT NULL,
        last_accessed_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        hit_count INTEGER NOT NULL DEFAULT 0 CHECK (hit_count >= 0),
        FOREIGN KEY (source_run_id) REFERENCES ai_runs(id) ON DELETE SET NULL
    );

    CREATE INDEX ai_cache_entries_expiry_idx
        ON ai_cache_entries(expires_at, last_accessed_at);
    CREATE INDEX ai_cache_entries_lru_idx
        ON ai_cache_entries(last_accessed_at, created_at, cache_key);
    CREATE INDEX ai_cache_entries_profile_idx
        ON ai_cache_entries(profile_id, last_accessed_at DESC);

    CREATE TABLE ai_request_flights (
        cache_key TEXT PRIMARY KEY CHECK (
            length(cache_key)=64 AND cache_key NOT GLOB '*[^0-9a-f]*'
        ),
        profile_id TEXT NOT NULL,
        owner_id TEXT NOT NULL,
        status TEXT NOT NULL CHECK (status IN ('running','complete','failed')),
        lease_expires_at TEXT NOT NULL,
        error_code TEXT,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );

    CREATE INDEX ai_request_flights_lease_idx
        ON ai_request_flights(status, lease_expires_at);
    """,
    23: """
    ALTER TABLE ai_runs ADD COLUMN provider_outcome TEXT CHECK (
        provider_outcome IS NULL OR provider_outcome IN (
            'not_called','succeeded','failed','mixed'
        )
    );
    ALTER TABLE ai_runs ADD COLUMN artifact_outcome TEXT CHECK (
        artifact_outcome IS NULL OR artifact_outcome IN (
            'valid','repaired','partial','invalid','not_applicable'
        )
    );
    ALTER TABLE ai_runs ADD COLUMN business_outcome TEXT CHECK (
        business_outcome IS NULL OR business_outcome IN (
            'complete','cache','hybrid','deterministic_fallback','partial','unavailable'
        )
    );
    ALTER TABLE ai_runs ADD COLUMN usable INTEGER CHECK (
        usable IS NULL OR usable IN (0,1)
    );
    ALTER TABLE ai_runs ADD COLUMN apply_ready INTEGER CHECK (
        apply_ready IS NULL OR apply_ready IN (0,1)
    );
    ALTER TABLE ai_runs ADD COLUMN degraded INTEGER CHECK (
        degraded IS NULL OR degraded IN (0,1)
    );
    ALTER TABLE ai_runs ADD COLUMN repair_attempts INTEGER NOT NULL DEFAULT 0
        CHECK (repair_attempts >= 0);

    ALTER TABLE ai_run_legs ADD COLUMN purpose TEXT NOT NULL DEFAULT 'legacy'
        CHECK (purpose IN (
            'initial','transport_retry','validation_repair','fallback','legacy'
        ));
    ALTER TABLE ai_run_legs ADD COLUMN validation_code TEXT;

    CREATE INDEX ai_runs_outcome_idx
        ON ai_runs(business_outcome,usable,degraded,created_at DESC);
    """,
    24: """
    CREATE TABLE ai_conversations_v24 (
        id TEXT PRIMARY KEY,
        attempt_id INTEGER,
        platform TEXT NOT NULL,
        problem_id TEXT NOT NULL,
        status TEXT NOT NULL DEFAULT 'active'
            CHECK (status IN ('active', 'closed')),
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL,
        closed_at TEXT,
        closed_reason TEXT
            CHECK (closed_reason IN ('user_cleared', 'attempt_closed', 'legacy')),
        superseded_by TEXT,
        provider_id TEXT,
        model TEXT,
        reasoning_strength TEXT CHECK (
            reasoning_strength IS NULL OR
            reasoning_strength IN ('auto', 'off', 'low', 'medium', 'high')
        ),
        provider_definition_hash TEXT,
        resolved_model TEXT,
        cache_session_key TEXT,
        route_policy_hash TEXT,
        FOREIGN KEY (attempt_id) REFERENCES attempts(id) ON DELETE CASCADE,
        FOREIGN KEY (platform, problem_id)
            REFERENCES problems(platform, problem_id) ON DELETE CASCADE
    );

    INSERT INTO ai_conversations_v24(
        id,attempt_id,platform,problem_id,status,created_at,updated_at,closed_at,
        closed_reason,superseded_by,provider_id,model,reasoning_strength,
        provider_definition_hash,resolved_model,cache_session_key,route_policy_hash
    )
    SELECT
        id,attempt_id,platform,problem_id,status,created_at,updated_at,closed_at,
        closed_reason,superseded_by,provider_id,model,reasoning_strength,
        provider_definition_hash,resolved_model,cache_session_key,route_policy_hash
      FROM ai_conversations;

    CREATE TABLE ai_runs_v24 (
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
        telemetry_json TEXT NOT NULL DEFAULT '{}',
        estimated_cost_json TEXT NOT NULL DEFAULT '{}',
        provider_id TEXT,
        profile_id TEXT,
        requested_model TEXT,
        resolved_model TEXT,
        provider_origin TEXT,
        credential_slot_id TEXT,
        fallback_json TEXT,
        cache_status TEXT,
        cache_read_tokens INTEGER,
        cache_write_tokens INTEGER,
        requested_reasoning_strength TEXT CHECK (
            requested_reasoning_strength IS NULL OR
            requested_reasoning_strength IN ('auto', 'off', 'low', 'medium', 'high')
        ),
        resolved_reasoning_strength TEXT CHECK (
            resolved_reasoning_strength IS NULL OR
            resolved_reasoning_strength IN ('auto', 'off', 'low', 'medium', 'high')
        ),
        resolved_provider_id TEXT,
        governance_json TEXT NOT NULL DEFAULT '{}',
        local_cache_status TEXT CHECK (
            local_cache_status IS NULL OR local_cache_status IN (
                'bypass','miss','hit','coalesced','refresh'
            )
        ),
        local_cache_key TEXT,
        cache_source_run_id TEXT,
        cache_validation_json TEXT NOT NULL DEFAULT '{}',
        provider_outcome TEXT CHECK (
            provider_outcome IS NULL OR provider_outcome IN (
                'not_called','succeeded','failed','mixed'
            )
        ),
        artifact_outcome TEXT CHECK (
            artifact_outcome IS NULL OR artifact_outcome IN (
                'valid','repaired','partial','invalid','not_applicable'
            )
        ),
        business_outcome TEXT CHECK (
            business_outcome IS NULL OR business_outcome IN (
                'complete','cache','hybrid','deterministic_fallback','partial','unavailable'
            )
        ),
        usable INTEGER CHECK (usable IS NULL OR usable IN (0,1)),
        apply_ready INTEGER CHECK (apply_ready IS NULL OR apply_ready IN (0,1)),
        degraded INTEGER CHECK (degraded IS NULL OR degraded IN (0,1)),
        repair_attempts INTEGER NOT NULL DEFAULT 0 CHECK (repair_attempts >= 0),
        FOREIGN KEY (conversation_id)
            REFERENCES ai_conversations(id) ON DELETE SET NULL,
        FOREIGN KEY (message_id) REFERENCES ai_messages(id) ON DELETE SET NULL
    );

    INSERT INTO ai_runs_v24(
        id,kind,model,conversation_id,message_id,request_summary_json,status,
        finish_reason,usage_json,error_json,created_at,completed_at,telemetry_json,
        estimated_cost_json,provider_id,profile_id,requested_model,resolved_model,
        provider_origin,credential_slot_id,fallback_json,cache_status,
        cache_read_tokens,cache_write_tokens,requested_reasoning_strength,
        resolved_reasoning_strength,resolved_provider_id,governance_json,
        local_cache_status,local_cache_key,cache_source_run_id,cache_validation_json,
        provider_outcome,artifact_outcome,business_outcome,usable,apply_ready,degraded,
        repair_attempts
    )
    SELECT
        id,kind,model,conversation_id,message_id,request_summary_json,status,
        finish_reason,usage_json,error_json,created_at,completed_at,telemetry_json,
        estimated_cost_json,provider_id,profile_id,requested_model,resolved_model,
        provider_origin,credential_slot_id,fallback_json,cache_status,
        cache_read_tokens,cache_write_tokens,requested_reasoning_strength,
        resolved_reasoning_strength,resolved_provider_id,governance_json,
        local_cache_status,local_cache_key,cache_source_run_id,cache_validation_json,
        provider_outcome,artifact_outcome,business_outcome,usable,apply_ready,degraded,
        repair_attempts
      FROM ai_runs;

    DROP TABLE ai_runs;
    DROP TABLE ai_conversations;
    ALTER TABLE ai_conversations_v24 RENAME TO ai_conversations;
    ALTER TABLE ai_runs_v24 RENAME TO ai_runs;

    CREATE UNIQUE INDEX ai_conversations_active_attempt_idx
        ON ai_conversations(attempt_id)
        WHERE attempt_id IS NOT NULL AND status='active';
    CREATE INDEX ai_conversations_problem_idx
        ON ai_conversations(platform, problem_id, updated_at DESC);
    CREATE INDEX ai_conversations_attempt_summary_idx
        ON ai_conversations(attempt_id, closed_reason, updated_at DESC);
    CREATE INDEX ai_runs_recent_idx ON ai_runs(created_at DESC, id);
    CREATE INDEX ai_runs_conversation_idx
        ON ai_runs(conversation_id, created_at DESC);
    CREATE INDEX ai_runs_local_cache_idx
        ON ai_runs(profile_id, local_cache_status, created_at DESC);
    CREATE INDEX ai_runs_outcome_idx
        ON ai_runs(business_outcome,usable,degraded,created_at DESC);
    """,
}
