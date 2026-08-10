# Structured Interface Contract

The local HTTP API and CLI JSON output share these semantics. Prefer the API while a healthy dashboard runtime exists; use CLI JSON as the compatibility fallback.

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

## DeepSeek AI Rules

- `GET /api/ai/status` returns only detection, source (`secure_store`, `environment`, `injected`, or `none`), persisted state, and a sanitized load error; it never returns the key.
- `POST /api/ai/credential` is the only endpoint that accepts a key. It is synchronous, loopback-token protected, excluded from the job registry, and persists only a Windows current-user DPAPI ciphertext. `{ "clear": true }` deletes that ciphertext. Browser code must clear the password input after every attempt and must not use browser storage.
- Models are restricted to `deepseek-v4-flash` and `deepseek-v4-pro`. Recommendation requests always disable thinking.
- AI recommendation items retain deterministic `score`, `breakdown`, and `reasons`, and add `ranking_basis`, `ai_reason`, `training_focus`, `ai_run_id`, and `ai_usage`. Provider/protocol/candidate failures return deterministic order plus `ai.fallback`.
- Coaching conversation messages persist locally. SSE event names are exactly `meta`, `delta`, `usage`, `done`, and `error`; a disconnected partial answer is stored as `interrupted`.
- `review` and every patch proposal count as hint level 4. `close` stores the maximum of explicit input and persisted AI assistant history.
- A patch proposal is a complete replacement guarded by a baseline hash. `apply` and `revert` conflicts are HTTP 409 and must never overwrite a newer local edit.

## AI Stress Rules

- AI stress is explicit opt-in. The preparation payload contains statement/title/problem ID and a deterministic stress contract, but never account fields, API keys, local paths, conversation history, notes, or the user's solution source.
- Source tiers are platform-specific: Codeforces uses official editorial, CNBlogs, then CSDN; Luogu uses CNBlogs, Luogu public solution pages, then CSDN. DeepSeek independently generates any still-missing reference slot. The local allowlisted crawler filters exact problem IDs deterministically, and fetched candidates must provide two distinct URLs and source hashes. Ordinary submissions and authenticated/challenge content are out of scope.
- New bundle artifact kinds are `generator`, `reference_primary`, `reference_secondary`, and optional `validator`; legacy `brute`/`reference` artifacts remain read-only compatibility data. Existing helpers are recorded as `local_existing`; remote provenance records URL, title, content hash, and license when known. Every generated or downloaded reference must satisfy full constraints and pass the applicable safety, compile, sample, audit, and preflight gates before application.
- Compatible static contracts first compile locally to `generator_recipe/v2`, currently for mutable-permutation operation streams and bracket-string interval queries. This path makes no provider request for a recipe or executable generator and fails closed on local preflight failure. Unsupported shapes may use `generator_recipe/v1` and then the explicit legacy AI C++ fallback.
- Full and strict preparation use bounded AI audit plus local debug preflight. Dashboard Minimal skips validator and AI audit and relaxes generic manifest/non-recipe coverage, but local recipe coverage and the 16-case seed/output-variation gate still run. A failure returns structured `artifact`, `profile`, `case_kind`, and `seed` when available, leaves previous helper hashes unchanged, and creates neither an applied bundle nor a stress run.
- Run status is `pending`, `preparing`, `running`, `stop_requested`, `stopped`, `mismatch`, `oracle_conflict`, `fault`, `interrupted`, or `completed`. A restart changes only runs whose owner process is no longer alive to `interrupted`; resume accepts stopped/interrupted/mismatch/oracle-conflict/fault states, reuses and recompiles the applied helpers without an AI call, and continues from persisted `next_seed` with accumulated counters.
- Profile-v2 runs samples, an exact lower-bound small case, an optional exact upper-bound large case, then a persistent 4:1 small/large cycle. Every profile executes solution, ref1, and ref2; strict mode validates each generated input first. Only `ref1 == ref2 != solution` is a confirmed mismatch, while `ref1 != ref2` is always `oracle_conflict`. Small cases use the normal 2 MiB limits; large cases use 32 MiB input and 16 MiB output limits. The latest mismatch or oracle conflict is atomically exported beside the solution as `<problem>_input.in`, `<problem>_current.out`, `<problem>_ref1.out`, and `<problem>_ref2.out`. History remains under `.acm/failures/`.
- HTTP endpoints are `GET /api/ai/stress/status`, `POST /api/jobs/ai/stress/start`, `GET /api/stress/runs[/{id}]`, `POST /api/stress/runs/{id}/stop|resume|finish`, `GET /api/stress/bundles/{id}`, and `POST /api/jobs/stress/bundles/{id}/revert`. `stop` is a resumable pause; `finish` waits for the active process tree to stop, then permanently completes the run and releases the single-active-run lock. The start payload uses `large_profile` (default true); removed profile fields are rejected rather than translated. CLI equivalents are `verify --ai-stress [--no-large]` and `stress status/stop/resume/artifacts/revert`.

## Plan and Recommendation Rules

- `source_mode` is `balanced`, `catalog_only`, or `plan_only`. Balanced mode caps plan tasks at `ceil(2 * count / 3)` and does not silently exceed the cap when the catalog is short.
- `plan_ids` optionally restricts recommendations to named enabled plans.
- CLI equivalents are `next --source-mode <mode> [--plan <plan-id> ...] --json`.
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
- Proposal status is `preview`, `applying`, `applied`, `conflict`, `failed`, `reverting`, or `reverted`. Only the latest `preview` revision with confidence at least 0.75 is applyable. Exact Source matches are AI-merged automatically; title-only or fuzzy similarity creates a new card without a choice step.
- Editing `entry_markdown` requires `knowledge refresh`; apply never accepts replacement candidate bytes from the client.
- Apply and revert are guarded by target/schema/proposal revisions and SHA-256. HTTP 409 means reload or preview again; never overwrite the external edit.

## Plan Tag Rules

- Tag completion is a two-step operation: preview first, apply only after confirmation.
- Preview output carries `base_revision` plus per-task `current_tags`, `suggested_tags`, `source`, and partial lookup errors. Applying must use that exact revision.
- The default mode only fills empty tag lists. Non-empty manual tags are authoritative unless the user explicitly requests overwrite.
- Codeforces and Luogu tags are platform metadata; Agent-generated tags must be identified as such and confirmed by the user.
- The API equivalents are `POST /api/jobs/plans/tags/preview` and `POST /api/plans/tags/apply`. The serverless fallback is `plan tags preview/apply --json`.
- Applying tags updates the managed plan revision. It does not update problem acceptance, attempts, sessions, or review state.
