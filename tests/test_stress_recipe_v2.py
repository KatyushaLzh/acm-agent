from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from tools.acm_agent.stress import (
    _parse_generator_manifest,
    compose_trusted_generator_harness,
)
from tools.acm_agent.stress_ai import prepare_stress
from tools.acm_agent.stress_recipe import RecipeValidationError, UnsupportedRecipeError
from tools.acm_agent.stress_recipe_v2 import (
    GENERATOR_RECIPE_V2_CATALOG_SHA256,
    GENERATOR_RECIPE_V2_COMPOSER_VERSION,
    GENERATOR_RECIPE_V2_ENGINE,
    GENERATOR_RECIPE_V2_SCHEMA_VERSION,
    compile_static_contract_v2,
    compose_generator_recipe_v2,
    recipe_v2_identity,
    supports_static_contract_v2,
    validate_generator_recipe_v2,
)


def permutation_contract(*, n_max: int = 80_000, m_max: int = 80_000) -> dict:
    return {
        "syntax": {
            "mode": "single_case",
            "sections": [
                {
                    "id": "header",
                    "kind": "scalar",
                    "fields": [{"name": "n", "type": "int"}, {"name": "m", "type": "int"}],
                },
                {
                    "id": "initial",
                    "kind": "list",
                    "count_from": "header.n",
                    "fields": [{"name": "book", "type": "int"}],
                },
                {
                    "id": "operations",
                    "kind": "operation_stream",
                    "count_from": "header.m",
                    "fields": [],
                    "variants": [
                        {"tag": "Top", "fields": [{"name": "s", "type": "int"}]},
                        {"tag": "Bottom", "fields": [{"name": "s", "type": "int"}]},
                        {
                            "tag": "Insert",
                            "fields": [
                                {"name": "s", "type": "int"},
                                {"name": "t", "type": "int", "minimum": -1, "maximum": 1},
                            ],
                        },
                        {"tag": "Ask", "fields": [{"name": "s", "type": "int"}]},
                        {"tag": "Query", "fields": [{"name": "k", "type": "int"}]},
                    ],
                },
            ],
        },
        "constraints": [
            {"id": "n_range", "kind": "range", "target": "header.n", "args": {"minimum": 1, "maximum": n_max}},
            {"id": "m_range", "kind": "range", "target": "header.m", "args": {"minimum": 0, "maximum": m_max}},
            {"id": "initial_perm", "kind": "permutation", "target": "initial.book", "args": {"minimum": 1, "maximum_from": "header.n"}},
            {"id": "top_bound", "kind": "dependent_bound", "target": "operations.Top.s", "args": {"minimum": 1, "maximum_from": "header.n"}},
            {"id": "bottom_bound", "kind": "dependent_bound", "target": "operations.Bottom.s", "args": {"minimum": 1, "maximum_from": "header.n"}},
            {"id": "insert_bound", "kind": "dependent_bound", "target": "operations.Insert.s", "args": {"minimum": 1, "maximum_from": "header.n"}},
            {"id": "ask_bound", "kind": "dependent_bound", "target": "operations.Ask.s", "args": {"minimum": 1, "maximum_from": "header.n"}},
            {"id": "query_bound", "kind": "dependent_bound", "target": "operations.Query.k", "args": {"minimum": 1, "maximum_from": "header.n"}},
            {"id": "insert_legal", "kind": "state_precondition", "target": "operations.Insert", "args": {"state": "current_order"}},
        ],
    }


