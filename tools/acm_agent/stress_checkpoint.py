"""Content-addressed checkpoints for AI stress helper preparation.

This module deliberately knows nothing about a user's solution.  Generation
identity is derived from the problem statement and prompt inputs; certification
identity is derived from generated helper candidates and the local gate
environment.  Mutable cache aliases are published only after an immutable,
valid protocol-specific certification exists.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Any, Mapping, Sequence

from .canonical import canonical_json_bytes as _canonical_json
from .storage import Database


IDENTITY_VERSION = 1
PERSISTED_ROLES = (
    "generator",
    "validator",
    "reference_primary",
    "reference_secondary",
    # Legacy v12/v13 candidates remain readable and certifiable for rollback.
    "brute",
    "reference",
)


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_text(value: str) -> str:
    return _sha256_bytes(str(value).encode("utf-8"))


def _identity_key(namespace: str, identity: Mapping[str, Any]) -> str:
    digest = _sha256_bytes(_canonical_json(dict(identity)))
    return f"stress-{namespace}-v{IDENTITY_VERSION}:{digest}"


def generation_identity(
    *,
    platform: str,
    problem_id: str,
    role: str,
    model: str,
    mode: str,
    prompt: str,
    prompt_version: int | str,
    statement: str,
    semantic_inputs: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build a privacy-minimized identity for one generated helper role."""

    selected_role = str(role).strip()
    if not selected_role:
        raise ValueError("role must not be empty")
    return {
        "identity_version": IDENTITY_VERSION,
        "platform": str(platform).strip().lower(),
        "problem_id": str(problem_id).strip(),
        "role": selected_role,
        "model": str(model).strip(),
        "mode": str(mode).strip(),
        "prompt_version": str(prompt_version),
        "prompt_sha256": _sha256_text(prompt),
        "statement_sha256": _sha256_text(statement),
        "semantic_inputs_sha256": _sha256_bytes(
            _canonical_json(dict(semantic_inputs or {}))
        ),
    }


def generation_key(identity: Mapping[str, Any]) -> str:
    """Return the stable content key for :func:`generation_identity`."""

    return _identity_key("generation", identity)


def _value(candidate: Mapping[str, Any] | "CandidateRef", key: str) -> Any:
    if isinstance(candidate, CandidateRef):
        return getattr(candidate, key)
    try:
        return candidate[key]
    except (KeyError, TypeError):
        raise ValueError(f"candidate is missing {key!r}") from None


@dataclass(frozen=True)
class CandidateRef:
    """The non-source portion of a persisted immutable helper candidate."""

    id: str
    role: str
    source_hash: str
    generation_key: str

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "CandidateRef":
        return cls(
            id=str(_value(row, "id")),
            role=str(_value(row, "role")),
            source_hash=str(_value(row, "source_hash")),
            generation_key=str(_value(row, "generation_key")),
        )


def candidate_ref(
    *,
    role: str,
    source_code: str,
    identity: Mapping[str, Any],
) -> CandidateRef:
    """Derive an immutable content-addressed helper reference."""

    if str(identity.get("role") or "") != str(role):
        raise ValueError("candidate role must match generation identity role")
    selected_generation_key = generation_key(identity)
    source_hash = _sha256_text(source_code)
    candidate_id = _identity_key(
        "candidate",
        {
            "generation_key": selected_generation_key,
            "source_sha256": source_hash,
        },
    )
    return CandidateRef(
        id=candidate_id,
        role=str(role),
        source_hash=source_hash,
        generation_key=selected_generation_key,
    )


def _candidate_identity(
    candidate: Mapping[str, Any] | CandidateRef,
    expected_role: str,
) -> dict[str, str]:
    actual_role = str(_value(candidate, "role"))
    if actual_role != expected_role:
        raise ValueError(
            f"expected {expected_role!r} candidate, received role {actual_role!r}"
        )
    candidate_id = str(_value(candidate, "id")).strip()
    source_hash = str(_value(candidate, "source_hash")).strip()
    if not candidate_id or not source_hash:
        raise ValueError("candidate id and source_hash must not be empty")
    return {"candidate_id": candidate_id, "source_sha256": source_hash}


def _sample_bytes(sample: Any, *names: str) -> bytes:
    value: Any = None
    found = False
    if isinstance(sample, Mapping):
        for name in names:
            if name in sample:
                value = sample[name]
                found = True
                break
    else:
        for name in names:
            if hasattr(sample, name):
                value = getattr(sample, name)
                found = True
                break
    if not found:
        raise ValueError(f"sample is missing one of {names!r}")
    if isinstance(value, bytes):
        return value
    if isinstance(value, bytearray):
        return bytes(value)
    return str(value).encode("utf-8")


