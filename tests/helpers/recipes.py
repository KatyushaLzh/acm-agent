"""Self-contained recipe fixtures shared by published stress tests."""

from __future__ import annotations

from tools.acm_agent.stress_recipe import SMALL_RECIPE_HARD_MAX_BYTES


def static_recipe() -> dict[str, object]:
    def case(profile: str, case_kind: str) -> dict[str, object]:
        small = profile == "small"
        return {
            "profile": profile,
            "case_kind": case_kind,
            "families": [
                {
                    "structure": {
                        "template_id": "array.uniform",
                        "parameters": {
                            "n_min": 1,
                            "n_max": 40 if small else 1000,
                            "value_min": 0,
                            "value_max": 9,
                        },
                    },
                    "labels": [],
                    "semantic_goals": ["seed_variation"],
                }
            ],
            "selection": {
                "policy": "balanced_round_robin_v1",
                "seed_stride": 1 if small else 5,
            },
            "serialization": {"format_id": "list_n"},
            "byte_budget": {
                "hard_max": (
                    SMALL_RECIPE_HARD_MAX_BYTES if small else 32 * 1024 * 1024
                ),
                "buckets": (
                    [
                        [1, 25],
                        [26, 50],
                        [51, 75],
                        [76, 100],
                        [101, SMALL_RECIPE_HARD_MAX_BYTES],
                    ]
                    if small
                    else [[1, 32 * 1024 * 1024]]
                ),
            },
        }

    return {
        "schema_version": 1,
        "engine": "local_templates_v1",
        "cases": [
            case("small", "lower_bound"),
            case("small", "random"),
            case("large", "upper_bound"),
            case("large", "random"),
        ],
    }


def p1111_recipe() -> dict[str, object]:
    def family(
        template_id: str,
        parameters: dict[str, object],
        goal: str,
        label: str,
        lo: int,
        hi: int,
    ) -> dict[str, object]:
        return {
            "structure": {"template_id": template_id, "parameters": parameters},
            "labels": [
                {
                    "template_id": label,
                    "parameters": {"label_min": lo, "label_max": hi},
                }
            ],
            "semantic_goals": [goal],
        }

    def case(
        profile: str,
        case_kind: str,
        families: list[dict[str, object]],
    ) -> dict[str, object]:
        return {
            "profile": profile,
            "case_kind": case_kind,
            "families": families,
            "selection": {
                "policy": "balanced_round_robin_v1",
                "seed_stride": 1 if profile == "small" else 5,
            },
            "serialization": {"format_id": "edge_list_n_m_u_v_w"},
            "byte_budget": {
                "hard_max": (
                    SMALL_RECIPE_HARD_MAX_BYTES
                    if profile == "small"
                    else 32 * 1024 * 1024
                ),
                "buckets": (
                    [
                        [1, 25],
                        [26, 50],
                        [51, 75],
                        [76, 100],
                        [101, SMALL_RECIPE_HARD_MAX_BYTES],
                    ]
                    if profile == "small"
                    else [[1, 32 * 1024 * 1024]]
                ),
            },
        }

    small_random = [
        family("graph.self_loops", {"n": 1, "m": 1}, "single_vertex", "label.equal", 1, 1),
        family("graph.components", {"n": 4, "m": 2, "component_count": 2}, "never_connects", "label.uniform", 1, 1),
        family("tree.star", {"n": 3}, "early_threshold_connects", "label.equal", 1, 1),
        family("tree.path", {"n": 4}, "last_threshold_connects", "label.distinct", 100, 102),
        family("tree.caterpillar", {"n": 3, "spine_length": 2}, "equal_labels", "label.equal", 1, 1),
        family("graph.self_loops", {"n": 4, "m": 4}, "self_loop", "label.uniform", 100, 999),
        family("graph.parallel_edges", {"n": 4, "m": 4}, "parallel_edges", "label.uniform", 100, 999),
        family("graph.connected", {"n": 4, "m": 4}, "connected", "label.uniform", 100, 999),
    ]
    return {
        "schema_version": 1,
        "engine": "local_templates_v1",
        "cases": [
            case("small", "lower_bound", [small_random[0]]),
            case("small", "random", small_random),
            case(
                "large",
                "upper_bound",
                [
                    family(
                        "graph.connected",
                        {"n": 1000, "m": 100000},
                        "upper_bound",
                        "label.uniform",
                        1,
                        100000,
                    )
                ],
            ),
            case(
                "large",
                "random",
                [
                    family(
                        "graph.connected",
                        {"n_min": 500, "n_max": 1000, "m_min": 999, "m_max": 5000},
                        "seed_variation",
                        "label.uniform",
                        1,
                        100000,
                    )
                ],
            ),
        ],
    }
