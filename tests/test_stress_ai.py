from __future__ import annotations

import json
import threading
import time
import unittest
from unittest import mock

from tools.acm_agent.deepseek import DeepSeekProtocolError
from tools.acm_agent.stress_ai import (
    CONTRACT_SCHEMA_VERSION,
    GeneratedArtifact,
    StressPreparation,
    StressPreparationError,
    _OPTIONAL_GENERATOR_CASES,
    _REQUIRED_GENERATOR_CASES,
    _compact_audit_contract,
    _compact_generator_contract,
    _generator_recipe_prompt_content,
    _canonicalize_recipe_case_slots,
    _normalize_recipe_case_identity,
    _normalize_recipe_semantic_goal,
    _materialize_recipe_boundary_parameters,
    audit_generated_artifact,
    extract_contract,
    generate_artifact,
    generate_generator_blueprint,
    generate_generator_recipe,
    normalize_stress_contract,
    prepare_stress,
    search_reference,
    validate_generator_blueprint,
)
from tools.acm_agent.stress_sources import SourceCandidate, SourceSearchError
from tools.acm_agent.stress_budget import PreparationBudget, PreparationBudgetExhausted
from tools.acm_agent.stress_recipe import UnsupportedRecipeError, compose_generator_recipe
from tests.helpers.recipes import (
    p1111_recipe as _p1111_recipe,
    static_recipe as checked_in_static_recipe,
)
from tests.helpers.fakes import FakeChatResult as Result


GENERATOR_SOURCE = (
    "#include <iostream>\n#include <string>\n"
    "void acm_generate_case(unsigned long long,const std::string&,"
    "const std::string&,std::ostream&){}\n"
)


class FakeClient:
    def __init__(self, json_results, *, emit_retry=False):
        self.json_results = list(json_results)
        self.prompts = []
        self.json_kwargs = []
        self.tool_prompts = []
        self.progress = []
        self.emit_retry = emit_retry

    def chat_json(self, messages, **kwargs):
        self.prompts.append(messages)
        self.json_kwargs.append(dict(kwargs))
        if self.emit_retry and kwargs.get("retry_callback"):
            kwargs["retry_callback"](1, 2, "network_error", 1.0)
            self.emit_retry = False
        selected = self.json_results.pop(0)
        if isinstance(selected, BaseException):
            raise selected
        return selected if isinstance(selected, Result) else Result(data=selected)

    def chat_with_tools(self, messages, *, tool_handler, **kwargs):
        self.tool_prompts.append(messages)
        payload = json.loads(messages[-1]["content"])
        result = tool_handler(
            "search_source",
            {
                "tier": payload["tier"],
                "problem_id": payload["problem_id"],
                "title": payload["title"],
            },
        )
        selected = result["items"][0]["candidate_id"] if result["items"] else None
        return Result(content=json.dumps({"selected_candidate_id": selected}))


class CodeOnlyClient:
    def __init__(self, content="#include <iostream>\nint main(){return 0;}\n"):
        self.content = content
        self.prompts = []
        self.kwargs = []

    def chat(self, messages, **kwargs):
        self.prompts.append(messages)
        self.kwargs.append(dict(kwargs))
        return Result(content=self.content, usage={"total_tokens": 3})

    def chat_json(self, messages, **kwargs):
        raise AssertionError("code-only capable client must not use JSON transport")


class SequencedCodeOnlyClient(CodeOnlyClient):
    def __init__(self, contents):
        super().__init__()
        self.contents = list(contents)

    def chat(self, messages, **kwargs):
        self.prompts.append(messages)
        self.kwargs.append(dict(kwargs))
        return Result(content=self.contents.pop(0), usage={"total_tokens": 3})


class EmptyThinkingThenPatchClient(CodeOnlyClient):
    def __init__(self, patch: str):
        super().__init__()
        self.patch = patch

    def chat(self, messages, **kwargs):
        self.prompts.append(messages)
        self.kwargs.append(dict(kwargs))
        if len(self.prompts) == 1:
            result = Result(content="", usage={"total_tokens": 12288, "reasoning_tokens": 12288})
            result.finish_reason = "length"
            return result
        return Result(content=self.patch, usage={"total_tokens": 200})


class EmptyBoundedThinkingThenCodeClient(CodeOnlyClient):
    def __init__(self, code: str):
        super().__init__()
        self.code = code

    def chat(self, messages, **kwargs):
        self.prompts.append(messages)
        self.kwargs.append(dict(kwargs))
        if len(self.prompts) == 1:
            return Result(
                content="",
                usage={
                    "total_tokens": kwargs["max_tokens"],
                    "reasoning_tokens": kwargs["max_tokens"],
                },
            )
        return Result(content=self.code, usage={"total_tokens": 300})


class CancelScope:
    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class FakeCrawler:
    def __init__(self, by_tier):
        self.by_tier = by_tier
        self.calls = []

    def search(self, tier, *, problem_id, title):
        self.calls.append(tier)
        return list(self.by_tier.get(tier, []))


class RejectingCrawler(FakeCrawler):
    def search(self, tier, *, problem_id, title):
        self.calls.append(tier)
        raise SourceSearchError("source_url_rejected", "blocked redirect")


class ParallelPreparationClient:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.active = 0
        self.max_active = 0
        self.roles = []
        self.prompts = []

    def chat_json(self, messages, **kwargs):
        self.prompts.append(messages)
        request = json.loads(messages[-1]["content"])
        if request.get("type") == "acm_stress_contract":
            return Result(data=dict(V2_CONTRACT))
        if request.get("type") == "acm_stress_generator_blueprint_v1":
            return Result(data=json.loads(json.dumps(VALID_BLUEPRINT)))
        role = str(request.get("artifact_kind") or "")
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.roles.append(role)
        time.sleep(0.05)
        with self.lock:
            self.active -= 1
        return Result(
            data={
                "code": (
                    GENERATOR_SOURCE
                    if role == "generator"
                    else "#include <iostream>\nint main(){return %d;}" % (
                        1 if role == "reference_primary" else 2
                    )
                ),
                "notes": role,
            }
        )


SETTINGS = {
    "model": "deepseek-v4-flash",
    "thinking": True,
    "reasoning_effort": "high",
}
V2_CONTRACT = {
    "input_summary": "one integer",
    "small_profile": "n <= 8 and at most 20 operations",
    "small_lower_boundary": "n = 1",
    "large_profile": "80000 <= n <= 100000",
    "large_upper_boundary": "n = 100000",
    "output_compare": "token",
    "generator_requirements": ["support profile v2"],
}


def contract_v3(statement: str = "Input one integer n with 1 <= n <= 10."):
    quote = "1 <= n <= 10"
    start = statement.index(quote)
    return {
        "schema_version": 3,
        "input_summary": "one integer n",
        "small_profile": "1 <= n <= 8",
        "small_lower_boundary": "n = 1",
        "large_profile": "n = 10",
        "large_upper_boundary": "n = 10",
        "output_compare": "token",
        "generator_requirements": ["emit n"],
        "syntax": {
            "mode": "single_case",
            "eof": "required",
            "sections": [
                {
                    "id": "header",
                    "kind": "scalar",
                    "fields": [{"name": "n", "type": "int"}],
                    "variants": [],
                    "evidence_ids": ["n_range"],
                }
            ],
        },
        "constraints": [
            {
                "id": "n_range",
                "kind": "range",
                "target": "header.n",
                "args": {"minimum": 1, "maximum": 10},
                "evidence_ids": ["n_range"],
            }
        ],
        "evidence": [
            {
                "id": "n_range",
                "quote": quote,
                "start": start,
                "end": start + len(quote),
            }
        ],
        "coverage_obligations": [
            {
                "id": "n_minimum",
                "scope": "small",
                "predicate": {
                    "kind": "constraint_boundary",
                    "target": "n_range",
                    "args": {"side": "minimum"},
                },
                "minimum_witnesses": 1,
                "evidence_ids": ["n_range"],
            }
        ],
    }


def static_recipe_contract_v3():
    contract = contract_v3()
    contract["constraints"][0]["args"]["maximum"] = 1000
    contract["input_summary"] = "one integer n followed by n integers"
    contract["syntax"]["sections"].append(
        {
            "id": "values",
            "kind": "list",
            "count_from": "header.n",
            "fields": [{"name": "value", "type": "int"}],
            "variants": [],
            "evidence_ids": ["n_range"],
        }
    )
    return contract
AUDIT_ACCEPT = {
    "verdict": "accept",
    "confidence": 0.96,
    "issues": [],
    "fault_origin": "implementation",
    "witness": {"failure_confirmed": False},
    "summary": "ok",
}
AUDIT_REJECT = {
    "verdict": "reject",
    "confidence": 0.98,
    "issues": [
        {
            "category": "bounds",
            "severity": "critical",
            "evidence": "数组容量小于题面最大下标",
        }
    ],
    "summary": "数组容量不足",
}
# The primary fixture declares only the three core required pairs.  It still
# carries an optional small/lower_bound *case* so the optional branch stays
# exercised end-to-end; LEGACY_FOUR_CASE_BLUEPRINT below covers the old
# four-pair `required_cases` wire format.
VALID_BLUEPRINT = {
    "schema_version": 1,
    "required_cases": [
        {"profile": profile, "case_kind": case_kind}
        for profile, case_kind in _REQUIRED_GENERATOR_CASES
    ],
    "dimensions": [{"name": "n", "minimum": 1, "maximum": 100000}],
    "operation_families": ["query"],
    "required_coverage_tags": ["lower", "seeded", "upper", "large_random"],
    "large_required_coverage_tags": ["upper", "large_random"],
    "cases": [
        {
            "profile": "small",
            "case_kind": "lower_bound",
            "dimensions": {"n": "minimum"},
            "operation_families": ["query"],
            "coverage_tags": ["lower"],
            "uses_seed": False,
            "construction": "emit the exact legal lower bound",
            "total_complexity": "O(output_size)",
        },
        {
            "profile": "small",
            "case_kind": "random",
            "dimensions": {"n": "small"},
            "operation_families": ["query"],
            "coverage_tags": ["lower", "seeded", "upper", "large_random"],
            "uses_seed": True,
            "construction": "seeded small random construction",
            "total_complexity": "O(output_size^2)",
        },
        {
            "profile": "large",
            "case_kind": "upper_bound",
            "dimensions": {"n": "maximum"},
            "operation_families": ["query"],
            "coverage_tags": ["upper"],
            "uses_seed": False,
            "construction": "stream the exact upper bound",
            "total_complexity": "O(output_size)",
        },
        {
            "profile": "large",
            "case_kind": "random",
            "dimensions": {"n": "large"},
            "operation_families": ["query"],
            "coverage_tags": ["large_random"],
            "uses_seed": True,
            "construction": "stream a seeded large random case",
            "total_complexity": "O(output_size log n)",
        },
    ],
}

# Deliberately spelled out as literals rather than derived from
# ``_OPTIONAL_GENERATOR_CASES``/``_REQUIRED_GENERATOR_CASES``.  This is the
# frozen legacy wire format of already-certified blueprints, so it must keep
# describing four required cases even if the live policy constants change.
LEGACY_FOUR_CASE_BLUEPRINT = {
    **json.loads(json.dumps(VALID_BLUEPRINT)),
    "required_cases": [
        {"profile": "small", "case_kind": "lower_bound"},
        {"profile": "small", "case_kind": "random"},
        {"profile": "large", "case_kind": "upper_bound"},
        {"profile": "large", "case_kind": "random"},
    ],
}


def blueprint_case_pairs(blueprint):
    """Return the (profile, case_kind) pairs declared in ``cases``."""

    return [(case["profile"], case["case_kind"]) for case in blueprint["cases"]]


def required_case_pairs(blueprint):
    """Return the (profile, case_kind) pairs declared in ``required_cases``."""

    return [
        (item["profile"], item["case_kind"]) for item in blueprint["required_cases"]
    ]


def blueprint_without_cases(*dropped):
    """Copy ``VALID_BLUEPRINT`` with the given case pairs removed."""

    blueprint = json.loads(json.dumps(VALID_BLUEPRINT))
    blueprint["cases"] = [
        case
        for case in blueprint["cases"]
        if (case["profile"], case["case_kind"]) not in set(dropped)
    ]
    return blueprint


def blueprint_case(blueprint, profile, case_kind):
    """Return the mutable case entry for a declared pair."""

    return blueprint["cases"][blueprint_case_index(blueprint, profile, case_kind)]


def blueprint_case_index(blueprint, profile, case_kind):
    """Return the ``cases`` index of a declared pair, for error-path asserts."""

    for index, case in enumerate(blueprint["cases"]):
        if (case["profile"], case["case_kind"]) == (profile, case_kind):
            return index
    raise AssertionError(f"fixture is missing the {profile}/{case_kind} case")