def bracket_contract(*, raw_records: bool = False, n_max: int = 1_000_000, q_max: int = 100_000) -> dict:
    return {
        "syntax": {
            "mode": "single_case",
            "sections": [
                {
                    "id": "text",
                    "kind": "string",
                    "alphabet": ["(", ")"],
                    "fields": [{"name": "s", "type": "string"}],
                },
                {"id": "count", "kind": "scalar", "fields": [{"name": "q", "type": "int"}]},
                {
                    "id": "queries",
                    "kind": "raw" if raw_records else "intervals",
                    "count_from": "count.q",
                    "fields": [{"name": "l", "type": "int"}, {"name": "r", "type": "int"}],
                },
            ],
        },
        "constraints": [
            {"id": "length", "kind": "range", "target": "text.s", "args": {"minimum": 1, "maximum": n_max}},
            {"id": "q", "kind": "range", "target": "count.q", "args": {"minimum": 0, "maximum": q_max}},
            {"id": "left", "kind": "dependent_bound", "target": "queries.l", "args": {"minimum": 1, "maximum_from": "text.s"}},
            {"id": "right", "kind": "dependent_bound", "target": "queries.r", "args": {"minimum_from": "queries.l", "maximum_from": "text.s"}},
        ],
    }


def single_raw_bracket_contract() -> dict:
    return {
        "syntax": {
            "mode": "single_case",
            "sections": [
                {
                    "id": "input",
                    "kind": "raw",
                    "alphabet": ["(", ")"],
                    "fields": [
                        {"name": "s", "type": "string"},
                        {"name": "q", "type": "int"},
                        {"name": "l", "type": "int", "count_from": "input.q"},
                        {"name": "r", "type": "int", "count_from": "input.q"},
                    ],
                }
            ],
        },
        "constraints": [
            {"id": "length", "kind": "range", "target": "input.s", "args": {"minimum": 1, "maximum": 1000}},
            {"id": "q", "kind": "range", "target": "input.q", "args": {"minimum": 0, "maximum": 1000}},
            {"id": "left", "kind": "dependent_bound", "target": "input.l", "args": {"minimum": 1, "maximum_from": "input.s"}},
            {"id": "right", "kind": "dependent_bound", "target": "input.r", "args": {"minimum_from": "input.l", "maximum_from": "input.s"}},
        ],
    }


def normalized_raw_bracket_alias_contract() -> dict:
    return {
        "syntax": {
            "mode": "single_case",
            "sections": [
                {
                    "id": "s",
                    "kind": "raw",
                    "fields": [{"name": "s", "type": "string"}],
                },
                {
                    "id": "q",
                    "kind": "scalar",
                    "fields": [{"name": "q", "type": "int"}],
                },
                {
                    "id": "queries",
                    "kind": "intervals",
                    "fields": [
                        {"name": "l", "type": "int"},
                        {"name": "r", "type": "int"},
                    ],
                },
            ],
        },
        "constraints": [
            {
                "id": "length",
                "kind": "range",
                "target": "s.length",
                "args": {"minimum": 1, "maximum": 1_000_000},
            },
            {
                "id": "q",
                "kind": "range",
                "target": "q",
                "args": {"minimum": 0, "maximum": 100_000},
            },
            {
                "id": "bounds",
                "kind": "dependent_bound",
                "target": "queries.l,queries.r",
                "args": {"lower": 1, "upper": "s.length"},
            },
            {
                "id": "alphabet",
                "kind": "custom_text",
                "target": "s",
                "args": {"pattern": "^[()]+$"},
            },
        ],
    }


def validate_permutation_case(data: bytes) -> None:
    lines = data.decode().splitlines()
    n, m = map(int, lines[0].split())
    order = list(map(int, lines[1].split()))
    assert sorted(order) == list(range(1, n + 1))
    assert len(lines) == m + 2
    position = {book: i for i, book in enumerate(order)}
    for line in lines[2:]:
        tokens = line.split()
        op = tokens[0]
        if op in {"Top", "Bottom", "Ask"}:
            assert len(tokens) == 2
            book = int(tokens[1])
            assert book in position
            if op == "Top":
                order.remove(book); order.insert(0, book)
            elif op == "Bottom":
                order.remove(book); order.append(book)
        elif op == "Query":
            assert len(tokens) == 2 and 1 <= int(tokens[1]) <= n
        else:
            assert op == "Insert" and len(tokens) == 3
            book, displacement = map(int, tokens[1:])
            old = order.index(book)
            assert displacement in {-1, 0, 1} and 0 <= old + displacement < n
            target = old + displacement
            order[old], order[target] = order[target], order[old]
        position = {book: i for i, book in enumerate(order)}


