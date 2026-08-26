# Structured Interface Contract

The local HTTP API and CLI JSON output share these semantics. Prefer the API while a healthy dashboard runtime exists; use CLI JSON as the compatibility fallback.

AI cost audits expose `provider_route_fallbacks` and `business_fallbacks` separately. Legacy `route_fallbacks` and recent-run `fallback_count` are provider-route compatibility aliases; deterministic/hybrid local degradation is counted only in the business fields. Provider fallback legs rebind model and reasoning controls from the selected leg before adapter invocation.

## Statuses

- `unknown`: no reliable platform or local evidence.
- `not_started`: known catalog or plan problem without an attempt.
- `local_only`: a matching source file exists but acceptance is unconfirmed.
- `attempted`: a platform non-AC submission or local session exists.
- `skipped`: the user explicitly marked the problem as mastered without implementation; this is reversible and is not AC.
- `accepted`: platform AC or explicit local `close --result ac`.
- `review_due`: an overlay used for scheduled closed-book review.

Never downgrade accepted state after a failed or partial sync.

Status precedence is `accepted > skipped > attempted > local_only > not_started > unknown`.

## Skip Fields

- Canonical disposition: `skipped_mastered`.
- Canonical reason: `idea_clear_without_editorial`.
- Canonical sources: `web`, `cli`, `agent`.
- Skip counts toward plan completion and progressive stage unlocks, but does not satisfy an explicit AC replacement condition.
- Skip creates no attempt, session, source file, hint level, failure mode, archive candidate, or review date.
- HTTP equivalents are `POST /api/problems/skip`, `POST /api/problems/unskip`, and `GET /api/problems/skipped`; CLI equivalents are `skip`, `unskip`, and `skipped --json`.

## Close Fields

- Canonical API/CLI result values: `AC`, `WA`, `TLE`, `RE`, `MLE`, `ABANDONED`. Service input is normalized to uppercase, but Agents should emit these canonical values.
- Hint levels: `0` independent, `1` counterexample question, `2` property hint, `3` transformation or pseudocode, `4` full solution/code.
- Failure modes: `none`, `modeling`, `invariant`, `implementation`, `edge_case`, `complexity`, `selection`, `editorial`.

## Agent JSON Rules

- Treat `fresh` as current and `stale` as an old successful snapshot. `failed` means the latest refresh failed; inspect `last_success_at` before deciding whether a last-good snapshot remains usable.
- Display `reasons` and score components for every recommendation.
- Do not silently continue when both platform state and cache are unavailable.
- Keep stdout JSON machine-readable; user-facing explanations belong in the Agent response.

## Initialization and Tag Completion

- A validated CLI `init` saves both account identifiers and waits for synchronization; `POST /api/setup` saves them and returns to the Dashboard while the same work runs as a recoverable background job. Fresh global catalogs are reused instead of being force-refetched.
- Luogu public AC state is committed before catalog maintenance. A complete catalog replaces the cached snapshot only after every requested page passes its structure guards; page failure preserves the previous snapshot and reports `partial`.
- Luogu tag enrichment uses bounded concurrency and persisted exponential retry backoff. Recent transport or schema failures are reported as `deferred`; a valid public page with an empty tag list is persisted as `tagless` and does not make the sync partial. A failed full-catalog crawl caps fallback tag lookups instead of attempting the whole accepted set.
- Sync jobs publish structured `phase`, `platform`, `step`, `total`, `completed`, `failed`, `started_at`, `last_activity_at`, and `usable` progress. Treat the per-platform result status, not the job executor status, as the business outcome.
- `--skip-validate` / `skip_validate=true` is an offline path: it saves configuration but performs no platform sync or tag request.
- AI recommendations only consume cached tags and never trigger tag scraping.

## AI Provider Rules

- `GET /api/ai/status` returns credential detection/source/persisted state, sanitized load errors, a non-sensitive `secure_store` diagnosis, and sanitized provider/connection/profile/credential-slot/governance metadata; it never returns a key.
- Synchronous loopback-token-protected credential and connection endpoints may accept a key in the request body. Bound secrets persist only through current-user Windows DPAPI, macOS Keychain, or Linux Secret Service. The service never auto-selects a file keyring or writes a plaintext/weakly encrypted fallback. Browser code clears the password input after every attempt and never uses browser storage.
- An unavailable secure store returns `credential_store_unavailable` with HTTP 503; a locked store returns `credential_store_locked` with HTTP 409. The Dashboard remains usable, and an explicit environment variable may supply a process-local credential, but corrupted or binding-mismatched stored credentials remain fail-closed.
- The built-in DeepSeek provider is restricted to `deepseek-v4-flash` and `deepseek-v4-pro`. Managed OpenAI-compatible connections may use only discovered models with current capability evidence. Each task profile owns its route and `reasoning_strength` (`auto`, `off`, `low`, `medium`, or `high`); `auto` omits provider reasoning controls, while `off` explicitly disables them.
- AI recommendation mode is `gap_fill` (low distinct-AC topic coverage) or `specialization` (high coverage), defaulting to `gap_fill`. Items retain deterministic `score`, `breakdown`, and `reasons`, and add `focus_topic`, `ranking_basis`, `ai_reason`, `training_focus`, `ai_run_id`, and `ai_usage`. Responses expose `ai.mode`, `ai.focus_topics`, `ai.submission_coverage`, and `ai.taxonomy_version`; provider/protocol/candidate failures may return a same-mode hybrid/deterministic fallback only after the complete local business validator passes, otherwise the result is structured `unavailable` with `ok=false`.
- Recommendation payloads contain only classified distinct platform-AC summaries and deterministic candidates. They exclude account fields, handles, UIDs, submission IDs, raw JSON, languages, notes, chats, source code, local paths, API keys, and runtime tokens.
- Coaching conversation messages persist locally. SSE event names are exactly `meta`, `delta`, `usage`, `done`, and `error`; a disconnected partial answer is stored as `interrupted`.
- `review` and every patch proposal count as hint level 4. `close` stores the maximum of explicit input and persisted AI assistant history.
- A patch proposal is a complete replacement guarded by a baseline hash. `apply` and `revert` conflicts are HTTP 409 and must never overwrite a newer local edit.

