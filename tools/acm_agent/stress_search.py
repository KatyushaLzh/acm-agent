"""Case selection for the stress loop: recipe seeds, mutation, enumeration.

Replaces ``seed = state.next_seed; next_seed += 1`` with a driver that decides,
per case, where the next input comes from.  Three sources:

``recipe``
    Ask the compiled generator for a seed, exactly as before.
``enumerate``
    Synthesize a tiny input directly.  The small profile is capped at 100 bytes
    (``SMALL_EXHAUSTIVE_MAX_BYTES``), which makes n <= 2-3 exhaustively
    enumerable.  Off by default — see ``DEFAULT_ENUMERATION_LIMIT`` for the
    measurement that says it does not pay on an ordinary array contract.
``mutate``
    Perturb an archive elite (:mod:`stress_corpus` / :mod:`stress_archive`).

Why the mix rather than pure mutation: measured on a 20-shape bug set, mutation
raises detection of low-entropy shapes (all-equal, sorted, two-valued) from 2%
to ~90%, but *lowers* detection of high-entropy shapes, because only one of the
array operators raises value entropy while four lower it — elites drift
low-entropy and descendants inherit the drift.  Keeping a recipe fraction fixes
that: detection by recipe mix was 0% -> 82%, 25% -> 86%, 50% -> 88%, 100% -> 44%.
So the driver interleaves, and ``DEFAULT_RECIPE_MIX`` sits inside the measured
band rather than at either end.

The driver never runs anything.  It hands out requests and is told what came
back, so it stays sandbox-agnostic and unit-testable.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from itertools import product
from typing import Any, Mapping

from .stress_archive import Archive, output_class
from .stress_corpus import Corpus, row_domain, set_scalar
from .stress_profiler import (
    ParsedInput,
    ProfileError,
    Profiler,
    SectionData,
    primary_section,
)

#: Fraction of post-seed small/random cases drawn from the recipe rather than
#: from mutation.  Measured optimum is a band (25%-50%); 0.40 sits inside it.
DEFAULT_RECIPE_MIX = 0.40

#: Recipe cases observed before mutation is allowed to start.  Below ~20 the
#: archive has not seen the large-n shapes the round-robin schedule reaches, and
#: the small-profile n=max shape gets lost.
DEFAULT_SEED_CASES = 20

#: Tiny-input enumeration is off by default.  It is correct and cheap, but
#: measured *negative* on an n<=40 / 0..9 array contract: the sweep consumed 110
#: of 1000 cases and displaced mutation, dropping detection from 86% to 84%.  The
#: shapes it guarantees (n<=2) were already reached by every arm.  Raise it for a
#: contract whose whole input space is genuinely tiny, where an exhaustive sweep
#: proves something sampling cannot.  Enumeration stops at the last row count
#: that fits entirely, so a budget is never spent on a truncated sweep.
DEFAULT_ENUMERATION_LIMIT = 0

#: Give up on mutation if this fraction of proposals is rejected by the sandbox
#: validator, measured after MIN_REJECT_SAMPLE mutated cases.  The Python-side
#: clamping cannot see cross-field constraints (sum limits, connectivity), so a
#: contract can exist where mutation is simply not viable.  Fail back to the
#: recipe rather than burning the budget.
MAX_REJECT_RATIO = 0.25
MIN_REJECT_SAMPLE = 20

#: Mutation is small-profile only.  Large inputs run to 32 MB; parsing and
#: archiving those would cost more memory than the diversity is worth.
MUTABLE_PROFILES = frozenset({"small"})


@dataclass(frozen=True, slots=True)
class CaseRequest:
    """One case to run.  ``data is None`` means "invoke the generator"."""

    source: str
    seed: int
    data: bytes | None = None
    origin: str = ""
    parent: str | None = None

    @property
    def synthetic(self) -> bool:
        return self.data is not None


@dataclass(slots=True)
class SearchStats:
    recipe: int = 0
    enumerated: int = 0
    mutated: int = 0
    rejected: int = 0
    stalls: int = 0
    disabled_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "recipe": self.recipe,
            "enumerated": self.enumerated,
            "mutated": self.mutated,
            "rejected": self.rejected,
            "stalls": self.stalls,
            "disabled_reason": self.disabled_reason,
        }


def _bounds_declared(section: SectionData, scalars: Mapping[str, int]) -> bool:
    """True when every payload column has a contract-declared numeric domain.

    ``row_domain`` falls back to the values observed in the input when a bound is
    absent, which is right for mutation — it perturbs an input that already
    passed the validator — but wrong for enumeration.  A domain inferred from one
    template can be a single value, and sweeping it would report an exhaustive
    sweep of a domain the contract never specified.  Require the real thing.
    """

    spec = section.spec
    for column in range(len(section.rows[0]) if section.rows else 0):
        field = spec.fields[min(column, len(spec.fields) - 1)]
        low = field.minimum if field.minimum_ref is None else scalars.get(field.minimum_ref)
        high = field.maximum if field.maximum_ref is None else scalars.get(field.maximum_ref)
        if low is None or high is None:
            return False
    return True


def enumerate_tiny(
    profiler: Profiler,
    template: bytes,
    *,
    limit: int = DEFAULT_ENUMERATION_LIMIT,
    max_bytes: int | None = None,
) -> list[bytes]:
    """Exhaustively enumerate the smallest inputs, using ``template`` for layout.

    Rewriting a real generator output rather than synthesizing bytes keeps the
    wire format exactly right — spacing, inline-vs-lines, trailing newline — with
    no second serializer to keep in step with the contract.

    Enumerates by ascending row count and stops before the first count that does
    not fit in ``limit``, so the result is always a complete sweep of every count
    it includes.  Returns ``[]`` when the contract is not parseable, the payload
    is not numeric, or the domain is too large to be worth enumerating.
    """

    if not profiler.structural:
        return []
    try:
        parsed = profiler.parse(template)
    except ProfileError:
        return []
    section = primary_section(parsed)
    if section is None or section.spec.kind == "string":
        return []
    if not _bounds_declared(section, parsed.scalars):
        return []
    columns = row_domain(section, parsed.scalars)
    if not columns:
        return []
    count_target = section.spec.count_from
    if not isinstance(count_target, str):
        return []
    bounds = profiler.ranges.get(count_target)
    if bounds is None:
        return []
    low, high = bounds
    domain = [range(start, stop + 1) for start, stop in columns]
    per_row = 1
    for span in domain:
        per_row *= len(span)
        if per_row > limit:
            return []
    results: list[bytes] = []
    for count in range(max(1, low), high + 1):
        block: list[bytes] = []
        total = per_row**count
        if total > limit - len(results):
            break
        for combination in product(*(product(*domain) for _ in range(count))):
            rendered = _render_rows(parsed, section, count_target, list(combination))
            if rendered is None:
                return results
            if max_bytes is not None and len(rendered) > max_bytes:
                return results
            block.append(rendered)
        results.extend(block)
        if len(results) >= limit:
            break
    return results


def _render_rows(
    parsed: ParsedInput,
    section: SectionData,
    count_target: str,
    rows: list[tuple[int, ...]],
) -> bytes | None:
    """Write ``rows`` into the template and re-serialize."""

    section.rows = [[str(value) for value in row] for row in rows]
    if not set_scalar(parsed, count_target, len(rows)):
        return None
    return parsed.render()


class SearchDriver:
    """Decides where each stress case's input comes from.

    Lifecycle per case: :meth:`next_case` -> run it -> :meth:`record` (or
    :meth:`reject` when the sandbox validator refused a synthesized input).
    """

    def __init__(
        self,
        contract: Mapping[str, Any] | None,
        *,
        first_seed: int = 0,
        max_bytes: int | None = None,
        rng_seed: int = 0,
        recipe_mix: float = DEFAULT_RECIPE_MIX,
        seed_cases: int = DEFAULT_SEED_CASES,
        enumeration_limit: int = DEFAULT_ENUMERATION_LIMIT,
    ) -> None:
        if not 0.0 <= recipe_mix <= 1.0:
            raise ValueError("recipe_mix must be within [0, 1]")
        self.profiler = Profiler(contract)
        self.max_bytes = max_bytes
        self.recipe_mix = recipe_mix
        self.seed_cases = max(0, seed_cases)
        self.enumeration_limit = max(0, enumeration_limit)
        self.next_seed = first_seed
        self.rng = random.Random(rng_seed)
        self.stats = SearchStats()
        self.archive: Archive | None = None
        if self.profiler.structural:
            self.archive = Archive(
                Corpus(self.profiler, max_bytes=max_bytes, seed=rng_seed)
            )
        else:
            self.stats.disabled_reason = self.profiler.reason or "contract not parseable"
        self._pending: list[bytes] = []
        self._enumerated = False
        self._observed = 0
        self._mutation_attempts = 0

    # -- state -----------------------------------------------------------
    @property
    def active(self) -> bool:
        """True when the driver can contribute anything beyond recipe seeds."""

        return self.archive is not None

    def report(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"search": self.stats.as_dict()}
        if self.archive is not None:
            payload["diversity"] = self.archive.report(target_limit=0)
        return payload

    # -- case selection --------------------------------------------------
    def next_case(self, profile: str, case_kind: str) -> CaseRequest:
        """The next input source for a ``(profile, case_kind)`` slot.

        Deterministic prelude cases (``lower_bound``, ``upper_bound``) and every
        non-mutable profile always go to the recipe: those slots exist to pin
        exact boundary shapes, and substituting a mutated input would silently
        drop the boundary the schedule promised to test.
        """

        if (
            self.archive is None
            or profile not in MUTABLE_PROFILES
            or case_kind != "random"
        ):
            return self._recipe_case()
        if self._pending:
            return self._synthetic_case(self._pending.pop(0), "enumerate", "enumerated")
        if self._observed < self.seed_cases:
            return self._recipe_case()
        if not self._enumerated:
            self._enumerated = True
            self._pending = self._seed_enumeration()
            if self._pending:
                return self._synthetic_case(
                    self._pending.pop(0), "enumerate", "enumerated"
                )
        if self.rng.random() < self.recipe_mix:
            return self._recipe_case()
        proposal = self.archive.propose()
        if proposal is None:
            self.stats.stalls += 1
            return self._recipe_case()
        data, origin, parent = proposal
        self._mutation_attempts += 1
        return self._synthetic_case(data, "mutate", origin, parent=parent)

    def _recipe_case(self) -> CaseRequest:
        request = CaseRequest("recipe", self.next_seed, origin="recipe")
        self.next_seed += 1
        return request

    def _synthetic_case(
        self, data: bytes, source: str, origin: str, *, parent: str | None = None
    ) -> CaseRequest:
        """Synthesized inputs still consume a seed number.

        The seed is what failure assets, resume bookkeeping, and the dashboard
        are keyed by, so a synthesized case must not reuse the previous one.
        """

        request = CaseRequest(source, self.next_seed, data=data, origin=origin, parent=parent)
        self.next_seed += 1
        return request

    def _seed_enumeration(self) -> list[bytes]:
        """Exhaustive tiny sweep, seeded from the smallest elite as template."""

        if self.enumeration_limit <= 0 or self.archive is None:
            return []
        entries = [entry for entry in self.archive.entries if entry.features.get("parsed")]
        if not entries:
            return []
        template = min(entries, key=lambda entry: entry.size).data
        return enumerate_tiny(
            self.profiler,
            template,
            limit=self.enumeration_limit,
            max_bytes=self.max_bytes,
        )

    # -- feedback --------------------------------------------------------
    def record(
        self,
        request: CaseRequest,
        input_data: bytes,
        *,
        reference_output: bytes | None = None,
    ) -> None:
        """Feed a completed case back into the archive.

        ``reference_output`` supplies the behavioural signal.  It must come from
        a reference, never from the solution under test — keying cells on the
        solution's output would make the search chase the bug it is meant to be
        looking for.
        """

        if self.archive is None:
            return
        if request.source == "recipe":
            self.stats.recipe += 1
        elif request.source == "enumerate":
            self.stats.enumerated += 1
        else:
            self.stats.mutated += 1
        self._observed += 1
        signal = output_class(reference_output) if reference_output is not None else None
        try:
            self.archive.observe(
                input_data,
                origin=request.origin or request.source,
                parent=request.parent,
                signal=signal,
            )
        except ProfileError:
            # An unparseable input is not a search failure: the archive simply
            # cannot key it.  Keep running the case stream.
            return

    def reject(self, request: CaseRequest) -> None:
        """The sandbox validator refused a synthesized input.

        Not a generator bug and not a solution bug, so it must not save failure
        assets or stop the run.  Count it, and if mutation turns out to be
        systematically invalid under this contract, switch it off.
        """

        self.stats.rejected += 1
        if request.source == "recipe":
            return
        if (
            self._mutation_attempts >= MIN_REJECT_SAMPLE
            and self.stats.rejected > MAX_REJECT_RATIO * self._mutation_attempts
        ):
            self.archive = None
            self._pending = []
            self.stats.disabled_reason = "validator rejected too many synthesized inputs"

    # -- persistence -----------------------------------------------------
    def save(self, path: str) -> None:
        if self.archive is not None:
            self.archive.save(path)

    def load(self, path: str) -> int:
        if self.archive is None:
            return 0
        admitted = self.archive.load(path)
        self._observed = max(self._observed, len(self.archive))
        return admitted


__all__ = [
    "CaseRequest",
    "DEFAULT_ENUMERATION_LIMIT",
    "DEFAULT_RECIPE_MIX",
    "DEFAULT_SEED_CASES",
    "MUTABLE_PROFILES",
    "SearchDriver",
    "SearchStats",
    "enumerate_tiny",
]