def validate_bracket_case(data: bytes) -> None:
    lines = data.decode().splitlines()
    assert lines and set(lines[0]) <= {"(", ")"} and lines[0]
    q = int(lines[1])
    assert len(lines) == q + 2
    for line in lines[2:]:
        left, right = map(int, line.split())
        assert 1 <= left <= right <= len(lines[0])


class StressRecipeV2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.compiler = shutil.which("g++")
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.executables: dict[str, Path] = {}

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    @classmethod
    def executable(cls, name: str, contract: dict) -> Path:
        if name in cls.executables:
            return cls.executables[name]
        if cls.compiler is None:
            raise unittest.SkipTest("g++ is unavailable")
        composed = compose_generator_recipe_v2(compile_static_contract_v2(contract), contract=contract)
        source = cls.root / f"{name}.cpp"
        executable = cls.root / f"{name}.exe"
        source.write_text(compose_trusted_generator_harness(composed.source), encoding="utf-8")
        built = subprocess.run(
            [cls.compiler, "-std=c++17", "-O2", str(source), "-o", str(executable)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if built.returncode != 0:
            raise AssertionError(built.stderr)
        cls.executables[name] = executable
        return executable

    def run_case(self, executable: Path, seed: int, profile: str = "small", kind: str = "random") -> bytes:
        return subprocess.run(
            [str(executable), str(seed), profile, kind],
            capture_output=True,
            check=True,
            timeout=10,
        ).stdout

    def test_public_identity_and_composed_metadata(self) -> None:
        identity = dict(recipe_v2_identity())
        self.assertEqual(identity["engine"], GENERATOR_RECIPE_V2_ENGINE)
        self.assertEqual(identity["recipe_schema_version"], GENERATOR_RECIPE_V2_SCHEMA_VERSION)
        self.assertEqual(identity["composer_version"], GENERATOR_RECIPE_V2_COMPOSER_VERSION)
        self.assertEqual(identity["catalog_sha256"], GENERATOR_RECIPE_V2_CATALOG_SHA256)
        composed = compose_generator_recipe_v2(compile_static_contract_v2(permutation_contract()))
        self.assertIn("// ACM_LOCAL_RECIPE_GENERATOR_V2", composed.source)
        self.assertNotRegex(composed.source, r"\b(?:int|auto)\s+main\s*\(")
        self.assertEqual(composed.metadata["state_machine"], "mutable_permutation")
        self.assertEqual(len(composed.recipe_sha256), 64)

    def test_shape_compilers_are_problem_id_independent(self) -> None:
        permutation = compile_static_contract_v2(permutation_contract())
        self.assertEqual(permutation["machine"]["kind"], "mutable_permutation")
        self.assertEqual(len(permutation["cases"]), 4)
        brackets = compile_static_contract_v2(bracket_contract())
        self.assertEqual(brackets["machine"]["kind"], "bracket_interval_queries")
        self.assertFalse(brackets["machine"]["raw_like"])
        raw = compile_static_contract_v2(bracket_contract(raw_records=True))
        self.assertTrue(raw["machine"]["raw_like"])
        normalized_raw = compile_static_contract_v2(
            normalized_raw_bracket_alias_contract()
        )
        self.assertTrue(normalized_raw["machine"]["raw_like"])
        self.assertEqual(
            [section["kind"] for section in normalized_raw["sections"]],
            ["string", "scalar", "record_stream"],
        )
        reconstructed = compile_static_contract_v2(single_raw_bracket_contract())
        self.assertTrue(reconstructed["machine"]["raw_like"])
        self.assertEqual(
            [section["kind"] for section in reconstructed["sections"]],
            ["string", "scalar", "record_stream"],
        )
        self.assertEqual(supports_static_contract_v2(bracket_contract()), (True, None))

        normalized_aliases = permutation_contract()
        normalized_aliases["syntax"]["sections"][2]["fields"] = [
            {"name": "op", "type": "string"}
        ]
        normalized_aliases["syntax"]["sections"][2]["variants"][4]["fields"][0][
            "name"
        ] = "s"
        normalized_aliases["syntax"]["sections"][2]["variants"][2]["fields"][1].pop(
            "minimum"
        )
        normalized_aliases["syntax"]["sections"][2]["variants"][2]["fields"][1].pop(
            "maximum"
        )
        normalized_aliases["constraints"] = [
            normalized_aliases["constraints"][0],
            normalized_aliases["constraints"][1],
            {
                "id": "permutation",
                "kind": "permutation",
                "target": "initial.book",
                "args": {"of": "1..n"},
            },
            {
                "id": "all_items",
                "kind": "range",
                "target": "operations.*.s",
                "args": {"minimum": 1, "maximum": 80000},
            },
            {
                "id": "insert_t",
                "kind": "range",
                "target": "operations.Insert.t",
                "args": {"minimum": -1, "maximum": 1},
            },
        ]
        self.assertEqual(
            compile_static_contract_v2(normalized_aliases)["machine"]["kind"],
            "mutable_permutation",
        )
        domain_alias = json.loads(json.dumps(normalized_aliases))
        domain_alias["constraints"][2]["args"] = {
            "domain": [1, "header.n"],
            "size": "header.n",
        }
        domain_alias["constraints"][3]["target"] = "operations.arg1"
        domain_alias["constraints"][4]["target"] = "operations.arg2"
        domain_alias["constraints"].append(
            {
                "id": "state",
                "kind": "state_precondition",
                "target": "operations",
                "args": {
                    "description": (
                        "Insert displacement must not cross current top/bottom boundary"
                    )
                },
            }
        )
        self.assertEqual(
            compile_static_contract_v2(domain_alias)["machine"]["kind"],
            "mutable_permutation",
        )

        inferred_counts = json.loads(json.dumps(normalized_aliases))
        inferred_counts["syntax"]["sections"][1].pop("count_from")
        inferred_counts["syntax"]["sections"][2].pop("count_from")
        self.assertEqual(
            compile_static_contract_v2(inferred_counts)["machine"]["bindings"]["m"],
            "header.m",
        )
        sectionless_counts = json.loads(json.dumps(normalized_aliases))
        sectionless_counts["syntax"]["sections"][1]["count_from"] = "n"
        sectionless_counts["syntax"]["sections"][2]["count_from"] = "m"
        self.assertEqual(
            compile_static_contract_v2(sectionless_counts)["machine"]["bindings"]["n"],
            "header.n",
        )

        bracket_aliases = bracket_contract()
        bracket_aliases["syntax"]["sections"][2]["count_from"] = "q"
        bracket_aliases["constraints"][0]["target"] = "text.length"
        bracket_aliases["constraints"][1]["target"] = "count.value"
        bracket_aliases["constraints"][2]["args"] = {
            "lower": 1,
            "upper_from": "text.length",
        }
        bracket_aliases["constraints"][3]["args"] = {
            "lower_from": "queries.l",
            "upper_from": "text.length",
        }
        self.assertEqual(
            compile_static_contract_v2(bracket_aliases)["machine"]["kind"],
            "bracket_interval_queries",
        )

        implicit_string = json.loads(json.dumps(bracket_aliases))
        implicit_string["syntax"]["sections"][0]["fields"] = []
        implicit_string["constraints"][2]["kind"] = "range"
        self.assertEqual(
            compile_static_contract_v2(implicit_string)["machine"]["bindings"]["string"],
            "text.value",
        )

        scalar_alias_and_inline_bounds = json.loads(json.dumps(implicit_string))
        scalar_alias_and_inline_bounds["syntax"]["sections"][1]["fields"][0][
            "name"
        ] = "q"
        scalar_alias_and_inline_bounds["syntax"]["sections"][2][
            "count_from"
        ] = "count.value"
        scalar_alias_and_inline_bounds["syntax"]["sections"][2]["fields"] = [
            {
                "name": "left",
                "type": "int",
                "minimum": 1,
                "maximum": "n",
            },
            {
                "name": "right",
                "type": "int",
                "minimum_from": "left",
                "maximum_from": "N",
            },
        ]
        scalar_alias_and_inline_bounds["constraints"] = (
            scalar_alias_and_inline_bounds["constraints"][:2]
        )
        compiled_inline = compile_static_contract_v2(
            scalar_alias_and_inline_bounds
        )
        self.assertEqual(compiled_inline["machine"]["bindings"]["q"], "count.q")
        self.assertEqual(
            compiled_inline["machine"]["bindings"]["left"], "queries.left"
        )

    def test_prepare_stress_never_requests_generator_provider_for_v2_shapes(self) -> None:
        class RejectProvider:
            def __init__(self) -> None:
                self.calls = 0

            def chat_json(self, *args, **kwargs):
                self.calls += 1
                raise AssertionError("v2 contract must not request a provider recipe")

            def chat(self, *args, **kwargs):
                self.calls += 1
                raise AssertionError("v2 contract must not request generator C++")

        for problem_id, contract in (
            ("P2596", permutation_contract()),
            ("CF380C", normalized_raw_bracket_alias_contract()),
        ):
            with self.subTest(problem_id=problem_id):
                provider = RejectProvider()
                with patch(
                    "tools.acm_agent.stress_ai.normalize_stress_contract",
                    return_value=contract,
                ):
                    prepared = prepare_stress(
                        provider,
                        object(),
                        platform=(
                            "luogu" if problem_id.startswith("P") else "codeforces"
                        ),
                        problem_id=problem_id,
                        title=problem_id,
                        statement="fixture statement",
                        compare="token",
                        settings={
                            "model": "deepseek-v4-flash",
                            "thinking": False,
                        },
                        generation_mode="hybrid",
                        include_reference_primary=False,
                        include_reference_secondary=False,
                        include_validator=False,
                        require_complete_probes=False,
                        prepared_contract=contract,
                        # A historical legacy cache entry must not hide a new
                        # deterministic contract compiler.
                        prepared_generator_blueprint={"engine": "legacy_ai_cpp"},
                    )
                self.assertEqual(provider.calls, 0)
                self.assertEqual(
                    prepared.generator_blueprint["engine"],
                    GENERATOR_RECIPE_V2_ENGINE,
                )
                self.assertEqual(
                    prepared.generation_metadata["recipe_source"],
                    "deterministic_contract",
                )
                self.assertTrue(
                    prepared.generation_metadata["contract_wire_shape"]["sections"]
                )
                self.assertEqual(prepared.generator.origin, "ai_recipe_composed")

    def test_closed_validation_rejects_source_expressions_and_unknown_semantics(self) -> None:
        recipe = compile_static_contract_v2(permutation_contract())
        injected = json.loads(json.dumps(recipe))
        injected["cpp"] = "int main(){}"
        with self.assertRaises(RecipeValidationError):
            validate_generator_recipe_v2(injected)
        injected = json.loads(json.dumps(recipe))
        injected["sections"][0]["binding"] = "header.n; system('x')"
        with self.assertRaises(RecipeValidationError):
            validate_generator_recipe_v2(injected)
        injected = json.loads(json.dumps(recipe))
        injected["machine"]["kind"] = "free_form_state"
        with self.assertRaisesRegex(RecipeValidationError, "unknown state semantics"):
            validate_generator_recipe_v2(injected)
        changed = permutation_contract(n_max=100)
        with self.assertRaisesRegex(RecipeValidationError, "different contract"):
            validate_generator_recipe_v2(recipe, contract=changed)

    def test_ambiguous_raw_and_wrong_operation_signature_fail_closed(self) -> None:
        ambiguous = bracket_contract(raw_records=True)
        ambiguous["syntax"]["sections"][2]["count_from"] = None
        with self.assertRaises(UnsupportedRecipeError):
            compile_static_contract_v2(ambiguous)
        wrong = permutation_contract()
        wrong["syntax"]["sections"][2]["variants"][2]["fields"].pop()
        with self.assertRaises(UnsupportedRecipeError):
            compile_static_contract_v2(wrong)

    def test_permutation_256_seeds_are_deterministic_and_dynamically_legal(self) -> None:
        executable = self.executable("permutation", permutation_contract())
        first_sixteen: set[bytes] = set()
        for seed in range(256):
            data = self.run_case(executable, seed)
            validate_permutation_case(data)
            self.assertEqual(data, self.run_case(executable, seed))
            if seed < 16:
                first_sixteen.add(data)
        self.assertGreaterEqual(len(first_sixteen), 8)

    def test_brackets_256_seeds_are_deterministic_and_valid(self) -> None:
        executable = self.executable("brackets", bracket_contract())
        first_sixteen: set[bytes] = set()
        for seed in range(256):
            data = self.run_case(executable, seed)
            validate_bracket_case(data)
            self.assertEqual(data, self.run_case(executable, seed))
            if seed < 16:
                first_sixteen.add(data)
        self.assertGreaterEqual(len(first_sixteen), 8)

    def test_exact_bounds_and_manifest_hash_buckets_and_tags(self) -> None:
        contract = permutation_contract(n_max=31, m_max=37)
        executable = self.executable("permutation_bounds", contract)
        lower = self.run_case(executable, 17, "small", "lower_bound")
        self.assertEqual(lower.splitlines()[0], b"1 0")
        upper = self.run_case(executable, 17, "large", "upper_bound")
        self.assertEqual(upper.splitlines()[0], b"31 37")
        manifest_payload = subprocess.run(
            [str(executable), "--manifest", "17", "large", "upper_bound"],
            capture_output=True,
            check=True,
            timeout=10,
        ).stdout
        manifest = json.loads(manifest_payload)
        parsed = _parse_generator_manifest(
            manifest_payload,
            profile="large",
            case_kind="upper_bound",
            seed=17,
            generated_input=upper,
        )
        self.assertEqual(parsed, manifest)
        self.assertEqual(manifest["manifest_version"], 2)
        self.assertEqual(manifest["engine"], "local_templates_v2")
        self.assertEqual(manifest["input_sha256"], hashlib.sha256(upper).hexdigest())
        self.assertIsInstance(manifest["actual_byte_bucket"], int)
        self.assertIsInstance(manifest["planned_byte_bucket"], int)
        self.assertEqual(manifest["recipe_source"], "deterministic_contract_shape_v2")
        self.assertEqual(manifest["section_family"], "permutation_operation_stream")
        self.assertEqual(manifest["operation_family"], "Top,Bottom,Insert,Ask,Query")
        self.assertIn("state_machine:mutable_permutation", manifest["coverage_tags"])

    def test_32_mib_upper_bound_rejected_before_composition(self) -> None:
        with self.assertRaisesRegex(RecipeValidationError, "32 MiB"):
            compile_static_contract_v2(permutation_contract(n_max=2_000_000, m_max=2_000_000))
        with self.assertRaisesRegex(RecipeValidationError, "32 MiB"):
            compile_static_contract_v2(bracket_contract(n_max=10_000_000, q_max=2_000_000))


if __name__ == "__main__":
    unittest.main()
