from __future__ import annotations

import unittest

from tools.acm_agent.stress_corpus import Corpus
from tools.acm_agent.stress_profiler import Profiler, descriptor
from tests.test_stress_profiler import (
    GRAPH_CONTRACT,
    INTERVAL_CONTRACT,
    MATRIX_CONTRACT,
    STRING_CONTRACT,
    list_contract,
)


def array_validator(data: bytes) -> bool:
    """Stand-in for the sandboxed validator: n agrees, values inside [-9, 9]."""

    try:
        tokens = [int(token) for token in data.split()]
    except ValueError:
        return False
    if not tokens:
        return False
    count, values = tokens[0], tokens[1:]
    return 1 <= count <= 10 and len(values) == count and all(-9 <= v <= 9 for v in values)


def graph_validator(data: bytes) -> bool:
    try:
        tokens = [int(token) for token in data.split()]
    except ValueError:
        return False
    if len(tokens) < 2:
        return False
    vertices, edges, rest = tokens[0], tokens[1], tokens[2:]
    if not (1 <= vertices <= 100 and 0 <= edges <= 200 and len(rest) == 2 * edges):
        return False
    return all(1 <= endpoint <= vertices for endpoint in rest)


def interval_validator(data: bytes) -> bool:
    try:
        tokens = [int(token) for token in data.split()]
    except ValueError:
        return False
    count, rest = tokens[0], tokens[1:]
    if not (1 <= count <= 6 and len(rest) == 2 * count):
        return False
    return all(1 <= low <= high <= 20 for low, high in zip(rest[::2], rest[1::2]))


def matrix_validator(data: bytes) -> bool:
    try:
        tokens = [int(token) for token in data.split()]
    except ValueError:
        return False
    if len(tokens) < 2:
        return False
    rows, cols, rest = tokens[0], tokens[1], tokens[2:]
    if not (1 <= rows <= 5 and 1 <= cols <= 5 and len(rest) == rows * cols):
        return False
    return all(0 <= value <= 9 for value in rest)


def string_validator(data: bytes) -> bool:
    parts = data.split()
    if len(parts) != 2:
        return False
    try:
        count = int(parts[0])
    except ValueError:
        return False
    text = parts[1].decode("ascii", "replace")
    return 1 <= count <= 12 and len(text) == count and set(text) <= set("abc")


def explore(
    contract: dict,
    seed_input: bytes,
    validator,
    *,
    budget: int = 300,
    seed: int = 4242,
    max_bytes: int | None = 100,
) -> tuple[Corpus, list[bytes]]:
    """Run the observe/propose loop and return the corpus plus every proposal."""

    corpus = Corpus(Profiler(contract), max_bytes=max_bytes, seed=seed, validator=validator)
    corpus.observe(seed_input, origin="seed")
    produced: list[bytes] = []
    for _ in range(budget):
        proposal = corpus.propose()
        if proposal is None:
            break
        data, origin, parent = proposal
        produced.append(data)
        corpus.observe(data, origin=origin, parent=parent)
    return corpus, produced


class ObserveTest(unittest.TestCase):
    def setUp(self) -> None:
        self.corpus = Corpus(Profiler(list_contract()), max_bytes=100, seed=1)

    def test_new_cell_is_admitted(self) -> None:
        self.assertEqual(self.corpus.observe(b"5\n1 2 3 4 5\n"), "new")
        self.assertEqual(len(self.corpus), 1)

    def test_same_cell_with_larger_input_is_a_duplicate(self) -> None:
        self.corpus.observe(b"5\n1 2 3 4 5\n")
        # Same shape, shifted values: same cell, and not smaller.
        self.assertEqual(self.corpus.observe(b"5\n2 3 4 5 6\n"), "duplicate")
        self.assertEqual(len(self.corpus), 1)

    def test_smaller_input_replaces_the_elite(self) -> None:
        # Same behaviour cell (constant, sorted, no bound touched, no negatives)
        # but fewer bytes, so the archive keeps the smaller representative.
        corpus = Corpus(
            Profiler(list_contract(minimum=-100, maximum=100)), max_bytes=100, seed=1
        )
        larger, smaller = b"5\n50 50 50 50 50\n", b"5\n1 1 1 1 1\n"
        self.assertEqual(corpus.observe(larger), "new")
        self.assertEqual(corpus.observe(smaller), "improved")
        self.assertEqual(len(corpus), 1)
        self.assertEqual(corpus.entries[0].data, smaller)

    def test_signal_extends_the_cell_key(self) -> None:
        # Identical bytes-shape, different observed behaviour: two cells.  This
        # is the hook that turns "every brute output was 0" into a real axis.
        self.corpus.observe(b"5\n1 2 3 4 5\n", signal="answer:0")
        self.assertEqual(self.corpus.observe(b"5\n2 3 4 5 6\n", signal="answer:7"), "new")
        self.assertEqual(len(self.corpus), 2)

    def test_report_counts_cells_and_origins(self) -> None:
        self.corpus.observe(b"5\n1 2 3 4 5\n", origin="seed")
        self.corpus.observe(b"5\n7 7 7 7 7\n", origin="set_constant")
        report = self.corpus.report()
        self.assertEqual(report["cells"], 2)
        self.assertEqual(report["observations"], 2)
        self.assertEqual(report["origins"], {"seed": 1, "set_constant": 1})


