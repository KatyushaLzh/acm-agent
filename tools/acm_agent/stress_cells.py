"""Layer 4: the model fills named empty cells, and is graded on whether it did.

The old pipeline asked one provider call to design an entire input distribution.
That is the task a language model is worst at — it is global, quantitative,
open-loop, and nothing checks the answer except "did it compile".  So the model
guessed, the guess was accepted, and iterating on the guess changed nothing.

Here the job is inverted.  The archive names a specific empty cell in words the
model can act on ("an array of 4..7 values where one value occupies 67%-100% of
the positions"), hands over a concrete nearby input to modify, and then
*verifies the answer against the profiler*.  A proposal counts only if the
profiler independently puts it in the requested cell.  The model's own claim
about what it produced is never taken as evidence.

That makes the loop closed and the task local:

* local — change one property of one small input, not design a distribution;
* checkable — the descriptor either matches or it does not;
* self-correcting — a cell that survives a genuine attempt is recorded
  infeasible (:meth:`Archive.mark_infeasible`) instead of being asked forever.

The provider call is injected, so this module stays free of transport, budget,
and credential concerns, and the tests need no network.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any, Callable, Mapping, Sequence

from .stress_archive import Archive, Target
from .stress_profiler import descriptor, describe_cell

CELL_PROPOSAL_SCHEMA_VERSION = 1

#: JSON shape the provider must return.  One input per requested cell.
PROPOSAL_SCHEMA: Mapping[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["schema_version", "proposals"],
    "properties": {
        "schema_version": {"const": CELL_PROPOSAL_SCHEMA_VERSION},
        "proposals": {
            "type": "array",
            "minItems": 1,
            "maxItems": 16,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["cell_id", "input"],
                "properties": {
                    "cell_id": {"type": "string", "minLength": 1},
                    "input": {"type": "string", "minLength": 1},
                    "rationale": {"type": "string", "maxLength": 500},
                },
            },
        },
    },
}


@dataclass(frozen=True)
class Verdict:
    """Objective grade for one proposal. ``hit`` is the only success."""

    cell_id: str
    hit: bool
    reason: str
    data: bytes | None = None
    #: Cell the profiler actually assigned, when it parsed at all.
    actual: tuple[Any, ...] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "cell_id": self.cell_id,
            "hit": self.hit,
            "reason": self.reason,
            "actual_cell": describe_cell(self.actual) if self.actual else None,
        }


_SYSTEM = (
    "You extend a competitive-programming stress corpus. You are given input "
    "shapes the corpus has never produced, each with a concrete nearby input "
    "that is one property away. For each target, return a valid input that has "
    "the requested property.\n"
    "Rules:\n"
    "- Obey the input format and every constraint exactly. An input that "
    "violates them is discarded.\n"
    "- Change the named property; keep the input as small as possible "
    "otherwise.\n"
    "- Each answer is checked mechanically against the requested shape. Do not "
    "explain instead of answering, and do not return the example unchanged.\n"
    "- Use '\\n' for line breaks inside the input string."
)


def _axis_brief(report: Mapping[str, Any]) -> str:
    starved = [axis for axis in report.get("axes", []) if axis.get("missing_labels")]
    if not starved:
        return "Every measured dimension has been reached at least once."
    lines = ["Dimensions with values never produced:"]
    for axis in starved:
        lines.append(
            f"- {axis['axis']} ({axis.get('description') or 'no description'}): "
            f"reached {axis['reached']}/{axis['total']}; "
            f"never produced {', '.join(axis['missing_labels'])}"
        )
    return "\n".join(lines)


def build_targeting_messages(
    archive: Archive,
    *,
    contract: Mapping[str, Any] | None = None,
    statement: str = "",
    target_limit: int = 6,
) -> tuple[list[dict[str, str]], tuple[Target, ...]]:
    """Build the provider messages, and return the targets they ask about.

    The caller needs the target tuple back in order to grade the reply, so the
    prompt and the grading key are produced together and cannot drift apart.
    """

    targets = archive.frontier(limit=target_limit)
    report = archive.report(target_limit=target_limit)
    sections: list[str] = []
    if statement.strip():
        sections.append("Problem statement:\n" + statement.strip()[:4000])
    if contract is not None:
        sections.append(
            "Input format (authoritative):\n"
            + json.dumps(contract.get("syntax", {}), ensure_ascii=False, indent=2)[:4000]
        )
        constraints = contract.get("constraints")
        if constraints:
            sections.append(
                "Constraints:\n"
                + json.dumps(constraints, ensure_ascii=False, indent=2)[:2000]
            )
    sections.append(
        f"Corpus so far: {report['cells']} distinct input shapes from "
        f"{report['observations']} inputs.\n" + _axis_brief(report)
    )
    if not targets:
        sections.append("No reachable target shapes remain.")
    else:
        lines = ["Targets. Return one input for each cell_id, exactly as written:"]
        for target in targets:
            lines.append(
                f"\ncell_id: {target.description}\n"
                f"  change this property: {target.axis}\n"
                f"  nearest input already produced ({target.neighbour_description}):\n"
                f"    {target.example.decode('ascii', 'replace')!r}"
            )
        sections.append("\n".join(lines))
    sections.append(
        "Reply with JSON only: "
        '{"schema_version": 1, "proposals": [{"cell_id": "...", "input": "...", '
        '"rationale": "..."}]}'
    )
    return (
        [
            {"role": "system", "content": _SYSTEM},
            {"role": "user", "content": "\n\n".join(sections)},
        ],
        targets,
    )


def _decode(value: str, *, max_bytes: int | None) -> tuple[bytes | None, str]:
    """Normalize a model-supplied input string into wire bytes."""

    text = value.replace("\r\n", "\n").replace("\r", "\n")
    if not text.endswith("\n"):
        text += "\n"
    try:
        data = text.encode("ascii")
    except UnicodeEncodeError:
        return None, "input is not ASCII"
    if not data.strip():
        return None, "input is empty"
    if max_bytes is not None and len(data) > max_bytes:
        return None, f"input is {len(data)} bytes, budget is {max_bytes}"
    return data, ""


def verify_proposals(
    archive: Archive,
    payload: Mapping[str, Any],
    targets: Sequence[Target],
    *,
    validator: Callable[[bytes], bool] | None = None,
) -> tuple[Verdict, ...]:
    """Grade a provider reply against the profiler. Never trusts the reply.

    A proposal is a hit only when the profiler, reading the bytes alone, assigns
    exactly the requested cell.  Anything else — unparseable, validator-rejected,
    landed elsewhere, cell not requested — is a miss with a reason attached, and
    the reason is what makes the next attempt better than the last.
    """

    wanted = {target.description: target for target in targets}
    seen: set[str] = set()
    verdicts: list[Verdict] = []
    raw_proposals = payload.get("proposals")
    if not isinstance(raw_proposals, Sequence) or isinstance(raw_proposals, (str, bytes)):
        return ()
    for item in raw_proposals:
        if not isinstance(item, Mapping):
            continue
        cell_id = str(item.get("cell_id") or "").strip()
        raw_input = item.get("input")
        if cell_id not in wanted:
            verdicts.append(Verdict(cell_id, False, "cell_id was not requested"))
            continue
        if cell_id in seen:
            verdicts.append(Verdict(cell_id, False, "duplicate cell_id in reply"))
            continue
        seen.add(cell_id)
        if not isinstance(raw_input, str):
            verdicts.append(Verdict(cell_id, False, "input is not a string"))
            continue
        data, error = _decode(raw_input, max_bytes=archive.corpus.max_bytes)
        if data is None:
            verdicts.append(Verdict(cell_id, False, error))
            continue
        target = wanted[cell_id]
        if data == target.example:
            verdicts.append(Verdict(cell_id, False, "input is the example unchanged"))
            continue
        if validator is not None and not validator(data):
            verdicts.append(
                Verdict(cell_id, False, "input violates the format or constraints", data)
            )
            continue
        features = archive.profiler.features(data)
        if not features.get("parsed"):
            verdicts.append(
                Verdict(
                    cell_id,
                    False,
                    f"input does not match the format: {features.get('parse_error')}",
                    data,
                )
            )
            continue
        actual = descriptor(features)
        if actual != target.cell:
            verdicts.append(
                Verdict(cell_id, False, "input landed in a different shape", data, actual)
            )
            continue
        verdicts.append(Verdict(cell_id, True, "reached the requested shape", data, actual))
    for cell_id, target in wanted.items():
        if cell_id not in seen:
            verdicts.append(Verdict(cell_id, False, "no proposal returned for this cell"))
    return tuple(verdicts)


#: The only miss that says anything about the *cell*.  Every other failure says
#: something about the reply — no answer, a malformed input, a constraint
#: violation — and retiring a cell on that evidence would silently shrink the
#: search space because the model wrote bad JSON.
_INFEASIBILITY_EVIDENCE = "input landed in a different shape"


def apply_verdicts(
    archive: Archive,
    verdicts: Sequence[Verdict],
    targets: Sequence[Target],
    *,
    signal_for: Callable[[bytes], Any] | None = None,
) -> dict[str, Any]:
    """Admit the hits, retire cells that a real attempt failed to reach.

    Note that a hit is admitted through :meth:`Archive.observe` like any other
    input.  It gets no special standing: if a mutation already occupies the cell
    with fewer bytes, the model's input loses.  The corpus stays a record of
    what was measured, not of who suggested it.
    """

    by_cell = {target.description: target for target in targets}
    admitted: list[str] = []
    retired: list[tuple[Any, ...]] = []
    for verdict in verdicts:
        if verdict.hit and verdict.data is not None:
            signal = signal_for(verdict.data) if signal_for is not None else None
            outcome = archive.observe(
                verdict.data, origin="llm_targeted", signal=signal
            )
            if outcome != "duplicate":
                admitted.append(verdict.cell_id)
            continue
        if verdict.reason != _INFEASIBILITY_EVIDENCE:
            continue
        target = by_cell.get(verdict.cell_id)
        if target is not None and archive.note_miss(target.cell):
            retired.append(target.cell)
    return {
        "requested": len(by_cell),
        "hits": sum(1 for verdict in verdicts if verdict.hit),
        "admitted": admitted,
        "retired_as_infeasible": len(retired),
        "verdicts": [verdict.as_dict() for verdict in verdicts],
    }


def fill_cells(
    archive: Archive,
    call_json: Callable[[list[dict[str, str]]], Mapping[str, Any]],
    *,
    contract: Mapping[str, Any] | None = None,
    statement: str = "",
    target_limit: int = 6,
    validator: Callable[[bytes], bool] | None = None,
    signal_for: Callable[[bytes], Any] | None = None,
) -> dict[str, Any]:
    """One targeting round: ask, verify, admit, retire.

    ``call_json`` takes the messages and returns the parsed JSON object; the
    caller supplies whatever provider plumbing it already has (in this codebase,
    a closure over ``_generate_json``).  Everything else here is pure.
    """

    messages, targets = build_targeting_messages(
        archive, contract=contract, statement=statement, target_limit=target_limit
    )
    if not targets:
        return {
            "requested": 0,
            "hits": 0,
            "admitted": [],
            "retired_as_infeasible": 0,
            "verdicts": [],
            "skipped": "no reachable targets",
        }
    payload = call_json(messages)
    if not isinstance(payload, Mapping):
        return {
            "requested": len(targets),
            "hits": 0,
            "admitted": [],
            "retired_as_infeasible": 0,
            "verdicts": [],
            "skipped": "provider reply was not a JSON object",
        }
    verdicts = verify_proposals(archive, payload, targets, validator=validator)
    return apply_verdicts(archive, verdicts, targets, signal_for=signal_for)


__all__ = [
    "CELL_PROPOSAL_SCHEMA_VERSION",
    "PROPOSAL_SCHEMA",
    "Verdict",
    "apply_verdicts",
    "build_targeting_messages",
    "fill_cells",
    "verify_proposals",
]
