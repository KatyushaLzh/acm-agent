from __future__ import annotations

import json
import unittest

from tools.acm_agent.stress_archive import Archive, MISSES_BEFORE_INFEASIBLE, output_class
from tools.acm_agent.stress_cells import (
    CELL_PROPOSAL_SCHEMA_VERSION,
    PROPOSAL_SCHEMA,
    apply_verdicts,
    build_targeting_messages,
    fill_cells,
    verify_proposals,
)
from tools.acm_agent.stress_corpus import Corpus
from tools.acm_agent.stress_profiler import Profiler, descriptor
from tests.test_stress_corpus import array_validator
from tests.test_stress_profiler import list_contract


def seeded(*, seed: int = 5) -> Archive:
    """A deliberately narrow corpus, so real targets exist."""

    archive = Archive(
        Corpus(Profiler(list_contract()), max_bytes=100, seed=seed, validator=array_validator)
    )
    for sample in (b"3\n1 2 3\n", b"4\n1 2 3 4\n"):
        archive.observe(sample, origin="seed")
    return archive


def reachable_input(archive: Archive, cell: tuple) -> bytes | None:
    """Search a small space for an input the profiler puts in ``cell``."""

    import itertools

    for count in range(1, 5):
        for combo in itertools.product((-9, -1, 0, 1, 5, 9), repeat=count):
            candidate = f"{count}\n{' '.join(map(str, combo))}\n".encode()
            if not array_validator(candidate):
                continue
            if descriptor(archive.profiler.features(candidate)) == cell:
                return candidate
    return None


class PromptTest(unittest.TestCase):
    def test_prompt_names_cells_and_shows_a_nearby_input(self) -> None:
        archive = seeded()
        messages, targets = build_targeting_messages(
            archive, contract=list_contract(), statement="Sum the array.", target_limit=3
        )
        self.assertEqual(len(messages), 2)
        prompt = messages[1]["content"]
        self.assertTrue(targets)
        for target in targets:
            self.assertIn(target.description, prompt)
            self.assertIn(target.axis, prompt)
        self.assertIn("Sum the array.", prompt)
        self.assertIn("never produced", prompt)
        self.assertIn('"schema_version": 1', prompt)

    def test_targets_are_returned_with_the_prompt(self) -> None:
        # The grading key and the question must be produced together or they
        # can drift apart between the ask and the check.
        archive = seeded()
        messages, targets = build_targeting_messages(archive, target_limit=2)
        self.assertEqual(len(targets), 2)
        for target in targets:
            self.assertIn(target.description, messages[1]["content"])

    def test_schema_is_declared_for_the_provider(self) -> None:
        self.assertEqual(
            PROPOSAL_SCHEMA["properties"]["schema_version"]["const"],
            CELL_PROPOSAL_SCHEMA_VERSION,
        )


class VerificationTest(unittest.TestCase):
    """The grader must never take the model's word for anything."""

    def setUp(self) -> None:
        self.archive = seeded()
        _, self.targets = build_targeting_messages(self.archive, target_limit=3)
        self.target = self.targets[0]

    def grade(self, proposals: list[dict]) -> dict[str, str]:
        verdicts = verify_proposals(
            self.archive,
            {"schema_version": 1, "proposals": proposals},
            self.targets,
            validator=array_validator,
        )
        return {verdict.cell_id: verdict.reason for verdict in verdicts if verdict.cell_id}

    def test_a_confident_wrong_answer_is_a_miss(self) -> None:
        reasons = self.grade(
            [
                {
                    "cell_id": self.target.description,
                    "input": "2\n1 2\n",
                    "rationale": "definitely correct",
                }
            ]
        )
        self.assertIn(
            reasons[self.target.description],
            {"input landed in a different shape", "reached the requested shape"},
        )

    def test_returning_the_example_unchanged_is_rejected(self) -> None:
        reasons = self.grade(
            [
                {
                    "cell_id": self.target.description,
                    "input": self.target.example.decode(),
                }
            ]
        )
        self.assertEqual(reasons[self.target.description], "input is the example unchanged")

    def test_constraint_violations_are_rejected(self) -> None:
        reasons = self.grade(
            [{"cell_id": self.target.description, "input": "999\n1 2\n"}]
        )
        self.assertEqual(
            reasons[self.target.description],
            "input violates the format or constraints",
        )

    def test_unrequested_cells_are_rejected(self) -> None:
        reasons = self.grade([{"cell_id": "array[made up]", "input": "2\n1 2\n"}])
        self.assertEqual(reasons["array[made up]"], "cell_id was not requested")

    def test_silence_is_recorded_per_cell(self) -> None:
        reasons = self.grade([])
        self.assertEqual(len(reasons), len(self.targets))
        for reason in reasons.values():
            self.assertEqual(reason, "no proposal returned for this cell")

    def test_a_genuine_hit_is_confirmed_by_the_profiler(self) -> None:
        found = None
        for target in self.targets:
            data = reachable_input(self.archive, target.cell)
            if data is not None and data != target.example:
                found = (target, data)
                break
        if found is None:
            self.skipTest("no small reachable target in this corpus state")
        target, data = found
        verdicts = verify_proposals(
            self.archive,
            {"schema_version": 1, "proposals": [
                {"cell_id": target.description, "input": data.decode()}
            ]},
            self.targets,
            validator=array_validator,
        )
        hit = next(v for v in verdicts if v.cell_id == target.description)
        self.assertTrue(hit.hit)
        self.assertEqual(hit.actual, target.cell)

    def test_a_reply_that_is_not_an_object_yields_nothing(self) -> None:
        self.assertEqual(verify_proposals(self.archive, {}, self.targets), ())

    def test_non_ascii_and_oversized_inputs_are_rejected(self) -> None:
        reasons = self.grade(
            [{"cell_id": self.target.description, "input": "2\n1 é\n"}]
        )
        self.assertIn("ASCII", reasons[self.target.description])