class ProposeContractTest(unittest.TestCase):
    def test_every_proposal_passes_the_validator(self) -> None:
        for label, contract, seed_input, validator in (
            ("array", list_contract(), b"5\n1 2 3 4 5\n", array_validator),
            ("graph", GRAPH_CONTRACT, b"4 3\n1 2\n2 3\n3 4\n", graph_validator),
            ("interval", INTERVAL_CONTRACT, b"3\n1 5\n2 8\n9 12\n", interval_validator),
            ("string", STRING_CONTRACT, b"6\nabcabc\n", string_validator),
            ("matrix", MATRIX_CONTRACT, b"2 3\n1 2 3\n4 5 6\n", matrix_validator),
        ):
            with self.subTest(shape=label):
                corpus, produced = explore(contract, seed_input, validator)
                self.assertTrue(produced, "no proposals were produced")
                for data in produced:
                    self.assertTrue(validator(data), f"invalid proposal {data!r}")
                # Domain clamping should make validator rejection rare enough
                # that the search is not wasting most of its attempts on it.
                stats = corpus.report()["propose"]
                self.assertLess(stats["rejected"], stats["produced"])

    def test_proposals_respect_the_byte_budget(self) -> None:
        corpus, produced = explore(
            list_contract(), b"5\n1 2 3 4 5\n", array_validator, max_bytes=16
        )
        self.assertTrue(produced)
        for data in produced:
            self.assertLessEqual(len(data), 16)
        self.assertTrue(all(entry.size <= 16 for entry in corpus.entries))

    def test_proposals_are_never_verbatim_repeats(self) -> None:
        _, produced = explore(list_contract(), b"5\n1 2 3 4 5\n", array_validator)
        self.assertEqual(len(produced), len(set(produced)))

    def test_empty_corpus_proposes_nothing(self) -> None:
        corpus = Corpus(Profiler(list_contract()), max_bytes=100, seed=1)
        self.assertIsNone(corpus.propose())

    def test_unparsable_parent_is_not_mutated(self) -> None:
        # An input that does not match its contract still occupies a cell, but
        # structural mutation must refuse it rather than emit garbage.
        corpus = Corpus(Profiler(list_contract()), max_bytes=100, seed=1)
        corpus.observe(b"not an input at all")
        self.assertEqual(len(corpus), 1)
        self.assertIsNone(corpus.propose())


class DomainSafetyTest(unittest.TestCase):
    def test_values_stay_inside_declared_bounds(self) -> None:
        _, produced = explore(list_contract(), b"5\n1 2 3 4 5\n", array_validator)
        for data in produced:
            values = [int(token) for token in data.split()[1:]]
            self.assertTrue(all(-9 <= value <= 9 for value in values), data)

    def test_endpoints_clamp_to_the_instance_vertex_count(self) -> None:
        # ``maximum: "header.n"`` resolves to 100 globally but 4 for this input.
        # Clamping to the global bound would make almost every child invalid.
        _, produced = explore(
            GRAPH_CONTRACT, b"4 3\n1 2\n2 3\n3 4\n", graph_validator, budget=200
        )
        self.assertTrue(produced)
        for data in produced:
            tokens = [int(token) for token in data.split()]
            vertices, rest = tokens[0], tokens[2:]
            self.assertTrue(all(1 <= x <= vertices for x in rest), data)

    def test_interval_order_is_repaired(self) -> None:
        _, produced = explore(
            INTERVAL_CONTRACT, b"3\n1 5\n2 8\n9 12\n", interval_validator, budget=200
        )
        self.assertTrue(produced)
        for data in produced:
            rest = [int(token) for token in data.split()[1:]]
            for low, high in zip(rest[::2], rest[1::2]):
                self.assertLessEqual(low, high, data)

    def test_declared_count_always_matches_the_payload(self) -> None:
        _, produced = explore(list_contract(), b"5\n1 2 3 4 5\n", array_validator)
        for data in produced:
            tokens = data.split()
            self.assertEqual(int(tokens[0]), len(tokens) - 1, data)


