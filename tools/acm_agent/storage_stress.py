"""Continuous-stress cache, artifact, proof, bundle, and run persistence."""

from __future__ import annotations
import hashlib
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Mapping, Sequence
from .storage_common import (
    StressArtifactBundleRevisionConflict,
    StressArtifactCandidateConflict,
    StressArtifactProofConflict,
    StressBundleCertificationConflict,
    StressCacheAliasRevisionConflict,
    StressPreparationCacheConflict,
    StressRunRevisionConflict,
    _UNSET,
    _json,
    _merge_json_objects,
    _process_is_alive,
    _sha256_text,
    utc_now,
)

class _StressStorageMixin:
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
        oracle_protocol: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[sqlite3.Row]:
        clauses: list[str] = []
        values: list[Any] = []
        for column, value in (
            ("platform", platform),
            ("problem_id", problem_id),
            ("oracle_protocol", oracle_protocol),
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
        brute_candidate_id: str | None = None,
        reference_candidate_id: str | None = None,
        reference_primary_candidate_id: str | None = None,
        reference_secondary_candidate_id: str | None = None,
        oracle_protocol: str | None = None,
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
        selected_protocol = str(oracle_protocol or "").strip()
        has_legacy = brute_candidate_id is not None or reference_candidate_id is not None
        has_dual = (
            reference_primary_candidate_id is not None
            or reference_secondary_candidate_id is not None
        )
        if not selected_protocol:
            selected_protocol = "dual_reference_v1" if has_dual else "legacy_trio"
        if selected_protocol == "legacy_trio":
            if has_dual or brute_candidate_id is None or reference_candidate_id is None:
                raise ValueError("legacy_trio requires brute and reference candidates only")
        elif selected_protocol == "dual_reference_v1":
            if (
                has_legacy
                or reference_primary_candidate_id is None
                or reference_secondary_candidate_id is None
            ):
                raise ValueError(
                    "dual_reference_v1 requires primary and secondary references only"
                )
        else:
            raise ValueError(f"unknown oracle_protocol: {selected_protocol}")
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
            "oracle_protocol": selected_protocol,
            "generator_candidate_id": str(generator_candidate_id),
            "brute_candidate_id": (
                str(brute_candidate_id) if brute_candidate_id is not None else None
            ),
            "reference_candidate_id": (
                str(reference_candidate_id)
                if reference_candidate_id is not None
                else None
            ),
            "reference_primary_candidate_id": (
                str(reference_primary_candidate_id)
                if reference_primary_candidate_id is not None
                else None
            ),
            "reference_secondary_candidate_id": (
                str(reference_secondary_candidate_id)
                if reference_secondary_candidate_id is not None
                else None
            ),
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
                       certification_key,platform,problem_id,oracle_protocol,
                       generator_candidate_id,brute_candidate_id,
                       reference_candidate_id,reference_primary_candidate_id,
                       reference_secondary_candidate_id,certification_identity_json,
                       scope_json,preflight_json,status,created_at,last_used_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    selected_key,
                    immutable["platform"],
                    immutable["problem_id"],
                    immutable["oracle_protocol"],
                    immutable["generator_candidate_id"],
                    immutable["brute_candidate_id"],
                    immutable["reference_candidate_id"],
                    immutable["reference_primary_candidate_id"],
                    immutable["reference_secondary_candidate_id"],
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
                   WHEN 'generator' THEN 0
                   WHEN 'validator' THEN 1
                   WHEN 'reference_primary' THEN 2
                   WHEN 'reference_secondary' THEN 3
                   WHEN 'brute' THEN 4
                   ELSE 5 END""",
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