class StressAITests(unittest.TestCase):
    def test_recipe_case_identity_normalizes_only_closed_enum_aliases(self) -> None:
        self.assertEqual(
            _normalize_recipe_case_identity(
                {"profile": "small_profile", "case_kind": "minimum"}
            ),
            {"profile": "small", "case_kind": "lower_bound"},
        )
        self.assertEqual(
            _normalize_recipe_case_identity(
                {"profile": "large", "case_kind": "seeded-random"}
            ),
            {"profile": "large", "case_kind": "random"},
        )
        self.assertEqual(
            _normalize_recipe_case_identity(
                {"profile": "mystery", "case_kind": "adversarial"}
            ),
            {"profile": "mystery", "case_kind": "adversarial"},
        )
        slots = _canonicalize_recipe_case_slots(
            [
                {"profile": "small", "case_kind": "boundary"},
                {"profile": "small", "case_kind": "random"},
                {"profile": "large", "case_kind": "boundary"},
                {"profile": "large", "case_kind": "random"},
            ]
        )
        self.assertEqual(
            [(case["profile"], case["case_kind"]) for case in slots],
            list(_REQUIRED_GENERATOR_CASES),
        )
        self.assertEqual(
            _normalize_recipe_semantic_goal(
                {
                    "template_id": "label.equal",
                    "parameters": {"label_min": 1, "label_max": 1},
                },
                case_kind="lower_bound",
            ),
            "equal_labels",
        )
        self.assertEqual(
            _normalize_recipe_semantic_goal(
                {"kind": "edge_time", "policy": "equal"},
                case_kind="lower_bound",
            ),
            "equal_labels",
        )
        self.assertEqual(
            _normalize_recipe_semantic_goal(
                {"kind": "random"}, case_kind="random"
            ),
            "seed_variation",
        )

    def test_boundary_dimensions_are_materialized_before_catalog_preconditions(self) -> None:
        from tools.acm_agent.stress_recipe import RecipeCatalog

        catalog = RecipeCatalog.load()
        structure = {
            "template_id": "graph.connected",
            "parameters": {
                "n_min": 1,
                "n_max": 10,
                "m_min": 1,
                "m_max": 100000,
            },
        }
        capability = {
            "bindings": {"n": "header.n", "m": "header.m"}
        }
        ranges = {"header.n": (1, 1000), "header.m": (1, 100000)}

        upper = _materialize_recipe_boundary_parameters(
            structure,
            capability=capability,
            ranges=ranges,
            case_kind="upper_bound",
            catalog=catalog,
        )

        self.assertEqual(
            upper["parameters"],
            {
                "n": 1000,
                "n_min": 1000,
                "n_max": 1000,
                "m": 100000,
                "m_min": 100000,
                "m_max": 100000,
            },
        )

    def test_generator_contract_keeps_operation_semantics_evidence(self) -> None:
        contract = contract_v3()
        contract["syntax"]["sections"].append(
            {
                "id": "operations",
                "kind": "operation_stream",
                "count_from": "header.n",
                "fields": [],
                "variants": [
                    {
                        "tag": "Insert",
                        "fields": [
                            {"name": "s", "type": "int"},
                            {"name": "t", "type": "int"},
                        ],
                        "evidence_ids": [],
                    }
                ],
                "evidence_ids": ["operation_semantics"],
            }
        )
        contract["evidence"].append(
            {
                "id": "operation_semantics",
                "quote": "t is a signed displacement, not another identifier",
                "start": 0,
                "end": 51,
            }
        )
        compact = _compact_generator_contract(contract)
        self.assertEqual(
            compact["semantic_evidence"],
            [
                {
                    "id": "operation_semantics",
                    "quote": "t is a signed displacement, not another identifier",
                }
            ],
        )

    def test_contract_normalizes_positional_operation_fields_from_evidence(self) -> None:
        statement = (
            "Input one integer n with 1 <= n <= 10. "
            "Operation syntax is `Insert s t`."
        )
        contract = contract_v3(statement)
        quote = "`Insert s t`"
        start = statement.index(quote)
        contract["evidence"].append(
            {
                "id": "insert_syntax",
                "quote": quote,
                "start": start,
                "end": start + len(quote),
            }
        )
        contract["syntax"]["sections"].append(
            {
                "id": "operations",
                "kind": "operation_stream",
                "count_from": "header.n",
                "fields": [],
                "variants": [
                    {
                        "tag": "Insert",
                        "fields": ["arg1", "arg2"],
                        "evidence_ids": ["insert_syntax"],
                    }
                ],
                "evidence_ids": ["insert_syntax"],
            }
        )
        contract["constraints"].extend(
            [
                {
                    "id": "insert_s_range",
                    "kind": "range",
                    "target": "operations.Insert.s",
                    "args": {"minimum": 1, "maximum": 10},
                    "evidence_ids": ["insert_syntax"],
                },
                {
                    "id": "insert_t_range",
                    "kind": "range",
                    "target": "operations.Insert.t",
                    "args": {"minimum": -1, "maximum": 1},
                    "evidence_ids": ["insert_syntax"],
                },
            ]
        )
        normalized = normalize_stress_contract(
            contract, compare="token", statement=statement
        )
        fields = normalized["syntax"]["sections"][1]["variants"][0]["fields"]
        self.assertEqual(
            fields,
            [{"name": "s", "type": "int"}, {"name": "t", "type": "int"}],
        )

    def test_contract_projects_field_source_and_normalizes_safe_type_aliases(self) -> None:
        statement = "Input one integer n with 1 <= n <= 10."
        contract = contract_v3(statement)
        contract["syntax"]["sections"][0]["fields"] = [
            {"name": "n", "type": "integer", "source": "input header"},
            {"name": "command", "type": "enum", "source": "statement grammar"},
        ]
        normalized = normalize_stress_contract(
            contract, compare="token", statement=statement
        )
        self.assertEqual(
            normalized["syntax"]["sections"][0]["fields"],
            [{"name": "n", "type": "int"}, {"name": "command", "type": "token"}],
        )

        invalid_source = contract_v3(statement)
        invalid_source["syntax"]["sections"][0]["fields"][0]["source"] = {
            "untrusted": True
        }
        with self.assertRaises(StressPreparationError):
            normalize_stress_contract(
                invalid_source, compare="token", statement=statement
            )

        unknown_type = contract_v3(statement)
        unknown_type["syntax"]["sections"][0]["fields"][0]["type"] = "pointer"
        with self.assertRaises(StressPreparationError) as raised:
            normalize_stress_contract(
                unknown_type, compare="token", statement=statement
            )
        self.assertEqual(raised.exception.details["actual_type"], "pointer")

    def test_dynamic_contract_requires_hidden_paired_validator_probe(self) -> None:
        statement = "Input one integer n with 1 <= n <= 10."
        contract = contract_v3(statement)
        contract["constraints"].append(
            {
                "id": "c_dynamic",
                "kind": "state_precondition",
                "target": "operations.Move",
                "args": {"description": "Move must remain inside the current sequence"},
                "evidence_ids": ["n_range"],
            }
        )
        with self.assertRaises(StressPreparationError) as missing:
            normalize_stress_contract(contract, compare="token", statement=statement)
        self.assertEqual(missing.exception.details["path"], "validator_probes")
        self.assertEqual(
            missing.exception.details["allowed_dynamic_constraint_ids"],
            ["c_dynamic"],
        )
        self.assertEqual(
            missing.exception.details["dynamic_constraint_kinds"],
            {"c_dynamic": "state_precondition"},
        )

        relaxed = normalize_stress_contract(
            contract,
            compare="token",
            statement=statement,
            require_complete_probes=False,
        )
        self.assertEqual(relaxed["validator_probes"], [])

        contract["validator_probes"] = [
            {
                "id": "vp_dynamic",
                "constraint_id": "c_dynamic",
                "valid_input": "2 1\n1 2\nMove 1 0\n",
                "invalid_input": "2 1\n1 2\nMove 1 -1\n",
                "evidence_ids": ["n_range"],
            }
        ]
        normalized = normalize_stress_contract(
            contract, compare="token", statement=statement
        )
        self.assertEqual(normalized["validator_probes"][0]["id"], "vp_dynamic")
        self.assertEqual(
            len(normalized["validator_probes"][0]["valid_input"].split()),
            len(normalized["validator_probes"][0]["invalid_input"].split()),
        )

        client = CodeOnlyClient()
        generate_artifact(
            client,
            kind="validator",
            problem_id="P1",
            statement=statement,
            contract=normalized,
            settings=SETTINGS,
            generation_mode="hybrid",
        )
        validator_prompt = json.dumps(client.prompts, ensure_ascii=False)
        self.assertNotIn("validator_probes", validator_prompt)
        self.assertNotIn("Move 1 -1", validator_prompt)

    def test_static_validator_probe_is_dropped_but_unknown_binding_fails_closed(self) -> None:
        statement = "Input one integer n with 1 <= n <= 10."
        contract = contract_v3(statement)
        contract["constraints"].append(
            {
                "id": "c_dynamic",
                "kind": "dependent_bound",
                "target": "values.x",
                "args": {"description": "x depends on the current state"},
                "evidence_ids": ["n_range"],
            }
        )
        static_probe = {
            "id": "vp_static",
            "constraint_id": "n_range",
            "valid_input": "1 1 1\n",
            "invalid_input": "1 1 2\n",
            "evidence_ids": ["n_range"],
        }
        dynamic_probe = {
            "id": "vp_dynamic",
            "constraint_id": "c_dynamic",
            "valid_input": "2 1 1\n",
            "invalid_input": "2 1 2\n",
            "evidence_ids": ["n_range"],
        }
        contract["validator_probes"] = [static_probe, dynamic_probe]

        normalized = normalize_stress_contract(
            contract, compare="token", statement=statement
        )

        self.assertEqual(
            [item["id"] for item in normalized["validator_probes"]],
            ["vp_dynamic"],
        )

        contract["validator_probes"] = [{**dynamic_probe, "constraint_id": "missing"}]
        with self.assertRaises(StressPreparationError) as unknown:
            normalize_stress_contract(contract, compare="token", statement=statement)
        self.assertEqual(
            unknown.exception.details["allowed_dynamic_constraint_ids"],
            ["c_dynamic"],
        )
        self.assertEqual(
            unknown.exception.details["dynamic_constraint_kinds"],
            {"c_dynamic": "dependent_bound"},
        )
        self.assertEqual(unknown.exception.details["constraint_id"], "missing")

        contract["validator_probes"] = [static_probe]
        with self.assertRaises(StressPreparationError) as missing:
            normalize_stress_contract(contract, compare="token", statement=statement)
        self.assertEqual(
            missing.exception.details["missing_constraint_ids"], ["c_dynamic"]
        )

    def test_p2596_reversed_state_probe_is_independently_certified(self) -> None:
        statement = (
            "Input one integer n with 1 <= n <= 10. Insert s -1 moves the book "
            "one position upward and must not cross the current top boundary."
        )
        raw = contract_v3(statement)
        raw["constraints"].append(
            {
                "id": "c_dynamic",
                "kind": "state_precondition",
                "target": "operations.Insert",
                "args": {"description": "Insert must not cross current boundary"},
                "evidence_ids": ["n_range"],
            }
        )
        reversed_probe = {
            "id": "vp_dynamic",
            "constraint_id": "c_dynamic",
            "valid_input": "3 2\n1 2 3\nTop 3\nInsert 3 -1\n",
            "invalid_input": "3 2\n1 2 3\nTop 3\nInsert 3 1\n",
            "evidence_ids": ["n_range"],
        }
        corrected_probe = {
            **reversed_probe,
            "valid_input": reversed_probe["invalid_input"],
            "invalid_input": reversed_probe["valid_input"],
        }
        raw["validator_probes"] = [reversed_probe]
        client = FakeClient(
            [raw, {"validator_probes": [corrected_probe]}]
        )

        contract, usage = extract_contract(
            client,
            problem_id="P1",
            statement=statement,
            compare="token",
            settings=SETTINGS,
            generation_mode="hybrid",
        )

        self.assertEqual(
            contract["validator_probes"][0]["valid_input"],
            corrected_probe["valid_input"],
        )
        self.assertEqual(usage["validator_probe_certification_requests"], 1)
        self.assertEqual(len(client.prompts), 2)
        certification = json.loads(client.prompts[1][-1]["content"])
        self.assertEqual(
            certification["type"],
            "acm_stress_validator_probe_certification_v1",
        )
        self.assertEqual(client.json_kwargs[1]["max_tokens"], 1536)
        self.assertFalse(client.json_kwargs[1]["thinking"])

    def test_probe_certification_semantic_failure_keeps_bounded_response_evidence(self) -> None:
        statement = (
            "Input one integer n with 1 <= n <= 10. Insert s -1 moves the book "
            "one position upward and must not cross the current top boundary."
        )
        raw = contract_v3(statement)
        raw["constraints"].append(
            {
                "id": "c_dynamic",
                "kind": "state_precondition",
                "target": "operations.Insert",
                "args": {"description": "Insert must not cross current boundary"},
                "evidence_ids": ["n_range"],
            }
        )
        raw["validator_probes"] = [
            {
                "id": "vp_dynamic",
                "constraint_id": "c_dynamic",
                "valid_input": "3 2\n1 2 3\nTop 3\nInsert 3 -1\n",
                "invalid_input": "3 2\n1 2 3\nTop 3\nInsert 3 1\n",
                "evidence_ids": ["n_range"],
            }
        ]
        malformed = {"probes_wrong_key": "bad payload"}
        client = FakeClient([raw, malformed, raw, malformed])
        with self.assertRaises(StressPreparationError) as caught:
            extract_contract(
                client,
                problem_id="P1",
                statement=statement,
                compare="token",
                settings=SETTINGS,
                generation_mode="hybrid",
            )
        details = caught.exception.details
        self.assertIn("validator_probes", str(details.get("path") or ""))
        self.assertEqual(len(details.get("response_sha256") or ""), 64)
        excerpt = str(details.get("response_excerpt") or "")
        self.assertIn("probes_wrong_key", excerpt)
        self.assertLessEqual(len(excerpt), 1000)

    def test_generated_code_rejects_unfinished_self_revision_comments(self) -> None:
        client = FakeClient(
            [
                {
                    "code": (
                        "#include <iostream>\nint main(){\n"
                        "// The code above is incomplete; I'll rewrite from scratch.\n"
                        "// We already emitted the records; we'll redo and rebuild the operation list.\n"
                        "// Actually we can't retroactively set the flags; I'll add them now.\n"
                        "// Boundary tags are optional coverage.\n"
                        "return 0;\n}\n"
                    ),
                    "notes": "",
                },
                {"code": "#include <iostream>\nint main(){return 0;}\n", "notes": ""},
            ]
        )
        artifact, usage = generate_artifact(
            client,
            kind="validator",
            problem_id="P1",
            statement="statement",
            contract=V2_CONTRACT,
            settings=SETTINGS,
            generation_mode="hybrid",
        )
        self.assertNotIn("incomplete", artifact.code)
        self.assertEqual(usage["validator_transport_repairs_used"], 1)
        failure = usage["validator_transport_failure"]
        self.assertEqual(failure["code"], "invalid_generated_code")
        self.assertGreater(failure["content_chars"], 0)
        self.assertIn("the code above is incomplete", failure["unfinished_markers"])
        self.assertIn("we'll redo", failure["unfinished_markers"])
        self.assertIn("rebuild the operation list", failure["unfinished_markers"])
        self.assertIn("actually we can't retroactively", failure["unfinished_markers"])
        self.assertLess(failure["repair_context_chars"], failure["content_chars"])
        self.assertIn("optional coverage", failure["unfinished_markers"])
        self.assertEqual(len(failure["content_sha256"]), 64)
        self.assertNotIn("validator_repairs_used", usage)

        repair_payload = json.loads(client.prompts[1][-1]["content"])
        repair_source = repair_payload["previous_code"].casefold()
        self.assertNotIn("we'll redo", repair_source)
        self.assertIn("unfinished self-revision tail omitted", repair_source)

    def test_contract_is_followed_by_three_parallel_helper_branches(self) -> None:
        client = ParallelPreparationClient()
        prepared = prepare_stress(
            client,
            FakeCrawler({}),
            platform="luogu",
            problem_id="P1",
            title="parallel",
            statement="statement",
            compare="token",
            settings=SETTINGS,
        )
        self.assertEqual(client.max_active, 3)
        self.assertEqual(
            set(client.roles),
            {"generator", "reference_primary", "reference_secondary"},
        )
        self.assertIsNotNone(prepared.generator)
        self.assertIsNotNone(prepared.reference_primary)
        self.assertIsNotNone(prepared.reference_secondary)
        prefixes = {}
        for messages in client.prompts[1:]:
            request = json.loads(messages[-1]["content"])
            key = request.get("artifact_kind") or "blueprint"
            prefixes[key] = messages[:3]
        self.assertEqual(
            set(prefixes),
            {"blueprint", "generator", "reference_primary", "reference_secondary"},
        )
        # Blueprint prewarms the compact contract-only prefix used by the two
        # input-side roles; solution roles retain the full statement.
        self.assertEqual(prefixes["blueprint"], prefixes["generator"])
        self.assertEqual(prefixes["reference_primary"], prefixes["reference_secondary"])
        compact_context = json.loads(prefixes["generator"][1]["content"])
        full_context = json.loads(prefixes["reference_primary"][1]["content"])
        self.assertTrue(compact_context["statement_omitted"])
        self.assertNotIn("statement", compact_context)
        self.assertEqual(full_context["statement"], "statement")

    def test_contract_v2_requires_manual_small_and_extreme_large_boundaries(self) -> None:
        client = FakeClient([dict(V2_CONTRACT)])
        contract, _ = extract_contract(
            client,
            problem_id="P1",
            statement="1 <= n <= 100000",
            compare="token",
            settings=SETTINGS,
        )
        self.assertEqual(contract["profile_version"], 2)
        self.assertEqual(contract["small_lower_boundary"], "n = 1")
        self.assertEqual(contract["large_upper_boundary"], "n = 100000")
        prompt = json.dumps(client.prompts, ensure_ascii=False)
        self.assertIn("手工核对", prompt)
        self.assertIn("80% 到 100%", prompt)
        self.assertIn("small/lower_bound 必须精确实现", prompt)
        self.assertIn("全局最大总规模", prompt)

    def test_contract_v3_is_canonical_and_evidence_bound(self) -> None:
        statement = "Input one integer n with 1 <= n <= 10."
        raw_contract = contract_v3(statement)
        raw_contract["syntax"]["sections"][0]["count_from"] = None
        contract = normalize_stress_contract(
            raw_contract, compare="token", statement=statement
        )
        self.assertEqual(contract["schema_version"], CONTRACT_SCHEMA_VERSION)
        self.assertEqual(contract["validation_level"], "structured")
        self.assertEqual(contract["syntax"]["sections"][0]["id"], "header")
        self.assertNotIn("count_from", contract["syntax"]["sections"][0])
        self.assertEqual(contract["constraints"][0]["evidence_ids"], ["n_range"])
        evidence = contract["evidence"][0]
        self.assertEqual(statement[evidence["start"] : evidence["end"]], evidence["quote"])
        self.assertEqual(
            contract["coverage_obligations"][0]["predicate"]["kind"],
            "constraint_boundary",
        )

    def test_contract_v3_binds_flattened_bullet_dash_evidence_quote(self) -> None:
        statement = (
            "The shelf holds books `1..n` from top to bottom.\n"
            "- `Top s`: move book `s` to the top.\n"
            "- `Insert s t`: swap book `s` with the adjacent book.\n"
            "Every legality check is against the current order."
        )
        flattened_quote = (
            "The shelf holds books 1..n from top to bottom. - Top s: move book "
            "s to the top. - Insert s t: swap book s with the adjacent book. "
            "Every legality check is against the current order."
        )
        raw_contract = contract_v3()
        raw_contract["evidence"][0]["quote"] = flattened_quote
        raw_contract["evidence"][0]["start"] = 0
        raw_contract["evidence"][0]["end"] = 20
        contract = normalize_stress_contract(
            raw_contract, compare="token", statement=statement
        )
        evidence = contract["evidence"][0]
        self.assertEqual(
            statement[evidence["start"] : evidence["end"]], evidence["quote"]
        )
        self.assertNotEqual(evidence["quote"], flattened_quote)
        self.assertIn("move book `s` to the top", evidence["quote"])

    def test_contract_v3_drops_degenerate_identical_probe_pairs(self) -> None:
        raw = contract_v3()
        raw["constraints"].append(
            {
                "id": "c_dynamic",
                "kind": "state_precondition",
                "target": "operations.Insert",
                "args": {"description": "Insert must not cross current boundary"},
                "evidence_ids": ["n_range"],
            }
        )
        raw["validator_probes"] = [
            {
                "id": "vp_ok",
                "constraint_id": "c_dynamic",
                "valid_input": "3 1\n1 2 3\nInsert 2 1\n",
                "invalid_input": "3 1\n1 2 3\nInsert 2 -1\n",
                "evidence_ids": ["n_range"],
            },
            {
                "id": "vp_degenerate",
                "constraint_id": "c_dynamic",
                "valid_input": "3 1\n1 2 3\nInsert 2 1\n",
                "invalid_input": "3 1\n1 2 3\nInsert 2 1\n",
                "evidence_ids": ["n_range"],
            },
        ]
        contract = normalize_stress_contract(raw, compare="token", statement="Input one integer n with 1 <= n <= 10.")
        self.assertEqual(
            [probe["id"] for probe in contract["validator_probes"]],
            ["vp_ok"],
        )

    def test_contract_v3_all_degenerate_probes_fail_closed(self) -> None:
        raw = contract_v3()
        raw["constraints"].append(
            {
                "id": "c_dynamic",
                "kind": "state_precondition",
                "target": "operations.Insert",
                "args": {"description": "Insert must not cross current boundary"},
                "evidence_ids": ["n_range"],
            }
        )
        raw["validator_probes"] = [
            {
                "id": "vp_degenerate",
                "constraint_id": "c_dynamic",
                "valid_input": "3 1\n1 2 3\nInsert 2 1\n",
                "invalid_input": "3 1\n1 2 3\nInsert 2 1\n",
                "evidence_ids": ["n_range"],
            }
        ]
        with self.assertRaises(StressPreparationError) as caught:
            normalize_stress_contract(
                raw, compare="token", statement="Input one integer n with 1 <= n <= 10."
            )
        self.assertEqual(caught.exception.details["path"], "validator_probes")
        self.assertIn("c_dynamic", str(caught.exception.details.get("missing_constraint_ids") or ""))

    def test_contract_v3_rejects_unbound_evidence_and_bad_references(self) -> None:
        statement = "Input one integer n with 1 <= n <= 10."
        unbound = contract_v3(statement)
        unbound["evidence"][0]["quote"] = "1 <= n <= 11"
        with self.assertRaises(StressPreparationError) as evidence_error:
            normalize_stress_contract(unbound, compare="token", statement=statement)
        self.assertEqual(evidence_error.exception.details["path"], "evidence[0]")

        missing = contract_v3(statement)
        missing["constraints"][0]["evidence_ids"] = ["not_present"]
        with self.assertRaises(StressPreparationError) as reference_error:
            normalize_stress_contract(missing, compare="token", statement=statement)
        self.assertIn("evidence", str(reference_error.exception))

    def test_contract_v3_recomputes_only_uniquely_bound_evidence_offsets(self) -> None:
        statement = "Input one integer n with 1 <= n <= 10."
        unique = contract_v3(statement)
        unique["evidence"][0]["start"] = 0
        unique["evidence"][0]["end"] = 1
        normalized = normalize_stress_contract(
            unique, compare="token", statement=statement
        )
        evidence = normalized["evidence"][0]
        self.assertEqual(
            statement[evidence["start"] : evidence["end"]], evidence["quote"]
        )

        ambiguous_statement = statement + " Again: 1 <= n <= 10."
        ambiguous = contract_v3(ambiguous_statement)
        ambiguous["evidence"][0]["start"] = 0
        ambiguous["evidence"][0]["end"] = 1
        normalized_ambiguous = normalize_stress_contract(
            ambiguous, compare="token", statement=ambiguous_statement
        )
        self.assertEqual(
            normalized_ambiguous["evidence"][0]["start"],
            ambiguous_statement.index("1 <= n <= 10"),
        )

    def test_contract_v3_binds_markdown_stripped_quote_to_raw_statement(self) -> None:
        raw_statement = "The first line contains `$n$` and   $m$."
        contract = contract_v3()
        contract["evidence"][0] = {
            "id": "n_range",
            "quote": "The first line contains n and m.",
            "start": 0,
            "end": 1,
        }
        normalized = normalize_stress_contract(
            contract, compare="token", statement=raw_statement
        )
        evidence = normalized["evidence"][0]
        self.assertEqual(evidence["quote"], "The first line contains `$n$` and   $m$.")
        self.assertEqual(
            raw_statement[evidence["start"] : evidence["end"]], evidence["quote"]
        )

    def test_contract_v3_binds_rendered_latex_operator_quote(self) -> None:
        raw_statement = r"For $100\%$ of data, $3 \leq n,m \leq 8 \times 10^4$."
        contract = contract_v3()
        contract["evidence"][0] = {
            "id": "n_range",
            "quote": "For 100% of data, 3 ≤ n,m ≤ 8 × 10^4.",
            "start": -1,
            "end": -1,
        }
        normalized = normalize_stress_contract(
            contract, compare="token", statement=raw_statement
        )
        evidence = normalized["evidence"][0]
        self.assertEqual(evidence["quote"], raw_statement)

    def test_contract_v3_binds_joined_markdown_list_items_without_losing_minus(self) -> None:
        raw_statement = (
            "- Never perform `Insert s -1` at the top.\n"
            "- Never perform `Insert s 1` at the bottom."
        )
        contract = contract_v3()
        contract["evidence"][0] = {
            "id": "n_range",
            "quote": (
                "Never perform Insert s -1 at the top. "
                "Never perform Insert s 1 at the bottom."
            ),
            "start": -1,
            "end": -1,
        }
        normalized = normalize_stress_contract(
            contract, compare="token", statement=raw_statement
        )
        evidence = normalized["evidence"][0]
        self.assertIn("Insert s -1", evidence["quote"])
        self.assertEqual(evidence["quote"], raw_statement[2:])

    def test_contract_v3_binds_flattened_code_bullets(self) -> None:
        raw_statement = (
            "Process operations in sequence.\n"
            "- `Top s`: move book s to the top.\n"
            "- `Bottom s`: move book s to the bottom.\n"
            "- `Query k`: inspect position k."
        )
        contract = contract_v3()
        contract["evidence"][0] = {
            "id": "n_range",
            "quote": (
                "Process operations in sequence. - `Top s`: move book s to the top. "
                "- `Bottom s`: move book s to the bottom. "
                "- `Query k`: inspect position k."
            ),
            "start": -1,
            "end": -1,
        }
        normalized = normalize_stress_contract(
            contract, compare="token", statement=raw_statement
        )
        evidence = normalized["evidence"][0]
        self.assertEqual(evidence["quote"], raw_statement)

    def test_contract_v3_expands_ordered_evidence_clauses_to_full_source_span(self) -> None:
        raw_statement = (
            r"- The size is $3 \leq n \leq 10$." "\n"
            "- The values form a permutation of 1 to n.\n"
            r"- The operation has $-1 \leq t \leq 1$, and op is one of five tags." "\n"
            "- Never perform `Insert s -1` at the top."
        )
        contract = contract_v3()
        contract["evidence"][0] = {
            "id": "n_range",
            "quote": (
                "The size is 3 ≤ n ≤ 10. "
                "The values form a permutation of 1 to n. "
                "The operation has -1 ≤ t ≤ 1. "
                "Never perform Insert s -1 at the top."
            ),
            "start": -1,
            "end": -1,
        }
        normalized = normalize_stress_contract(
            contract, compare="token", statement=raw_statement
        )
        evidence = normalized["evidence"][0]
        self.assertIn("op is one of five tags", evidence["quote"])
        self.assertEqual(evidence["quote"], raw_statement[2:])

    def test_contract_v3_binds_short_numeric_clause_in_ordered_span(self) -> None:
        raw_statement = (
            r"- $3 \leq n, m \leq 8 \times 10^4$." "\n"
            r"- $p_i$ is a permutation of 1 to n." "\n"
            r"- $1 \leq s \leq n$, $-1 \leq t \leq 1$, and op is legal." "\n"
            "- When there is no book above s, Insert s -1 is not performed."
        )
        contract = contract_v3()
        contract["evidence"][0] = {
            "id": "n_range",
            "quote": (
                "3 ≤ n, m ≤ 8 × 10^4. "
                "p_i is a permutation of 1 to n. "
                "1 ≤ s ≤ n, -1 ≤ t ≤ 1. "
                "When there is no book above s, Insert s -1 is not performed."
            ),
            "start": -1,
            "end": -1,
        }
        normalized = normalize_stress_contract(
            contract, compare="token", statement=raw_statement
        )
        evidence = normalized["evidence"][0]
        self.assertEqual(
            evidence["quote"],
            raw_statement[evidence["start"] : evidence["end"]],
        )
        self.assertIn("and op is legal", evidence["quote"])

    def test_contract_v3_expands_ordered_ellipsis_anchors(self) -> None:
        raw_statement = (
            "Each line starts with a string `op`.\n"
            "- If op is Top, followed by an integer s, move it first.\n"
            "- If op is Bottom, followed by s, move it last.\n"
            "- If op is Insert, followed by two integers s, t, move locally.\n"
            "- If op is Query, followed by an integer s, inspect it."
        )
        contract = contract_v3()
        contract["evidence"][0] = {
            "id": "n_range",
            "quote": (
                "Each line starts with a string op. "
                "If op is Top, followed by an integer s, ... "
                "If op is Bottom, ... "
                "If op is Insert, followed by two integers s, t, ... "
                "If op is Query, followed by an integer s"
            ),
            "start": -1,
            "end": -1,
        }
        normalized = normalize_stress_contract(
            contract, compare="token", statement=raw_statement
        )
        evidence = normalized["evidence"][0]
        self.assertIn("move it last", evidence["quote"])
        self.assertIn("move locally", evidence["quote"])

    def test_contract_v3_binds_comma_list_of_operation_signatures(self) -> None:
        raw_statement = (
            "- Top s: move s first.\n"
            "- Bottom s: move s last.\n"
            "- Insert s t: move s locally.\n"
            "- Ask s: print its rank.\n"
            "- Query k: print the item at k."
        )
        contract = contract_v3()
        contract["evidence"][0] = {
            "id": "n_range",
            "quote": "Top s, Bottom s, Insert s t, Ask s, Query k",
            "start": -1,
            "end": -1,
        }
        normalized = normalize_stress_contract(
            contract, compare="token", statement=raw_statement
        )
        evidence = normalized["evidence"][0]
        self.assertEqual(
            evidence["quote"],
            raw_statement[evidence["start"] : evidence["end"]],
        )
        self.assertIn("move s locally", evidence["quote"])

    def test_contract_v3_normalizes_unambiguous_variant_name_alias(self) -> None:
        statement = "Input one integer n with 1 <= n <= 10."
        contract = contract_v3(statement)
        section = contract["syntax"]["sections"][0]
        section["kind"] = "operation_stream"
        section["fields"] = []
        section["variants"] = [
            {
                "name": "Ask",
                "fields": [{"name": "x", "type": "int", "count": "header.n"}],
                "evidence_ids": ["n_range"],
            }
        ]
        normalized = normalize_stress_contract(
            contract, compare="token", statement=statement
        )
        self.assertEqual(
            normalized["syntax"]["sections"][0]["variants"][0]["tag"], "Ask"
        )
        self.assertEqual(
            normalized["syntax"]["sections"][0]["variants"][0]["fields"][0][
                "count_from"
            ],
            "header.n",
        )
        self.assertEqual(
            normalized["syntax"]["sections"][0]["variants"][0]["evidence_ids"],
            ["n_range"],
        )

        section["variants"][0]["tag"] = "Query"
        with self.assertRaises(StressPreparationError):
            normalize_stress_contract(contract, compare="token", statement=statement)

    def test_contract_v3_obligations_are_enforced_by_generator_recipe(self) -> None:
        statement = "Input one integer n with 1 <= n <= 10."
        contract = normalize_stress_contract(
            contract_v3(statement), compare="token", statement=statement
        )
        recipe = json.loads(json.dumps(VALID_BLUEPRINT))
        validated = validate_generator_blueprint(recipe, contract=contract)
        self.assertNotIn("n_minimum", validated["required_coverage_tags"])
        self.assertEqual(
            validated["cases"][0]["coverage_tags"], ["n_minimum"]
        )
        self.assertNotIn(
            "n_minimum", validated["cases"][1]["coverage_tags"]
        )
        self.assertEqual(validate_generator_blueprint(validated), validated)

    def test_contract_v3_normalizes_unambiguous_line_section(self) -> None:
        statement = "Input one integer n with 1 <= n <= 10."
        raw = contract_v3(statement)
        raw["syntax"]["sections"][0]["kind"] = "line"
        normalized = normalize_stress_contract(
            raw, compare="token", statement=statement
        )
        self.assertEqual(normalized["syntax"]["sections"][0]["kind"], "scalar")

    def test_contract_v3_derives_boundary_operation_and_special_value_coverage(self) -> None:
        statement = "Input one integer n with 1 <= n <= 10."
        raw = contract_v3(statement)
        raw["coverage_obligations"] = []
        raw["syntax"]["sections"].append(
            {
                "id": "operations",
                "kind": "operation_stream",
                "count_from": "header.n",
                "fields": [],
                "variants": [
                    {
                        "id": "Insert",
                        "op": "Insert",
                        "fields": [
                            {
                                "name": "t",
                                "type": "int",
                                "minimum": -1,
                                "maximum": 1,
                            }
                        ],
                        "evidence_ids": ["n_range"],
                    }
                ],
                "evidence_ids": ["n_range"],
            }
        )
        contract = normalize_stress_contract(
            raw, compare="token", statement=statement
        )
        self.assertEqual(
            contract["syntax"]["sections"][1]["variants"][0]["tag"],
            "Insert",
        )
        predicates = [item["predicate"] for item in contract["coverage_obligations"]]
        self.assertIn(
            {"kind": "constraint_boundary", "target": "n_range", "args": {"side": "minimum"}},
            predicates,
        )
        self.assertIn(
            {"kind": "constraint_boundary", "target": "n_range", "args": {"side": "maximum"}},
            predicates,
        )
        self.assertIn(
            {"kind": "operation_variant", "target": "Insert", "args": {}},
            predicates,
        )
        for value in (-1, 0, 1):
            self.assertIn(
                {"kind": "value_class", "target": "Insert.t", "args": {"value": value}},
                predicates,
            )

    def test_structured_recipe_accepts_only_case_planning_fields(self) -> None:
        statement = "Input one integer n with 1 <= n <= 10."
        contract = normalize_stress_contract(
            contract_v3(statement), compare="token", statement=statement
        )
        cases = []
        for profile, case_kind in (
            ("small", "lower_bound"),
            ("small", "random"),
            ("large", "upper_bound"),
            ("large", "random"),
        ):
            cases.append(
                {
                    "profile": profile,
                    "case_kind": case_kind,
                    "dimensions": {
                        "n": (
                            1
                            if case_kind == "lower_bound"
                            else 8
                            if profile == "small"
                            else 10
                        )
                    },
                    "construction": "emit n; random consumes seed"
                    if case_kind == "random"
                    else "emit the exact boundary",
                    "total_complexity": "O(output_size)",
                }
            )
        validated = validate_generator_blueprint(
            {"schema_version": 1, "cases": cases}, contract=contract
        )
        self.assertEqual(
            required_case_pairs(validated),
            list(_REQUIRED_GENERATOR_CASES),
        )
        self.assertEqual(validated["dimensions"], [{"name": "n"}])
        self.assertEqual(validated["cases"][0]["coverage_tags"], ["n_minimum"])
        self.assertTrue(validated["cases"][1]["uses_seed"])

    def test_structured_recipe_clamps_oversized_small_entity_dimension(self) -> None:
        statement = "Input one integer n with 1 <= n <= 10."
        contract = normalize_stress_contract(
            contract_v3(statement), compare="token", statement=statement
        )
        recipe = json.loads(json.dumps(VALID_BLUEPRINT))
        recipe["cases"][1]["dimensions"]["n"] = 10
        validated = validate_generator_blueprint(recipe, contract=contract)
        self.assertEqual(validated["cases"][1]["dimensions"]["n"], 8)

    def test_legacy_contract_is_explicitly_compatible_not_structured(self) -> None:
        contract = normalize_stress_contract(
            V2_CONTRACT, compare="token", statement="statement"
        )
        self.assertEqual(contract["schema_version"], 3)
        self.assertEqual(contract["source_schema_version"], 2)
        self.assertEqual(contract["validation_level"], "legacy_text")
        self.assertEqual(contract["syntax"]["mode"], "legacy_text")
        self.assertEqual(contract["constraints"], [])
        reused = normalize_stress_contract(
            contract, compare="token", statement="statement"
        )
        self.assertEqual(reused, contract)

    def test_hybrid_contract_repairs_fast_with_fixed_caps(self) -> None:
        statement = "Input one integer n with 1 <= n <= 10."
        invalid = contract_v3(statement)
        invalid["evidence"][0]["quote"] = "hallucinated constraint"
        client = FakeClient([invalid, contract_v3(statement)])
        contract, usage = extract_contract(
            client,
            problem_id="P1",
            statement=statement,
            compare="token",
            settings=SETTINGS,
            generation_mode="hybrid",
        )
        self.assertEqual(contract["validation_level"], "structured")
        self.assertEqual(usage["contract_repairs_used"], 1)
        self.assertEqual(
            [item["thinking"] for item in client.json_kwargs], [False, False]
        )
        self.assertEqual(
            [item["max_tokens"] for item in client.json_kwargs], [2048, 4096]
        )
        repair = json.loads(client.prompts[1][-1]["content"])
        self.assertEqual(repair["type"], "acm_stress_contract_v3_repair")
        self.assertEqual(repair["structured_diagnostic"]["path"], "evidence[0]")
        self.assertEqual(repair["repair_scope"], "evidence_only")
        self.assertIn("禁止翻译、改写", repair["instructions"])

    def test_failed_contract_repair_preserves_both_raw_attempts(self) -> None:
        statement = "Input one integer n with 1 <= n <= 10."
        invalid = contract_v3(statement)
        invalid["evidence"][0]["quote"] = "paraphrased and hallucinated evidence"
        client = FakeClient([invalid, invalid, invalid, invalid])
        with self.assertRaises(StressPreparationError) as raised:
            extract_contract(
                client,
                problem_id="P1",
                statement=statement,
                compare="token",
                settings=SETTINGS,
                generation_mode="hybrid",
            )
        attempts = raised.exception.details["contract_attempts"]
        self.assertEqual([item["attempt"] for item in attempts], [1, 2, 3, 4])
        self.assertTrue(all(len(item["sha256"]) == 64 for item in attempts))
        self.assertTrue(
            all("paraphrased and hallucinated" in item["raw_json_excerpt"] for item in attempts)
        )

    def test_minimal_contract_rebinds_only_evidence_after_bounded_repairs(self) -> None:
        statement = "Input one integer n with 1 <= n <= 10."
        invalid = contract_v3(statement)
        invalid["evidence"][0]["quote"] = "paraphrased and hallucinated evidence"
        client = FakeClient([invalid, invalid, invalid, invalid])
        contract, usage = extract_contract(
            client,
            problem_id="P1",
            statement=statement,
            compare="token",
            settings=SETTINGS,
            generation_mode="hybrid",
            require_complete_probes=False,
        )
        self.assertTrue(usage["contract_evidence_rebound"])
        self.assertEqual(usage["contract_repairs_used"], 3)
        self.assertEqual(
            contract["syntax"]["sections"][0]["id"],
            invalid["syntax"]["sections"][0]["id"],
        )
        self.assertEqual(
            contract["constraints"][0]["target"],
            invalid["constraints"][0]["target"],
        )
        self.assertEqual(contract["evidence"][0]["quote"], statement)
        self.assertEqual(contract["validator_probes"], [])

    def test_contract_protocol_length_failure_uses_explicit_repair(self) -> None:
        statement = "Input one integer n with 1 <= n <= 10."
        failure = DeepSeekProtocolError(
            "invalid_json_output",
            "completion token limit reached",
            usage={"completion_tokens": 2048, "total_tokens": 2100},
            finish_reason="length",
        )
        client = FakeClient([failure, contract_v3(statement)])
        contract, usage = extract_contract(
            client,
            problem_id="P1",
            statement=statement,
            compare="token",
            settings=SETTINGS,
            generation_mode="hybrid",
        )
        self.assertEqual(contract["validation_level"], "structured")
        self.assertEqual(usage["contract_repairs_used"], 1)
        self.assertEqual(
            [item["max_tokens"] for item in client.json_kwargs], [2048, 4096]
        )
        self.assertEqual(
            [item["thinking"] for item in client.json_kwargs], [False, False]
        )
        self.assertEqual([item["json_retries"] for item in client.json_kwargs], [0, 1])
        repair = json.loads(client.prompts[1][-1]["content"])
        self.assertEqual(repair["structured_diagnostic"]["path"], "$")

    def test_blueprint_protocol_length_failure_uses_larger_explicit_retry(self) -> None:
        failure = DeepSeekProtocolError(
            "invalid_json_output",
            "completion token limit reached",
            usage={"completion_tokens": 6144, "total_tokens": 6200},
            finish_reason="length",
        )
        client = FakeClient([failure, json.loads(json.dumps(VALID_BLUEPRINT))])
        blueprint, usage = generate_generator_blueprint(
            client,
            problem_id="P1",
            statement="statement",
            contract={**V2_CONTRACT, "profile_version": 2},
            settings=SETTINGS,
            generation_mode="hybrid",
            repair_limit=1,
        )
        self.assertEqual(blueprint["schema_version"], 1)
        self.assertEqual(usage["blueprint_repairs_used"], 1)
        self.assertEqual(
            [item["max_tokens"] for item in client.json_kwargs], [6144, 8192]
        )
        repair = json.loads(client.prompts[1][-1]["content"])
        self.assertEqual(
            repair["structured_diagnostic"]["code"], "invalid_json_output"
        )
        self.assertEqual([item["json_retries"] for item in client.json_kwargs], [1, 1])

    def test_contract_normalizes_structured_profiles_and_supplies_hard_requirements(self) -> None:
        raw = dict(V2_CONTRACT)
        raw["large_profile"] = {"n": 80_000, "operations": ["Top", "Insert 0"]}
        raw["generator_requirements"] = []
        client = FakeClient([raw])
        contract, _ = extract_contract(
            client,
            problem_id="P2596",
            statement="3 <= n,m <= 80000",
            compare="token",
            settings=SETTINGS,
        )
        self.assertEqual(
            contract["large_profile"],
            '{"n":80000,"operations":["Top","Insert 0"]}',
        )
        requirements = "\n".join(contract["generator_requirements"])
        self.assertIn("small/random", requirements)
        self.assertIn("线性 erase/insert", requirements)
        self.assertIn("恒等、无移动", requirements)
        prompt = json.dumps(client.prompts, ensure_ascii=False)
        self.assertIn("不得返回嵌套对象", prompt)
        self.assertIn("O(输出规模 log n)", prompt)

    def test_blueprint_validator_enforces_seed_coverage_and_large_complexity(self) -> None:
        valid = validate_generator_blueprint(
            json.loads(json.dumps(VALID_BLUEPRINT))
        )
        self.assertEqual(valid["schema_version"], 1)
        self.assertEqual(len(valid["cases"]), 4)

        invalid_seed = json.loads(json.dumps(VALID_BLUEPRINT))
        invalid_seed["cases"][1]["uses_seed"] = False
        with self.assertRaises(StressPreparationError) as seed_error:
            validate_generator_blueprint(invalid_seed)
        self.assertEqual(seed_error.exception.code, "stress_blueprint_invalid")
        self.assertIn("uses_seed", seed_error.exception.details["path"])

        invalid_coverage = json.loads(json.dumps(VALID_BLUEPRINT))
        invalid_coverage["cases"][3]["coverage_tags"] = ["seeded"]
        with self.assertRaises(StressPreparationError) as coverage_error:
            validate_generator_blueprint(invalid_coverage)
        self.assertEqual(coverage_error.exception.code, "stress_blueprint_invalid")
        self.assertIn("coverage", str(coverage_error.exception))

        invalid_complexity = json.loads(json.dumps(VALID_BLUEPRINT))
        invalid_complexity["cases"][2]["total_complexity"] = "O(n^2)"
        with self.assertRaises(StressPreparationError) as complexity_error:
            validate_generator_blueprint(invalid_complexity)
        self.assertEqual(complexity_error.exception.code, "stress_blueprint_invalid")
        self.assertIn("total_complexity", complexity_error.exception.details["path"])

    def test_blueprint_normalizes_dimension_mapping_without_inventing_missing_dimensions(self) -> None:
        raw = json.loads(json.dumps(VALID_BLUEPRINT))
        raw["dimensions"] = {
            "q": {"name": "conflicting", "minimum": 1, "maximum": 100000},
            "n": {"minimum": 1, "maximum": 100000},
        }
        for case in raw["cases"]:
            case["dimensions"]["q"] = case["dimensions"]["n"]
        normalized = validate_generator_blueprint(raw)
        self.assertEqual(
            [dimension["name"] for dimension in normalized["dimensions"]],
            ["n", "q"],
        )

        missing = json.loads(json.dumps(VALID_BLUEPRINT))
        del missing["dimensions"]
        with self.assertRaises(StressPreparationError) as missing_error:
            validate_generator_blueprint(missing)
        self.assertEqual(missing_error.exception.details["path"], "dimensions")

    def test_generator_case_policy_pins_four_required_cases(self) -> None:
        # Single place that pins the policy itself.  Every other test derives
        # its expectations from these constants, so a deliberate policy change
        # fails loudly here instead of silently rewriting the whole suite.
        self.assertEqual(
            _REQUIRED_GENERATOR_CASES,
            (
                ("small", "lower_bound"),
                ("small", "random"),
                ("large", "upper_bound"),
                ("large", "random"),
            ),
        )
        self.assertEqual(_OPTIONAL_GENERATOR_CASES, ())
        self.assertIn(("small", "lower_bound"), _REQUIRED_GENERATOR_CASES)
        self.assertFalse(
            set(_REQUIRED_GENERATOR_CASES) & set(_OPTIONAL_GENERATOR_CASES)
        )

    def test_blueprint_validates_without_any_optional_case(self) -> None:
        blueprint = blueprint_without_cases(*_OPTIONAL_GENERATOR_CASES)
        self.assertEqual(
            blueprint_case_pairs(blueprint), list(_REQUIRED_GENERATOR_CASES)
        )
        validated = validate_generator_blueprint(blueprint)
        self.assertEqual(validated["schema_version"], 1)
        self.assertEqual(
            blueprint_case_pairs(validated), list(_REQUIRED_GENERATOR_CASES)
        )
        self.assertEqual(
            required_case_pairs(validated), list(_REQUIRED_GENERATOR_CASES)
        )
        # Re-validating a validated blueprint must be a fixed point.
        self.assertEqual(validate_generator_blueprint(validated), validated)

    def test_blueprint_rejects_a_missing_required_case(self) -> None:
        for pair in _REQUIRED_GENERATOR_CASES:
            with self.subTest(missing=pair):
                incomplete = blueprint_without_cases(pair)
                self.assertNotIn(pair, blueprint_case_pairs(incomplete))
                with self.assertRaises(StressPreparationError) as error:
                    validate_generator_blueprint(incomplete)
                self.assertEqual(error.exception.code, "stress_blueprint_invalid")
                self.assertEqual(error.exception.details["path"], "cases")

    def test_declared_optional_case_is_still_fully_validated(self) -> None:
        for pair in _OPTIONAL_GENERATOR_CASES:
            profile, case_kind = pair
            # Positive control: a well-formed optional case is accepted and kept.
            accepted = validate_generator_blueprint(
                json.loads(json.dumps(VALID_BLUEPRINT))
            )
            self.assertIn(pair, blueprint_case_pairs(accepted))
            self.assertNotIn(pair, required_case_pairs(accepted))

            index = blueprint_case_index(VALID_BLUEPRINT, profile, case_kind)
            malformations = (
                ("dimensions", {}, f"cases[{index}].dimensions"),
                ("dimensions", 7, f"cases[{index}].dimensions"),
                ("uses_seed", "no", f"cases[{index}].uses_seed"),
                ("total_complexity", "", f"cases[{index}].total_complexity"),
            )
            for field, bad_value, expected_path in malformations:
                with self.subTest(case=pair, field=field, value=bad_value):
                    invalid = json.loads(json.dumps(VALID_BLUEPRINT))
                    blueprint_case(invalid, profile, case_kind)[field] = bad_value
                    with self.assertRaises(StressPreparationError) as error:
                        validate_generator_blueprint(invalid)
                    self.assertEqual(
                        error.exception.code, "stress_blueprint_invalid"
                    )
                    self.assertEqual(
                        error.exception.details["path"], expected_path
                    )

    def test_legacy_four_case_required_cases_blueprint_still_validates(self) -> None:
        """Backward compatibility: already-certified four-pair blueprints."""

        legacy_pairs = required_case_pairs(LEGACY_FOUR_CASE_BLUEPRINT)
        self.assertEqual(len(legacy_pairs), 4)
        self.assertEqual(
            set(legacy_pairs),
            set(_REQUIRED_GENERATOR_CASES) | set(_OPTIONAL_GENERATOR_CASES),
        )

        validated = validate_generator_blueprint(
            json.loads(json.dumps(LEGACY_FOUR_CASE_BLUEPRINT))
        )
        self.assertEqual(validated["schema_version"], 1)
        # The historical four-case declaration is still the canonical policy.
        self.assertEqual(
            required_case_pairs(validated), list(_REQUIRED_GENERATOR_CASES)
        )
        self.assertEqual(
            blueprint_case_pairs(validated),
            list(_OPTIONAL_GENERATOR_CASES) + list(_REQUIRED_GENERATOR_CASES),
        )
        # A legacy blueprint normalizes to exactly the current four-pair fixture.
        self.assertEqual(
            validated,
            validate_generator_blueprint(json.loads(json.dumps(VALID_BLUEPRINT))),
        )

        bogus = json.loads(json.dumps(VALID_BLUEPRINT))
        bogus["required_cases"] = [
            {"profile": "small", "case_kind": "upper_bound"},
            *bogus["required_cases"],
        ]
        with self.assertRaises(StressPreparationError) as error:
            validate_generator_blueprint(bogus)
        self.assertEqual(error.exception.details["path"], "required_cases")

    def test_blueprint_prompt_is_compact_and_keeps_fixed_cases(self) -> None:
        client = FakeClient([json.loads(json.dumps(VALID_BLUEPRINT))])
        generate_generator_blueprint(
            client,
            problem_id="P1",
            statement="statement",
            contract={**V2_CONTRACT, "profile_version": 2},
            settings=SETTINGS,
        )
        request = json.loads(client.prompts[0][3]["content"])
        # The prompt pins all four required pairs.
        self.assertEqual(
            [(item["profile"], item["case_kind"]) for item in request["fixed_cases"]],
            list(_REQUIRED_GENERATOR_CASES),
        )
        self.assertEqual(
            [
                (item["profile"], item["case_kind"])
                for item in request["optional_compatibility_cases"]
            ],
            list(_OPTIONAL_GENERATOR_CASES),
        )
        self.assertNotIn("json_schema", request)
        self.assertNotIn("template", request)
        self.assertEqual(request["case_shape"]["construction"], "<=240 chars")
        serialized = json.dumps(client.prompts[0], ensure_ascii=False)
        self.assertNotIn("generator_requirements", serialized)

    def test_small_random_existing_coverage_is_normalized_to_declared_obligations(self) -> None:
        invalid = json.loads(json.dumps(VALID_BLUEPRINT))
        invalid["cases"][1]["coverage_tags"] = ["seeded"]
        client = FakeClient([invalid])
        blueprint, usage = generate_generator_blueprint(
            client,
            problem_id="P2596",
            statement="statement",
            contract={**V2_CONTRACT, "profile_version": 2},
            settings=SETTINGS,
            generation_mode="hybrid",
            repair_limit=1,
        )
        self.assertEqual(len(client.prompts), 1)
        self.assertEqual(
            set(blueprint["cases"][1]["coverage_tags"]),
            set(blueprint["required_coverage_tags"]),
        )
        self.assertEqual(
            set(blueprint["cases"][1]["operation_families"]),
            set(blueprint["operation_families"]),
        )
        self.assertEqual(usage["blueprint_repairs_used"], 0)

    def test_invalid_blueprint_is_repaired_with_stable_context_and_aggregated_usage(self) -> None:
        invalid = json.loads(json.dumps(VALID_BLUEPRINT))
        del invalid["dimensions"]
        client = FakeClient(
            [
                Result(
                    data=invalid,
                    usage={
                        "prompt_tokens": 3,
                        "completion_tokens": 5,
                        "reasoning_content": "must not escape",
                        "completion_tokens_details": {
                            "reasoning_tokens": 2,
                            "reasoning_content": "also private",
                        },
                    },
                ),
                Result(
                    data=json.loads(json.dumps(VALID_BLUEPRINT)),
                    usage={"prompt_tokens": 7, "completion_tokens": 11},
                ),
            ]
        )
        blueprint, usage = generate_generator_blueprint(
            client,
            problem_id="P1",
            statement="statement",
            contract={**V2_CONTRACT, "profile_version": 2},
            settings=SETTINGS,
            generation_mode="hybrid",
            repair_limit=1,
        )
        self.assertEqual(blueprint["schema_version"], 1)
        self.assertEqual(len(client.prompts), 2)
        self.assertEqual(client.prompts[0], client.prompts[1][:4])
        repair = json.loads(client.prompts[1][4]["content"])
        self.assertEqual(repair["previous_raw_json"], invalid)
        self.assertEqual(repair["structured_diagnostic"]["path"], "dimensions")
        self.assertIn("dimensions", repair["structured_diagnostic"]["message"])
        self.assertEqual(
            [kwargs["thinking"] for kwargs in client.json_kwargs], [False, False]
        )
        self.assertIn("temperature", client.json_kwargs[0])
        self.assertIn("temperature", client.json_kwargs[1])
        self.assertEqual(
            [kwargs["max_tokens"] for kwargs in client.json_kwargs], [6144, 8192]
        )
        self.assertEqual(usage["prompt_tokens"], 10)
        self.assertEqual(usage["completion_tokens"], 16)
        self.assertEqual(usage["reasoning_tokens"], 2)
        self.assertEqual(usage["blueprint_repairs_used"], 1)
        self.assertNotIn("reasoning_content", usage)
        self.assertNotIn(
            "reasoning_content", usage["completion_tokens_details"]
        )

    def test_fast_blueprint_repair_stays_nonthinking_and_exhaustion_is_structured(self) -> None:
        invalid = json.loads(json.dumps(VALID_BLUEPRINT))
        invalid["cases"][1]["uses_seed"] = False
        success_client = FakeClient(
            [invalid, json.loads(json.dumps(VALID_BLUEPRINT))]
        )
        _, usage = generate_generator_blueprint(
            success_client,
            problem_id="P1",
            statement="statement",
            contract={**V2_CONTRACT, "profile_version": 2},
            settings=SETTINGS,
            generation_mode="fast",
            repair_limit=1,
        )
        self.assertEqual(usage["blueprint_repairs_used"], 1)
        self.assertTrue(
            all(not kwargs["thinking"] for kwargs in success_client.json_kwargs)
        )
        self.assertTrue(
            all(kwargs["temperature"] == 0.1 for kwargs in success_client.json_kwargs)
        )

        exhausted_client = FakeClient(
            [
                Result(data=invalid, usage={"total_tokens": 2}),
                Result(
                    data=invalid,
                    usage={"total_tokens": 3, "reasoning_content": "private"},
                ),
            ]
        )
        with self.assertRaises(StressPreparationError) as exhausted:
            generate_generator_blueprint(
                exhausted_client,
                problem_id="P1",
                statement="statement",
                contract={**V2_CONTRACT, "profile_version": 2},
                settings=SETTINGS,
                generation_mode="fast",
                repair_limit=1,
            )
        self.assertEqual(exhausted.exception.details["path"], "cases[1].uses_seed")
        self.assertEqual(exhausted.exception.details["attempts"], 2)
        self.assertEqual(exhausted.exception.details["repairs_used"], 1)
        self.assertEqual(exhausted.exception.usage["total_tokens"], 5)
        self.assertEqual(exhausted.exception.usage["blueprint_repairs_used"], 1)
        self.assertNotIn("reasoning_content", exhausted.exception.usage)

    def test_blueprint_cancel_scope_is_optional_for_legacy_clients(self) -> None:
        scope = CancelScope()
        modern = FakeClient([json.loads(json.dumps(VALID_BLUEPRINT))])
        generate_generator_blueprint(
            modern,
            problem_id="P1",
            statement="statement",
            contract={**V2_CONTRACT, "profile_version": 2},
            settings=SETTINGS,
            cancel_scope=scope,
        )
        self.assertIs(modern.json_kwargs[0]["cancel_scope"], scope)

        class LegacyClient:
            def __init__(self) -> None:
                self.calls = 0

            def chat_json(
                self,
                messages,
                *,
                model,
                thinking,
                reasoning_effort,
                max_tokens,
                request_retries,
                json_retries,
                retry_callback,
                temperature=None,
            ):
                self.calls += 1
                return Result(data=json.loads(json.dumps(VALID_BLUEPRINT)))

        legacy = LegacyClient()
        generate_generator_blueprint(
            legacy,
            problem_id="P1",
            statement="statement",
            contract={**V2_CONTRACT, "profile_version": 2},
            settings=SETTINGS,
            cancel_scope=scope,
        )
        self.assertEqual(legacy.calls, 1)

    def test_p2596_blueprint_rejects_the_two_observed_contract_failures(self) -> None:
        operations = [
            "Top", "Bottom", "Insert -1", "Insert 0", "Insert 1", "Ask", "Query"
        ]
        contract = {
            **V2_CONTRACT,
            "generator_requirements": [
                "small/random covers Top Bottom Insert -1 Insert 0 Insert 1 Ask Query",
                "large/random contains a modifying operation and a query",
            ],
        }
        blueprint = json.loads(json.dumps(VALID_BLUEPRINT))
        blueprint["operation_families"] = operations
        blueprint["required_coverage_tags"] = operations
        blueprint["large_required_coverage_tags"] = ["Top", "Ask"]
        blueprint["cases"][0]["operation_families"] = ["Query"]
        blueprint["cases"][1]["operation_families"] = operations
        blueprint["cases"][1]["coverage_tags"] = operations
        blueprint["cases"][2]["operation_families"] = ["Top", "Ask"]
        blueprint["cases"][2]["coverage_tags"] = ["Top", "Ask"]
        blueprint["cases"][3]["operation_families"] = ["Top", "Ask"]
        blueprint["cases"][3]["coverage_tags"] = ["Top", "Ask"]
        validated = validate_generator_blueprint(blueprint, contract=contract)
        self.assertEqual(len(validated["cases"]), 4)

        no_insert_zero = json.loads(json.dumps(blueprint))
        no_insert_zero["operation_families"].remove("Insert 0")
        no_insert_zero["cases"][1]["operation_families"].remove("Insert 0")
        no_insert_zero["required_coverage_tags"].remove("Insert 0")
        no_insert_zero["cases"][1]["coverage_tags"].remove("Insert 0")
        with self.assertRaises(StressPreparationError):
            validate_generator_blueprint(no_insert_zero, contract=contract)

        contradictory_random = json.loads(json.dumps(blueprint))
        contradictory_random["cases"][1]["construction"] = "random 无需随机，忽略 seed"
        with self.assertRaises(StressPreparationError):
            validate_generator_blueprint(contradictory_random, contract=contract)

        large_without_update = json.loads(json.dumps(blueprint))
        large_without_update["cases"][3]["operation_families"] = ["Ask", "Query"]
        with self.assertRaises(StressPreparationError):
            validate_generator_blueprint(large_without_update, contract=contract)

    def test_structured_blueprint_does_not_parse_insert_subject_as_family(self) -> None:
        contract = contract_v3()
        contract["validation_level"] = "structured"
        contract["small_profile"] = "Operations include Insert 3 -1 and Insert 4 1."
        contract["coverage_obligations"] = []
        contract["generator_requirements"] = [
            "Generate those valid examples without treating s as a variant."
        ]
        validated = validate_generator_blueprint(
            json.loads(json.dumps(VALID_BLUEPRINT)), contract=contract
        )
        self.assertEqual(validated["operation_families"], ["query"])

    def test_structured_blueprint_keeps_full_operations_small_and_safe_subset_large(self) -> None:
        contract = contract_v3()
        contract["validation_level"] = "structured"
        contract["small_profile"] = "n=5; fixed legal skeleton: Top 3, Ask 1"
        contract["large_profile"] = "n=10; stream Ask 1 and Query 1 operations"
        contract["coverage_obligations"] = []
        contract["syntax"]["sections"].append(
            {
                "id": "operations",
                "kind": "operation_stream",
                "count_from": "header.n",
                "fields": [],
                "variants": [
                    {"tag": tag, "fields": [], "evidence_ids": []}
                    for tag in ("Top", "Insert", "Ask", "Query")
                ],
                "evidence_ids": [],
            }
        )
        validated = validate_generator_blueprint(
            json.loads(json.dumps(VALID_BLUEPRINT)), contract=contract
        )
        by_case = {
            (item["profile"], item["case_kind"]): item
            for item in validated["cases"]
        }
        self.assertEqual(
            by_case[("small", "random")]["operation_families"],
            ["Top", "Insert", "Ask", "Query"],
        )
        self.assertEqual(
            by_case[("large", "random")]["operation_families"],
            ["Ask", "Query"],
        )
        small_construction = by_case[("small", "random")]["construction"]
        self.assertIn("Candidate strategy", small_construction)
        self.assertIn("seeded small random construction", small_construction)
        self.assertNotIn("fixed legal skeleton: Top 3, Ask 1", small_construction)
        self.assertNotIn(
            "n=5; fixed legal skeleton: Top 3, Ask 1",
            by_case[("small", "random")]["construction"],
        )
        self.assertIn("stream a seeded large random case", by_case[("large", "random")]["construction"])
        self.assertNotIn("Top", by_case[("large", "random")]["construction"])
        self.assertNotIn("Insert", by_case[("large", "random")]["construction"])
        self.assertEqual(validate_generator_blueprint(validated), validated)

    def test_contract_profile_cannot_lock_blueprint_to_illegal_stateful_skeleton(self) -> None:
        contract = contract_v3()
        contract["validation_level"] = "structured"
        contract["small_profile"] = (
            "n=5 m=8; Top 3, Ask 1, Query 1, Top 3, Insert 3 -1, Query 2, "
            "Bottom 2, Query 5"
        )
        raw = json.loads(json.dumps(VALID_BLUEPRINT))
        raw["cases"][1]["construction"] = (
            "build an operation list, replay current state, and replace any illegal move"
        )
        validated = validate_generator_blueprint(raw, contract=contract)
        small_random = next(
            case
            for case in validated["cases"]
            if case["profile"] == "small" and case["case_kind"] == "random"
        )
        self.assertIn("replace any illegal move", small_random["construction"])
        self.assertNotIn("Insert 3 -1", small_random["construction"])
        self.assertIn("structured obligations", small_random["construction"])

    def test_generation_modes_control_thinking_temperature_and_token_caps(self) -> None:
        blueprint_client = FakeClient([json.loads(json.dumps(VALID_BLUEPRINT))])
        blueprint, _ = generate_generator_blueprint(
            blueprint_client,
            problem_id="P1",
            statement="statement",
            contract={**V2_CONTRACT, "profile_version": 2},
            settings=SETTINGS,
            generation_mode="hybrid",
        )
        self.assertEqual(blueprint["schema_version"], 1)
        blueprint_kwargs = blueprint_client.json_kwargs[0]
        self.assertFalse(blueprint_kwargs["thinking"])
        self.assertEqual(blueprint_kwargs["max_tokens"], 6144)
        self.assertIn("temperature", blueprint_kwargs)

        contract_client = FakeClient([dict(V2_CONTRACT)])
        extract_contract(
            contract_client,
            problem_id="P1",
            statement="statement",
            compare="token",
            settings=SETTINGS,
            generation_mode="full_thinking",
        )
        contract_kwargs = contract_client.json_kwargs[0]
        self.assertTrue(contract_kwargs["thinking"])
        self.assertEqual(contract_kwargs["max_tokens"], 2048)
        self.assertNotIn("temperature", contract_kwargs)

        initial_client = FakeClient(
            [{"code": GENERATOR_SOURCE, "notes": "gen"}]
        )
        generate_artifact(
            initial_client,
            kind="generator",
            problem_id="P1",
            statement="statement",
            contract={**V2_CONTRACT, "profile_version": 2},
            settings=SETTINGS,
            generator_blueprint=VALID_BLUEPRINT,
            generation_mode="hybrid",
        )
        self.assertFalse(initial_client.json_kwargs[0]["thinking"])
        self.assertEqual(initial_client.json_kwargs[0]["max_tokens"], 8192)
        self.assertIn("temperature", initial_client.json_kwargs[0])

        repair_client = FakeClient(
            [{"code": GENERATOR_SOURCE, "notes": "repair"}]
        )
        generate_artifact(
            repair_client,
            kind="generator",
            problem_id="P1",
            statement="statement",
            contract={**V2_CONTRACT, "profile_version": 2},
            settings=SETTINGS,
            generator_blueprint=VALID_BLUEPRINT,
            generation_mode="hybrid",
            previous_code=GENERATOR_SOURCE,
            diagnostic="{}",
        )
        self.assertTrue(repair_client.json_kwargs[0]["thinking"])
        self.assertEqual(repair_client.json_kwargs[0]["max_tokens"], 12288)
        self.assertNotIn("temperature", repair_client.json_kwargs[0])

        full_caps = {"generator": 8192, "brute": 4096, "reference": 8192}
        for kind, cap in full_caps.items():
            client = FakeClient(
                [{"code": GENERATOR_SOURCE if kind == "generator" else "#include <iostream>\nint main(){return 0;}", "notes": kind}]
            )
            generate_artifact(
                client,
                kind=kind,
                problem_id="P1",
                statement="statement",
                contract={**V2_CONTRACT, "profile_version": 2},
                settings=SETTINGS,
                generator_blueprint=VALID_BLUEPRINT if kind == "generator" else None,
                generation_mode="full_thinking",
            )
            self.assertTrue(client.json_kwargs[0]["thinking"])
            self.assertEqual(client.json_kwargs[0]["max_tokens"], cap)
            self.assertNotIn("temperature", client.json_kwargs[0])

    def test_hybrid_low_budget_degrades_once_but_full_thinking_fails_closed(self) -> None:
        now = [0.0]
        hybrid_budget = PreparationBudget(60, clock=lambda: now[0])
        now[0] = 10.0
        hybrid_client = FakeClient([json.loads(json.dumps(VALID_BLUEPRINT))])
        generate_generator_blueprint(
            hybrid_client,
            problem_id="P1",
            statement="statement",
            contract={**V2_CONTRACT, "profile_version": 2},
            settings=SETTINGS,
            generation_mode="hybrid",
            provider_reserve_seconds=80,
            budget=hybrid_budget,
        )
        self.assertFalse(hybrid_client.json_kwargs[0]["thinking"])
        self.assertIn("temperature", hybrid_client.json_kwargs[0])
        self.assertLess(hybrid_client.json_kwargs[0]["request_timeout"], 30)
        self.assertEqual(hybrid_client.json_kwargs[0]["request_retries"], 1)
        self.assertEqual(hybrid_client.json_kwargs[0]["json_retries"], 1)

        now[0] = 0.0
        full_budget = PreparationBudget(60, clock=lambda: now[0])
        now[0] = 10.0
        full_client = FakeClient([json.loads(json.dumps(VALID_BLUEPRINT))])
        with self.assertRaises(PreparationBudgetExhausted):
            generate_generator_blueprint(
                full_client,
                problem_id="P1",
                statement="statement",
                contract={**V2_CONTRACT, "profile_version": 2},
                settings=SETTINGS,
                generation_mode="full_thinking",
                provider_reserve_seconds=80,
                budget=full_budget,
            )
        self.assertEqual(full_client.prompts, [])

    def test_generator_prompt_declares_v2_capability_and_case_protocol(self) -> None:
        client = FakeClient(
            [{"code": GENERATOR_SOURCE, "notes": "gen"}]
        )
        artifact, _ = generate_artifact(
            client,
            kind="generator",
            problem_id="P1",
            statement="statement",
            contract={**V2_CONTRACT, "profile_version": 2},
            settings=SETTINGS,
            generator_blueprint=VALID_BLUEPRINT,
        )
        self.assertEqual(artifact.kind, "generator")
        prompt = json.dumps(client.prompts, ensure_ascii=False)
        self.assertIn("--capabilities", prompt)
        self.assertIn("--manifest", prompt)
        self.assertIn("不要实现 --capabilities 或 --manifest", prompt)
        self.assertIn("可信本地 harness", prompt)
        self.assertIn("独立 validator", prompt)
        self.assertIn("seed/profile/case_kind 协议", prompt)
        self.assertIn("lower_bound", prompt)
        self.assertIn("upper_bound", prompt)
        self.assertIn("单一结构体", prompt)
        self.assertIn("并行数组", prompt)
        self.assertIn("不可变的初始输入状态", prompt)
        self.assertIn("全量重建位置数组", prompt)
        self.assertIn("摊销 O(log n)", prompt)
        self.assertIn("恒等/不移动参数", prompt)
        self.assertIn("流式输出操作", prompt)
        role_request = json.loads(client.prompts[0][3]["content"])
        self.assertEqual(role_request["generator_blueprint"]["schema_version"], 1)
        self.assertEqual(
            role_request["hard_random_seed_acceptance"]["applies_to"],
            ["small/random", "large/random"],
        )
        self.assertEqual(
            required_case_pairs(role_request["generator_blueprint"]),
            list(_REQUIRED_GENERATOR_CASES),
        )

    def test_reference_prompt_requires_general_trivial_initial_state_check(self) -> None:
        for kind in ("reference_primary", "reference_secondary"):
            with self.subTest(kind=kind):
                client = FakeClient(
                    [{"code": "#include <iostream>\nint main(){return 0;}\n", "notes": "ref"}]
                )
                generate_artifact(
                    client,
                    kind=kind,
                    problem_id="P1111",
                    statement="A single village is already connected.",
                    contract={**V2_CONTRACT, "profile_version": 2},
                    settings=SETTINGS,
                )
                role_request = json.loads(client.prompts[0][-1]["content"])
                self.assertIn("单元素", role_request["rules"])
                self.assertIn("初态满足", role_request["rules"])
                self.assertIn("零时刻或零操作答案", role_request["rules"])

        unrelated = FakeClient(
            [{"code": "#include <iostream>\nint main(){return 0;}\n", "notes": "ref"}]
        )
        generate_artifact(
            unrelated,
            kind="reference_primary",
            problem_id="P1001",
            statement="statement",
            contract={**V2_CONTRACT, "profile_version": 2},
            settings=SETTINGS,
        )
        unrelated_request = json.loads(unrelated.prompts[0][-1]["content"])
        self.assertIn("单元素", unrelated_request["rules"])
        self.assertIn("初态满足", unrelated_request["rules"])

    def test_generator_recipe_prompt_materializes_checked_in_catalog(self) -> None:
        provider_recipe = checked_in_static_recipe()
        for case in provider_recipe["cases"]:
            # Provider-owned role guesses are intentionally ignored; the exact
            # contract matcher owns serializer bindings locally.
            case["serialization"]["bindings"] = {"u": "values.value"}
        client = FakeClient([provider_recipe])
        recipe, usage = generate_generator_recipe(
            client,
            problem_id="P1001",
            statement="Input one integer n with 1 <= n <= 10.",
            contract=static_recipe_contract_v3(),
            settings=SETTINGS,
        )

        self.assertEqual(recipe["engine"], "local_templates_v1")
        self.assertTrue(
            all(
                case["serialization"]["bindings"] == {"n": "header.n"}
                for case in recipe["cases"]
            )
        )
        self.assertEqual(usage["total_tokens"], 1)
        self.assertEqual(len(client.prompts), 1)
        request = json.loads(client.prompts[0][-1]["content"])
        self.assertEqual(request["type"], "acm_stress_generator_recipe_v1")
        self.assertNotIn("case_shape", request)
        self.assertTrue(request["structure_templates"])
        self.assertFalse(
            {"rng", "transform"}.intersection(
                template["kind"] for template in request["structure_templates"]
            )
        )
        self.assertEqual(
            [item["format_id"] for item in request["serializer_candidates"]],
            ["list_n"],
        )
        for template in request["structure_templates"]:
            self.assertIsInstance(template["parameters"], dict)
            for specification in template["parameters"].values():
                self.assertIsInstance(specification, dict)

    def test_public_generator_recipe_api_rejects_missing_contract_before_provider(self) -> None:
        client = FakeClient([checked_in_static_recipe()])
        with self.assertRaises(UnsupportedRecipeError) as caught:
            generate_generator_recipe(
                client,
                problem_id="generic",
                statement="statement",
                contract=None,  # type: ignore[arg-type]
                settings=SETTINGS,
            )

        self.assertEqual(caught.exception.reason, "contract_missing")
        self.assertEqual(client.prompts, [])

    def test_matrix_and_interval_capabilities_reach_provider_and_local_composer(self) -> None:
        def shaped_recipe(template_id: str, parameters: dict[str, int], format_id: str):
            def case(profile: str, case_kind: str):
                return {
                    "profile": profile,
                    "case_kind": case_kind,
                    "families": [
                        {
                            "structure": {
                                "template_id": template_id,
                                "parameters": dict(parameters),
                            },
                            "labels": [],
                            "semantic_goals": ["seed_variation"],
                        }
                    ],
                    "selection": {
                        "policy": "balanced_round_robin_v1",
                        "seed_stride": 1 if profile == "small" else 5,
                    },
                    "serialization": {"format_id": format_id},
                    "byte_budget": {
                        "hard_max": 2 * 1024 * 1024 if profile == "small" else 32 * 1024 * 1024,
                        "buckets": (
                            [[1, 25], [26, 50], [51, 75], [76, 100], [101, 2 * 1024 * 1024]]
                            if profile == "small"
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

        scenarios = [
            (
                "matrix_n_m",
                {
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
                        ],
                    },
                    "constraints": [
                        {"kind": "range", "target": "header.n", "args": {"minimum": 1, "maximum": 4}},
                        {"kind": "range", "target": "header.m", "args": {"minimum": 1, "maximum": 5}},
                    ],
                },
                shaped_recipe(
                    "matrix.uniform",
                    {"rows_min": 1, "rows_max": 4, "cols_min": 1, "cols_max": 5, "value_min": 0, "value_max": 9},
                    "matrix_n_m",
                ),
            ),
            (
                "intervals_n",
                {
                    "syntax": {
                        "mode": "single_case",
                        "sections": [
                            {"id": "header", "kind": "scalar", "fields": [{"name": "n", "type": "int"}]},
                            {
                                "id": "segments",
                                "kind": "intervals",
                                "count_from": "header.n",
                                "fields": [{"name": "l", "type": "int"}, {"name": "r", "type": "int"}],
                            },
                        ],
                    },
                    "constraints": [
                        {"kind": "range", "target": "header.n", "args": {"minimum": 1, "maximum": 10}}
                    ],
                },
                shaped_recipe(
                    "interval.random",
                    {"n_min": 1, "n_max": 10, "lo": 0, "hi": 20},
                    "intervals_n",
                ),
            ),
        ]

        for format_id, contract, provider_recipe in scenarios:
            with self.subTest(format_id=format_id):
                client = FakeClient([provider_recipe])
                normalized, _ = generate_generator_recipe(
                    client,
                    problem_id="generic",
                    statement="statement",
                    contract=contract,
                    settings=SETTINGS,
                )
                request = json.loads(client.prompts[0][-1]["content"])
                self.assertEqual(
                    [item["format_id"] for item in request["serializer_candidates"]],
                    [format_id],
                )
                composed = compose_generator_recipe(normalized, contract=contract)
                self.assertIn("// ACM_LOCAL_RECIPE_GENERATOR_V1", composed.source)

    def test_weighted_recipe_omitted_labels_are_filled_from_contract_range(self) -> None:
        provider_recipe = json.loads(json.dumps(_p1111_recipe()))
        for case in provider_recipe["cases"]:
            for family in case["families"]:
                family["labels"] = []
        provider_recipe["cases"][0]["families"][0]["structure"] = {
            "template_id": "tree.path",
            "parameters": {"n": 1},
        }
        connected = provider_recipe["cases"][1]["families"][-1]["structure"]
        connected["parameters"] = {
            "n_min": 1,
            "n_max": 8,
            "m_min": 7,
            "m_max": 20,
        }
        components = provider_recipe["cases"][1]["families"][1]["structure"]
        components["parameters"] = {
            "n_min": 1,
            "n_max": 8,
            "m_min": 1,
            "m_max": 20,
            "component_count": 2,
        }
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
                        "id": "edges",
                        "kind": "edge_list",
                        "count_from": "header.m",
                        "fields": [
                            {"name": "u", "type": "int"},
                            {"name": "v", "type": "int"},
                            {"name": "t", "type": "int"},
                        ],
                    },
                ],
            },
            "constraints": [
                {"kind": "range", "target": "header.n", "args": {"minimum": 1, "maximum": 1000}},
                {"kind": "range", "target": "header.m", "args": {"minimum": 1, "maximum": 100000}},
                {"kind": "range", "target": "edges.t", "args": {"minimum": 1, "maximum": 100000}},
            ],
        }

        normalized, _ = generate_generator_recipe(
            FakeClient([provider_recipe]),
            problem_id="generic-weighted-graph",
            statement="Parallel roads and self-loops are harmless and allowed.",
            contract=contract,
            settings=SETTINGS,
        )

        for case in normalized["cases"]:
            for family in case["families"]:
                self.assertEqual(len(family["labels"]), 1)
                self.assertEqual(
                    family["labels"][0]["parameters"]["label_min"], 1
                )
                self.assertEqual(
                    family["labels"][0]["parameters"]["label_max"], 100000
                )
        lower_structure = normalized["cases"][0]["families"][0]["structure"]
        self.assertEqual(lower_structure["template_id"], "graph.self_loops")
        self.assertEqual(lower_structure["parameters"]["n"], 1)
        repaired = normalized["cases"][1]["families"][-1]["structure"]["parameters"]
        self.assertEqual(repaired["n_min"], 5)
        self.assertEqual(repaired["m_min"], 7)
        self.assertEqual(repaired["m_max"], 10)
        repaired_components = normalized["cases"][1]["families"][1][
            "structure"
        ]["parameters"]
        self.assertEqual(repaired_components["n_min"], 6)
        self.assertEqual(repaired_components["n_max"], 8)
        self.assertEqual(repaired_components["m_min"], 6)
        self.assertEqual(repaired_components["m_max"], 6)

    def test_static_recipe_preparation_composes_generator_without_cpp_request(self) -> None:
        client = FakeClient([checked_in_static_recipe()])
        prepared = prepare_stress(
            client,
            FakeCrawler({}),
            platform="luogu",
            problem_id="P1001",
            title="A+B Problem",
            statement="Input one integer n with 1 <= n <= 10.",
            compare="token",
            settings=SETTINGS,
            include_reference_primary=False,
            include_reference_secondary=False,
            prepared_contract=static_recipe_contract_v3(),
            require_complete_probes=False,
        )

        self.assertIsNotNone(prepared.generator)
        self.assertEqual(prepared.generator.origin, "ai_recipe_composed")
        self.assertIn("// ACM_LOCAL_RECIPE_GENERATOR_V1", prepared.generator.code)
        self.assertEqual(prepared.generation_metadata["generator_engine"], "local_templates_v1")
        self.assertEqual(len(client.prompts), 1)
        self.assertEqual(
            json.loads(client.prompts[0][-1]["content"])["type"],
            "acm_stress_generator_recipe_v1",
        )

    def test_generator_recipe_prompt_rejects_unknown_local_type(self) -> None:
        with self.assertRaises(StressPreparationError) as caught:
            _generator_recipe_prompt_content({"templates": [{"bad": object()}]})

        self.assertEqual(
            caught.exception.code, "stress_recipe_prompt_serialization_failed"
        )
        self.assertEqual(caught.exception.details["category"], "internal")
        self.assertEqual(
            caught.exception.details["substage"], "recipe_prompt_serialization"
        )
        self.assertEqual(caught.exception.details["cause_type"], "object")
        self.assertEqual(caught.exception.details["path"], "$.templates[0].bad")

    def test_recipe_prompt_serialization_keeps_internal_root_cause(self) -> None:
        serialization_error = StressPreparationError(
            "stress_recipe_prompt_serialization_failed",
            "generator recipe prompt JSON 编码失败",
            details={
                "category": "internal",
                "failure_phase": "preparation",
                "stage": "prepare_generator",
                "substage": "recipe_prompt_serialization",
                "cause_type": "TypeError",
                "path": "$.templates",
            },
        )
        with mock.patch(
            "tools.acm_agent.stress_ai._generator_recipe_prompt_content",
            side_effect=serialization_error,
        ):
            with self.assertRaises(StressPreparationError) as caught:
                prepare_stress(
                    FakeClient([]),
                    FakeCrawler({}),
                    platform="luogu",
                    problem_id="P1001",
                    title="A+B Problem",
                    statement="Input one integer n with 1 <= n <= 10.",
                    compare="token",
                    settings=SETTINGS,
                    include_reference_primary=False,
                    include_reference_secondary=False,
                    prepared_contract=static_recipe_contract_v3(),
                    require_complete_probes=False,
                )

        self.assertEqual(caught.exception.code, "stress_artifact_stage_failed")
        failure = caught.exception.details["primary_failure"]
        self.assertEqual(failure["role"], "generator")
        self.assertEqual(
            failure["code"], "stress_recipe_prompt_serialization_failed"
        )
        self.assertEqual(failure["category"], "internal")
        self.assertEqual(failure["cause_type"], "TypeError")
        self.assertEqual(failure["substage"], "recipe_prompt_serialization")
        self.assertIn("generator recipe 本地准备失败", str(caught.exception))
        self.assertNotIn("blueprint 校验失败", str(caught.exception))

    def test_invalid_static_recipe_falls_back_to_legacy_generator(self) -> None:
        invalid_recipe = json.loads(json.dumps(checked_in_static_recipe()))
        invalid_recipe["unexpected"] = True
        client = FakeClient(
            [
                invalid_recipe,
                json.loads(json.dumps(VALID_BLUEPRINT)),
                {"code": GENERATOR_SOURCE, "notes": "legacy fallback"},
            ]
        )
        prepared = prepare_stress(
            client,
            FakeCrawler({}),
            platform="luogu",
            problem_id="P1001",
            title="A+B Problem",
            statement="Input one integer n with 1 <= n <= 10.",
            compare="token",
            settings=SETTINGS,
            include_reference_primary=False,
            include_reference_secondary=False,
            prepared_contract=static_recipe_contract_v3(),
            require_complete_probes=False,
            blueprint_repair_limit=0,
        )

        self.assertEqual(prepared.generator.origin, "ai_generated")
        self.assertTrue(
            prepared.generation_metadata["generator_engine"].startswith(
                "legacy_ai_cpp:recipe_validation_failed"
            )
        )
        self.assertEqual(
            prepared.generation_metadata["recipe_fallback_reason"],
            "recipe_validation_failed",
        )

    def test_code_only_transport_returns_plain_cpp_and_keeps_role_isolated(self) -> None:
        client = CodeOnlyClient()
        artifact, usage = generate_artifact(
            client,
            kind="brute",
            problem_id="P1",
            statement="public statement only",
            contract={**V2_CONTRACT, "profile_version": 2},
            settings=SETTINGS,
            generation_mode="hybrid",
        )
        self.assertEqual(artifact.kind, "brute")
        self.assertTrue(artifact.code.startswith("#include <iostream>"))
        self.assertEqual(usage["completion_transport"], "code_only")
        self.assertEqual(client.kwargs[0]["max_tokens"], 4096)
        serialized = json.dumps(client.prompts, ensure_ascii=False)
        self.assertIn("acm_stress_brute_code_only", serialized)
        self.assertIn("不要 JSON", serialized)
        self.assertNotIn("user_source", serialized)

    def test_code_only_transport_strips_a_single_markdown_fence(self) -> None:
        client = CodeOnlyClient(
            "```cpp\n#include <iostream>\nint main(){return 0;}\n```"
        )
        artifact, _ = generate_artifact(
            client,
            kind="reference",
            problem_id="P1",
            statement="statement",
            contract={**V2_CONTRACT, "profile_version": 2},
            settings=SETTINGS,
            generation_mode="fast",
        )
        self.assertNotIn("```", artifact.code)

    def test_code_only_transport_normalizes_legacy_json_code_wrapper(self) -> None:
        source = "#include <iostream>\nint main(){return 0;}\n"
        client = CodeOnlyClient(json.dumps({"code": source, "notes": ""}))
        artifact, _ = generate_artifact(
            client,
            kind="reference",
            problem_id="P1",
            statement="statement",
            contract={**V2_CONTRACT, "profile_version": 2},
            settings=SETTINGS,
            generation_mode="fast",
        )
        self.assertEqual(artifact.code, source)

    def test_code_only_transport_recovers_wrapper_missing_only_object_brace(self) -> None:
        source = "#include <iostream>\nint main(){return 0;}\n"
        truncated_wrapper = json.dumps({"code": source}, separators=(",", ":"))[:-1]
        client = CodeOnlyClient(truncated_wrapper)

        artifact, usage = generate_artifact(
            client,
            kind="reference",
            problem_id="P1",
            statement="statement",
            contract={**V2_CONTRACT, "profile_version": 2},
            settings=SETTINGS,
            generation_mode="hybrid",
        )

        self.assertEqual(artifact.code, source)
        self.assertNotIn("reference_transport_repairs_used", usage)
        self.assertIn("不得把纯源码再次包装进 JSON", client.prompts[0][0]["content"])

    def test_code_only_repair_applies_exact_patch_and_preserves_source(self) -> None:
        previous = "#include <iostream>\nint main(){return 1;}\n// keep\n"
        client = CodeOnlyClient(
            json.dumps(
                {
                    "patches": [
                        {
                            "search": "int main(){return 1;}",
                            "replace": "int main(){return 0;}",
                        }
                    ]
                }
            )
        )
        artifact, usage = generate_artifact(
            client,
            kind="brute",
            problem_id="P1",
            statement="statement",
            contract={**V2_CONTRACT, "profile_version": 2},
            settings=SETTINGS,
            generation_mode="hybrid",
            diagnostic='{"code":"compile_failed"}',
            previous_code=previous,
        )
        self.assertEqual(
            artifact.code,
            "#include <iostream>\nint main(){return 0;}\n// keep\n",
        )
        self.assertEqual(usage["completion_transport"], "code_patch")
        self.assertEqual(client.kwargs[0]["max_tokens"], 6144)
        request = json.loads(client.prompts[0][-1]["content"])
        self.assertEqual(request["transport"], "exact_search_replace_json")
        self.assertIn("search 必须逐字复制", request["instructions"])

    def test_invalid_exact_patch_gets_one_full_source_transport_fallback(self) -> None:
        previous = "#include <iostream>\nint main(){return 1;}\n"
        replacement = "#include <iostream>\nint main(){return 0;}\n"
        client = SequencedCodeOnlyClient(
            [
                json.dumps(
                    {
                        "patches": [
                            {
                                "search": "int main() { return 1; }",
                                "replace": "int main(){return 0;}",
                            }
                        ]
                    }
                ),
                replacement,
            ]
        )
        artifact, usage = generate_artifact(
            client,
            kind="brute",
            problem_id="P1",
            statement="statement",
            contract={**V2_CONTRACT, "profile_version": 2},
            settings=SETTINGS,
            generation_mode="hybrid",
            diagnostic='{"code":"compile_failed"}',
            previous_code=previous,
        )
        self.assertEqual(artifact.code, replacement)
        self.assertEqual(usage["completion_transport"], "code_only_patch_fallback")
        self.assertEqual([item["thinking"] for item in client.kwargs], [True, False])
        self.assertEqual(usage["total_tokens"], 6)

    def test_machine_gate_reference_repair_uses_full_source_replacement(self) -> None:
        previous = "#include <iostream>\nint main(){for(;;){}}\n"
        replacement = "#include <iostream>\nint main(){return 0;}\n"
        client = CodeOnlyClient(replacement)
        artifact, usage = generate_artifact(
            client,
            kind="reference",
            problem_id="P1",
            statement="statement",
            contract={**V2_CONTRACT, "profile_version": 2},
            settings=SETTINGS,
            generation_mode="hybrid",
            diagnostic=json.dumps(
                {
                    "stage": "pre_audit_machine_gate",
                    "repair_attempt": 1,
                    "code": "stress_helper_preflight_failed",
                    "message": "reference_timeout",
                }
            ),
            previous_code=previous,
        )
        self.assertEqual(artifact.code.strip(), replacement.strip())
        self.assertEqual(usage["completion_transport"], "code_only")
        self.assertTrue(client.kwargs[0]["thinking"])
        self.assertEqual(client.kwargs[0]["max_tokens"], 12288)
        request = json.loads(client.prompts[0][-1]["content"])
        self.assertEqual(request["transport"], "code_only")

    def test_code_only_generator_repair_uses_full_replacement_and_role_cap(self) -> None:
        previous = GENERATOR_SOURCE + "\n// keep\n"
        replacement = GENERATOR_SOURCE + "\n// repaired\n"
        client = CodeOnlyClient(replacement)
        artifact, _ = generate_artifact(
            client,
            kind="generator",
            problem_id="P1",
            statement="statement",
            contract={**V2_CONTRACT, "profile_version": 2},
            settings=SETTINGS,
            generator_blueprint=VALID_BLUEPRINT,
            generation_mode="hybrid",
            diagnostic='{"code":"runtime_validation_failed"}',
            previous_code=previous,
        )
        self.assertEqual(artifact.code.strip(), replacement.strip())
        self.assertEqual(client.kwargs[0]["max_tokens"], 12288)
        self.assertTrue(client.kwargs[0]["thinking"])
        request = json.loads(client.prompts[0][-1]["content"])
        self.assertEqual(request["transport"], "code_only")

    def test_first_generator_machine_repair_is_fast_before_thinking_fallback(self) -> None:
        client = CodeOnlyClient(GENERATOR_SOURCE)
        generate_artifact(
            client,
            kind="generator",
            problem_id="P1",
            statement="statement",
            contract={**V2_CONTRACT, "profile_version": 2},
            settings=SETTINGS,
            generator_blueprint=VALID_BLUEPRINT,
            generation_mode="hybrid",
            diagnostic=json.dumps(
                {
                    "stage": "pre_audit_machine_gate",
                    "repair_attempt": 1,
                    "code": "stress_generated_input_invalid",
                }
            ),
            previous_code=GENERATOR_SOURCE,
        )
        self.assertFalse(client.kwargs[0]["thinking"])
        self.assertEqual(client.kwargs[0]["max_tokens"], 8192)

    def test_first_generator_machine_repair_prefers_exact_patch_transport(self) -> None:
        source = GENERATOR_SOURCE + "// old\n"
        client = CodeOnlyClient(
            json.dumps(
                {
                    "patches": [
                        {"search": "// old", "replace": "// fixed"}
                    ]
                }
            )
        )
        artifact, usage = generate_artifact(
            client,
            kind="generator",
            problem_id="P1",
            statement="statement",
            contract={**V2_CONTRACT, "profile_version": 2},
            settings=SETTINGS,
            generator_blueprint=VALID_BLUEPRINT,
            generation_mode="hybrid",
            diagnostic=json.dumps(
                {
                    "stage": "pre_audit_machine_gate",
                    "repair_attempt": 1,
                    "code": "stress_generated_input_invalid",
                }
            ),
            previous_code=source,
        )
        self.assertIn("// fixed", artifact.code)
        self.assertEqual(usage["completion_transport"], "code_patch")

    def test_first_generator_local_preflight_repair_is_also_fast(self) -> None:
        client = CodeOnlyClient(GENERATOR_SOURCE)
        generate_artifact(
            client,
            kind="generator",
            problem_id="P1",
            statement="statement",
            contract={**V2_CONTRACT, "profile_version": 2},
            settings=SETTINGS,
            generator_blueprint=VALID_BLUEPRINT,
            generation_mode="hybrid",
            diagnostic=json.dumps(
                {
                    "stage": "local_preflight",
                    "repair_attempt": 1,
                    "code": "generator_timeout",
                    "profile": "large",
                    "case_kind": "random",
                }
            ),
            previous_code=GENERATOR_SOURCE,
        )
        self.assertFalse(client.kwargs[0]["thinking"])
        self.assertEqual(client.kwargs[0]["max_tokens"], 8192)

    def test_generator_seed_variation_repair_uses_fast_exact_patch(self) -> None:
        client = CodeOnlyClient(
            json.dumps(
                {
                    "patches": [
                        {
                            "search": "out << 1;",
                            "replace": "out << (rng() % 2 + 1);",
                        }
                    ]
                }
            )
        )
        source = (
            "#include <iostream>\n#include <random>\n#include <string>\n"
            "void acm_generate_case(unsigned long long seed,const std::string&,"
            "const std::string&,std::ostream& out){std::mt19937_64 rng(seed);out << 1;}\n"
        )
        artifact, usage = generate_artifact(
            client,
            kind="generator",
            problem_id="P1",
            statement="statement",
            contract={**V2_CONTRACT, "profile_version": 2},
            settings=SETTINGS,
            generator_blueprint=VALID_BLUEPRINT,
            generation_mode="hybrid",
            diagnostic=json.dumps(
                {
                    "stage": "pre_audit_machine_gate",
                    "repair_attempt": 1,
                    "code": "stress_generator_seed_variation_failed",
                    "seed": 9,
                }
            ),
            previous_code=source,
        )
        self.assertIn("rng() % 2 + 1", artifact.code)
        self.assertEqual(usage["completion_transport"], "code_patch")
        self.assertFalse(client.kwargs[0]["thinking"])
        self.assertEqual(client.kwargs[0]["max_tokens"], 8192)

    def test_reproducible_validator_audit_repair_is_fast_exact_patch(self) -> None:
        previous = "#include <iostream>\nint main(){return 1;}\n"
        client = CodeOnlyClient(
            json.dumps(
                {
                    "patches": [
                        {
                            "search": "return 1;",
                            "replace": "return 0;",
                        }
                    ]
                }
            )
        )
        artifact, _ = generate_artifact(
            client,
            kind="validator",
            problem_id="P1",
            statement="statement",
            contract={**V2_CONTRACT, "profile_version": 2},
            settings=SETTINGS,
            generation_mode="hybrid",
            diagnostic=json.dumps(
                {
                    "stage": "static_audit",
                    "issues": [{"category": "state", "severity": "critical"}],
                    "witness": {
                        "code_expression": "return 1;",
                        "input_excerpt": "1",
                        "trace": "reproducible validator state trace",
                    },
                }
            ),
            previous_code=previous,
        )
        self.assertIn("return 0;", artifact.code)
        self.assertFalse(client.kwargs[0]["thinking"])
        self.assertEqual(client.kwargs[0]["max_tokens"], 8192)

    def test_validator_machine_gate_repair_is_fast_exact_patch(self) -> None:
        previous = "#include <iostream>\nint main(){return 1;}\n"
        client = CodeOnlyClient(
            json.dumps(
                {
                    "patches": [
                        {"search": "return 1;", "replace": "return 0;"}
                    ]
                }
            )
        )
        artifact, _ = generate_artifact(
            client,
            kind="validator",
            problem_id="P1",
            statement="statement",
            contract={**V2_CONTRACT, "profile_version": 2},
            settings=SETTINGS,
            generation_mode="hybrid",
            diagnostic=json.dumps(
                {
                    "stage": "pre_audit_machine_gate",
                    "repair_attempt": 1,
                    "code": "stress_generated_input_invalid",
                    "seed": 7,
                    "stderr": "ERR_STATE 3",
                }
            ),
            previous_code=previous,
        )
        self.assertIn("return 0;", artifact.code)
        self.assertFalse(client.kwargs[0]["thinking"])
        self.assertEqual(client.kwargs[0]["max_tokens"], 8192)

    def test_empty_thinking_patch_uses_one_nonthinking_transport_finalizer(self) -> None:
        previous = GENERATOR_SOURCE + "\n// keep\n"
        replacement = GENERATOR_SOURCE + "\n// finalized\n"
        client = EmptyThinkingThenPatchClient(replacement)
        artifact, usage = generate_artifact(
            client,
            kind="generator",
            problem_id="P1",
            statement="statement",
            contract={**V2_CONTRACT, "profile_version": 2},
            settings=SETTINGS,
            generator_blueprint=VALID_BLUEPRINT,
            generation_mode="hybrid",
            diagnostic='{"code":"runtime_validation_failed"}',
            previous_code=previous,
        )
        self.assertIn("// finalized", artifact.code)
        self.assertEqual([item["thinking"] for item in client.kwargs], [True, False])
        self.assertEqual(client.kwargs[1]["max_tokens"], 8192)
        self.assertEqual(usage["total_tokens"], 12488)

    def test_second_generator_rewrite_bounds_reasoning_then_finalizes_code(self) -> None:
        replacement = GENERATOR_SOURCE + "// rebuilt\n"
        client = EmptyBoundedThinkingThenCodeClient(replacement)
        artifact, usage = generate_artifact(
            client,
            kind="generator",
            problem_id="P1",
            statement="statement",
            contract={**V2_CONTRACT, "profile_version": 2},
            settings=SETTINGS,
            generator_blueprint=VALID_BLUEPRINT,
            generation_mode="hybrid",
            diagnostic=json.dumps(
                {
                    "stage": "pre_audit_machine_gate",
                    "repair_attempt": 2,
                    "code": "stress_generated_input_invalid",
                }
            ),
            repair_from_scratch=True,
        )
        self.assertIn("// rebuilt", artifact.code)
        self.assertEqual(
            [item["thinking"] for item in client.kwargs], [True, False]
        )
        self.assertEqual(client.kwargs[0]["max_tokens"], 4096)
        self.assertEqual(client.kwargs[1]["max_tokens"], 8192)
        self.assertEqual(usage["total_tokens"], 4396)

    def test_invalid_optional_custom_coverage_is_ignored_and_local_coverage_remains(self) -> None:
        contract = contract_v3()
        contract["coverage_obligations"] = [
            {
                "id": "optional_bad_scope",
                "scope": "occasionally",
                "predicate": {"kind": "unknown", "target": "x", "args": {}},
                "minimum_witnesses": 1,
                "evidence_ids": ["e1"],
            }
        ]
        normalized = normalize_stress_contract(
            contract, statement="Input one integer n with 1 <= n <= 10."
        )
        ids = {item["id"] for item in normalized["coverage_obligations"]}
        self.assertNotIn("optional_bad_scope", ids)
        self.assertTrue(any(item.startswith("auto_") for item in ids))

    def test_text_audit_parser_accepts_one_outer_object_with_nested_witness(self) -> None:
        client = CodeOnlyClient(
            "Result follows:\n"
            + json.dumps(
                {
                    "verdict": "accept",
                    "confidence": 0.95,
                    "issues": [],
                    "fault_origin": "implementation",
                    "summary": "ok",
                    "witness": {
                        "code_expression": "",
                        "input_excerpt": "",
                        "trace": "",
                        "seed": None,
                    },
                }
            )
        )
        audit, _ = audit_generated_artifact(
            client,
            GeneratedArtifact(
                kind="validator",
                code="#include <iostream>\nint main(){return 0;}",
                notes="",
                origin="ai_generated",
            ),
            problem_id="P1",
            statement="statement",
            contract={**V2_CONTRACT, "profile_version": 2},
            settings=SETTINGS,
            deadline=time.monotonic() + 5,
        )
        self.assertTrue(audit.accepted)

    def test_structured_blueprint_binds_boundary_and_small_dimensions_locally(self) -> None:
        statement = "Input one integer n with 1 <= n <= 10."
        contract = normalize_stress_contract(
            contract_v3(statement), compare="token", statement=statement
        )
        raw = json.loads(json.dumps(VALID_BLUEPRINT))
        for case in raw["cases"]:
            case["dimensions"] = {"n": 10}
        validated = validate_generator_blueprint(raw, contract=contract)
        dimensions = {
            (case["profile"], case["case_kind"]): case["dimensions"]["n"]
            for case in validated["cases"]
        }
        self.assertEqual(dimensions[("small", "lower_bound")], 1)
        self.assertEqual(dimensions[("small", "random")], 8)
        self.assertEqual(dimensions[("large", "upper_bound")], 10)
        self.assertEqual(dimensions[("large", "random")], 10)
        self.assertTrue(
            all("Authoritative dimensions" in case["construction"] for case in validated["cases"])
        )

    def test_structured_blueprint_binds_complexity_policy_locally(self) -> None:
        statement = "Input one integer n with 1 <= n <= 10."
        contract = normalize_stress_contract(
            contract_v3(statement), compare="token", statement=statement
        )
        raw = json.loads(json.dumps(VALID_BLUEPRINT))
        for case in raw["cases"]:
            case["total_complexity"] = "linear-ish model prose"
        validated = validate_generator_blueprint(raw, contract=contract)
        complexity = {
            (case["profile"], case["case_kind"]): case["total_complexity"]
            for case in validated["cases"]
        }
        self.assertEqual(complexity[("small", "lower_bound")], "O(output_size)")
        self.assertEqual(complexity[("small", "random")], "O(output_size)")
        self.assertEqual(complexity[("large", "upper_bound")], "O(output_size)")
        self.assertEqual(
            complexity[("large", "random")], "O(output_size log n)"
        )

    def test_text_audit_protocol_error_retries_without_source_repair(self) -> None:
        class StructuredRetryAuditClient(SequencedCodeOnlyClient):
            def chat_json(self, messages, **kwargs):
                self.prompts.append(messages)
                self.kwargs.append(dict(kwargs))
                return Result(
                    data=json.loads(self.contents.pop(0)),
                    usage={"total_tokens": 3},
                )

        client = StructuredRetryAuditClient(
            [
                '{"verdict":"accept"}\n{"verdict":"accept"}',
                json.dumps(AUDIT_ACCEPT),
            ]
        )
        audit, usage = audit_generated_artifact(
            client,
            GeneratedArtifact(
                kind="reference",
                code="#include <iostream>\nint main(){return 0;}",
                notes="",
                origin="ai_generated",
            ),
            problem_id="P1",
            statement="statement",
            contract={**V2_CONTRACT, "profile_version": 2},
            settings=SETTINGS,
            deadline=time.monotonic() + 20,
        )
        self.assertTrue(audit.accepted)
        self.assertEqual(len(client.prompts), 2)
        self.assertEqual(usage["total_tokens"], 6)
        self.assertIn("上一次响应不是唯一完整 JSON 对象", client.prompts[1][0]["content"])

    def test_code_only_repair_rejects_ambiguous_search_patch(self) -> None:
        previous = "#include <iostream>\nint x;\nint x;\nint main(){return 0;}\n"
        client = CodeOnlyClient(
            json.dumps(
                {"patches": [{"search": "int x;", "replace": "int y;"}]}
            )
        )
        with self.assertRaises(StressPreparationError) as caught:
            generate_artifact(
                client,
                kind="brute",
                problem_id="P1",
                statement="statement",
                contract={**V2_CONTRACT, "profile_version": 2},
                settings=SETTINGS,
                generation_mode="hybrid",
                diagnostic='{"code":"compile_failed"}',
                previous_code=previous,
            )
        self.assertEqual(caught.exception.code, "invalid_generated_patch")

    def test_incomplete_initial_code_uses_one_fast_role_repair(self) -> None:
        client = SequencedCodeOnlyClient(
            [
                "I will describe the validator instead.",
                "#include <iostream>\nint main(){return 0;}\n",
            ]
        )
        artifact, usage = generate_artifact(
            client,
            kind="validator",
            problem_id="P1",
            statement="statement",
            contract={**V2_CONTRACT, "profile_version": 2},
            settings=SETTINGS,
            generation_mode="hybrid",
        )
        self.assertIn("int main", artifact.code)
        self.assertEqual(usage["validator_transport_repairs_used"], 1)
        self.assertNotIn("validator_repairs_used", usage)
        self.assertEqual([item["max_tokens"] for item in client.kwargs], [6144, 6144])
        self.assertEqual([item["thinking"] for item in client.kwargs], [False, False])
        repair = json.loads(client.prompts[1][-1]["content"])
        self.assertEqual(repair["type"], "acm_stress_artifact_repair")
        self.assertEqual(repair["structured_diagnostic"]["path"], "$output")

    def test_repair_appends_structured_diagnostic_after_stable_role_prefix(self) -> None:
        client = FakeClient(
            [
                {"code": GENERATOR_SOURCE, "notes": "initial"},
                {"code": GENERATOR_SOURCE, "notes": "repair"},
            ]
        )
        common = {
            "client": client,
            "kind": "generator",
            "problem_id": "P2596",
            "statement": "statement",
            "contract": {**V2_CONTRACT, "profile_version": 2},
            "settings": SETTINGS,
        }
        generate_artifact(**common)
        diagnostic = {
            "stage": "static_audit",
            "issues": [
                {
                    "category": "complexity",
                    "severity": "critical",
                    "evidence": "large uses deque erase(begin)",
                }
            ],
        }
        generate_artifact(
            **common,
            previous_code=GENERATOR_SOURCE + "// OLD_GENERATOR\n",
            diagnostic=json.dumps(diagnostic),
        )
        initial_messages, repair_messages = client.prompts
        self.assertNotIn("statement", json.dumps(repair_messages, ensure_ascii=False))
        repair_context = json.loads(repair_messages[1]["content"])
        self.assertEqual(
            repair_context["type"], "acm_stress_artifact_repair_context_v1"
        )
        self.assertIn("contract_summary", repair_context)
        base_request = json.loads(repair_messages[2]["content"])
        self.assertNotIn("previous_code", base_request)
        repair_request = json.loads(repair_messages[3]["content"])
        self.assertEqual(repair_request["type"], "acm_stress_artifact_repair")
        self.assertEqual(repair_request["structured_diagnostic"], diagnostic)
        self.assertIn("OLD_GENERATOR", repair_request["previous_code"])
        checklist = "\n".join(repair_request["acceptance_checklist"])
        self.assertIn("small 与 large 使用显式分离", checklist)
        self.assertIn("vector/deque 中部 erase/insert", checklist)
        self.assertIn("定点修复机会", repair_messages[3]["content"])

    def test_hybrid_role_repairs_enable_thinking_with_role_specific_caps(self) -> None:
        expected_caps = {
            "brute": 6144,
            "validator": 8192,
            "reference": 12288,
        }
        for kind, expected_cap in expected_caps.items():
            with self.subTest(kind=kind):
                client = FakeClient(
                    [
                        {
                            "code": "#include <iostream>\nint main(){return 0;}",
                            "notes": "repaired",
                        }
                    ]
                )
                generate_artifact(
                    client,
                    kind=kind,
                    problem_id="P1",
                    statement="statement",
                    contract={**V2_CONTRACT, "profile_version": 2},
                    settings=SETTINGS,
                    generation_mode="hybrid",
                    diagnostic='{"code":"compile_failed"}',
                    previous_code="#include <iostream>\nint main(){}",
                )
                self.assertTrue(client.json_kwargs[0]["thinking"])
                self.assertEqual(client.json_kwargs[0]["max_tokens"], expected_cap)

    def test_validator_generation_is_fast_first_isolated_and_strict_json(self) -> None:
        client = FakeClient(
            [
                {
                    "code": "#include <iostream>\nint main(){return 0;}",
                    "notes": "validator",
                }
            ]
        )
        artifact, usage = generate_artifact(
            client,
            kind="validator",
            problem_id="P1",
            statement="Input one integer n.",
            contract={**V2_CONTRACT, "profile_version": 2},
            settings=SETTINGS,
            generation_mode="hybrid",
        )
        self.assertEqual(artifact.kind, "validator")
        self.assertEqual(usage["completion_transport"], "json_compat")
        self.assertFalse(client.json_kwargs[0]["thinking"])
        self.assertEqual(client.json_kwargs[0]["max_tokens"], 6144)
        payload = json.loads(client.prompts[0][-1]["content"])
        self.assertEqual(payload["artifact_kind"], "validator")
        self.assertIn('"coverage_tags":[]', payload["rules"])
        self.assertIn('"records":0', payload["rules"])
        self.assertIn("禁止增加 schema_version", payload["rules"])
        serialized = json.dumps(client.prompts[0], ensure_ascii=False)
        self.assertNotIn("USER_SOLUTION_SENTINEL", serialized)
        self.assertNotIn("previous_code", serialized)

    def test_prepare_stress_can_generate_validator_without_other_helpers(self) -> None:
        client = FakeClient(
            [
                {
                    "code": "#include <iostream>\nint main(){return 0;}",
                    "notes": "validator",
                }
            ]
        )
        prepared = prepare_stress(
            client,
            FakeCrawler({}),
            platform="luogu",
            problem_id="P1",
            title="test",
            statement="statement",
            compare="token",
            settings=SETTINGS,
            generation_mode="hybrid",
            include_generator=False,
            include_reference_primary=False,
            include_reference_secondary=False,
            include_validator=True,
            prepared_contract={**V2_CONTRACT, "profile_version": 2},
        )
        self.assertIsNotNone(prepared.validator)
        self.assertEqual(prepared.validator.kind, "validator")
        self.assertIsNone(prepared.generator)
        self.assertIsNone(prepared.reference_primary)
        self.assertIsNone(prepared.reference_secondary)
        self.assertEqual(
            prepared.generation_metadata["requests"]["validator"],
            {"thinking": False, "max_tokens": 6144},
        )
        self.assertEqual(
            prepared.generation_metadata["requests"]["validator_repair"],
            {"thinking": True, "max_tokens": 8192},
        )

    def test_stress_preparation_old_positional_shape_defaults_validator(self) -> None:
        prepared = StressPreparation({}, None, None, None, {}, None, {})
        self.assertIsNone(prepared.validator)

    def test_generated_artifact_audit_is_independent_and_deadline_bounded(self) -> None:
        client = FakeClient([AUDIT_ACCEPT])
        artifact = GeneratedArtifact(
            kind="generator",
            code="#include <iostream>\nint main(){return 0;}\n",
            origin="ai_generated",
            notes="must not be sent",
        )
        audit, usage = audit_generated_artifact(
            client,
            artifact,
            problem_id="P2596",
            statement="public statement",
            contract=V2_CONTRACT,
            settings=SETTINGS,
            deadline=130.0,
            clock=lambda: 100.0,
        )
        self.assertTrue(audit.accepted)
        self.assertEqual(audit.kind, "generator")
        self.assertEqual(audit.fault_origin, "implementation")
        self.assertEqual(usage["total_tokens"], 1)
        kwargs = client.json_kwargs[0]
        self.assertFalse(kwargs["thinking"])
        self.assertEqual(kwargs["max_tokens"], 1024)
        self.assertEqual(kwargs["request_timeout"], 30.0)
        self.assertEqual(kwargs["request_retries"], 1)
        self.assertEqual(kwargs["json_retries"], 1)
        payload = json.loads(client.prompts[0][1]["content"])
        self.assertEqual(
            set(payload),
            {
                "type",
                "artifact_kind",
                "problem_id",
                "statement_excerpt",
                "stress_contract",
                "artifact_code",
                "machine_gate",
            },
        )
        serialized = json.dumps(client.prompts, ensure_ascii=False)
        self.assertNotIn("must not be sent", serialized)
        self.assertIn("并行数组", serialized)
        self.assertIn("初始输入状态", serialized)
        self.assertIn("全量重建", serialized)
        self.assertIn("只给最重要的 1 项 issue", serialized)
        self.assertIn("少于 320 tokens 并闭合", serialized)
        self.assertFalse(payload["machine_gate"]["completed_before_this_audit"])
        self.assertEqual(payload["machine_gate"]["checks"], [])

    def test_contradictory_audit_retries_without_repairing_source(self) -> None:
        source = "int marker = 0;\nint main(){return marker;}\n"
        contradictory = {
            "verdict": "reject",
            "confidence": 0.95,
            "issues": [
                {
                    "category": "logic",
                    "severity": "critical",
                    "evidence": "该路径实际不会造成错误",
                }
            ],
            "fault_origin": "implementation",
            "summary": "无具体风险，接受",
            "witness": {
                "code_expression": "int marker = 0;",
                "input_excerpt": "1",
                "trace": "逐步检查后确认该表达式不会导致任何功能错误。",
                "seed": None,
                "failure_confirmed": False,
            },
        }
        client = FakeClient([contradictory, AUDIT_ACCEPT])
        audit, usage = audit_generated_artifact(
            client,
            GeneratedArtifact("reference", source, "ai_generated", ""),
            problem_id="P1",
            statement="statement",
            contract=V2_CONTRACT,
            settings=SETTINGS,
            deadline=130.0,
            clock=lambda: 100.0,
        )
        self.assertTrue(audit.accepted)
        self.assertEqual(len(client.prompts), 2)
        self.assertEqual(usage["total_tokens"], 2)

    def test_repeated_contradictory_audit_fails_closed(self) -> None:
        source = "int marker = 0;\nint main(){return marker;}\n"
        contradictory = {
            "verdict": "reject",
            "confidence": 0.95,
            "issues": [
                {"category": "logic", "severity": "critical", "evidence": "无错误"}
            ],
            "summary": "接受",
            "witness": {
                "code_expression": "int marker = 0;",
                "input_excerpt": "1",
                "trace": "逐步检查后确认该表达式不会导致任何功能错误。",
                "seed": None,
                "failure_confirmed": False,
            },
        }
        with self.assertRaises(StressPreparationError) as caught:
            audit_generated_artifact(
                FakeClient([contradictory, contradictory]),
                GeneratedArtifact("reference", source, "ai_generated", ""),
                problem_id="P1",
                statement="statement",
                contract=V2_CONTRACT,
                settings=SETTINGS,
                deadline=130.0,
                clock=lambda: 100.0,
            )
        self.assertEqual(caught.exception.code, "stress_artifact_audit_invalid")
        self.assertEqual(caught.exception.usage["total_tokens"], 2)

    def test_audit_contract_projection_keeps_role_semantics_without_provenance(self) -> None:
        contract = contract_v3()
        contract["validation_level"] = "structured"
        contract["generator_requirements"] = ["PROVEN_GENERATOR_PROSE"]
        contract["evidence"][0]["quote"] = "PROVEN_EVIDENCE_QUOTE"
        section = contract["syntax"]["sections"][0]
        section["description"] = "KEEP_SECTION_SEMANTICS"
        section["fields"][0]["description"] = "KEEP_FIELD_SEMANTICS"

        generator = _compact_audit_contract(contract, kind="generator")
        validator = _compact_audit_contract(contract, kind="validator")
        brute = _compact_audit_contract(contract, kind="brute")
        reference = _compact_audit_contract(contract, kind="reference")

        for projected in (generator, validator, brute, reference):
            serialized = json.dumps(projected, ensure_ascii=False)
            self.assertNotIn("generator_requirements", projected)
            self.assertNotIn("evidence", projected)
            self.assertNotIn("evidence_ids", serialized)
            self.assertNotIn("PROVEN_GENERATOR_PROSE", serialized)
            self.assertNotIn("PROVEN_EVIDENCE_QUOTE", serialized)
            self.assertIn("KEEP_SECTION_SEMANTICS", serialized)
            self.assertIn("KEEP_FIELD_SEMANTICS", serialized)
            self.assertEqual(projected["constraints"][0]["args"]["minimum"], 1)

        for projected in (generator, validator):
            self.assertEqual(
                projected["coverage_obligations"][0]["predicate"]["kind"],
                "constraint_boundary",
            )
            self.assertIn("large_profile", projected)
            self.assertNotIn("output_compare", projected)

        self.assertNotIn("coverage_obligations", brute)
        self.assertNotIn("large_profile", brute)
        self.assertEqual(brute["output_compare"], "token")
        self.assertNotIn("coverage_obligations", reference)
        self.assertEqual(reference["large_upper_boundary"], "n = 10")
        self.assertEqual(reference["output_compare"], "token")

    def test_brute_audit_ignores_only_small_scope_complexity_rejection(self) -> None:
        client = FakeClient(
            [
                {
                    "verdict": "reject",
                    "confidence": 0.99,
                    "issues": [
                        {
                            "category": "complexity",
                            "severity": "critical",
                            "evidence": "vector 实现对原题 8e4 规模为 O(nm)",
                        }
                    ],
                    "summary": "无法通过完整约束",
                }
            ]
        )
        artifact = GeneratedArtifact(
            kind="brute",
            code="#include <vector>\nint main(){return 0;}\n",
            origin="ai_generated",
            notes="",
        )
        audit, _usage = audit_generated_artifact(
            client,
            artifact,
            problem_id="P2596",
            statement="public statement",
            contract=V2_CONTRACT,
            settings=SETTINGS,
            deadline=130.0,
            clock=lambda: 100.0,
        )
        self.assertTrue(audit.accepted)
        self.assertEqual(audit.verdict, "accept")
        self.assertEqual(audit.issues[0]["severity"], "warning")
        payload = json.loads(client.prompts[0][1]["content"])
        self.assertEqual(payload["execution_scope"]["profiles"], ["small"])

    def test_brute_audit_keeps_small_scope_logic_rejection(self) -> None:
        client = FakeClient(
            [
                {
                    "verdict": "reject",
                    "confidence": 0.99,
                    "issues": [
                        {
                            "category": "logic",
                            "severity": "critical",
                            "evidence": "Insert 0 分支遗漏",
                        }
                    ],
                    "summary": "分支错误",
                    "witness": {
                        "code_expression": "int broken = 0;",
                        "input_excerpt": "1 1\n1\nInsert 1 0\n",
                        "trace": "该分支对这条合法输入不执行题目要求的状态转换。",
                        "seed": None,
                        "failure_confirmed": True,
                    },
                }
            ]
        )
        artifact = GeneratedArtifact(
            "brute", "int broken = 0;\nint main(){}", "ai_generated", ""
        )
        audit, _usage = audit_generated_artifact(
            client,
            artifact,
            problem_id="P2596",
            statement="public statement",
            contract=V2_CONTRACT,
            settings=SETTINGS,
            deadline=130.0,
            clock=lambda: 100.0,
        )
        self.assertFalse(audit.accepted)
        self.assertEqual(audit.issues[0]["severity"], "critical")

    def test_audit_long_presentation_text_does_not_hide_reproducible_reject(self) -> None:
        expression = "int broken = 0;"
        client = FakeClient(
            [
                {
                    "verdict": "reject",
                    "confidence": 0.99,
                    "issues": [
                        {
                            "category": "logic",
                            "severity": "critical",
                            "evidence": "E" * 200,
                        }
                    ],
                    "summary": "S" * 100,
                    "witness": {
                        "code_expression": expression,
                        "input_excerpt": "1 1\n1\nQuery 1\n",
                        "trace": "The exact branch returns the wrong deterministic result.",
                        "seed": None,
                        "failure_confirmed": True,
                    },
                }
            ]
        )
        audit, _usage = audit_generated_artifact(
            client,
            GeneratedArtifact(
                "reference", expression + "\nint main(){}", "ai_generated", ""
            ),
            problem_id="P1",
            statement="statement",
            contract=V2_CONTRACT,
            settings=SETTINGS,
            deadline=130.0,
            clock=lambda: 100.0,
        )
        self.assertFalse(audit.accepted)
        self.assertEqual(len(client.prompts), 1)

    def test_audit_normalizes_unambiguous_accept_verdict_alias(self) -> None:
        decision = dict(AUDIT_ACCEPT)
        decision["verdict"] = "passed"
        client = FakeClient([decision])
        audit, _usage = audit_generated_artifact(
            client,
            GeneratedArtifact(
                "reference", "int main(){}", "ai_generated", ""
            ),
            problem_id="P1",
            statement="statement",
            contract=V2_CONTRACT,
            settings=SETTINGS,
            deadline=130.0,
            clock=lambda: 100.0,
        )
        self.assertTrue(audit.accepted)
        self.assertEqual(audit.verdict, "accept")
        self.assertEqual(len(client.prompts), 1)

    def test_audit_normalizes_unambiguous_decision_field_alias(self) -> None:
        decision = dict(AUDIT_ACCEPT)
        decision.pop("verdict")
        decision["decision"] = "approved"
        client = FakeClient([decision])
        audit, _usage = audit_generated_artifact(
            client,
            GeneratedArtifact(
                "reference", "int main(){}", "ai_generated", ""
            ),
            problem_id="P1",
            statement="statement",
            contract=V2_CONTRACT,
            settings=SETTINGS,
            deadline=130.0,
            clock=lambda: 100.0,
        )
        self.assertTrue(audit.accepted)
        self.assertEqual(audit.verdict, "accept")
        self.assertEqual(len(client.prompts), 1)

    def test_reference_audit_without_reproducible_witness_is_warning(self) -> None:
        client = FakeClient(
            [
                {
                    "verdict": "reject",
                    "confidence": 0.9,
                    "issues": [
                        {
                            "category": "logic",
                            "severity": "critical",
                            "evidence": "多个检查其实都正确，最后的风险描述被截断",
                        }
                    ],
                    "summary": "ok",
                    "witness": {
                        "code_expression": "",
                        "input_excerpt": "",
                        "trace": "",
                        "seed": None,
                    },
                },
                AUDIT_ACCEPT,
            ]
        )
        audit, _usage = audit_generated_artifact(
            client,
            GeneratedArtifact(
                "reference", "int main(){}", "ai_generated", ""
            ),
            problem_id="P2596",
            statement="statement",
            contract=V2_CONTRACT,
            settings=SETTINGS,
            deadline=130.0,
            clock=lambda: 100.0,
        )
        self.assertTrue(audit.accepted)
        self.assertEqual(audit.verdict, "accept")
        self.assertEqual(audit.issues, ())
        self.assertEqual(len(client.prompts), 2)

    def test_generated_artifact_audits_share_caller_deadline(self) -> None:
        client = FakeClient([AUDIT_ACCEPT, AUDIT_ACCEPT])
        generator = GeneratedArtifact(
            "generator", "#include <iostream>\nint main(){return 0;}\n", "ai_generated", ""
        )
        brute = GeneratedArtifact(
            "brute", "#include <iostream>\nint main(){return 0;}\n", "ai_generated", ""
        )
        for artifact, now in ((generator, 10.0), (brute, 24.5)):
            audit_generated_artifact(
                client,
                artifact,
                problem_id="P2596",
                statement="statement",
                contract=V2_CONTRACT,
                settings=SETTINGS,
                deadline=30.0,
                clock=lambda now=now: now,
            )
        self.assertEqual(client.json_kwargs[0]["request_timeout"], 20.0)
        self.assertEqual(client.json_kwargs[1]["request_timeout"], 5.5)

    def test_generated_artifact_audit_fails_closed_after_shared_deadline(self) -> None:
        client = FakeClient([])
        artifact = GeneratedArtifact(
            "reference", "#include <iostream>\nint main(){return 0;}\n", "ai_generated", ""
        )
        with self.assertRaises(StressPreparationError) as caught:
            audit_generated_artifact(
                client,
                artifact,
                problem_id="P2596",
                statement="statement",
                contract=V2_CONTRACT,
                settings=SETTINGS,
                deadline=30.0,
                clock=lambda: 30.0,
            )
        self.assertEqual(caught.exception.code, "stress_artifact_audit_timeout")
        self.assertEqual(client.prompts, [])

    def test_generated_artifact_audit_retries_accept_with_critical_issue(self) -> None:
        client = FakeClient(
            [
                {
                    "verdict": "accept",
                    "confidence": 0.99,
                    "issues": [
                        {
                            "category": "bounds",
                            "severity": "critical",
                            "evidence": "b 只保存 Insert 参数却按所有操作读取 b[i]",
                        }
                    ],
                    "summary": "存在越界",
                },
                AUDIT_ACCEPT,
            ]
        )
        audit, _ = audit_generated_artifact(
            client,
            GeneratedArtifact(
                "generator", "#include <iostream>\nint main(){return 0;}\n", "ai_generated", ""
            ),
            problem_id="P2596",
            statement="statement",
            contract=V2_CONTRACT,
            settings=SETTINGS,
            deadline=30.0,
            clock=lambda: 0.0,
        )
        self.assertTrue(audit.accepted)
        self.assertEqual(audit.verdict, "accept")
        self.assertEqual(audit.issues, ())
        self.assertEqual(len(client.prompts), 2)

    def test_generated_artifact_audit_refuses_non_ai_helper(self) -> None:
        with self.assertRaises(ValueError):
            audit_generated_artifact(
                FakeClient([]),
                GeneratedArtifact(
                    "reference",
                    "#include <iostream>\nint main(){return 0;}\n",
                    "luogu_solution",
                    "",
                ),
                problem_id="P2596",
                statement="statement",
                contract=V2_CONTRACT,
                settings=SETTINGS,
                deadline=30.0,
                clock=lambda: 0.0,
            )

    def test_rejected_source_tier_falls_through_to_ai_reference(self) -> None:
        client = FakeClient([])
        crawler = RejectingCrawler({})
        selected, _ = search_reference(
            client,
            crawler,
            platform="luogu",
            problem_id="P2596",
            title="书架",
            statement="statement",
            contract={},
            settings=SETTINGS,
        )
        self.assertIsNone(selected)
        self.assertEqual(crawler.calls, ["cnblogs", "luogu_solutions", "csdn"])

    def test_search_respects_source_order_and_stops_at_first_selected_tier(self) -> None:
        candidate = SourceCandidate(
            "c1",
            "cnblogs",
            "https://www.cnblogs.com/x/p/1",
            "CF1A solution",
            "CF1A explanation",
            "#include <bits/stdc++.h>\nint main(){return 0;}\n",
            "a" * 64,
            True,
        )
        client = FakeClient([])
        crawler = FakeCrawler({"cnblogs": [candidate]})
        selected, _ = search_reference(
            client,
            crawler,
            platform="codeforces",
            problem_id="CF1A",
            title="Theatre Square",
            statement="statement",
            contract={},
            settings=SETTINGS,
        )
        self.assertEqual(selected, candidate)
        self.assertEqual(crawler.calls, ["codeforces_official", "cnblogs"])

    def test_search_can_retain_safe_same_tier_reference_alternates(self) -> None:
        first = SourceCandidate(
            "c1", "cnblogs", "https://www.cnblogs.com/x/p/1", "first", "",
            "#include <bits/stdc++.h>\nint main(){return 0;}\n", "a" * 64, True,
        )
        second = SourceCandidate(
            "c2", "cnblogs", "https://www.cnblogs.com/x/p/2", "second", "",
            "#include <bits/stdc++.h>\nint main(){return 1;}\n", "b" * 64, True,
        )
        pool: list[SourceCandidate] = []
        crawler = FakeCrawler({"cnblogs": [first, second]})
        selected, _ = search_reference(
            FakeClient([]),
            crawler,
            platform="luogu",
            problem_id="P1",
            title="problem",
            statement="statement",
            contract={},
            settings=SETTINGS,
            candidate_pool=pool,
        )
        self.assertEqual(selected, first)
        self.assertEqual(pool, [first, second])
        self.assertEqual(crawler.calls, ["cnblogs"])

    def test_search_collects_two_references_with_distinct_url_and_source_hash(self) -> None:
        first = SourceCandidate(
            "c1", "cnblogs", "https://www.cnblogs.com/x/p/1", "first", "",
            "#include <iostream>\nint main(){return 1;}\n", "a" * 64, True,
        )
        duplicate_url = SourceCandidate(
            "c2", "cnblogs", first.url, "same url", "",
            "#include <iostream>\nint main(){return 2;}\n", "b" * 64, True,
        )
        duplicate_code = SourceCandidate(
            "c3", "cnblogs", "https://www.cnblogs.com/x/p/3", "same code", "",
            first.code, "c" * 64, True,
        )
        second = SourceCandidate(
            "c4", "csdn", "https://blog.csdn.net/x/article/details/4", "second", "",
            "#include <iostream>\nint main(){return 4;}\n", "d" * 64, True,
        )
        pool: list[SourceCandidate] = []
        crawler = FakeCrawler(
            {"cnblogs": [first, duplicate_url, duplicate_code], "csdn": [second]}
        )
        selected, _ = search_reference(
            FakeClient([]),
            crawler,
            platform="luogu",
            problem_id="P1",
            title="problem",
            statement="statement",
            contract={},
            settings=SETTINGS,
            candidate_pool=pool,
        )
        self.assertEqual(selected, first)
        self.assertEqual(pool, [first, second])
        self.assertEqual(crawler.calls, ["cnblogs", "luogu_solutions", "csdn"])

    def test_search_can_defer_luogu_ai_audit_to_executable_gates(self) -> None:
        candidate = SourceCandidate(
            "l1", "luogu_solutions", "https://www.luogu.com.cn/article/l1",
            "solution", "", "#include <iostream>\nint main(){return 0;}\n",
            "d" * 64, True,
        )
        crawler = FakeCrawler({"luogu_solutions": [candidate]})
        client = FakeClient([])
        pool: list[SourceCandidate] = []
        selected, usage = search_reference(
            client,
            crawler,
            platform="luogu",
            problem_id="P1",
            title="problem",
            statement="statement",
            contract={},
            settings=SETTINGS,
            candidate_pool=pool,
            audit_external_sources=False,
        )
        self.assertEqual(selected, candidate)
        self.assertEqual(pool, [candidate])
        self.assertEqual(usage, {})
        self.assertEqual(client.prompts, [])

    def test_complete_source_code_wins_over_selected_explanation_fragment(self) -> None:
        fragment = SourceCandidate(
            "fragment",
            "luogu_solutions",
            "https://www.luogu.com.cn/article/fragment",
            "P2596 explanation",
            "P2596 only explanation",
            None,
            "a" * 64,
            False,
        )
        complete = SourceCandidate(
            "complete",
            "luogu_solutions",
            "https://www.luogu.com.cn/article/complete",
            "P2596 complete solution",
            "P2596",
            "#include <bits/stdc++.h>\nint main(){return 0;}\n",
            "b" * 64,
            True,
        )
        selected, _ = search_reference(
            FakeClient([AUDIT_ACCEPT]),
            FakeCrawler({"luogu_solutions": [fragment, complete]}),
            platform="luogu",
            problem_id="P2596",
            title="书架",
            statement="statement",
            contract={"large_profile": "n <= 80000"},
            settings=SETTINGS,
        )
        self.assertEqual(selected.candidate_id, complete.candidate_id)
        self.assertTrue(selected.static_audit["accepted"])

    def test_unsafe_selected_source_falls_through_to_safe_complete_source(self) -> None:
        unsafe = SourceCandidate(
            "unsafe",
            "luogu_solutions",
            "https://www.luogu.com.cn/article/unsafe",
            "P2596 unsafe",
            "P2596",
            '#include "local.h"\nint main(){return 0;}\n',
            "a" * 64,
            True,
        )
        safe = SourceCandidate(
            "safe",
            "luogu_solutions",
            "https://www.luogu.com.cn/article/safe",
            "P2596 safe",
            "P2596",
            "#include <bits/stdc++.h>\nint main(){return 0;}\n",
            "b" * 64,
            True,
        )
        selected, _ = search_reference(
            FakeClient([AUDIT_ACCEPT]),
            FakeCrawler({"luogu_solutions": [unsafe, safe]}),
            platform="luogu",
            problem_id="P2596",
            title="书架",
            statement="statement",
            contract={"large_profile": "n <= 80000"},
            settings=SETTINGS,
        )
        self.assertEqual(selected.candidate_id, safe.candidate_id)
        self.assertTrue(selected.static_audit["accepted"])

    def test_static_audit_rejects_suspicious_luogu_candidate_and_tries_next(self) -> None:
        first = SourceCandidate(
            "first",
            "luogu_solutions",
            "https://www.luogu.com.cn/article/first",
            "P2596 first",
            "P2596",
            "#include <bits/stdc++.h>\nint main(){return 0;}\n",
            "a" * 64,
            True,
        )
        second = SourceCandidate(
            "second",
            "luogu_solutions",
            "https://www.luogu.com.cn/article/second",
            "P2596 second",
            "P2596",
            "#include <iostream>\nint main(){return 0;}\n",
            "b" * 64,
            True,
        )
        client = FakeClient([AUDIT_REJECT, AUDIT_ACCEPT])
        selected, usage = search_reference(
            client,
            FakeCrawler({"luogu_solutions": [first, second]}),
            platform="luogu",
            problem_id="P2596",
            title="书架",
            statement="n <= 80000; output answers",
            contract={"large_profile": "n <= 80000"},
            settings=SETTINGS,
            compile_checker=lambda code: (True, "warning fixture"),
        )
        self.assertEqual(selected.candidate_id, "second")
        self.assertTrue(selected.static_audit["accepted"])
        self.assertEqual(usage["total_tokens"], 2)
        self.assertEqual(client.tool_prompts, [])
        for kwargs in client.json_kwargs:
            self.assertFalse(kwargs["thinking"])
            self.assertEqual(kwargs["max_tokens"], 512)
            self.assertLessEqual(kwargs["request_timeout"], 24.0)
            self.assertEqual(kwargs["request_retries"], 0)
            self.assertEqual(kwargs["json_retries"], 0)
        audit_prompts = json.dumps(client.prompts, ensure_ascii=False)
        self.assertIn("数组容量", audit_prompts)
        self.assertIn("输入输出", audit_prompts)
        self.assertIn("source_code", audit_prompts)

    def test_static_compile_failure_skips_candidate_before_ai_audit(self) -> None:
        broken = SourceCandidate(
            "broken",
            "luogu_solutions",
            "https://www.luogu.com.cn/article/broken",
            "P2596 broken",
            "P2596",
            "#include <iostream>\nint main(){ missing(); }\n",
            "a" * 64,
            True,
        )
        good = SourceCandidate(
            "good",
            "luogu_solutions",
            "https://www.luogu.com.cn/article/good",
            "P2596 good",
            "P2596",
            "#include <iostream>\nint main(){return 0;}\n",
            "b" * 64,
            True,
        )
        client = FakeClient([AUDIT_ACCEPT])
        selected, _ = search_reference(
            client,
            FakeCrawler({"luogu_solutions": [broken, good]}),
            platform="luogu",
            problem_id="P2596",
            title="书架",
            statement="statement",
            contract={},
            settings=SETTINGS,
            compile_checker=lambda code: (
                (False, "missing was not declared")
                if "missing()" in code
                else (True, "")
            ),
        )
        self.assertEqual(selected.candidate_id, "good")
        self.assertEqual(len(client.prompts), 1)

    def test_search_with_only_incomplete_material_returns_none_for_ai_fallback(self) -> None:
        incomplete = SourceCandidate(
            "explanation-only",
            "luogu_solutions",
            "https://www.luogu.com.cn/article/explanation-only",
            "Explanation without full source",
            "P1",
            None,
            None,
            False,
        )
        selected, usage = search_reference(
            FakeClient([]),
            FakeCrawler({"luogu_solutions": [incomplete]}),
            platform="luogu",
            problem_id="P1",
            title="P1",
            statement="statement",
            contract={},
            settings=SETTINGS,
        )
        self.assertIsNone(selected)
        self.assertEqual(usage, {})

    def test_minimal_policy_can_disable_unaudited_external_references(self) -> None:
        external = SourceCandidate(
            "external",
            "luogu_solutions",
            "https://www.luogu.com.cn/article/external",
            "External",
            "P1",
            "#include <iostream>\nint main(){std::cout<<0;}",
            "a" * 64,
            True,
        )
        client = FakeClient(
            [{"code": "#include <iostream>\nint main(){std::cout<<1;}", "notes": "ai"}]
        )
        prepared = prepare_stress(
            client,
            FakeCrawler({"luogu_solutions": [external]}),
            platform="luogu",
            problem_id="P1",
            title="P1",
            statement="Input one integer n with 1 <= n <= 10.",
            compare="token",
            settings=SETTINGS,
            include_generator=False,
            include_reference_primary=True,
            include_reference_secondary=False,
            allow_external_references=False,
            prepared_contract=contract_v3(),
            require_complete_probes=False,
        )
        self.assertEqual(prepared.reference_primary.origin, "ai_generated")
        self.assertIsNone(prepared.reference_primary.source_url)
        self.assertEqual(len(client.prompts), 1)

    def test_prepare_uses_independent_prompts_and_direct_complete_reference(self) -> None:
        reference = SourceCandidate(
            "r1",
            "luogu_solutions",
            "https://www.luogu.com.cn/problem/solution/P1000",
            "P1000 题解",
            "P1000",
            "#include <iostream>\nint main(){return 0;}\n",
            "b" * 64,
            True,
        )
        client = FakeClient(
            [
                dict(V2_CONTRACT),
                json.loads(json.dumps(VALID_BLUEPRINT)),
                {"code": GENERATOR_SOURCE, "notes": "gen"},
                {"code": "#include <iostream>\nint main(){return 1;}", "notes": "ref2"},
                AUDIT_ACCEPT,
            ]
        )
        prepared = prepare_stress(
            client,
            FakeCrawler({"luogu_solutions": [reference]}),
            platform="luogu",
            problem_id="P1000",
            title="超级玛丽游戏",
            statement="public statement",
            compare="token",
            settings=SETTINGS,
            progress_callback=lambda stage, label, step, total: client.progress.append(
                (stage, label, step, total)
            ),
        )
        self.assertEqual(prepared.reference_primary.origin, "luogu_solution")
        self.assertEqual(prepared.reference_primary.source_url, reference.url)
        self.assertIsNone(prepared.reference_primary.static_audit)
        self.assertEqual(prepared.reference_secondary.kind, "reference_secondary")
        self.assertEqual(prepared.reference_secondary.origin, "ai_generated")
        prompts = json.dumps(client.prompts, ensure_ascii=False)
        self.assertNotIn("user solution", prompts)
        self.assertNotIn('"artifact_kind": "brute"', prompts)
        self.assertIn("最大约束", prompts)
        self.assertIn("不得使用只适用于小数据的暴力", prompts)
        self.assertEqual(prepared.usage["total_tokens"], 4)
        self.assertEqual(prepared.generator_blueprint["schema_version"], 1)
        # SETTINGS carries no mode key, so this exercises the library fallback,
        # which now matches the declared product default (config.py's
        # STRESS_GENERATION_MODE_DEFAULT) instead of diverging to "fast".
        self.assertEqual(prepared.generation_metadata["mode"], "hybrid")
        self.assertEqual(prepared.generation_metadata["blueprint_repairs_used"], 0)
        self.assertEqual(
            [item[0] for item in client.progress],
            [
                "extract_contract",
                "generate_generator",
                "generate_generator",
                "prepare_reference_primary",
                "prepare_reference_secondary",
            ],
        )
        self.assertEqual([item[2] for item in client.progress], [2, 3, 3, 5, 6])
        self.assertIn("secondary reference", client.progress[-1][1])

    def test_prepare_retries_one_generated_reference_once_on_duplicate_source(self) -> None:
        class DuplicateReferenceClient:
            def __init__(self) -> None:
                self.lock = threading.Lock()
                self.calls: list[str] = []
                self.secondary_calls = 0

            def chat_json(self, messages, **kwargs):
                request = json.loads(messages[-1]["content"])
                role = str(request.get("artifact_kind") or "")
                with self.lock:
                    self.calls.append(role)
                    if role == "reference_secondary":
                        self.secondary_calls += 1
                        attempt = self.secondary_calls
                    else:
                        attempt = 1
                code = "#include <iostream>\nint main(){return 0;}\n"
                if role == "reference_secondary" and attempt == 2:
                    code = "#include <iostream>\nint main(){return 1;}\n"
                return Result(data={"code": code, "notes": role})

        client = DuplicateReferenceClient()
        prepared = prepare_stress(
            client,
            FakeCrawler({}),
            platform="luogu",
            problem_id="P1",
            title="problem",
            statement="statement",
            compare="token",
            settings=SETTINGS,
            include_generator=False,
            prepared_contract={**V2_CONTRACT, "profile_version": 2},
        )
        self.assertEqual(client.secondary_calls, 2)
        self.assertNotEqual(
            prepared.reference_primary.code,
            prepared.reference_secondary.code,
        )
        self.assertNotIn("brute", client.calls)

    def test_prepare_fails_closed_when_generated_references_remain_duplicates(self) -> None:
        class AlwaysDuplicateClient:
            def __init__(self) -> None:
                self.roles: list[str] = []

            def chat_json(self, messages, **kwargs):
                request = json.loads(messages[-1]["content"])
                role = str(request.get("artifact_kind") or "")
                self.roles.append(role)
                return Result(
                    data={
                        "code": "#include <iostream>\nint main(){return 0;}\n",
                        "notes": role,
                    }
                )

        client = AlwaysDuplicateClient()
        with self.assertRaises(StressPreparationError) as caught:
            prepare_stress(
                client,
                FakeCrawler({}),
                platform="luogu",
                problem_id="P1",
                title="problem",
                statement="statement",
                compare="token",
                settings=SETTINGS,
                include_generator=False,
                prepared_contract={**V2_CONTRACT, "profile_version": 2},
            )
        self.assertEqual(caught.exception.code, "stress_reference_duplicate")
        self.assertEqual(client.roles.count("reference_secondary"), 2)
        self.assertNotIn("brute", client.roles)

    def test_prepare_exposes_blueprint_repairs_and_shares_cancel_scope(self) -> None:
        invalid = json.loads(json.dumps(VALID_BLUEPRINT))
        del invalid["dimensions"]
        scope = CancelScope()
        client = FakeClient(
            [
                invalid,
                json.loads(json.dumps(VALID_BLUEPRINT)),
                {
                    "code": GENERATOR_SOURCE,
                    "notes": "generator",
                },
            ]
        )
        prepared = prepare_stress(
            client,
            FakeCrawler({}),
            platform="luogu",
            problem_id="P1",
            title="test",
            statement="statement",
            compare="token",
            settings=SETTINGS,
            generation_mode="hybrid",
            include_reference_primary=False,
            include_reference_secondary=False,
            prepared_contract={**V2_CONTRACT, "profile_version": 2},
            blueprint_repair_limit=1,
            cancel_scope=scope,
        )
        self.assertEqual(prepared.generation_metadata["blueprint_repairs_used"], 1)
        self.assertEqual(prepared.usage["blueprint_repairs_used"], 1)
        self.assertTrue(
            all(kwargs.get("cancel_scope") is scope for kwargs in client.json_kwargs)
        )
        self.assertFalse(scope.cancelled)

        reused_client = FakeClient(
            [
                {
                    "code": GENERATOR_SOURCE,
                    "notes": "generator",
                }
            ]
        )
        reused = prepare_stress(
            reused_client,
            FakeCrawler({}),
            platform="luogu",
            problem_id="P1",
            title="test",
            statement="statement",
            compare="token",
            settings=SETTINGS,
            include_reference_primary=False,
            include_reference_secondary=False,
            prepared_contract={**V2_CONTRACT, "profile_version": 2},
            prepared_generator_blueprint=VALID_BLUEPRINT,
            initial_usage={"blueprint_repairs_used": 7},
        )
        self.assertEqual(reused.generation_metadata["blueprint_repairs_used"], 0)
        self.assertEqual(reused.usage["blueprint_repairs_used"], 7)

    def test_prepare_cancels_scope_on_terminal_blueprint_failure(self) -> None:
        invalid = json.loads(json.dumps(VALID_BLUEPRINT))
        del invalid["dimensions"]
        scope = CancelScope()
        with self.assertRaises(StressPreparationError) as caught:
            prepare_stress(
                FakeClient(
                    [
                        json.loads(json.dumps(invalid)),
                        json.loads(json.dumps(invalid)),
                        json.loads(json.dumps(invalid)),
                    ]
                ),
                FakeCrawler({}),
                platform="luogu",
                problem_id="P1",
                title="test",
                statement="statement",
                compare="token",
                settings=SETTINGS,
                include_reference_primary=False,
                include_reference_secondary=False,
                prepared_contract={**V2_CONTRACT, "profile_version": 2},
                blueprint_repair_limit=2,
                cancel_scope=scope,
            )
        self.assertTrue(scope.cancelled)
        primary = caught.exception.details["primary_failure"]
        self.assertEqual(primary["role"], "generator")
        self.assertEqual(primary["substage"], "blueprint")
        self.assertEqual(primary["path"], "dimensions")
        self.assertEqual(primary["attempts"], 2)
        self.assertIn("结构修复 2/2", str(caught.exception))

    def test_fast_audit_rejects_source_over_context_budget_without_ai(self) -> None:
        candidate = SourceCandidate(
            "huge",
            "luogu_solutions",
            "https://www.luogu.com.cn/article/huge",
            "P1000 huge",
            "P1000",
            "#include <iostream>\nint main(){return 0;}\n" + ("// filler\n" * 4000),
            "a" * 64,
            True,
        )
        client = FakeClient([])
        selected, usage = search_reference(
            client,
            FakeCrawler({"luogu_solutions": [candidate]}),
            platform="luogu",
            problem_id="P1000",
            title="test",
            statement="statement",
            contract={},
            settings=SETTINGS,
        )
        self.assertIsNone(selected)
        self.assertEqual(usage, {})
        self.assertEqual(client.prompts, [])

    def test_prepare_reports_network_retry_inside_current_stage(self) -> None:
        client = FakeClient(
            [dict(V2_CONTRACT)],
            emit_retry=True,
        )
        prepared = prepare_stress(
            client,
            FakeCrawler({}),
            platform="luogu",
            problem_id="P1000",
            title="test",
            statement="statement",
            compare="token",
            settings=SETTINGS,
            include_generator=False,
            include_reference_primary=False,
            include_reference_secondary=False,
            progress_callback=lambda stage, label, step, total: client.progress.append(
                (stage, label, step, total)
            ),
        )
        self.assertIsNone(prepared.generator)
        retry = next(item for item in client.progress if "连接中断" in item[1])
        self.assertEqual((retry[0], retry[2], retry[3]), ("extract_contract", 2, 10))
        self.assertIn("重试 1/2", retry[1])


if __name__ == "__main__":
    unittest.main()