def _samples_identity(samples: Sequence[Any]) -> dict[str, Any]:
    pairs = sorted(
        (
            _sha256_bytes(_sample_bytes(sample, "input_data", "input")),
            _sha256_bytes(
                _sample_bytes(sample, "expected_output", "output")
            ),
        )
        for sample in samples
    )
    return {
        "count": len(pairs),
        "sha256": _sha256_bytes(_canonical_json(pairs)),
    }


def certification_identity(
    *,
    generator: Mapping[str, Any] | CandidateRef,
    validator: Mapping[str, Any] | CandidateRef | None,
    compiler: Mapping[str, Any] | str,
    sandbox: Mapping[str, Any] | str,
    samples: Sequence[Any],
    protocol: Mapping[str, Any],
    gate: Mapping[str, Any] | None = None,
    oracle_protocol: str | None = None,
    brute: Mapping[str, Any] | CandidateRef | None = None,
    reference: Mapping[str, Any] | CandidateRef | None = None,
    reference_primary: Mapping[str, Any] | CandidateRef | None = None,
    reference_secondary: Mapping[str, Any] | CandidateRef | None = None,
) -> dict[str, Any]:
    """Build a legacy-trio or dual-reference certification identity.

    ``validator`` is intentionally outside the three applied helpers, but its
    immutable candidate id and source hash are part of the certification.
    A ``None`` validator marks an unvalidated (degraded) bundle whose verdicts
    rely on the small-case triple oracle instead of input certification.
    Compiler and sandbox descriptions are retained only as canonical hashes.
    """

    has_legacy = brute is not None or reference is not None
    has_dual = reference_primary is not None or reference_secondary is not None
    selected_protocol = str(oracle_protocol or "").strip()
    if not selected_protocol:
        selected_protocol = "dual_reference_v1" if has_dual else "legacy_trio"
    if selected_protocol == "legacy_trio":
        if has_dual or brute is None or reference is None:
            raise ValueError("legacy_trio requires brute and reference candidates only")
        helpers = {
            "generator": _candidate_identity(generator, "generator"),
            "brute": _candidate_identity(brute, "brute"),
            "reference": _candidate_identity(reference, "reference"),
        }
    elif selected_protocol == "dual_reference_v1":
        if (
            has_legacy
            or reference_primary is None
            or reference_secondary is None
        ):
            raise ValueError(
                "dual_reference_v1 requires primary and secondary references only"
            )
        helpers = {
            "generator": _candidate_identity(generator, "generator"),
            "reference_primary": _candidate_identity(
                reference_primary, "reference_primary"
            ),
            "reference_secondary": _candidate_identity(
                reference_secondary, "reference_secondary"
            ),
        }
    else:
        raise ValueError(f"unknown oracle_protocol: {selected_protocol}")
    return {
        "identity_version": IDENTITY_VERSION,
        "oracle_protocol": selected_protocol,
        "helpers": helpers,
        "validator": (
            _candidate_identity(validator, "validator")
            if validator is not None
            else {"candidate_id": "unvalidated", "source_sha256": "none"}
        ),
        "compiler_sha256": _sha256_bytes(_canonical_json(compiler)),
        "sandbox_sha256": _sha256_bytes(_canonical_json(sandbox)),
        "samples": _samples_identity(samples),
        "protocol_sha256": _sha256_bytes(_canonical_json(dict(protocol))),
        "gate_sha256": _sha256_bytes(_canonical_json(dict(gate or {}))),
    }


def certification_key(identity: Mapping[str, Any]) -> str:
    """Return the stable content key for :func:`certification_identity`."""

    return _identity_key("certification", identity)


