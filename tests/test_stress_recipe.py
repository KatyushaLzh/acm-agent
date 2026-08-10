from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from tools.acm_agent.stress_recipe import (
    SMALL_RECIPE_HARD_MAX_BYTES,
    RecipeCatalog,
    RecipeCatalogError,
    RecipeValidationError,
    UnsupportedRecipeError,
    compose_generator_recipe,
    static_contract_capabilities,
    supports_static_contract,
    validate_generator_recipe,
    _normalize_parameter,
    _LOCAL_CPP_RUNTIME,
    _SERIALIZER_KINDS,
    _validate_template_preconditions,
)


def write_catalog(root: Path) -> None:
    (root / "cpp").mkdir(parents=True)
    (root / "cpp" / "primitives.hpp").write_text(
        "// SPDX-License-Identifier: MIT\nnamespace audited_recipe_asset {}\n",
        encoding="utf-8",
    )
    catalog = {
        "schema_version": 1,
        "metadata": {"upstream_commit": "0123456789abcdef", "license": "MIT"},
        "templates": {
            "array.uniform": {
                "kind": "array",
                "source": "cpp/primitives.hpp",
                "parameters": {
                    "n": {"type": "integer", "minimum": 1, "maximum": 20000000},
                    "n_min": {"type": "integer", "minimum": 1, "maximum": 20000000},
                    "n_max": {"type": "integer", "minimum": 1, "maximum": 20000000},
                    "value_min": {"type": "integer"},
                    "value_max": {"type": "integer"},
                },
            },
            "array.equal": {
                "kind": "array",
                "source": "cpp/primitives.hpp",
                "parameters": {
                    "n": {"type": "integer", "minimum": 1, "maximum": 20000000},
                    "value": {"type": "integer"},
                },
            },
            "tree.path": {
                "kind": "tree",
                "source": "cpp/primitives.hpp",
                "parameters": {
                    "n_min": {"type": "integer", "minimum": 1},
                    "n_max": {"type": "integer", "minimum": 1},
                },
            },
            "label.layered": {
                "kind": "label",
                "source": "cpp/primitives.hpp",
                "parameters": {
                    "label_min": {"type": "integer"},
                    "label_max": {"type": "integer"},
                },
            },
        },
        "serializers": {
            "list_n": {"kind": "array"},
            "edge_list_n_m_u_v_w": {"kind": "edge"},
        },
    }
    for entry in catalog["templates"].values():
        entry.update(
            {
                "symbol": "audited_recipe_asset",
                "preconditions": [],
                "invariants": ["test fixture invariant"],
                "complexity": "O(output_size)",
                "index_base": "not_applicable",
                "self_loops": "not_applicable",
                "parallel_edges": "not_applicable",
                "profiles": ["small", "large"],
            }
        )
    (root / "catalog.json").write_text(
        json.dumps(catalog, sort_keys=True), encoding="utf-8"
    )