class DeterminismTest(unittest.TestCase):
    def test_same_seed_yields_the_same_search(self) -> None:
        first, produced_first = explore(list_contract(), b"5\n1 2 3 4 5\n", array_validator)
        second, produced_second = explore(list_contract(), b"5\n1 2 3 4 5\n", array_validator)
        self.assertEqual(produced_first, produced_second)
        self.assertEqual(
            {entry.cell for entry in first.entries},
            {entry.cell for entry in second.entries},
        )

    def test_different_seeds_diverge(self) -> None:
        _, first = explore(list_contract(), b"5\n1 2 3 4 5\n", array_validator, seed=1)
        _, second = explore(list_contract(), b"5\n1 2 3 4 5\n", array_validator, seed=2)
        self.assertNotEqual(first, second)


class CoverageGrowthTest(unittest.TestCase):
    def test_one_seed_reaches_many_cells(self) -> None:
        corpus, _ = explore(list_contract(), b"5\n1 2 3 4 5\n", array_validator)
        self.assertGreater(len(corpus), 40)

    def test_search_covers_the_whole_count_axis(self) -> None:
        # The frozen recipe could only emit the sizes baked into it; resizing
        # operators must reach both ends of the declared count range.
        corpus, _ = explore(list_contract(), b"5\n1 2 3 4 5\n", array_validator)
        counts = {entry.features.get("count") for entry in corpus.entries}
        self.assertEqual(counts, set(range(1, 11)))

    def test_search_finds_shapes_the_seed_did_not_have(self) -> None:
        corpus, _ = explore(list_contract(), b"5\n1 2 3 4 5\n", array_validator)
        # The seed is sorted, all-distinct, and touches no domain bound.
        self.assertTrue(any(e.features.get("sorted_ratio") == 0.0 for e in corpus.entries))
        self.assertTrue(
            any(e.features.get("max_multiplicity_ratio") == 1.0 for e in corpus.entries)
        )
        self.assertTrue(any(e.features.get("boundary_ratio") == 1.0 for e in corpus.entries))

    def test_graph_search_reaches_topology_corners(self) -> None:
        corpus, _ = explore(
            GRAPH_CONTRACT, b"4 3\n1 2\n2 3\n3 4\n", graph_validator, budget=400
        )
        features = [entry.features for entry in corpus.entries]
        self.assertTrue(any(f.get("connected") for f in features))
        self.assertTrue(any(f.get("components", 0) > 1 for f in features))
        self.assertTrue(any(f.get("self_loop_count", 0) > 0 for f in features))
        self.assertTrue(any(f.get("parallel_edge_count", 0) > 0 for f in features))
        # ``resize_domain`` is the only way to move the vertex axis.
        self.assertGreater(len({f.get("vertices") for f in features}), 3)

    def test_matrix_search_moves_both_dimensions(self) -> None:
        corpus, _ = explore(
            MATRIX_CONTRACT, b"2 3\n1 2 3\n4 5 6\n", matrix_validator, budget=400
        )
        rows = {entry.features.get("count") for entry in corpus.entries}
        cols = {entry.features.get("cols") for entry in corpus.entries}
        self.assertGreater(len(rows), 2)
        self.assertGreater(len(cols), 2)

    def test_cells_match_their_stored_features(self) -> None:
        corpus, _ = explore(list_contract(), b"5\n1 2 3 4 5\n", array_validator)
        for entry in corpus.entries:
            self.assertEqual(entry.cell, descriptor(entry.features))


if __name__ == "__main__":
    unittest.main()
