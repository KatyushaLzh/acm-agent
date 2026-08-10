from __future__ import annotations

import unittest

from tools.acm_agent.stress_profiler import (
    ProfileError,
    Profiler,
    build_specs,
    descriptor,
    primary_section,
)


def list_contract(*, minimum: int = -9, maximum: int = 9) -> dict:
    return {
        "syntax": {
            "mode": "single_case",
            "sections": [
                {"id": "header", "kind": "scalar", "fields": [{"name": "n", "type": "int"}]},
                {
                    "id": "values",
                    "kind": "list",
                    "count_from": "header.n",
                    "fields": [
                        {"name": "a", "type": "int", "minimum": minimum, "maximum": maximum}
                    ],
                },
            ],
        },
        "constraints": [
            {"kind": "range", "target": "header.n", "args": {"minimum": 1, "maximum": 10}}
        ],
    }


GRAPH_CONTRACT = {
    "syntax": {
        "mode": "single_case",
        "sections": [
            {
                "id": "header",
                "kind": "scalar",
                "fields": [{"name": "n", "type": "int"}, {"name": "m", "type": "int"}],
            },
            {
                "id": "edges",
                "kind": "edge_list",
                "count_from": "header.m",
                "fields": [
                    {"name": "u", "type": "int", "minimum": 1, "maximum": "header.n"},
                    {"name": "v", "type": "int", "minimum": 1, "maximum": "header.n"},
                ],
            },
        ],
    },
    "constraints": [
        {"kind": "range", "target": "header.n", "args": {"minimum": 1, "maximum": 100}},
        {"kind": "range", "target": "header.m", "args": {"minimum": 0, "maximum": 200}},
    ],
}

INTERVAL_CONTRACT = {
    "syntax": {
        "mode": "single_case",
        "sections": [
            {"id": "header", "kind": "scalar", "fields": [{"name": "n", "type": "int"}]},
            {
                "id": "segs",
                "kind": "list",
                "count_from": "header.n",
                "fields": [
                    {"name": "l", "type": "int", "minimum": 1, "maximum": 20},
                    {"name": "r", "type": "int", "minimum": 1, "maximum": 20},
                ],
            },
        ],
    },
    "constraints": [
        {"kind": "range", "target": "header.n", "args": {"minimum": 1, "maximum": 6}}
    ],
}

MATRIX_CONTRACT = {
    "syntax": {
        "mode": "single_case",
        "sections": [
            {
                "id": "header",
                "kind": "scalar",
                "fields": [
                    {"name": "n", "type": "int"},
                    {"name": "cols", "type": "int"},
                ],
            },
            {
                "id": "grid",
                "kind": "matrix",
                "count_from": "header.n",
                "fields": [{"name": "cell", "type": "int", "minimum": 0, "maximum": 9}],
            },
        ],
    },
    "constraints": [
        {"kind": "range", "target": "header.n", "args": {"minimum": 1, "maximum": 5}},
        {"kind": "range", "target": "header.cols", "args": {"minimum": 1, "maximum": 5}},
    ],
}

STRING_CONTRACT = {
    "syntax": {
        "mode": "single_case",
        "sections": [
            {"id": "header", "kind": "scalar", "fields": [{"name": "n", "type": "int"}]},
            {
                "id": "text",
                "kind": "string",
                "count_from": "header.n",
                "fields": [{"name": "s", "type": "string"}],
            },
        ],
    },
    "constraints": [
        {"kind": "range", "target": "header.n", "args": {"minimum": 1, "maximum": 12}}
    ],
}


class BuildSpecsTest(unittest.TestCase):
    def test_rejects_unsupported_shapes(self) -> None:
        for mutate, reason in (
            (lambda c: c["syntax"].__setitem__("mode", "multi_case"), "mode"),
            (lambda c: c["syntax"].__setitem__("sections", []), "sections"),
            (
                lambda c: c["syntax"]["sections"][1].__setitem__("kind", "operation_stream"),
                "kind",
            ),
            (lambda c: c["syntax"]["sections"][1].__setitem__("kind", "raw"), "kind"),
            (
                lambda c: c["syntax"]["sections"][1].__setitem__(
                    "variants", [{"tag": "add", "fields": []}]
                ),
                "variants",
            ),
            (lambda c: c["syntax"]["sections"][1].pop("count_from"), "count"),
        ):
            contract = list_contract()
            mutate(contract)
            with self.subTest(reason=reason):
                with self.assertRaises(ProfileError):
                    build_specs(contract)

    def test_unparseable_contract_degrades_instead_of_raising(self) -> None:
        profiler = Profiler({"syntax": {"mode": "multi_case", "sections": []}})
        self.assertFalse(profiler.structural)
        self.assertIsNotNone(profiler.reason)
        features = profiler.features(b"5\n1 2 3 4 5\n")
        self.assertFalse(features["parsed"])
        # A vector is still produced, so unparseable problems keep a diversity
        # signal even though structural mutation is unavailable.
        self.assertEqual(features["bytes"], 12)
        self.assertEqual(features["tokens"], 6)
        self.assertEqual(descriptor(features)[0], "unparsed")