def recipe() -> dict[str, object]:
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
                            "n_max": 40 if small else 80,
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
                "hard_max": SMALL_RECIPE_HARD_MAX_BYTES if small else 32 * 1024 * 1024,
                "buckets": (
                    [[1, 25], [26, 50], [51, 75], [76, 100], [101, SMALL_RECIPE_HARD_MAX_BYTES]]
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


class StressRecipeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        write_catalog(self.root)
        self.catalog = RecipeCatalog.load(self.root)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_strict_validation_normalizes_active_buckets(self) -> None:
        normalized = validate_generator_recipe(recipe(), catalog=self.catalog)
        self.assertEqual(len(normalized["cases"]), 4)
        small = normalized["cases"][1]
        self.assertLessEqual(
            len(small["byte_budget"]["active_buckets"]),
            len(small["byte_budget"]["buckets"]),
        )
        self.assertTrue(small["byte_budget"]["active_buckets"])
        self.assertEqual(
            validate_generator_recipe(normalized, catalog=self.catalog),
            normalized,
        )

    def test_unknown_recipe_fields_and_parameters_are_rejected(self) -> None:
        value = recipe()
        value["cpp"] = "int main(){}"
        with self.assertRaises(RecipeValidationError):
            validate_generator_recipe(value, catalog=self.catalog)

        value = recipe()
        value["cases"][1]["families"][0]["semantic_goals"] = ["boundary_values"]
        with self.assertRaisesRegex(RecipeValidationError, "extreme"):
            validate_generator_recipe(value, catalog=self.catalog)

        value = recipe()
        value["cases"][0]["families"][0]["structure"]["parameters"]["expression"] = "n+1"
        with self.assertRaises(RecipeValidationError):
            validate_generator_recipe(value, catalog=self.catalog)

    def test_requires_all_profile_v2_cases_and_fixed_strides(self) -> None:
        value = recipe()
        value["cases"].pop()
        with self.assertRaises(RecipeValidationError):
            validate_generator_recipe(value, catalog=self.catalog)

    def test_large_stride_five_advances_through_complete_family_cycle(self) -> None:
        value = recipe()
        large_random = value["cases"][3]
        large_random["families"].append(
            {
                "structure": {
                    "template_id": "array.equal",
                    "parameters": {"n": 20, "value": 7},
                },
                "labels": [],
                "semantic_goals": ["duplicate_values"],
            }
        )
        normalized = validate_generator_recipe(value, catalog=self.catalog)
        schedule = normalized["cases"][3]["selection"]["schedule"]
        self.assertEqual([item["family_index"] for item in schedule], [0, 1])
        self.assertEqual(
            [schedule[(seed // 5) % len(schedule)]["family_index"] for seed in (0, 5)],
            [0, 1],
        )
        value = recipe()
        value["cases"][3]["selection"]["seed_stride"] = 1
        with self.assertRaises(RecipeValidationError):
            validate_generator_recipe(value, catalog=self.catalog)

    def test_unsupported_contract_is_explicit_and_bindings_are_checked(self) -> None:
        raw_contract = {
            "syntax": {
                "sections": [{"id": "body", "kind": "operation_stream", "fields": []}]
            }
        }
        with self.assertRaises(UnsupportedRecipeError) as caught:
            validate_generator_recipe(recipe(), catalog=self.catalog, contract=raw_contract)
        self.assertEqual(caught.exception.reason, "unsupported_contract_section:operation_stream")

        contract = {
            "syntax": {
                "sections": [
                    {"id": "values", "kind": "list", "fields": [{"name": "a", "type": "int"}]}
                ]
            }
        }
        value = recipe()
        value["cases"][0]["serialization"]["bindings"] = {"values": "missing.a"}
        with self.assertRaises(RecipeValidationError):
            validate_generator_recipe(value, catalog=self.catalog, contract=contract)

    def test_static_support_requires_an_exact_single_serializer_wire_shape(self) -> None:
        self.assertEqual(
            supports_static_contract(None, catalog=self.catalog),
            (False, "contract_missing"),
        )
        scalar_only = {
            "syntax": {
                "mode": "single_case",
                "sections": [
                    {
                        "id": "header",
                        "kind": "scalar",
                        "fields": [
                            {"name": "x", "type": "int"},
                            {"name": "y", "type": "int"},
                        ],
                    }
                ]
            }
        }
        supported, reason = supports_static_contract(
            scalar_only, catalog=self.catalog
        )
        self.assertFalse(supported)
        self.assertEqual(reason, "contract_wire_shape_not_representable")

        list_contract = {
            "syntax": {
                "mode": "single_case",
                "sections": [
                    {
                        "id": "header",
                        "kind": "scalar",
                        "fields": [{"name": "n", "type": "int"}],
                    },
                    {
                        "id": "values",
                        "kind": "list",
                        "count_from": "header.n",
                        "fields": [{"name": "a", "type": "int"}],
                    },
                ]
            },
            "constraints": [
                {
                    "id": "n_range",
                    "kind": "range",
                    "target": "header.n",
                    "args": {"minimum": 1, "maximum": 100},
                }
            ],
        }
        self.assertEqual(
            static_contract_capabilities(list_contract, catalog=self.catalog),
            {
                "list_n": {
                    "structure_kind": "array",
                    "bindings": {"n": "header.n"},
                }
            },
        )
        self.assertEqual(
            supports_static_contract(list_contract, catalog=self.catalog),
            (True, None),
        )

        missing_range = json.loads(json.dumps(list_contract))
        missing_range["constraints"] = []
        self.assertEqual(
            supports_static_contract(missing_range, catalog=self.catalog),
            (False, "contract_binding_range_missing"),
        )

        multi_case = json.loads(json.dumps(list_contract))
        multi_case["syntax"]["mode"] = "multi_case"
        self.assertEqual(
            supports_static_contract(multi_case, catalog=self.catalog),
            (False, "contract_mode_not_single_case"),
        )

        composite = json.loads(json.dumps(list_contract))
        composite["syntax"]["sections"].append(
            {
                "id": "queries",
                "kind": "list",
                "count_from": "header.n",
                "fields": [{"name": "q", "type": "int"}],
            }
        )
        self.assertFalse(
            supports_static_contract(composite, catalog=self.catalog)[0]
        )

    def test_checked_in_static_capabilities_cover_every_real_serializer(self) -> None:
        checked = RecipeCatalog.load()
        self.assertEqual(set(_SERIALIZER_KINDS), set(checked.serializers))

        matrix_contract = {
            "syntax": {
                "mode": "single_case",
                "sections": [
                    {
                        "id": "header",
                        "kind": "scalar",
                        "fields": [
                            {"name": "n", "type": "int"},
                            {"name": "m", "type": "int"},
                        ],
                    },
                    {
                        "id": "grid",
                        "kind": "matrix",
                        "count_from": "header.n",
                        "fields": [{"name": "value", "type": "int"}],
                    },
                ]
            }
        }
        self.assertEqual(
            static_contract_capabilities(matrix_contract, catalog=checked),
            {
                "matrix_n_m": {
                    "structure_kind": "matrix",
                    "bindings": {"rows": "header.n", "cols": "header.m"},
                }
            },
        )

        interval_contract = {
            "syntax": {
                "mode": "single_case",
                "sections": [
                    {
                        "id": "header",
                        "kind": "scalar",
                        "fields": [{"name": "n", "type": "int"}],
                    },
                    {
                        "id": "segments",
                        "kind": "list",
                        "count_from": "header.n",
                        "fields": [
                            {"name": "l", "type": "int"},
                            {"name": "r", "type": "int"},
                        ],
                    },
                ]
            }
        }
        self.assertEqual(
            static_contract_capabilities(interval_contract, catalog=checked),
            {
                "intervals_n": {
                    "structure_kind": "interval",
                    "bindings": {"n": "header.n"},
                }
            },
        )

        weighted_edges = {
            "syntax": {
                "mode": "single_case",
                "sections": [
                    {
                        "id": "header",
                        "kind": "scalar",
                        "fields": [
                            {"name": "n", "type": "int"},
                            {"name": "m", "type": "int"},
                        ],
                    },
                    {
                        "id": "edges",
                        "kind": "edge_list",
                        "count_from": "header.m",
                        "fields": [
                            {"name": "u", "type": "int"},
                            {"name": "v", "type": "int"},
                            {"name": "w", "type": "int"},
                        ],
                    },
                ]
            },
            "constraints": [
                {"kind": "range", "target": "header.n", "args": {"minimum": 1, "maximum": 1000}},
                {"kind": "range", "target": "header.m", "args": {"minimum": 1, "maximum": 100000}},
                {"kind": "range", "target": "edges.w", "args": {"minimum": 1, "maximum": 100000}},
            ],
        }
        self.assertEqual(
            static_contract_capabilities(weighted_edges, catalog=checked),
            {
                "edge_list_n_m_u_v_w": {
                    "structure_kind": "edge",
                    "bindings": {
                        "n": "header.n",
                        "m": "header.m",
                        "label": "edges.w",
                    },
                }
            },
        )
        self.assertEqual(
            supports_static_contract(weighted_edges, catalog=checked),
            (True, None),
        )

    def test_multi_payload_array_and_range_queries_fall_back_to_legacy(self) -> None:
        contract = {
            "syntax": {
                "mode": "single_case",
                "sections": [
                    {
                        "id": "header",
                        "kind": "scalar",
                        "fields": [
                            {"name": "n", "type": "int"},
                            {"name": "m", "type": "int"},
                        ],
                    },
                    {
                        "id": "values",
                        "kind": "list",
                        "count_from": "header.n",
                        "fields": [{"name": "a", "type": "int"}],
                    },
                    {
                        "id": "queries",
                        "kind": "list",
                        "count_from": "header.m",
                        "fields": [
                            {"name": "l", "type": "int"},
                            {"name": "r", "type": "int"},
                            {"name": "k", "type": "int"},
                        ],
                    },
                ],
            },
            "constraints": [
                {"kind": "range", "target": "header.n", "args": {"minimum": 1, "maximum": 200000}},
                {"kind": "range", "target": "header.m", "args": {"minimum": 0, "maximum": 200000}},
            ],
        }

        self.assertEqual(static_contract_capabilities(contract), {})
        self.assertEqual(
            supports_static_contract(contract),
            (False, "contract_wire_shape_not_representable"),
        )

    def test_string_parameters_allow_safe_punctuation_but_reject_controls(self) -> None:
        spec = {"type": "string", "minLength": 1, "maxLength": 8}
        self.assertEqual(
            _normalize_parameter("value", "()[]", spec, "$.value"),
            "()[]",
        )
        with self.assertRaisesRegex(RecipeValidationError, "control"):
            _normalize_parameter("value", "a\nb", spec, "$.value")
        with self.assertRaisesRegex(RecipeValidationError, "too long"):
            _normalize_parameter("value", "123456789", spec, "$.value")
        with self.assertRaisesRegex(RecipeValidationError, "unsafe"):
            _normalize_parameter(
                "policy", "not allowed!", {"type": "enum"}, "$.policy"
            )

    def test_contract_ranges_bind_boundary_dimensions_and_reject_overflow(self) -> None:
        contract = {
            "syntax": {
                "mode": "single_case",
                "sections": [
                    {"id": "header", "kind": "scalar", "fields": [{"name": "n", "type": "int"}]},
                    {"id": "values", "kind": "list", "count_from": "header.n", "fields": [{"name": "a", "type": "int"}]},
                ]
            },
            "constraints": [
                {"id": "n_range", "kind": "range", "target": "header.n", "args": {"minimum": 1, "maximum": 100}}
            ],
        }
        normalized = validate_generator_recipe(recipe(), catalog=self.catalog, contract=contract)
        self.assertEqual(normalized["cases"][0]["serialization"]["bindings"], {"n": "header.n"})
        self.assertEqual(normalized["cases"][0]["families"][0]["structure"]["parameters"]["n"], 1)
        self.assertEqual(normalized["cases"][2]["families"][0]["structure"]["parameters"]["n"], 100)

        invalid = recipe()
        invalid["cases"][1]["families"][0]["structure"]["parameters"]["n_max"] = 101
        with self.assertRaises(RecipeValidationError):
            validate_generator_recipe(invalid, catalog=self.catalog, contract=contract)

    def test_oversized_contract_upper_bound_fails_before_cpp_generation(self) -> None:
        contract = {
            "syntax": {
                "mode": "single_case",
                "sections": [
                    {"id": "header", "kind": "scalar", "fields": [{"name": "n", "type": "int"}]},
                    {"id": "values", "kind": "list", "count_from": "header.n", "fields": [{"name": "a", "type": "int"}]},
                ]
            },
            "constraints": [
                {"id": "n_range", "kind": "range", "target": "header.n", "args": {"minimum": 1, "maximum": 20_000_000}}
            ],
        }
        value = recipe()
        for case in value["cases"]:
            if case["profile"] == "large":
                case["families"][0]["structure"]["parameters"]["n_max"] = 20_000_000
        with self.assertRaisesRegex(RecipeValidationError, "32 MiB"):
            validate_generator_recipe(value, catalog=self.catalog, contract=contract)

    def test_catalog_hash_covers_metadata_and_rejects_traversal(self) -> None:
        first = self.catalog.sha256
        (self.root / "NOTICE").write_text("provenance changed\n", encoding="utf-8")
        second = RecipeCatalog.load(self.root).sha256
        self.assertNotEqual(first, second)

        payload = json.loads((self.root / "catalog.json").read_text(encoding="utf-8"))
        payload["templates"]["array.uniform"]["source"] = "../outside.hpp"
        (self.root / "catalog.json").write_text(json.dumps(payload), encoding="utf-8")
        with self.assertRaises(RecipeCatalogError):
            RecipeCatalog.load(self.root)

    def test_composed_source_metadata_and_provider_free_execution(self) -> None:
        composed = compose_generator_recipe(recipe(), catalog=self.catalog)
        self.assertIn("// ACM_LOCAL_RECIPE_GENERATOR_V1", composed.source)
        self.assertIn("void acm_generate_case", composed.source)
        self.assertIn("void acm_generate_manifest", composed.source)
        self.assertNotRegex(composed.source, r"\b(?:int|auto)\s+main\s*\(")
        self.assertEqual(composed.metadata["engine"], "local_templates_v1")
        self.assertEqual(
            composed.metadata["hard_small_bytes"], SMALL_RECIPE_HARD_MAX_BYTES
        )
        self.assertEqual(len(composed.recipe_sha256), 64)

        compiler = shutil.which("g++")
        if compiler is None:
            self.skipTest("g++ is unavailable")
        source = self.root / "recipe_test.cpp"
        executable = self.root / "recipe_test.exe"
        source.write_text(
            composed.source
            + "\nint main(int argc,char** argv){unsigned long long seed=std::stoull(argv[2]);"
            + "if(std::string(argv[1])==\"manifest\")acm_generate_manifest(seed,argv[3],argv[4],std::cout);"
            + "else acm_generate_case(seed,argv[3],argv[4],std::cout);}\n",
            encoding="utf-8",
        )
        built = subprocess.run(
            [compiler, "-std=c++17", "-O2", str(source), "-o", str(executable)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(built.returncode, 0, built.stderr)
        outputs: set[bytes] = set()
        bucket_counts: dict[str, int] = {}
        for seed in range(16):
            generated = subprocess.run(
                [str(executable), "case", str(seed), "small", "random"],
                capture_output=True,
                timeout=5,
                check=True,
            ).stdout
            self.assertGreater(len(generated), 0)
            self.assertLessEqual(len(generated), 100)
            outputs.add(generated)
            manifest_bytes = subprocess.run(
                [str(executable), "manifest", str(seed), "small", "random"],
                capture_output=True,
                timeout=5,
                check=True,
            ).stdout
            manifest = json.loads(manifest_bytes)
            self.assertEqual(manifest["input_sha256"], hashlib.sha256(generated).hexdigest())
            self.assertEqual(len(manifest["coverage_tags"]), 3)
            bucket_tag = next(tag for tag in manifest["coverage_tags"] if tag.startswith("byte_bucket:"))
            bucket_counts[bucket_tag] = bucket_counts.get(bucket_tag, 0) + 1
        self.assertGreaterEqual(len(outputs), 12)
        self.assertLessEqual(max(bucket_counts.values()) - min(bucket_counts.values()), 1)

    def test_checked_in_catalog_composes_a_standalone_translation_unit(self) -> None:
        checked_in = Path("tools/acm_agent/generator_templates")
        if not (checked_in / "catalog.json").is_file():
            self.skipTest("checked-in generator template catalog is unavailable")
        value = recipe()
        for case in value["cases"]:
            small = case["profile"] == "small"
            case["families"][0]["structure"]["parameters"] = {
                "n": 20 if small else 1000,
                "lo": 0,
                "hi": 9,
            }
            case["serialization"]["format_id"] = "list_n"
        composed = compose_generator_recipe(value, catalog_root=checked_in)
        self.assertIn("namespace acm_recipe", composed.source)
        compiler = shutil.which("g++")
        if compiler is None:
            self.skipTest("g++ is unavailable")
        built = subprocess.run(
            [compiler, "-std=c++17", "-fsyntax-only", "-x", "c++", "-"],
            input=composed.source,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(built.returncode, 0, built.stderr)

    def test_embedded_graph_runtime_near_capacity_is_bounded_and_deterministic(self) -> None:
        compiler = shutil.which("g++")
        if compiler is None:
            self.skipTest("g++ is unavailable")
        checks = r'''
using namespace acm_recipe_local;

static int component_count(int n,const std::vector<Edge>& edges){
    std::vector<int> parent(n);for(int i=0;i<n;++i)parent[i]=i;
    std::function<int(int)> find=[&](int x){return parent[x]==x?x:parent[x]=find(parent[x]);};
    for(auto edge:edges){int a=find(static_cast<int>(edge.u-1)),b=find(static_cast<int>(edge.v-1));if(a!=b)parent[a]=b;}
    std::set<int> roots;for(int i=0;i<n;++i)roots.insert(find(i));return static_cast<int>(roots.size());
}
static void simple_linear(int n,const std::vector<Edge>& edges){
    std::vector<unsigned char> seen(static_cast<std::size_t>(n)*n);
    for(auto edge:edges){
        assert(1<=edge.u && edge.u<=n && 1<=edge.v && edge.v<=n && edge.u!=edge.v);
        if(edge.u>edge.v)std::swap(edge.u,edge.v);
        std::size_t key=static_cast<std::size_t>(edge.u-1)*n+static_cast<std::size_t>(edge.v-1);
        assert(!seen[key]);seen[key]=1;
    }
}
static bool same(const std::vector<Edge>& a,const std::vector<Edge>& b){
    if(a.size()!=b.size())return false;
    for(std::size_t i=0;i<a.size();++i)if(a[i].u!=b[i].u || a[i].v!=b[i].v || a[i].w!=b[i].w)return false;
    return true;
}
int main(){
    std::vector<long long> p1111_micros;
    for(unsigned long long seed_base:{200001ULL,300001ULL}){
        for(unsigned long long seed=seed_base;seed<seed_base+20;++seed){
            constexpr long long n=96,complete=n*(n-1)/2;
            Rng r0(seed),r0_again(seed);
            auto g0=make_edges("graph","random_simple",n,complete-1,1,0,0,r0);
            auto g0_again=make_edges("graph","random_simple",n,complete-1,1,0,0,r0_again);
            assert(g0.size()==complete-1 && same(g0,g0_again));simple_linear(n,g0);
            Rng r1(seed);auto g1=make_edges("graph","connected",n,complete-1,1,0,0,r1);
            assert(g1.size()==complete-1 && component_count(n,g1)==1);simple_linear(n,g1);
            Rng r2(seed);auto g2=make_edges("graph","components",n,2*48*47/2-1,1,2,0,r2);
            assert(g2.size()==2*48*47/2-1 && component_count(n,g2)==2);simple_linear(n,g2);
            Rng r3(seed);auto g3=make_edges("graph","bipartite",n,48*48-1,1,48,48,r3);
            assert(g3.size()==48*48-1);simple_linear(n,g3);for(auto edge:g3)assert(edge.u<=48 && edge.v>48);

            Rng performance_rng(seed);const auto started=std::chrono::steady_clock::now();
            auto p1111=make_edges("graph","components",1000,249499,1,2,0,performance_rng);
            const auto elapsed=std::chrono::steady_clock::now()-started;
            assert(p1111.size()==249499 && component_count(1000,p1111)==2);simple_linear(1000,p1111);
            assert(elapsed<std::chrono::seconds(1));
            p1111_micros.push_back(std::chrono::duration_cast<std::chrono::microseconds>(elapsed).count());
        }
    }
    std::sort(p1111_micros.begin(),p1111_micros.end());assert(p1111_micros[37]<200000);
}
'''
        source = (
            "#include <algorithm>\n#include <cassert>\n#include <chrono>\n#include <cstdint>\n"
            "#include <functional>\n#include <iostream>\n#include <set>\n#include <sstream>\n"
            "#include <stdexcept>\n#include <string>\n#include <utility>\n#include <vector>\n"
            + _LOCAL_CPP_RUNTIME
            + checks
        )
        source_path = self.root / "embedded_graph_runtime.cpp"
        executable = self.root / "embedded_graph_runtime.exe"
        source_path.write_text(source, encoding="utf-8")
        built = subprocess.run(
            [compiler, "-std=c++17", "-O2", "-Wall", "-Wextra", str(source_path), "-o", str(executable)],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(built.returncode, 0, built.stderr)
        ran = subprocess.run([str(executable)], capture_output=True, text=True, timeout=30)
        self.assertEqual(ran.returncode, 0, ran.stderr)

    def test_every_checked_in_catalog_parameter_type_is_accepted(self) -> None:
        checked_in = Path("tools/acm_agent/generator_templates")
        if not (checked_in / "catalog.json").is_file():
            self.skipTest("checked-in generator template catalog is unavailable")
        catalog = RecipeCatalog.load(checked_in)
        for entry in catalog.templates.values():
            for name, spec in entry.parameters.items():
                if "const" in spec:
                    value = spec["const"]
                elif spec.get("type") in {"boolean", "bool"}:
                    value = True
                elif spec.get("type") == "string":
                    value = "a" * max(1, int(spec.get("minLength", 1)))
                else:
                    value = max(0, int(spec.get("minimum", 0)))
                with self.subTest(template=entry.template_id, parameter=name):
                    _normalize_parameter(name, value, spec, "$.parameter")

    def test_checked_in_json_schema_is_closed_and_catalog_aligned(self) -> None:
        schema = json.loads(Path("tools/acm_agent/generator_recipe.schema.json").read_text(encoding="utf-8"))
        self.assertFalse(schema["additionalProperties"])
        for definition in ("case", "family", "structure_ref", "label_ref", "parameters"):
            self.assertFalse(schema["$defs"][definition]["additionalProperties"])
        catalog = RecipeCatalog.load(Path("tools/acm_agent/generator_templates"))
        for template_id, entry in catalog.templates.items():
            with self.subTest(template_metadata=template_id):
                for field in (
                    "preconditions", "invariants", "complexity", "index_base",
                    "self_loops", "parallel_edges", "profiles", "source", "symbol",
                ):
                    self.assertIn(field, entry.metadata)
        structure_ids = {
            template_id
            for template_id in catalog.templates
            if template_id.split(".", 1)[0] in {"array", "string", "matrix", "interval", "graph", "tree"}
        }
        label_ids = {
            template_id
            for template_id, entry in catalog.templates.items()
            if entry.kind == "label"
        }
        self.assertEqual(set(schema["$defs"]["structure_ref"]["properties"]["template_id"]["enum"]), structure_ids)
        self.assertEqual(set(schema["$defs"]["label_ref"]["properties"]["template_id"]["enum"]), label_ids)

    def test_graph_parameter_domains_fail_closed_for_independent_sampling(self) -> None:
        with self.assertRaisesRegex(RecipeValidationError, "capacity"):
            _validate_template_preconditions(
                "graph.random_simple",
                {"n_min": 2, "n_max": 20, "m_min": 0, "m_max": 10},
                path="$.family",
            )
        with self.assertRaisesRegex(RecipeValidationError, "m >= n-1"):
            _validate_template_preconditions(
                "graph.connected",
                {"n_min": 10, "n_max": 20, "m_min": 10, "m_max": 20},
                path="$.family",
            )


if __name__ == "__main__":
    unittest.main()