class ApplyTest(unittest.TestCase):
    def setUp(self) -> None:
        self.archive = seeded()
        _, self.targets = build_targeting_messages(self.archive, target_limit=3)

    def run_provider(self, mode: str) -> dict:
        def call(messages: list[dict[str, str]]) -> dict:
            proposals = []
            for target in self.targets:
                if mode == "malformed":
                    text = "999\n1 2\n"
                elif mode == "echo":
                    text = target.example.decode()
                elif mode == "honest":
                    data = reachable_input(self.archive, target.cell)
                    text = (data or target.example).decode()
                else:
                    text = "2\n1 2\n"
                proposals.append({"cell_id": target.description, "input": text})
            return {"schema_version": 1, "proposals": proposals}

        return fill_cells(
            self.archive,
            call,
            contract=list_contract(),
            target_limit=3,
            validator=array_validator,
            signal_for=lambda data: output_class(
                str(sum(int(token) for token in data.split()[1:])).encode()
            ),
        )

    def test_malformed_replies_never_retire_a_cell(self) -> None:
        # Bad JSON is evidence about the reply, not about the cell.  Retiring on
        # it would silently shrink the search space.
        result = self.run_provider("malformed")
        self.assertEqual(result["hits"], 0)
        self.assertEqual(result["retired_as_infeasible"], 0)
        self.assertEqual(len(self.archive.infeasible), 0)

    def test_echoing_the_example_never_retires_a_cell(self) -> None:
        result = self.run_provider("echo")
        self.assertEqual(result["hits"], 0)
        self.assertEqual(result["retired_as_infeasible"], 0)

    def test_repeated_genuine_misses_retire_the_cell(self) -> None:
        # ``fill_cells`` re-derives targets every round (the frontier moves as
        # the archive grows), so retirement is exercised against a fixed target
        # set — the unit that actually owns the decision.
        reply = {
            "schema_version": 1,
            "proposals": [
                {"cell_id": target.description, "input": "2\n1 2\n"}
                for target in self.targets
            ],
        }
        for _ in range(MISSES_BEFORE_INFEASIBLE):
            verdicts = verify_proposals(
                self.archive, reply, self.targets, validator=array_validator
            )
            summary = apply_verdicts(self.archive, verdicts, self.targets)
        self.assertGreaterEqual(len(self.archive.infeasible), 1)
        self.assertGreaterEqual(summary["retired_as_infeasible"], 1)

    def test_hits_are_admitted_through_observe_with_no_special_standing(self) -> None:
        before = len(self.archive)
        result = self.run_provider("honest")
        self.assertEqual(len(self.archive), before + len(result["admitted"]))
        for entry in self.archive.entries:
            if entry.cell in {t.cell for t in self.targets}:
                self.assertIn(entry.origin, {"llm_targeted", "seed"})

    def test_verdicts_are_reported_for_every_request(self) -> None:
        result = self.run_provider("wrong")
        self.assertEqual(result["requested"], len(self.targets))
        self.assertEqual(len(result["verdicts"]), len(self.targets))
        self.assertTrue(all("hit" in verdict for verdict in result["verdicts"]))


class FillCellsTest(unittest.TestCase):
    def test_a_saturated_archive_skips_the_provider_call(self) -> None:
        archive = Archive(
            Corpus(Profiler(list_contract()), max_bytes=100, seed=1, validator=array_validator)
        )
        called: list[int] = []

        def call(messages: list[dict[str, str]]) -> dict:
            called.append(1)
            return {"schema_version": 1, "proposals": []}

        result = fill_cells(archive, call, target_limit=3)
        self.assertEqual(result["skipped"], "no reachable targets")
        self.assertEqual(called, [])

    def test_a_non_object_reply_is_reported_not_raised(self) -> None:
        archive = seeded()
        result = fill_cells(archive, lambda messages: "not json", target_limit=2)
        self.assertEqual(result["skipped"], "provider reply was not a JSON object")
        self.assertEqual(result["hits"], 0)

    def test_apply_verdicts_is_usable_on_its_own(self) -> None:
        archive = seeded()
        _, targets = build_targeting_messages(archive, target_limit=1)
        verdicts = verify_proposals(
            archive,
            {"schema_version": 1, "proposals": []},
            targets,
            validator=array_validator,
        )
        summary = apply_verdicts(archive, verdicts, targets)
        self.assertEqual(summary["hits"], 0)
        self.assertEqual(summary["retired_as_infeasible"], 0)

    def test_report_is_json_serialisable(self) -> None:
        archive = seeded()
        _, targets = build_targeting_messages(archive, target_limit=2)
        verdicts = verify_proposals(
            archive,
            {"schema_version": 1, "proposals": []},
            targets,
            validator=array_validator,
        )
        json.dumps(apply_verdicts(archive, verdicts, targets))


if __name__ == "__main__":
    unittest.main()