class StressCheckpointStore:
    """Small v14 persistence facade intended for ``StressCoordinator``."""

    def __init__(
        self,
        database: Database,
        *,
        platform: str,
        problem_id: str,
        producer_ai_run_id: str | None = None,
    ) -> None:
        self.database = database
        self.platform = str(platform).strip().lower()
        self.problem_id = str(problem_id).strip()
        self.producer_ai_run_id = (
            str(producer_ai_run_id) if producer_ai_run_id is not None else None
        )

    def save_candidate(
        self,
        *,
        role: str,
        source_code: str,
        identity: Mapping[str, Any],
        source_kind: str = "ai_generated",
        provenance: Mapping[str, Any] | None = None,
        usage: Mapping[str, Any] | None = None,
        status: str = "generated",
    ) -> CandidateRef:
        if str(role) not in PERSISTED_ROLES:
            raise ValueError(f"unknown stress artifact role: {role}")
        reference = candidate_ref(
            role=role,
            source_code=source_code,
            identity=identity,
        )
        row = self.database.save_stress_artifact_candidate(
            reference.id,
            generation_key=reference.generation_key,
            platform=self.platform,
            problem_id=self.problem_id,
            role=str(role),
            source_code=str(source_code),
            source_kind=str(source_kind),
            generation_identity=dict(identity),
            producer_ai_run_id=self.producer_ai_run_id,
            provenance=provenance,
            usage=usage,
            status=str(status),
        )
        return CandidateRef.from_row(row)

    def save_proof(
        self,
        *,
        candidate: Mapping[str, Any] | CandidateRef,
        proof_kind: str,
        identity: Mapping[str, Any],
        status: str,
        result: Mapping[str, Any] | None = None,
        executable_path: str | Path | None = None,
        executable_hash: str | None = None,
    ) -> Mapping[str, Any]:
        candidate_id = str(_value(candidate, "id"))
        proof_key = _identity_key(
            "proof",
            {
                "candidate_id": candidate_id,
                "proof_kind": str(proof_kind),
                "certification_identity": dict(identity),
            },
        )
        return self.database.save_stress_artifact_proof(
            proof_key,
            candidate_id=candidate_id,
            proof_kind=str(proof_kind),
            certification_identity=dict(identity),
            status=str(status),
            result=result,
            executable_path=executable_path,
            executable_hash=executable_hash,
        )

    def save_exact_trio_certification(
        self,
        *,
        generator: Mapping[str, Any] | CandidateRef,
        brute: Mapping[str, Any] | CandidateRef,
        reference: Mapping[str, Any] | CandidateRef,
        validator: Mapping[str, Any] | CandidateRef | None,
        compiler: Mapping[str, Any] | str,
        sandbox: Mapping[str, Any] | str,
        samples: Sequence[Any],
        protocol: Mapping[str, Any],
        gate: Mapping[str, Any] | None = None,
        scope: Mapping[str, Any] | None = None,
        preflight: Mapping[str, Any] | None = None,
        status: str = "valid",
    ) -> Mapping[str, Any]:
        identity = certification_identity(
            generator=generator,
            brute=brute,
            reference=reference,
            oracle_protocol="legacy_trio",
            validator=validator,
            compiler=compiler,
            sandbox=sandbox,
            samples=samples,
            protocol=protocol,
            gate=gate,
        )
        selected_key = certification_key(identity)
        return self.database.save_stress_bundle_certification(
            selected_key,
            platform=self.platform,
            problem_id=self.problem_id,
            generator_candidate_id=str(_value(generator, "id")),
            brute_candidate_id=str(_value(brute, "id")),
            reference_candidate_id=str(_value(reference, "id")),
            oracle_protocol="legacy_trio",
            certification_identity=identity,
            scope=scope,
            preflight=preflight,
            status=str(status),
        )

    def save_dual_reference_certification(
        self,
        *,
        generator: Mapping[str, Any] | CandidateRef,
        reference_primary: Mapping[str, Any] | CandidateRef,
        reference_secondary: Mapping[str, Any] | CandidateRef,
        validator: Mapping[str, Any] | CandidateRef | None,
        compiler: Mapping[str, Any] | str,
        sandbox: Mapping[str, Any] | str,
        samples: Sequence[Any],
        protocol: Mapping[str, Any],
        gate: Mapping[str, Any] | None = None,
        scope: Mapping[str, Any] | None = None,
        preflight: Mapping[str, Any] | None = None,
        status: str = "valid",
    ) -> Mapping[str, Any]:
        """Persist an immutable generator plus two-reference certification."""

        identity = certification_identity(
            generator=generator,
            reference_primary=reference_primary,
            reference_secondary=reference_secondary,
            validator=validator,
            compiler=compiler,
            sandbox=sandbox,
            samples=samples,
            protocol=protocol,
            gate=gate,
            oracle_protocol="dual_reference_v1",
        )
        selected_key = certification_key(identity)
        return self.database.save_stress_bundle_certification(
            selected_key,
            platform=self.platform,
            problem_id=self.problem_id,
            oracle_protocol="dual_reference_v1",
            generator_candidate_id=str(_value(generator, "id")),
            reference_primary_candidate_id=str(_value(reference_primary, "id")),
            reference_secondary_candidate_id=str(_value(reference_secondary, "id")),
            certification_identity=identity,
            scope=scope,
            preflight=preflight,
            status=str(status),
        )

    def publish_certification_alias(
        self,
        alias_key: str,
        certification: Mapping[str, Any],
        *,
        succeeded: bool,
        expected_revision: int | None = None,
    ) -> Mapping[str, Any] | None:
        """CAS-publish a valid result; failed/cancelled cold runs are no-ops."""

        if not succeeded or str(_value(certification, "status")) != "valid":
            return None
        return self.database.publish_stress_cache_alias(
            str(alias_key),
            alias_kind="certification",
            target_id=str(_value(certification, "certification_key")),
            expected_revision=expected_revision,
        )
