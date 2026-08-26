"""AI conversation, message, run, and patch proposal persistence."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .ai_cache import (
    DEFAULT_CACHE_MAX_ENTRY_BYTES,
    DEFAULT_FLIGHT_FAILURE_COOLDOWN_SECONDS,
    DEFAULT_FLIGHT_LEASE_SECONDS,
    DEFAULT_FLIGHT_WAIT_TIMEOUT_SECONDS,
    EXACT_CACHE_PROFILES,
    CacheIntegrityError,
    CacheValidation,
    encode_cache_artifact,
    validate_cached_artifact,
)
from .ai_reliability import validate_ai_outcome
from .ai_telemetry import estimate_cost, load_price_catalog, price_catalog_hash
from .storage_common import (
    _UNSET,
    _json,
    _merge_json_objects,
    utc_now,
)
from .usage import normalize_usage


_REASONING_STRENGTHS = frozenset({"auto", "off", "low", "medium", "high"})
_LOCAL_CACHE_STATUSES = frozenset({"bypass", "miss", "hit", "coalesced", "refresh"})


def _cache_stamp(value: str | datetime | None = None) -> tuple[str, datetime]:
    if value is None:
        stamp = datetime.now(timezone.utc)
    elif isinstance(value, datetime):
        stamp = value
    else:
        stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    stamp = stamp.astimezone(timezone.utc).replace(microsecond=0)
    return stamp.isoformat(), stamp


def _cache_status(value: Any, *, allow_none: bool = True) -> str | None:
    if value is None and allow_none:
        return None
    selected = str(value or "").strip().lower()
    if selected not in _LOCAL_CACHE_STATUSES:
        raise ValueError("local cache status is invalid")
    return selected


def _route_identity(value: Any) -> tuple[str, str, str] | None:
    if not isinstance(value, Mapping):
        return None
    provider_id = str(value.get("provider_id") or "").strip()
    model = str(value.get("resolved_model") or value.get("model") or "").strip()
    reasoning_strength = str(value.get("reasoning_strength") or "").strip()
    if not provider_id and not model:
        return None
    return provider_id, model, reasoning_strength


def _provider_route_fallback_count(
    governance: Mapping[str, Any], legs: Sequence[Mapping[str, Any]]
) -> int:
    """Count route transitions without confusing retries or business fallbacks."""

    route_events = governance.get("fallbacks")
    event_count = 0
    if isinstance(route_events, list):
        for event in route_events:
            if not isinstance(event, Mapping):
                continue
            source = _route_identity(event.get("from"))
            target = _route_identity(event.get("to"))
            if source is not None and target is not None and source != target:
                event_count += 1

    leg_count = 0
    previous: tuple[str, str, str] | None = None
    for leg in legs:
        identity = _route_identity(leg)
        if (
            str(leg.get("route_kind") or "") == "fallback"
            and identity is not None
            and identity != previous
        ):
            leg_count += 1
        if identity is not None:
            previous = identity
    return max(event_count, leg_count)


def _business_fallback_count(fallback: Mapping[str, Any]) -> int:
    """Count local business degradation events, never provider route events."""

    events = fallback.get("events")
    event_count = 0
    if isinstance(events, list):
        event_count = sum(
            1
            for event in events
            if isinstance(event, Mapping) and str(event.get("target") or "").strip()
        )
    if event_count:
        return event_count
    return int(str(fallback.get("outcome") or "") in {"hybrid", "deterministic_fallback"})


def _reasoning_strength(value: Any, *, allow_none: bool = True) -> str | None:
    if value is None and allow_none:
        return None
    selected = str(value or "").strip().lower()
    if selected not in _REASONING_STRENGTHS:
        raise ValueError("reasoning strength must be auto, off, low, medium, or high")
    return selected


class _AiStorageMixin:
    def reconcile_interrupted_ai_state(self) -> None:
        """Close AI state left in-flight by a previous process."""

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

    def get_or_create_ai_conversation(
        self,
        conversation_id: str,
        attempt_id: int,
        platform: str,
        problem_id: str,
        *,
        provider_id: str | None = None,
        model: str | None = None,
        reasoning_strength: str | None = None,
        provider_definition_hash: str | None = None,
        resolved_model: str | None = None,
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

        route = self._normalize_ai_conversation_route(
            provider_id=provider_id,
            model=model,
            reasoning_strength=reasoning_strength,
            provider_definition_hash=provider_definition_hash,
        )
        existing = self.active_ai_conversation(attempt_id)
        if existing is not None:
            return self._bind_ai_conversation_route(existing, **route), False
        now = utc_now()
        try:
            self.connection.execute(
                """INSERT INTO ai_conversations(
                       id,attempt_id,platform,problem_id,status,created_at,updated_at,
                       provider_id,model,reasoning_strength,provider_definition_hash,
                       resolved_model,cache_session_key)
                   VALUES(?,?,?,?,'active',?,?,?,?,?,?,?,?)""",
                (
                    str(conversation_id), attempt_id, platform, problem_id, now, now,
                    route["provider_id"], route["model"], route["reasoning_strength"],
                    route["provider_definition_hash"],
                    str(resolved_model).strip() if resolved_model is not None else None,
                    hashlib.sha256(str(conversation_id).encode("utf-8")).hexdigest()[:32],
                ),
            )
        except sqlite3.IntegrityError:
            # A concurrent creator may have won the partial unique index.
            existing = self.active_ai_conversation(attempt_id)
            if existing is None:
                raise
            return self._bind_ai_conversation_route(existing, **route), False
        row = self.ai_conversation(str(conversation_id))
        assert row is not None
        return row, True

    @staticmethod
    def _normalize_ai_conversation_route(
        *,
        provider_id: str | None,
        model: str | None,
        reasoning_strength: str | None,
        provider_definition_hash: str | None,
    ) -> dict[str, str | None]:
        def optional_text(value: str | None, label: str) -> str | None:
            if value is None:
                return None
            selected = str(value).strip()
            if not selected:
                raise ValueError(f"{label} must not be empty")
            return selected

        return {
            "provider_id": optional_text(provider_id, "provider_id"),
            "model": optional_text(model, "model"),
            "reasoning_strength": _reasoning_strength(reasoning_strength),
            "provider_definition_hash": optional_text(
                provider_definition_hash, "provider_definition_hash"
            ),
        }

    def _bind_ai_conversation_route(
        self,
        conversation: sqlite3.Row,
        *,
        provider_id: str | None,
        model: str | None,
        reasoning_strength: str | None,
        provider_definition_hash: str | None,
    ) -> sqlite3.Row:
        """Bind an unbound legacy conversation once and reject later route changes."""

        route = {
            "provider_id": provider_id,
            "model": model,
            "reasoning_strength": reasoning_strength,
            "provider_definition_hash": provider_definition_hash,
        }
        with self.atomic():
            current_row = self.ai_conversation(conversation["id"])
            if current_row is None:
                raise KeyError(f"AI conversation {conversation['id']!r} not found")
            assignments: list[str] = []
            values: list[Any] = []
            for column, requested in route.items():
                if requested is None:
                    continue
                current = current_row[column]
                if current is not None and current != requested:
                    raise ValueError(
                        f"AI conversation route is already bound to another {column}"
                    )
                if current is None:
                    assignments.append(f"{column}=?")
                    values.append(requested)
            if current_row["cache_session_key"] is None and any(
                value is not None for value in route.values()
            ):
                assignments.append("cache_session_key=?")
                values.append(
                    hashlib.sha256(str(conversation["id"]).encode("utf-8")).hexdigest()[:32]
                )
            if assignments:
                assignments.append("updated_at=?")
                values.append(utc_now())
                values.append(conversation["id"])
                self.connection.execute(
                    f"UPDATE ai_conversations SET {','.join(assignments)} WHERE id=?",
                    values,
                )
            refreshed = self.ai_conversation(conversation["id"])
            assert refreshed is not None
            return refreshed

    def bind_ai_conversation_route(
        self,
        conversation_id: str,
        *,
        provider_id: str | None = None,
        model: str | None = None,
        reasoning_strength: str | None = None,
        provider_definition_hash: str | None = None,
    ) -> sqlite3.Row:
        """Pin routing metadata on a legacy/unbound conversation exactly once."""

        conversation = self.ai_conversation(str(conversation_id))
        if conversation is None:
            raise KeyError(f"AI conversation {conversation_id!r} not found")
        route = self._normalize_ai_conversation_route(
            provider_id=provider_id,
            model=model,
            reasoning_strength=reasoning_strength,
            provider_definition_hash=provider_definition_hash,
        )
        return self._bind_ai_conversation_route(conversation, **route)

    def bind_ai_conversation_resolved_model(
        self, conversation_id: str, resolved_model: str
    ) -> sqlite3.Row:
        """Pin the provider's first actual model and reject later drift."""

        selected = str(resolved_model or "").strip()
        if not selected:
            raise ValueError("resolved_model must not be empty")
        with self.atomic():
            row = self.ai_conversation(str(conversation_id))
            if row is None:
                raise KeyError(f"AI conversation {conversation_id!r} not found")
            current = row["resolved_model"]
            if current is not None and str(current) != selected:
                raise ValueError("AI conversation resolved model changed")
            if current is None:
                self.connection.execute(
                    "UPDATE ai_conversations SET resolved_model=?,updated_at=? WHERE id=?",
                    (selected, utc_now(), str(conversation_id)),
                )
            refreshed = self.ai_conversation(str(conversation_id))
            assert refreshed is not None
            return refreshed

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
                _json(normalize_usage(usage or {})),
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
            values.append(_json(normalize_usage(usage)))
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
        provider_id: str | None = None,
        profile_id: str | None = None,
        requested_model: str | None = None,
        provider_origin: str | None = None,
        credential_slot_id: str | None = None,
        requested_reasoning_strength: str | None = None,
        resolved_reasoning_strength: str | None = None,
        local_cache_status: str | None = None,
        local_cache_key: str | None = None,
        cache_source_run_id: str | None = None,
        cache_validation: Mapping[str, Any] | None = None,
        outcome: Mapping[str, Any] | None = None,
    ) -> sqlite3.Row:
        requested_strength = _reasoning_strength(requested_reasoning_strength)
        resolved_strength = _reasoning_strength(resolved_reasoning_strength)
        selected_outcome = validate_ai_outcome(outcome) if outcome is not None else None
        self.connection.execute(
            """INSERT INTO ai_runs(
                   id,kind,model,conversation_id,message_id,
                   request_summary_json,status,created_at,provider_id,profile_id,
                   requested_model,provider_origin,credential_slot_id,
                   requested_reasoning_strength,resolved_reasoning_strength,
                   local_cache_status,local_cache_key,cache_source_run_id,
                   cache_validation_json,provider_outcome,artifact_outcome,
                   business_outcome,usable,apply_ready,degraded,repair_attempts)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                str(run_id),
                kind,
                model,
                conversation_id,
                message_id,
                _json(request_summary or {}),
                status,
                created_at or utc_now(),
                provider_id,
                profile_id,
                requested_model or model,
                provider_origin,
                credential_slot_id,
                requested_strength,
                resolved_strength,
                _cache_status(local_cache_status),
                str(local_cache_key) if local_cache_key is not None else None,
                str(cache_source_run_id) if cache_source_run_id is not None else None,
                _json(cache_validation or {}),
                selected_outcome["provider_outcome"] if selected_outcome else None,
                selected_outcome["artifact_outcome"] if selected_outcome else None,
                selected_outcome["business_outcome"] if selected_outcome else None,
                int(selected_outcome["usable"]) if selected_outcome else None,
                int(selected_outcome["apply_ready"]) if selected_outcome else None,
                int(selected_outcome["degraded"]) if selected_outcome else None,
                selected_outcome["repair_attempts"] if selected_outcome else 0,
            ),
        )
        row = self.ai_run(str(run_id))
        assert row is not None
        return row

    def update_ai_run(
        self,
        run_id: str,
        *,
        request_summary: Mapping[str, Any] | object = _UNSET,
        status: str | object = _UNSET,
        finish_reason: str | None | object = _UNSET,
        usage: Mapping[str, Any] | object = _UNSET,
        telemetry: Mapping[str, Any] | object = _UNSET,
        estimated_cost: Mapping[str, Any] | object = _UNSET,
        error: Mapping[str, Any] | object = _UNSET,
        completed_at: str | None | object = _UNSET,
        resolved_model: str | None | object = _UNSET,
        resolved_provider_id: str | None | object = _UNSET,
        fallback: Mapping[str, Any] | None | object = _UNSET,
        governance: Mapping[str, Any] | None | object = _UNSET,
        cache_status: str | None | object = _UNSET,
        requested_reasoning_strength: str | None | object = _UNSET,
        resolved_reasoning_strength: str | None | object = _UNSET,
        local_cache_status: str | None | object = _UNSET,
        local_cache_key: str | None | object = _UNSET,
        cache_source_run_id: str | None | object = _UNSET,
        cache_validation: Mapping[str, Any] | object = _UNSET,
        outcome: Mapping[str, Any] | object = _UNSET,
    ) -> sqlite3.Row:
        assignments: list[str] = []
        values: list[Any] = []
        if request_summary is not _UNSET:
            assignments.append("request_summary_json=?")
            values.append(_json(request_summary))
        for column, value in (
            ("status", status),
            ("finish_reason", finish_reason),
            ("completed_at", completed_at),
            ("resolved_model", resolved_model),
            ("resolved_provider_id", resolved_provider_id),
            ("cache_status", cache_status),
            ("local_cache_key", local_cache_key),
            ("cache_source_run_id", cache_source_run_id),
        ):
            if value is not _UNSET:
                assignments.append(f"{column}=?")
                values.append(value)
        if local_cache_status is not _UNSET:
            assignments.append("local_cache_status=?")
            values.append(_cache_status(local_cache_status))
        if cache_validation is not _UNSET:
            assignments.append("cache_validation_json=?")
            values.append(_json(cache_validation))
        if outcome is not _UNSET:
            selected_outcome = validate_ai_outcome(outcome)
            for column in (
                "provider_outcome", "artifact_outcome", "business_outcome"
            ):
                assignments.append(f"{column}=?")
                values.append(selected_outcome[column])
            for column in ("usable", "apply_ready", "degraded"):
                assignments.append(f"{column}=?")
                values.append(int(selected_outcome[column]))
            assignments.append("repair_attempts=?")
            values.append(selected_outcome["repair_attempts"])
        for column, value in (
            ("requested_reasoning_strength", requested_reasoning_strength),
            ("resolved_reasoning_strength", resolved_reasoning_strength),
        ):
            if value is not _UNSET:
                assignments.append(f"{column}=?")
                values.append(_reasoning_strength(value))
        normalized_usage: Mapping[str, Any] | object = _UNSET
        if usage is not _UNSET:
            normalized_usage = normalize_usage(usage)
            assignments.append("usage_json=?")
            values.append(_json(normalized_usage))
        if telemetry is not _UNSET:
            assignments.append("telemetry_json=?")
            values.append(_json(telemetry))
        selected_governance = governance if isinstance(governance, Mapping) else None
        if estimated_cost is _UNSET and normalized_usage is not _UNSET:
            current = self.ai_run(str(run_id))
            if current is not None:
                selected_provider = (
                    resolved_provider_id if resolved_provider_id is not _UNSET
                    else current["resolved_provider_id"] or current["provider_id"]
                )
                selected_model = (
                    resolved_model if resolved_model is not _UNSET
                    else current["resolved_model"] or current["requested_model"] or current["model"]
                )
                estimated_cost = self._estimate_ai_run_cost(
                    provider_id=str(selected_provider or "") or None,
                    model=str(selected_model or current["model"]),
                    usage=normalized_usage,
                    created_at=str(current["created_at"] or "") or None,
                    governance=selected_governance,
                )
        if estimated_cost is not _UNSET:
            assignments.append("estimated_cost_json=?")
            values.append(_json(estimated_cost))
        if fallback is not _UNSET:
            assignments.append("fallback_json=?")
            values.append(_json(fallback or {}))
        if governance is not _UNSET:
            assignments.append("governance_json=?")
            values.append(_json(governance or {}))
        if normalized_usage is not _UNSET:
            cache_read = normalized_usage.get("cache_read_tokens")
            cache_write = normalized_usage.get("cache_write_tokens")
            if cache_status is _UNSET and isinstance(cache_read, (int, float)) and not isinstance(cache_read, bool):
                assignments.append("cache_status=?")
                values.append("hit" if cache_read > 0 else "miss")
            if isinstance(cache_read, (int, float)) and not isinstance(cache_read, bool):
                assignments.append("cache_read_tokens=?")
                values.append(int(cache_read))
            if isinstance(cache_write, (int, float)) and not isinstance(cache_write, bool):
                assignments.append("cache_write_tokens=?")
                values.append(int(cache_write))
        if error is not _UNSET:
            assignments.append("error_json=?")
            values.append(_json(error))
        if not assignments:
            row = self.ai_run(str(run_id))
            if row is None:
                raise KeyError(f"AI run {run_id!r} not found")
            return row
        with self.atomic():
            values.append(str(run_id))
            cursor = self.connection.execute(
                f"UPDATE ai_runs SET {','.join(assignments)} WHERE id=?", values
            )
            if cursor.rowcount != 1:
                raise KeyError(f"AI run {run_id!r} not found")
            if selected_governance is not None:
                self._replace_ai_run_legs(str(run_id), selected_governance)
            if isinstance(estimated_cost, Mapping):
                self._record_ai_cost_estimate(
                    str(run_id), estimated_cost, basis="at_run_time"
                )
        row = self.ai_run(str(run_id))
        assert row is not None
        return row

    @staticmethod
    def _estimate_ai_run_cost(
        *,
        provider_id: str | None,
        model: str,
        usage: Mapping[str, Any],
        created_at: str | None,
        governance: Mapping[str, Any] | None,
        catalog: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        selected_catalog = dict(catalog or load_price_catalog())
        legs = governance.get("legs") if isinstance(governance, Mapping) else None
        if not isinstance(legs, list) or not legs:
            return estimate_cost(
                provider_id=(
                    provider_id
                    or ("deepseek" if model in {"deepseek-v4-flash", "deepseek-v4-pro"} else None)
                ),
                model=model,
                usage=usage,
                created_at=created_at,
                catalog=selected_catalog,
            )
        estimates: list[dict[str, Any]] = []
        known_amount = 0.0
        known_savings = 0.0
        unknown = 0
        for leg in legs:
            if not isinstance(leg, Mapping):
                unknown += 1
                continue
            estimate = estimate_cost(
                provider_id=str(leg.get("provider_id") or "") or None,
                model=str(leg.get("resolved_model") or leg.get("model") or ""),
                usage=dict(leg.get("usage") or {}),
                created_at=created_at,
                catalog=selected_catalog,
            )
            estimates.append(estimate)
            if estimate.get("status") == "known":
                known_amount += float(estimate["amount"])
                known_savings += float(estimate["cache_savings"])
            else:
                unknown += 1
        status = "known" if estimates and unknown == 0 else "partial" if known_amount else "unknown"
        return {
            "estimated": True,
            "status": status,
            "currency": str(selected_catalog.get("currency") or "CNY"),
            "price_version": selected_catalog.get("catalog_version"),
            "price_source": selected_catalog.get("source"),
            "catalog_sha256": price_catalog_hash(selected_catalog),
            "amount": round(known_amount, 12) if status != "unknown" else None,
            "amount_decimal": f"{known_amount:.12f}" if status != "unknown" else None,
            "cache_savings": round(known_savings, 12) if status != "unknown" else None,
            "cache_savings_decimal": f"{known_savings:.12f}" if status != "unknown" else None,
            "unknown_leg_count": unknown,
            "legs": estimates,
        }

    def _current_price_estimate(
        self,
        row: Mapping[str, Any],
        *,
        catalog: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Price a persisted run with the current catalog without rewriting history."""

        selected_catalog = dict(catalog or load_price_catalog())
        stored: dict[str, Any]
        try:
            stored = json.loads(str(row.get("estimated_cost_json") or "{}"))
        except json.JSONDecodeError:
            stored = {}
        if (
            stored.get("currency") == selected_catalog.get("currency")
            and stored.get("price_version") == selected_catalog.get("catalog_version")
            and stored.get("catalog_sha256") == price_catalog_hash(selected_catalog)
        ):
            return stored
        try:
            usage = json.loads(str(row.get("usage_json") or "{}"))
        except json.JSONDecodeError:
            usage = {}
        try:
            governance = json.loads(str(row.get("governance_json") or "{}"))
        except json.JSONDecodeError:
            governance = {}
        return self._estimate_ai_run_cost(
            model=str(
                row.get("resolved_model")
                or row.get("requested_model")
                or row.get("model")
                or ""
            ),
            provider_id=str(
                row.get("resolved_provider_id") or row.get("provider_id") or ""
            ) or None,
            usage=usage,
            created_at=str(row.get("created_at") or "") or None,
            governance=governance,
            catalog=selected_catalog,
        )

    def _replace_ai_run_legs(self, run_id: str, governance: Mapping[str, Any]) -> None:
        legs = governance.get("legs")
        if not isinstance(legs, list):
            return
        run = self.ai_run(run_id)
        if run is None:
            raise KeyError(f"AI run {run_id!r} not found")
        self.connection.execute("DELETE FROM ai_run_legs WHERE run_id=?", (run_id,))
        for index, raw in enumerate(legs):
            if not isinstance(raw, Mapping):
                continue
            usage = normalize_usage(dict(raw.get("usage") or {}))
            cache_read = usage.get("cache_read_tokens")
            cache_write = usage.get("cache_write_tokens")
            cache_status = (
                "hit" if isinstance(cache_read, (int, float)) and cache_read > 0
                else "miss" if isinstance(cache_read, (int, float)) and cache_read == 0
                else None
            )
            leg_cost = estimate_cost(
                provider_id=str(raw.get("provider_id") or "") or None,
                model=str(raw.get("resolved_model") or raw.get("model") or ""),
                usage=usage,
                created_at=str(run["created_at"] or "") or None,
            )
            provider_requests = usage.get("provider_requests")
            self.connection.execute(
                """INSERT INTO ai_run_legs(
                       run_id,ordinal,route_kind,provider_id,profile_id,
                       requested_model,resolved_model,reasoning_strength,status,error_code,
                       provider_requests,usage_json,cache_status,cache_read_tokens,
                       cache_write_tokens,estimated_cost_json,purpose,validation_code)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    run_id,
                    index,
                    str(raw.get("route_kind") or ("primary" if index == 0 else "fallback")),
                    str(raw.get("provider_id") or "") or None,
                    run["profile_id"],
                    str(raw.get("model") or "") or None,
                    str(raw.get("resolved_model") or "") or None,
                    str(raw.get("reasoning_strength") or "") or None,
                    str(raw.get("status") or "unknown"),
                    str(raw.get("error_code") or "") or None,
                    int(provider_requests) if isinstance(provider_requests, (int, float)) and not isinstance(provider_requests, bool) else None,
                    _json(usage),
                    cache_status,
                    int(cache_read) if isinstance(cache_read, (int, float)) and not isinstance(cache_read, bool) else None,
                    int(cache_write) if isinstance(cache_write, (int, float)) and not isinstance(cache_write, bool) else None,
                    _json(leg_cost),
                    str(raw.get("purpose") or "initial"),
                    str(raw.get("validation_code") or "")[:128] or None,
                ),
            )

    def _record_ai_cost_estimate(
        self, run_id: str, estimate: Mapping[str, Any], *, basis: str
    ) -> None:
        version = str(estimate.get("price_version") or "unknown")
        digest = str(estimate.get("catalog_sha256") or "unknown")
        status = str(estimate.get("status") or "unknown")
        if status not in {"known", "partial", "unknown"}:
            status = "unknown"
        conflicting = self.connection.execute(
            """SELECT 1 FROM ai_run_cost_estimates
               WHERE catalog_version=? AND catalog_sha256<>? LIMIT 1""",
            (version, digest),
        ).fetchone()
        if conflicting is not None:
            raise ValueError("price catalog version is already bound to a different SHA-256")
        self.connection.execute(
            """INSERT OR IGNORE INTO ai_run_cost_estimates(
                   run_id,catalog_version,catalog_sha256,basis,status,currency,
                   amount_decimal,cache_savings_decimal,estimate_json,computed_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                run_id, version, digest, basis, status,
                str(estimate.get("currency") or "") or None,
                str(estimate.get("amount_decimal")) if estimate.get("amount_decimal") is not None else None,
                str(estimate.get("cache_savings_decimal")) if estimate.get("cache_savings_decimal") is not None else None,
                _json(estimate), utc_now(),
            ),
        )

    def ai_run_legs(self, run_id: str) -> list[sqlite3.Row]:
        return self.query(
            "SELECT * FROM ai_run_legs WHERE run_id=? ORDER BY ordinal", (str(run_id),)
        )

    @staticmethod
    def _scoped_provider_cost(
        row: Mapping[str, Any],
        estimate: Mapping[str, Any],
        *,
        provider_id: str = "deepseek",
    ) -> dict[str, Any]:
        """Return one provider's immutable cost slice without pricing other providers."""

        selected_provider = str(provider_id)
        legs = estimate.get("legs")
        if isinstance(legs, list):
            scoped = [
                leg for leg in legs
                if isinstance(leg, Mapping)
                and str(leg.get("provider_id") or "") == selected_provider
            ]
            if not scoped:
                return {
                    "provider_id": selected_provider,
                    "currency": estimate.get("currency"),
                    "status": "out_of_scope",
                    "in_scope": False,
                    "amount": None,
                    "known_leg_count": 0,
                    "unknown_leg_count": 0,
                    "price_version": estimate.get("price_version"),
                }
            known_amount = 0.0
            known_legs = 0
            unknown_legs = 0
            unknown_reason = None
            for leg in scoped:
                if leg.get("status") == "known" and isinstance(
                    leg.get("amount"), (int, float)
                ) and not isinstance(leg.get("amount"), bool):
                    known_amount += float(leg["amount"])
                    known_legs += 1
                else:
                    unknown_legs += 1
                    if unknown_reason is None and leg.get("unknown_reason"):
                        unknown_reason = str(leg["unknown_reason"])
            status = (
                "known" if unknown_legs == 0
                else "partial" if known_legs else "unknown"
            )
            return {
                "provider_id": selected_provider,
                "currency": estimate.get("currency"),
                "status": status,
                "in_scope": True,
                "amount": round(known_amount, 12) if known_legs else None,
                "known_leg_count": known_legs,
                "unknown_leg_count": unknown_legs,
                "unknown_reason": unknown_reason,
                "price_version": estimate.get("price_version"),
            }

        row_provider = str(
            row.get("resolved_provider_id") or row.get("provider_id") or ""
        )
        estimate_provider = str(estimate.get("provider_id") or "")
        legacy_model = str(
            row.get("resolved_model")
            or row.get("requested_model")
            or row.get("model")
            or ""
        )
        in_scope = (
            estimate_provider == selected_provider
            or row_provider == selected_provider
            or (
                not estimate_provider
                and not row_provider
                and legacy_model in {"deepseek-v4-flash", "deepseek-v4-pro"}
            )
        )
        if not in_scope:
            return {
                "provider_id": selected_provider,
                "currency": estimate.get("currency"),
                "status": "out_of_scope",
                "in_scope": False,
                "amount": None,
                "known_leg_count": 0,
                "unknown_leg_count": 0,
                "price_version": estimate.get("price_version"),
            }
        status = str(estimate.get("status") or "unknown")
        known = status == "known" and isinstance(
            estimate.get("amount"), (int, float)
        ) and not isinstance(estimate.get("amount"), bool)
        return {
            "provider_id": selected_provider,
            "currency": estimate.get("currency"),
            "status": "known" if known else "unknown",
            "in_scope": True,
            "amount": round(float(estimate["amount"]), 12) if known else None,
            "known_leg_count": 1 if known else 0,
            "unknown_leg_count": 0 if known else 1,
            "unknown_reason": (
                str(estimate.get("unknown_reason"))
                if estimate.get("unknown_reason") else None
            ),
            "price_version": estimate.get("price_version"),
        }

    def ai_cost_spend(self, *, now: str | None = None) -> dict[str, Any]:
        stamp = str(now or utc_now())
        catalog = load_price_catalog()
        result: dict[str, Any] = {}
        for period, modifier in (("daily", "-1 day"), ("monthly", "-1 month")):
            rows = self.query(
                """SELECT provider_id,resolved_provider_id,model,requested_model,
                          resolved_model,estimated_cost_json,usage_json,
                          governance_json,created_at
                   FROM ai_runs
                   WHERE created_at>=datetime(?,?)""",
                (stamp, modifier),
            )
            known = 0.0
            unknown = 0
            scoped_runs = 0
            for row in rows:
                estimate = self._current_price_estimate(dict(row), catalog=catalog)
                scoped = self._scoped_provider_cost(dict(row), estimate)
                if not scoped["in_scope"]:
                    continue
                scoped_runs += 1
                if scoped.get("amount") is not None:
                    known += float(scoped["amount"])
                if scoped["status"] != "known":
                    unknown += 1
            result[period] = {
                "known_cny": round(known, 12),
                "currency": "CNY",
                "unknown_runs": unknown,
                "runs": scoped_runs,
                "provider_id": "deepseek",
            }
        return result

    def ai_cost_audit(self, *, days: int = 30, recent_limit: int = 20) -> dict[str, Any]:
        window = max(1, min(3650, int(days)))
        rows = self.query(
            """SELECT * FROM ai_runs
               WHERE created_at>=datetime('now',?)
               ORDER BY created_at DESC,id DESC""",
            (f"-{window} days",),
        )
        totals: dict[str, Any] = {
            "runs": len(rows),
            "known_estimated_cny": 0.0,
            "currency": "CNY",
            "unknown_cost_runs": 0,
            "partial_cost_runs": 0,
            "provider_requests_known": 0,
            "provider_requests_unknown_runs": 0,
            "input_tokens_known": 0,
            "output_tokens_known": 0,
            "cache_read_tokens_known": 0,
            "cache_read_unknown_runs": 0,
            # Compatibility alias: route_fallbacks has provider-route semantics.
            "route_fallbacks": 0,
            "provider_route_fallbacks": 0,
            "business_fallbacks": 0,
            "budget_blocks": 0,
        }
        deepseek_cost: dict[str, Any] = {
            "provider_id": "deepseek",
            "runs": 0,
            "known_estimated_cny": 0.0,
            "currency": "CNY",
            "unknown_cost_runs": 0,
            "partial_cost_runs": 0,
            "price_versions": [],
        }
        all_model_tokens: dict[str, Any] = {
            "runs": len(rows),
            "total_tokens_known": 0,
            "input_tokens_known": 0,
            "output_tokens_known": 0,
            "unknown_runs": 0,
        }
        cache_metrics: dict[str, Any] = {
            "cache_read_tokens_known": 0,
            "eligible_input_tokens": 0,
            "hit_rate_percent": None,
            "observed_runs": 0,
            "unknown_runs": 0,
            "invalid_runs": 0,
        }
        leg_metrics = self.connection.execute(
            """SELECT COUNT(*) AS legs,
                      COALESCE(SUM(CASE WHEN l.status='complete' THEN 1 ELSE 0 END),0)
                         AS successes
                 FROM ai_run_legs l JOIN ai_runs r ON r.id=l.run_id
                WHERE r.created_at>=datetime('now',?)""",
            (f"-{window} days",),
        ).fetchone()
        outcome_metrics: dict[str, Any] = {
            "observed_runs": 0,
            "provider_legs": int(leg_metrics["legs"] if leg_metrics else 0),
            "provider_leg_successes": int(leg_metrics["successes"] if leg_metrics else 0),
            "provider_artifacts": 0,
            "provider_valid_artifacts": 0,
            "repair_attempted_runs": 0,
            "repair_recovered_runs": 0,
            "full_business_successes": 0,
            "degraded_usable_runs": 0,
            "partial_unavailable_runs": 0,
        }
        deepseek_versions: set[str] = set()
        groups: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        recent: list[dict[str, Any]] = []
        versions: set[str] = set()
        route_legs: dict[str, list[dict[str, Any]]] = {}
        for leg in self.query(
            """SELECT l.run_id,l.ordinal,l.route_kind,l.provider_id,
                      l.requested_model,l.resolved_model,l.reasoning_strength
                 FROM ai_run_legs l JOIN ai_runs r ON r.id=l.run_id
                WHERE r.created_at>=datetime('now',?)
                ORDER BY l.run_id,l.ordinal""",
            (f"-{window} days",),
        ):
            route_legs.setdefault(str(leg["run_id"]), []).append(dict(leg))
        catalog = load_price_catalog()
        for row in rows:
            if row["business_outcome"] is not None:
                outcome_metrics["observed_runs"] += 1
                provider_outcome = str(row["provider_outcome"] or "")
                artifact_outcome = str(row["artifact_outcome"] or "")
                business_outcome = str(row["business_outcome"] or "")
                repairs = int(row["repair_attempts"] or 0)
                if provider_outcome in {"succeeded", "mixed"}:
                    outcome_metrics["provider_artifacts"] += 1
                    if artifact_outcome in {"valid", "repaired"}:
                        outcome_metrics["provider_valid_artifacts"] += 1
                if repairs > 0:
                    outcome_metrics["repair_attempted_runs"] += 1
                    if artifact_outcome == "repaired":
                        outcome_metrics["repair_recovered_runs"] += 1
                if business_outcome in {
                    "complete", "cache", "hybrid", "deterministic_fallback"
                }:
                    outcome_metrics["full_business_successes"] += 1
                if bool(row["usable"]) and bool(row["degraded"]):
                    outcome_metrics["degraded_usable_runs"] += 1
                if business_outcome in {"partial", "unavailable"}:
                    outcome_metrics["partial_unavailable_runs"] += 1
            usage = normalize_usage(json.loads(row["usage_json"] or "{}"))
            estimate = self._current_price_estimate(dict(row), catalog=catalog)
            try:
                fallback = json.loads(row["fallback_json"] or "{}")
            except json.JSONDecodeError:
                fallback = {}
            try:
                governance = json.loads(row["governance_json"] or "{}")
            except json.JSONDecodeError:
                governance = {}
            status = str(estimate.get("status") or "unknown")
            if status in {"known", "partial"} and estimate.get("currency") == "CNY":
                amount = float(estimate.get("amount") or 0)
                totals["known_estimated_cny"] += amount
            else:
                amount = None
            if status == "partial":
                totals["partial_cost_runs"] += 1
            elif status != "known":
                totals["unknown_cost_runs"] += 1
            if estimate.get("price_version"):
                versions.add(str(estimate["price_version"]))
            scoped_cost = self._scoped_provider_cost(dict(row), estimate)
            if scoped_cost["in_scope"]:
                deepseek_cost["runs"] += 1
                if scoped_cost.get("amount") is not None:
                    deepseek_cost["known_estimated_cny"] += float(scoped_cost["amount"])
                if scoped_cost["status"] == "partial":
                    deepseek_cost["partial_cost_runs"] += 1
                    deepseek_cost["unknown_cost_runs"] += 1
                elif scoped_cost["status"] != "known":
                    deepseek_cost["unknown_cost_runs"] += 1
                if scoped_cost.get("price_version"):
                    deepseek_versions.add(str(scoped_cost["price_version"]))
            requests = usage.get("provider_requests")
            if isinstance(requests, (int, float)) and not isinstance(requests, bool):
                totals["provider_requests_known"] += int(requests)
            else:
                totals["provider_requests_unknown_runs"] += 1
            for key, target in (("input_tokens", "input_tokens_known"), ("output_tokens", "output_tokens_known")):
                value = usage.get(key)
                if isinstance(value, (int, float)) and not isinstance(value, bool):
                    totals[target] += int(value)
                    all_model_tokens[target] += int(value)
            total_tokens = usage.get("total_tokens")
            if isinstance(total_tokens, (int, float)) and not isinstance(total_tokens, bool):
                all_model_tokens["total_tokens_known"] += int(total_tokens)
            else:
                all_model_tokens["unknown_runs"] += 1
            cache_read = usage.get("cache_read_tokens")
            if isinstance(cache_read, (int, float)) and not isinstance(cache_read, bool):
                totals["cache_read_tokens_known"] += int(cache_read)
            else:
                totals["cache_read_unknown_runs"] += 1
            input_tokens = usage.get("input_tokens")
            if not (
                isinstance(input_tokens, (int, float))
                and not isinstance(input_tokens, bool)
                and isinstance(cache_read, (int, float))
                and not isinstance(cache_read, bool)
            ):
                cache_metrics["unknown_runs"] += 1
            elif cache_read < 0 or input_tokens < 0 or cache_read > input_tokens:
                cache_metrics["invalid_runs"] += 1
            else:
                cache_metrics["observed_runs"] += 1
                cache_metrics["eligible_input_tokens"] += int(input_tokens)
                cache_metrics["cache_read_tokens_known"] += int(cache_read)
            provider_fallback_count = _provider_route_fallback_count(
                governance, route_legs.get(str(row["id"]), [])
            )
            business_fallback_count = _business_fallback_count(fallback)
            totals["provider_route_fallbacks"] += provider_fallback_count
            totals["route_fallbacks"] += provider_fallback_count
            totals["business_fallbacks"] += business_fallback_count
            if governance.get("blocked_reason") or (
                isinstance(json.loads(row["error_json"] or "{}"), Mapping)
                and json.loads(row["error_json"] or "{}").get("code") in {
                    "budget_exceeded", "cost_limit_exceeded", "cost_limit_unknown"
                }
            ):
                totals["budget_blocks"] += 1
            provider_id = str(row["resolved_provider_id"] or row["provider_id"] or "unknown")
            profile_id = str(row["profile_id"] or "unknown")
            model = str(row["resolved_model"] or row["requested_model"] or row["model"] or "unknown")
            kind = str(row["kind"] or "unknown")
            key = (provider_id, profile_id, model, kind)
            group = groups.setdefault(
                key,
                {
                    "provider_id": provider_id,
                    "profile_id": profile_id,
                    "model": model,
                    "kind": kind,
                    "runs": 0,
                    "known_estimated_cny": 0.0,
                    "unknown_cost_runs": 0,
                    "provider_requests_known": 0,
                    "total_tokens_known": 0,
                    "cache_read_tokens_known": 0,
                    "route_fallbacks": 0,
                    "provider_route_fallbacks": 0,
                    "business_fallbacks": 0,
                    "deepseek_cost": {
                        "provider_id": "deepseek",
                        "currency": "CNY",
                        "status": "out_of_scope",
                        "runs": 0,
                        "amount": 0.0,
                        "unknown_cost_runs": 0,
                        "partial_cost_runs": 0,
                    },
                },
            )
            group["runs"] += 1
            if amount is None:
                group["unknown_cost_runs"] += 1
            else:
                group["known_estimated_cny"] += amount
            if isinstance(requests, (int, float)) and not isinstance(requests, bool):
                group["provider_requests_known"] += int(requests)
            if isinstance(total_tokens, (int, float)) and not isinstance(total_tokens, bool):
                group["total_tokens_known"] += int(total_tokens)
            if isinstance(cache_read, (int, float)) and not isinstance(cache_read, bool):
                group["cache_read_tokens_known"] += int(cache_read)
            group["provider_route_fallbacks"] += provider_fallback_count
            group["route_fallbacks"] += provider_fallback_count
            group["business_fallbacks"] += business_fallback_count
            group_cost = group["deepseek_cost"]
            if scoped_cost["in_scope"]:
                group_cost["runs"] += 1
                if scoped_cost.get("amount") is not None:
                    group_cost["amount"] += float(scoped_cost["amount"])
                if scoped_cost["status"] == "partial":
                    group_cost["partial_cost_runs"] += 1
                    group_cost["unknown_cost_runs"] += 1
                elif scoped_cost["status"] != "known":
                    group_cost["unknown_cost_runs"] += 1
                group_cost["status"] = (
                    "partial" if group_cost["unknown_cost_runs"] and group_cost["amount"]
                    else "unknown" if group_cost["unknown_cost_runs"]
                    else "known"
                )
            if len(recent) < max(0, int(recent_limit)):
                recent.append(
                    {
                        "id": row["id"],
                        "created_at": row["created_at"],
                        "status": row["status"],
                        "kind": kind,
                        "profile_id": profile_id,
                        "provider_id": str(row["provider_id"] or "unknown"),
                        "resolved_provider_id": str(row["resolved_provider_id"] or "") or None,
                        "requested_model": str(row["requested_model"] or row["model"]),
                        "resolved_model": str(row["resolved_model"] or "") or None,
                        "provider_requests": requests,
                        "total_tokens": usage.get("total_tokens"),
                        "cache_read_tokens": cache_read,
                        # Compatibility alias consumed by the existing UI.
                        "fallback_count": provider_fallback_count,
                        "provider_route_fallback_count": provider_fallback_count,
                        "business_fallback_count": business_fallback_count,
                        "deepseek_cost": scoped_cost,
                        "estimated_cost": {
                            key: estimate.get(key)
                            for key in (
                                "status", "currency", "amount", "price_version",
                                "unknown_reason", "unknown_leg_count",
                            )
                            if estimate.get(key) is not None
                        },
                        "outcome": (
                            {
                                "version": 1,
                                "provider_outcome": row["provider_outcome"],
                                "artifact_outcome": row["artifact_outcome"],
                                "business_outcome": row["business_outcome"],
                                "usable": bool(row["usable"]),
                                "apply_ready": bool(row["apply_ready"]),
                                "degraded": bool(row["degraded"]),
                                "repair_attempts": int(row["repair_attempts"] or 0),
                            }
                            if row["business_outcome"] is not None else None
                        ),
                    }
                )
        totals["known_estimated_cny"] = round(totals["known_estimated_cny"], 12)
        deepseek_cost["known_estimated_cny"] = round(
            deepseek_cost["known_estimated_cny"], 12
        )
        deepseek_cost["price_versions"] = sorted(deepseek_versions)
        denominator = cache_metrics["eligible_input_tokens"]
        if denominator > 0:
            cache_metrics["hit_rate_percent"] = round(
                cache_metrics["cache_read_tokens_known"] / denominator * 100, 2
            )
        for group in groups.values():
            group["known_estimated_cny"] = round(group["known_estimated_cny"], 12)
            group["deepseek_cost"]["amount"] = round(
                group["deepseek_cost"]["amount"], 12
            )
        def rate(numerator: str, denominator: str) -> float | None:
            total = int(outcome_metrics[denominator])
            return round(int(outcome_metrics[numerator]) / total * 100, 2) if total else None

        outcome_metrics.update(
            provider_leg_success_rate_percent=rate("provider_leg_successes", "provider_legs"),
            provider_valid_artifact_rate_percent=rate(
                "provider_valid_artifacts", "provider_artifacts"
            ),
            repair_recovery_rate_percent=rate(
                "repair_recovered_runs", "repair_attempted_runs"
            ),
            full_business_success_rate_percent=rate(
                "full_business_successes", "observed_runs"
            ),
            degraded_usable_rate_percent=rate(
                "degraded_usable_runs", "observed_runs"
            ),
            partial_unavailable_rate_percent=rate(
                "partial_unavailable_runs", "observed_runs"
            ),
        )
        return {
            "window_days": window,
            "totals": totals,
            "deepseek_cost": deepseek_cost,
            "all_model_tokens": all_model_tokens,
            "cache_metrics": cache_metrics,
            "outcome_metrics": outcome_metrics,
            "price_versions": sorted(versions),
            "groups": sorted(groups.values(), key=lambda item: (-item["runs"], item["profile_id"], item["provider_id"], item["model"])),
            "recent_runs": recent,
            "hard_limit_spend": self.ai_cost_spend(),
        }

    def reprice_ai_runs(
        self, *, catalog: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        selected = dict(catalog or load_price_catalog())
        version = str(selected.get("catalog_version") or "")
        digest = price_catalog_hash(selected)
        rows = self.query("SELECT * FROM ai_runs ORDER BY created_at,id")
        counts = {"known": 0, "partial": 0, "unknown": 0}
        with self.atomic():
            for row in rows:
                try:
                    governance = json.loads(row["governance_json"] or "{}")
                except json.JSONDecodeError:
                    governance = {}
                estimate = self._estimate_ai_run_cost(
                    provider_id=str(row["resolved_provider_id"] or row["provider_id"] or "") or None,
                    model=str(row["resolved_model"] or row["requested_model"] or row["model"]),
                    usage=normalize_usage(json.loads(row["usage_json"] or "{}")),
                    created_at=str(row["created_at"] or "") or None,
                    governance=governance,
                    catalog=selected,
                )
                self._record_ai_cost_estimate(str(row["id"]), estimate, basis="repriced")
                counts[str(estimate.get("status") or "unknown")] += 1
        return {
            "catalog_version": version,
            "catalog_sha256": digest,
            "runs": len(rows),
            "counts": counts,
        }

    def ai_run(self, run_id: str) -> sqlite3.Row | None:
        return self.connection.execute(
            "SELECT * FROM ai_runs WHERE id=?", (str(run_id),)
        ).fetchone()

    def get_ai_cache_entry(
        self,
        cache_key: str,
        *,
        now: str | datetime | None = None,
        touch: bool = True,
    ) -> sqlite3.Row | None:
        """Return one unexpired entry and optionally update its LRU metadata."""

        stamp, _ = _cache_stamp(now)
        key = str(cache_key)
        with self.atomic():
            self.connection.execute(
                "DELETE FROM ai_cache_entries WHERE cache_key=? AND expires_at<=?",
                (key, stamp),
            )
            row = self.connection.execute(
                "SELECT * FROM ai_cache_entries WHERE cache_key=?", (key,)
            ).fetchone()
            if row is not None and touch:
                self.connection.execute(
                    """UPDATE ai_cache_entries
                          SET last_accessed_at=?,hit_count=hit_count+1
                        WHERE cache_key=?""",
                    (stamp, key),
                )
                row = self.connection.execute(
                    "SELECT * FROM ai_cache_entries WHERE cache_key=?", (key,)
                ).fetchone()
        return row

    def put_ai_cache_entry(
        self,
        *,
        key: str,
        profile_id: str,
        artifact: Any,
        proof: Mapping[str, Any],
        manifest_hash: str,
        ttl_seconds: int,
        max_entry_bytes: int = DEFAULT_CACHE_MAX_ENTRY_BYTES,
        source_run_id: str | None = None,
        now: str | datetime | None = None,
    ) -> sqlite3.Row:
        """Persist only a validated, proof-bound structured artifact."""

        cache_key = str(key)
        if cache_key != str(manifest_hash):
            raise ValueError("cache key must equal its manifest hash")
        if isinstance(ttl_seconds, bool) or not isinstance(ttl_seconds, int) or ttl_seconds <= 0:
            raise ValueError("ttl_seconds must be a positive integer")
        selected_profile = str(profile_id or "").strip().lower()
        if selected_profile not in EXACT_CACHE_PROFILES:
            raise ValueError("profile_id is not eligible for persistent exact cache")
        stamp, instant = _cache_stamp(now)
        expires = (instant + timedelta(seconds=ttl_seconds)).isoformat()
        encoded = encode_cache_artifact(
            artifact,
            proof,
            manifest_hash=str(manifest_hash),
            max_entry_bytes=max_entry_bytes,
        )
        with self.atomic():
            self.connection.execute(
                """INSERT INTO ai_cache_entries(
                       cache_key,profile_id,manifest_hash,artifact_json,
                       artifact_hash,proof_json,proof_hash,size_bytes,
                       source_run_id,created_at,last_accessed_at,expires_at,hit_count)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,0)
                   ON CONFLICT(cache_key) DO UPDATE SET
                       profile_id=excluded.profile_id,
                       manifest_hash=excluded.manifest_hash,
                       artifact_json=excluded.artifact_json,
                       artifact_hash=excluded.artifact_hash,
                       proof_json=excluded.proof_json,
                       proof_hash=excluded.proof_hash,
                       size_bytes=excluded.size_bytes,
                       source_run_id=excluded.source_run_id,
                       created_at=excluded.created_at,
                       last_accessed_at=excluded.last_accessed_at,
                       expires_at=excluded.expires_at,
                       hit_count=0""",
                (
                    cache_key,
                    selected_profile,
                    str(manifest_hash),
                    encoded["artifact_json"],
                    encoded["artifact_hash"],
                    encoded["proof_json"],
                    encoded["proof_hash"],
                    encoded["size_bytes"],
                    str(source_run_id) if source_run_id is not None else None,
                    stamp,
                    stamp,
                    expires,
                ),
            )
        row = self.get_ai_cache_entry(cache_key, now=stamp, touch=False)
        assert row is not None
        return row

    def read_validated_ai_cache_entry(
        self,
        cache_key: str,
        *,
        validator: Any = None,
        lowering: Any = None,
        now: str | datetime | None = None,
    ) -> CacheValidation | None:
        """Read, revalidate, and evict an unusable exact-cache entry."""

        row = self.get_ai_cache_entry(cache_key, now=now, touch=False)
        if row is None:
            return None
        try:
            result = validate_cached_artifact(
                row, validator=validator, lowering=lowering
            )
        except (CacheIntegrityError, TypeError, ValueError, KeyError):
            self.delete_ai_cache_entry(cache_key)
            return None
        stamp, _ = _cache_stamp(now)
        self.connection.execute(
            """UPDATE ai_cache_entries
                  SET last_accessed_at=?,hit_count=hit_count+1 WHERE cache_key=?""",
            (stamp, str(cache_key)),
        )
        return result

    def delete_ai_cache_entry(self, cache_key: str) -> bool:
        with self.atomic():
            cursor = self.connection.execute(
                "DELETE FROM ai_cache_entries WHERE cache_key=?", (str(cache_key),)
            )
        return cursor.rowcount == 1

    def clear_ai_cache(self, profile_ids: Sequence[str] | None = None) -> dict[str, int]:
        """Clear every exact entry or only the selected profiles."""

        params: list[Any] = []
        where = ""
        if profile_ids is not None:
            if isinstance(profile_ids, (str, bytes, bytearray)):
                raise TypeError("profile_ids must be a sequence")
            selected = sorted({str(item).strip().lower() for item in profile_ids})
            if any(not item for item in selected):
                raise ValueError("profile_ids must not contain empty values")
            if not selected:
                return {"removed_entries": 0, "removed_bytes": 0}
            where = f" WHERE profile_id IN ({','.join('?' for _ in selected)})"
            params.extend(selected)
        with self.atomic():
            before = self.connection.execute(
                f"SELECT COUNT(*) AS entries,COALESCE(SUM(size_bytes),0) AS bytes "
                f"FROM ai_cache_entries{where}",
                params,
            ).fetchone()
            self.connection.execute(f"DELETE FROM ai_cache_entries{where}", params)
        return {
            "removed_entries": int(before["entries"]),
            "removed_bytes": int(before["bytes"]),
        }

    def prune_ai_cache(
        self,
        *,
        now: str | datetime | None = None,
        max_entries: int = 512,
        max_bytes: int = 64 * 1024 * 1024,
    ) -> dict[str, int]:
        """Drop expired entries first, then evict the least recently used."""

        for label, value in (("max_entries", max_entries), ("max_bytes", max_bytes)):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{label} must be a positive integer")
        stamp, _ = _cache_stamp(now)
        removed_entries = 0
        removed_bytes = 0
        with self.atomic():
            expired = list(
                self.connection.execute(
                    "SELECT cache_key,size_bytes FROM ai_cache_entries WHERE expires_at<=?",
                    (stamp,),
                )
            )
            if expired:
                self.connection.execute(
                    "DELETE FROM ai_cache_entries WHERE expires_at<=?", (stamp,)
                )
                removed_entries += len(expired)
                removed_bytes += sum(int(row["size_bytes"]) for row in expired)
            rows = list(
                self.connection.execute(
                    """SELECT cache_key,size_bytes FROM ai_cache_entries
                         ORDER BY last_accessed_at,created_at,cache_key"""
                )
            )
            remaining_entries = len(rows)
            remaining_bytes = sum(int(row["size_bytes"]) for row in rows)
            victims: list[str] = []
            for row in rows:
                if remaining_entries <= max_entries and remaining_bytes <= max_bytes:
                    break
                victims.append(str(row["cache_key"]))
                remaining_entries -= 1
                size = int(row["size_bytes"])
                remaining_bytes -= size
                removed_entries += 1
                removed_bytes += size
            if victims:
                self.connection.executemany(
                    "DELETE FROM ai_cache_entries WHERE cache_key=?",
                    ((key,) for key in victims),
                )
            self.connection.execute(
                """DELETE FROM ai_request_flights
                    WHERE status<>'running' AND lease_expires_at<=?""",
                (stamp,),
            )
        return {
            "removed_entries": removed_entries,
            "removed_bytes": removed_bytes,
            "entries": remaining_entries,
            "bytes": remaining_bytes,
        }

    def ai_cache_status(self, *, now: str | datetime | None = None) -> dict[str, Any]:
        stamp, _ = _cache_stamp(now)
        active = self.connection.execute(
            """SELECT COUNT(*) AS entries,COALESCE(SUM(size_bytes),0) AS bytes,
                      COALESCE(SUM(hit_count),0) AS entry_hits
                 FROM ai_cache_entries WHERE expires_at>?""",
            (stamp,),
        ).fetchone()
        expired = self.connection.execute(
            "SELECT COUNT(*) FROM ai_cache_entries WHERE expires_at<=?", (stamp,)
        ).fetchone()[0]
        audit_rows = self.connection.execute(
            """SELECT local_cache_status,COUNT(*) AS count FROM ai_runs
                WHERE local_cache_status IS NOT NULL
                GROUP BY local_cache_status"""
        )
        audit = {str(row["local_cache_status"]): int(row["count"]) for row in audit_rows}
        hits = audit.get("hit", 0)
        coalesced = audit.get("coalesced", 0)
        eligible = sum(audit.get(name, 0) for name in ("hit", "miss", "coalesced", "refresh"))
        logical_requests = int(
            self.connection.execute(
                """SELECT COUNT(*) FROM ai_runs
                    WHERE kind IN (
                        'recommendation','plan_import','coaching','patch',
                        'markdown_summary'
                    )"""
            ).fetchone()[0]
        )
        return {
            "entries": int(active["entries"]),
            "bytes": int(active["bytes"]),
            "expired_entries": int(expired),
            "entry_hits": int(active["entry_hits"]),
            "audit": audit,
            "eligible_lookups": eligible,
            "exact_hits": hits,
            "coalesced_followers": coalesced,
            "logical_requests": logical_requests,
            "exact_hit_rate": (hits / eligible) if eligible else None,
            "provider_avoidance": (
                (hits + coalesced) / logical_requests if logical_requests else None
            ),
        }

    def acquire_ai_request_flight(
        self,
        cache_key: str,
        *,
        owner_id: str,
        profile_id: str,
        lease_seconds: int = DEFAULT_FLIGHT_LEASE_SECONDS,
        failure_cooldown_seconds: int = DEFAULT_FLIGHT_FAILURE_COOLDOWN_SECONDS,
        now: str | datetime | None = None,
    ) -> str:
        """Acquire a cross-process lease, or identify the caller as follower."""

        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be a positive integer")
        if (
            isinstance(failure_cooldown_seconds, bool)
            or not isinstance(failure_cooldown_seconds, int)
            or failure_cooldown_seconds < 0
        ):
            raise ValueError("failure_cooldown_seconds must be a non-negative integer")
        key = str(cache_key)
        owner = str(owner_id or "").strip()
        profile = str(profile_id or "").strip().lower()
        if not owner or not profile:
            raise ValueError("owner_id and profile_id must not be empty")
        stamp, instant = _cache_stamp(now)
        expires = (instant + timedelta(seconds=lease_seconds)).isoformat()
        with self.atomic():
            row = self.connection.execute(
                "SELECT * FROM ai_request_flights WHERE cache_key=?", (key,)
            ).fetchone()
            if row is None:
                self.connection.execute(
                    """INSERT INTO ai_request_flights(
                           cache_key,profile_id,owner_id,status,lease_expires_at,
                           created_at,updated_at)
                       VALUES(?,?,?,'running',?,?,?)""",
                    (key, profile, owner, expires, stamp, stamp),
                )
                return "leader"
            if row["status"] == "running" and row["owner_id"] == owner:
                self.connection.execute(
                    """UPDATE ai_request_flights
                          SET lease_expires_at=?,updated_at=? WHERE cache_key=?""",
                    (expires, stamp, key),
                )
                return "leader"
            if row["status"] == "running" and str(row["lease_expires_at"]) > stamp:
                return "follower"
            if row["status"] == "failed" and failure_cooldown_seconds:
                _, failed_at = _cache_stamp(str(row["updated_at"]))
                if instant < failed_at + timedelta(seconds=failure_cooldown_seconds):
                    return "failed"
            stolen = row["status"] == "running"
            self.connection.execute(
                """UPDATE ai_request_flights
                      SET profile_id=?,owner_id=?,status='running',lease_expires_at=?,
                          error_code=NULL,created_at=?,updated_at=?
                    WHERE cache_key=?""",
                (profile, owner, expires, stamp, stamp, key),
            )
            return "stolen" if stolen else "leader"

    def renew_ai_request_flight(
        self,
        cache_key: str,
        *,
        owner_id: str,
        lease_seconds: int = DEFAULT_FLIGHT_LEASE_SECONDS,
        now: str | datetime | None = None,
    ) -> bool:
        if isinstance(lease_seconds, bool) or not isinstance(lease_seconds, int) or lease_seconds <= 0:
            raise ValueError("lease_seconds must be a positive integer")
        stamp, instant = _cache_stamp(now)
        expires = (instant + timedelta(seconds=lease_seconds)).isoformat()
        with self.atomic():
            cursor = self.connection.execute(
                """UPDATE ai_request_flights SET lease_expires_at=?,updated_at=?
                    WHERE cache_key=? AND owner_id=? AND status='running'""",
                (expires, stamp, str(cache_key), str(owner_id)),
            )
        return cursor.rowcount == 1

    def release_ai_request_flight(
        self,
        cache_key: str,
        *,
        owner_id: str,
        status: str = "complete",
        error_code: str | None = None,
        now: str | datetime | None = None,
    ) -> bool:
        selected_status = str(status).strip().lower()
        if selected_status not in {"complete", "failed"}:
            raise ValueError("flight release status must be complete or failed")
        stamp, _ = _cache_stamp(now)
        with self.atomic():
            cursor = self.connection.execute(
                """UPDATE ai_request_flights
                      SET status=?,error_code=?,updated_at=?
                    WHERE cache_key=? AND owner_id=? AND status='running'""",
                (
                    selected_status,
                    str(error_code)[:128] if error_code is not None else None,
                    stamp,
                    str(cache_key),
                    str(owner_id),
                ),
            )
        return cursor.rowcount == 1

    def wait_ai_request_flight(
        self,
        cache_key: str,
        *,
        owner_id: str | None = None,
        timeout_seconds: float = float(DEFAULT_FLIGHT_WAIT_TIMEOUT_SECONDS),
        poll_seconds: float = 0.05,
    ) -> str:
        """Wait for a leader terminal state without holding a transaction."""

        if timeout_seconds < 0 or poll_seconds <= 0:
            raise ValueError("flight wait durations are invalid")
        deadline = time.monotonic() + float(timeout_seconds)
        key = str(cache_key)
        while True:
            row = self.connection.execute(
                "SELECT owner_id,status,lease_expires_at FROM ai_request_flights WHERE cache_key=?",
                (key,),
            ).fetchone()
            if row is None:
                return "released"
            if owner_id is not None and row["owner_id"] == str(owner_id):
                return "leader"
            if row["status"] != "running":
                return str(row["status"])
            now_stamp, _ = _cache_stamp()
            if str(row["lease_expires_at"]) <= now_stamp:
                return "expired"
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return "timeout"
            time.sleep(min(float(poll_seconds), remaining))

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
        """Insert or refresh one named statement sample, deduplicating content."""

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

    def problem_samples(self, platform: str, problem_id: str) -> list[sqlite3.Row]:
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
        """Atomically replace statement samples owned by one source."""

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
