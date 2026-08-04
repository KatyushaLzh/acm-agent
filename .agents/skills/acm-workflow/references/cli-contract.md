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

## Plan Tag Rules

- Tag completion is a two-step operation: preview first, apply only after confirmation.
- Preview output carries `base_revision` plus per-task `current_tags`, `suggested_tags`, `source`, and partial lookup errors. Applying must use that exact revision.
- The default mode only fills empty tag lists. Non-empty manual tags are authoritative unless the user explicitly requests overwrite.
- Codeforces and Luogu tags are platform metadata; Agent-generated tags must be identified as such and confirmed by the user.
- The API equivalents are `POST /api/jobs/plans/tags/preview` and `POST /api/plans/tags/apply`. The serverless fallback is `plan tags preview/apply --json`.
- Applying tags updates the managed plan revision. It does not update problem acceptance, attempts, sessions, or review state.