class ParseTest(unittest.TestCase):
    def test_round_trip_is_byte_exact(self) -> None:
        profiler = Profiler(list_contract())
        for raw in (
            b"5\n1 2 3 4 5\n",
            b"3\n1\n2\n3\n",
            b"3\n1 2 3",
            b"1\n7\n",
        ):
            with self.subTest(raw=raw):
                self.assertEqual(profiler.parse(raw).render(), raw)

    def test_trailing_tokens_are_rejected(self) -> None:
        profiler = Profiler(list_contract())
        with self.assertRaises(ProfileError):
            profiler.parse(b"2\n1 2 3\n")

    def test_truncated_input_is_rejected(self) -> None:
        profiler = Profiler(list_contract())
        with self.assertRaises(ProfileError):
            profiler.parse(b"5\n1 2\n")

    def test_scalars_are_bound_for_count_resolution(self) -> None:
        parsed = Profiler(GRAPH_CONTRACT).parse(b"4 3\n1 2\n2 3\n3 4\n")
        self.assertEqual(parsed.scalars, {"header.n": 4, "header.m": 3})
        section = primary_section(parsed)
        assert section is not None
        self.assertEqual(section.spec.id, "edges")
        self.assertEqual(len(section.rows), 3)

    def test_matrix_columns_come_from_the_sibling_scalar(self) -> None:
        profiler = Profiler(MATRIX_CONTRACT)
        raw = b"2 3\n1 2 3\n4 5 6\n"
        self.assertEqual(profiler.parse(raw).render(), raw)
        features = profiler.features(raw)
        self.assertEqual(features["shape"], "matrix")
        self.assertEqual((features["count"], features["cols"]), (2, 3))
        self.assertEqual(profiler.cols_target(profiler.specs[1], profiler.parse(raw)), "header.cols")

    def test_matrix_without_a_column_scalar_is_unparseable(self) -> None:
        contract = {
            "syntax": {
                "mode": "single_case",
                "sections": [
                    {"id": "header", "kind": "scalar", "fields": [{"name": "n", "type": "int"}]},
                    {
                        "id": "grid",
                        "kind": "matrix",
                        "count_from": "header.n",
                        "fields": [{"name": "cell", "type": "int"}],
                    },
                ],
            },
            "constraints": [],
        }
        with self.assertRaises(ProfileError):
            Profiler(contract).parse(b"2\n1 2\n3 4\n")


class ArrayFeatureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.profiler = Profiler(list_contract())

    def test_values_drive_the_vector(self) -> None:
        features = self.profiler.features(b"5\n1 2 3 4 5\n")
        self.assertEqual(features["shape"], "array")
        self.assertEqual(features["count"], 5)
        self.assertEqual(features["distinct_ratio"], 1.0)
        self.assertEqual(features["sorted_ratio"], 1.0)
        self.assertEqual(features["max_multiplicity_ratio"], 0.2)
        self.assertEqual(features["boundary_ratio"], 0.0)

    def test_constant_array_is_fully_multiple(self) -> None:
        features = self.profiler.features(b"5\n7 7 7 7 7\n")
        self.assertEqual(features["distinct_ratio"], 0.2)
        self.assertEqual(features["max_multiplicity_ratio"], 1.0)

    def test_reverse_sorted_array_has_zero_sortedness(self) -> None:
        self.assertEqual(self.profiler.features(b"5\n5 4 3 2 1\n")["sorted_ratio"], 0.0)

    def test_boundary_ratio_counts_declared_bounds(self) -> None:
        features = self.profiler.features(b"4\n-9 9 -9 9\n")
        self.assertEqual(features["boundary_ratio"], 1.0)
        self.assertEqual(features["negative_ratio"], 0.5)

    def test_boundary_ratio_is_relative_to_the_contract_not_the_data(self) -> None:
        # The same bytes score differently under a wider domain.  This is the
        # property a generator-declared "boundary_values" tag cannot have.
        raw = b"4\n-9 9 -9 9\n"
        wide = Profiler(list_contract(minimum=-100, maximum=100))
        self.assertEqual(self.profiler.features(raw)["boundary_ratio"], 1.0)
        self.assertEqual(wide.features(raw)["boundary_ratio"], 0.0)


class GraphFeatureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.profiler = Profiler(GRAPH_CONTRACT)

    def test_path_is_a_connected_tree(self) -> None:
        features = self.profiler.features(b"4 3\n1 2\n2 3\n3 4\n")
        self.assertEqual(features["shape"], "graph")
        self.assertEqual(features["vertices"], 4)
        self.assertEqual(features["components"], 1)
        self.assertTrue(features["connected"])
        self.assertTrue(features["is_tree"])

    def test_isolated_vertices_are_counted(self) -> None:
        features = self.profiler.features(b"5 2\n1 2\n3 4\n")
        # Two edge components plus the untouched vertex 5.
        self.assertEqual(features["components"], 3)
        self.assertFalse(features["connected"])

    def test_self_loops_and_parallel_edges_are_separated(self) -> None:
        features = self.profiler.features(b"3 3\n1 1\n1 2\n1 2\n")
        self.assertEqual(features["self_loop_count"], 1)
        self.assertEqual(features["parallel_edge_count"], 1)
        self.assertFalse(features["is_tree"])

    def test_star_concentrates_degree(self) -> None:
        star = self.profiler.features(b"5 4\n1 2\n1 3\n1 4\n1 5\n")
        path = self.profiler.features(b"5 4\n1 2\n2 3\n3 4\n4 5\n")
        self.assertGreater(star["max_degree_ratio"], path["max_degree_ratio"])
        self.assertGreater(star["degree_gini"], path["degree_gini"])


class IntervalFeatureTest(unittest.TestCase):
    def setUp(self) -> None:
        self.profiler = Profiler(INTERVAL_CONTRACT)

    def test_disjoint_intervals_never_stack(self) -> None:
        features = self.profiler.features(b"3\n1 2\n4 5\n7 8\n")
        self.assertEqual(features["shape"], "interval")
        self.assertEqual(features["max_overlap"], 1)
        self.assertEqual(features["nested_ratio"], 0.0)

    def test_nesting_and_overlap_are_measured(self) -> None:
        features = self.profiler.features(b"3\n1 20\n2 5\n3 4\n")
        self.assertEqual(features["max_overlap"], 3)
        self.assertEqual(features["nested_ratio"], round(2 / 3, 6))

    def test_points_are_detected(self) -> None:
        self.assertEqual(self.profiler.features(b"2\n3 3\n5 5\n")["point_ratio"], 1.0)


class StringFeatureTest(unittest.TestCase):
    def test_count_is_the_length_not_the_row_count(self) -> None:
        features = Profiler(STRING_CONTRACT).features(b"6\nabcabc\n")
        self.assertEqual(features["shape"], "string")
        self.assertEqual(features["count"], 6)
        self.assertEqual(features["length"], 6)
        self.assertEqual(features["alphabet_size"], 3)

    def test_runs_lower_the_run_ratio(self) -> None:
        profiler = Profiler(STRING_CONTRACT)
        alternating = profiler.features(b"6\nababab\n")
        runs = profiler.features(b"6\naaabbb\n")
        self.assertGreater(alternating["run_ratio"], runs["run_ratio"])


class DescriptorTest(unittest.TestCase):
    def cells(self, profiler: Profiler, inputs: tuple[bytes, ...]) -> list[tuple]:
        return [descriptor(profiler.features(raw)) for raw in inputs]

    def test_distinct_value_shapes_land_in_distinct_cells(self) -> None:
        profiler = Profiler(list_contract())
        cells = self.cells(
            profiler,
            (
                b"5\n1 2 3 4 5\n",  # sorted, all distinct
                b"5\n5 4 3 2 1\n",  # reverse sorted
                b"5\n7 7 7 7 7\n",  # constant
                b"5\n-9 -9 9 9 9\n",  # boundary heavy
            ),
        )
        self.assertEqual(len(set(cells)), len(cells))

    def test_cells_are_coarse_enough_to_group_equivalents(self) -> None:
        # Relabelling values without changing shape must not mint a new cell,
        # otherwise every input is "novel" and the archive becomes a log.
        profiler = Profiler(list_contract())
        first, second = self.cells(profiler, (b"5\n1 2 3 4 5\n", b"5\n2 3 4 5 6\n"))
        self.assertEqual(first, second)

    def test_cells_are_hashable_and_stable(self) -> None:
        profiler = Profiler(GRAPH_CONTRACT)
        raw = b"4 3\n1 2\n2 3\n3 4\n"
        cell = descriptor(profiler.features(raw))
        self.assertEqual(cell, descriptor(profiler.features(raw)))
        self.assertEqual(len({cell, descriptor(profiler.features(raw))}), 1)

    def test_matrix_width_is_its_own_axis(self) -> None:
        profiler = Profiler(MATRIX_CONTRACT)
        narrow = descriptor(profiler.features(b"2 2\n1 2\n3 4\n"))
        wide = descriptor(profiler.features(b"2 5\n1 2 3 4 5\n6 7 8 9 0\n"))
        self.assertNotEqual(narrow, wide)

    def test_graph_topology_separates_cells(self) -> None:
        profiler = Profiler(GRAPH_CONTRACT)
        cells = self.cells(
            profiler,
            (
                b"5 4\n1 2\n2 3\n3 4\n4 5\n",  # path: connected tree
                b"5 4\n1 2\n1 3\n1 4\n1 5\n",  # star: connected tree, hub
                b"5 2\n1 2\n3 4\n",  # disconnected
                b"3 3\n1 1\n1 2\n1 2\n",  # self loop + parallel
            ),
        )
        self.assertEqual(len(set(cells)), len(cells))


if __name__ == "__main__":
    unittest.main()