## Local Stress Rules

- Finite local stress accepts explicitly selected existing generator/reference/user `.cpp` files; without explicit selections it requires hand-written `<problem>.bf.cpp` and `<problem>.gen.cpp` beside the managed source.
- `verify --stress-iterations <N> --seed <seed>` controls the bounded case count and deterministic starting seed. It does not generate helpers or persist resumable runs.
- A mismatch publishes the latest `.stress.in`, `.reference.out`, and `.user.out` beside the source. Generator/runtime/output-limit diagnostics and replay metadata stay under `.acm/failures/`; a source-adjacent publication failure also falls back there.

## Plan and Recommendation Rules

- Recommendation slots cycle by thirds as `current_plus_100`, `recent_solved_average`, and `target_rating`. Recent difficulty is the combined CF-equivalent arithmetic mean of up to 50 latest distinct solved problems per platform; responses expose the resolved targets and sample coverage in `difficulty_profile`.
- `source_mode` is `balanced`, `catalog_only`, or `plan_only`. Balanced mode is the ordinary union of catalog and plan candidates: plan membership, dates, levels, and source do not add score, win ties, or impose a plan quota.
- `plan_ids` optionally restricts recommendations to named enabled plans.
- CLI equivalents are `next --source-mode <mode> [--plan <plan-id> ...] --json` and `next --ai --ai-mode gap_fill|specialization --json`.
- A plan edit is an atomic full-document replacement guarded by `expected_revision`. HTTP `409 revision_conflict` means the caller must reload before retrying.
- Removing a task or plan preserves all platform, attempt, session, review, and local-file state.
- Platform counts and ratios are read-only derived metadata computed from `stages[].tasks` (replacement alternatives are excluded). They must not be persisted in `plan.json` or sent as editable plan fields.
- Plan detail returns runtime `task_statuses` keyed by stable `task_key`; these fields are API metadata and must not be copied into `plan.json` on edit or export.
- Each task status keeps `judge_result`, `workflow_status`, and `skipped` distinct. An accepted verdict has permanent result priority; otherwise the latest known Codeforces submission or local closed session supplies the result. An active session may report `ACTIVE`.
- Luogu anonymous synchronization proves public AC only. Non-AC Luogu results exist in task status only when recorded by a local session.

## Markdown Knowledge Rules

- Closing an attempt and generating a Markdown summary are separate operations. A summary failure never changes the closed attempt.
- A target is one registered absolute local `.md` path plus a revisioned `summary-schema-v1`. Existing root `algorithms.md` and `tricks.md` are automatically registered with their fixed schemas; unregistering any target never deletes the file.
- Preview sends only the closed attempt context and necessary schema shape to DeepSeek. Cleared conversations, accounts, API keys, local paths, reasoning content, and the full target file are excluded. One old card may additionally be sent only when its normalized `Source` problem ID exactly matches, so the model can merge it with the new evidence.
- Proposal status is `preview`, `applying`, `applied`, `conflict`, `failed`, `reverting`, or `reverted`. Only the latest `preview` revision is applyable. Confidence below 0.75 shows the soft warning `模型置信度低，需人工核对` but does not prevent editing, refreshing, or applying. Exact Source matches are AI-merged automatically; title-only or fuzzy similarity creates a new card without a choice step.
- Editing `entry_markdown` requires `knowledge refresh`; apply never accepts replacement candidate bytes from the client.
- Apply and revert are guarded by target/schema/proposal revisions and SHA-256. HTTP 409 means reload or preview again; never overwrite the external edit.

## Plan Tag Rules

- Tag completion is a two-step operation: preview first, apply only after confirmation.
- Preview output carries `base_revision` plus per-task `current_tags`, `suggested_tags`, `source`, and partial lookup errors. Applying must use that exact revision.
- The default mode only fills empty tag lists. Non-empty manual tags are authoritative unless the user explicitly requests overwrite.
- Codeforces and Luogu tags are platform metadata; Agent-generated tags must be identified as such and confirmed by the user.
- The API equivalents are `POST /api/jobs/plans/tags/preview` and `POST /api/plans/tags/apply`. The serverless fallback is `plan tags preview/apply --json`.
- Applying tags updates the managed plan revision. It does not update problem acceptance, attempts, sessions, or review state.
