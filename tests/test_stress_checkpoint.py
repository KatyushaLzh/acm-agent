from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.acm_agent.storage import Database, StressCacheAliasRevisionConflict
from tools.acm_agent.stress_checkpoint import (
    StressCheckpointStore,
    candidate_ref,
    certification_identity,
    certification_key,
    generation_identity,
    generation_key,
)


class StressCheckpointIdentityTests(unittest.TestCase):
    def test_generation_key_is_sensitive_to_prompt_model_and_statement(self) -> None:
        common = {
            "platform": "luogu",
            "problem_id": "P2596",
            "role": "generator",
            "model": "deepseek-chat",
            "mode": "hybrid",
            "prompt": "generate adapter v1",
            "prompt_version": 3,
            "statement": "Ask is zero based.",
            "semantic_inputs": {"contract_schema": 3},
        }
        baseline = generation_key(generation_identity(**common))
        mutations = (
            {"prompt": "generate adapter v2"},
            {"prompt_version": 4},
            {"model": "deepseek-reasoner"},
            {"mode": "full-thinking"},
            {"statement": "Ask is one based."},
            {"semantic_inputs": {"contract_schema": 4}},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.assertNotEqual(
                    generation_key(generation_identity(**(common | mutation))),
                    baseline,
                )

    def test_certification_key_covers_sources_environment_samples_and_protocol(self) -> None:
        def candidate(role: str, suffix: str = "") -> dict[str, str]:
            return {
                "id": f"candidate-{role}{suffix}",
                "role": role,
                "source_hash": (role[0] + suffix + "0" * 64)[:64],
            }

        common = {
            "generator": candidate("generator"),
            "brute": candidate("brute"),
            "reference": candidate("reference"),
            "validator": candidate("validator"),
            "compiler": {"fingerprint": "g++-14"},
            "sandbox": {"backend": "appcontainer", "policy": 3},
            "samples": [{"input": "1 2\n", "output": "3\n"}],
            "protocol": {"generator": "profile-v2", "manifest": 2},
            "gate": {"preflight": 3},
        }
        baseline_identity = certification_identity(**common)
        baseline = certification_key(baseline_identity)
        self.assertEqual(
            baseline_identity["validator"]["candidate_id"], "candidate-validator"
        )
        self.assertIn("source_sha256", baseline_identity["validator"])
        mutations = (
            {"generator": candidate("generator", "x")},
            {"validator": candidate("validator", "x")},
            {"compiler": {"fingerprint": "clang-20"}},
            {"sandbox": {"backend": "appcontainer", "policy": 4}},
            {"samples": [{"input": "2 2\n", "output": "4\n"}]},
            {"protocol": {"generator": "profile-v3", "manifest": 2}},
            {"gate": {"preflight": 4}},
        )
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                self.assertNotEqual(
                    certification_key(certification_identity(**(common | mutation))),
                    baseline,
                )


class StressCheckpointStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(Path(self.temp.name) / "state.db")
        self.db.upsert_problem({"platform": "luogu", "problem_id": "P2596"})
        self.store = StressCheckpointStore(
            self.db,
            platform="luogu",
            problem_id="P2596",
        )

    def tearDown(self) -> None:
        self.db.close()
        self.temp.cleanup()

    def _candidate(self, role: str, source: str | None = None):
        identity = generation_identity(
            platform="luogu",
            problem_id="P2596",
            role=role,
            model="deepseek-chat",
            mode="hybrid",
            prompt=f"prompt for {role}",
            prompt_version=3,
            statement="P2596 statement",
            semantic_inputs={"contract": "v3"},
        )
        selected_source = source or f"int main(){{/* {role} */}}\n"
        return self.store.save_candidate(
            role=role,
            source_code=selected_source,
            identity=identity,
            usage={"total_tokens": 7},
        )

    def test_candidate_proof_and_exact_trio_are_immutable(self) -> None:
        generator = self._candidate("generator")
        self.assertEqual(generator, self._candidate("generator"))
        changed = self._candidate("generator", "int main(){return 1;}\n")
        self.assertNotEqual(generator.id, changed.id)
        proof = self.store.save_proof(
            candidate=generator,
            proof_kind="compile",
            identity={"compiler": "g++-14", "sandbox": "appcontainer-v3"},
            status="passed",
            result={"returncode": 0},
            executable_hash="e" * 64,
        )
        same = self.store.save_proof(
            candidate=generator,
            proof_kind="compile",
            identity={"compiler": "g++-14", "sandbox": "appcontainer-v3"},
            status="passed",
            result={"returncode": 0},
            executable_hash="e" * 64,
        )
        self.assertEqual(proof["proof_key"], same["proof_key"])

        candidates = {
            "generator": generator,
            "brute": self._candidate("brute"),
            "reference": self._candidate("reference"),
            "validator": self._candidate("validator"),
        }
        certification = self.store.save_exact_trio_certification(
            **candidates,
            compiler={"fingerprint": "g++-14"},
            sandbox={"backend": "appcontainer", "policy": 3},
            samples=[{"input": "1\n", "output": "1\n"}],
            protocol={"profile": 2, "manifest": 2},
            gate={"preflight": 3},
            scope={"small": 16, "large": 4},
            preflight={"passed": True},
        )
        stored_identity = json.loads(certification["certification_identity_json"])
        self.assertEqual(
            stored_identity["validator"]["candidate_id"], candidates["validator"].id
        )
        self.assertEqual(
            {
                certification["generator_candidate_id"],
                certification["brute_candidate_id"],
                certification["reference_candidate_id"],
            },
            {candidates[role].id for role in ("generator", "brute", "reference")},
        )

    def test_failed_cold_run_never_publishes_or_replaces_alias(self) -> None:
        candidates = {
            role: self._candidate(role)
            for role in ("generator", "brute", "reference", "validator")
        }
        valid = self.store.save_exact_trio_certification(
            **candidates,
            compiler="g++-14",
            sandbox="appcontainer-v3",
            samples=[],
            protocol={"profile": 2},
        )
        published = self.store.publish_certification_alias(
            "luogu:P2596:certified",
            valid,
            succeeded=True,
        )
        self.assertIsNotNone(published)
        self.assertEqual(published["revision"], 1)

        failed = self.store.save_exact_trio_certification(
            generator=self._candidate("generator", "int main(){return 9;}\n"),
            brute=candidates["brute"],
            reference=candidates["reference"],
            validator=candidates["validator"],
            compiler="g++-14",
            sandbox="appcontainer-v3",
            samples=[],
            protocol={"profile": 2},
            status="invalidated",
            preflight={"passed": False},
        )
        self.assertIsNone(
            self.store.publish_certification_alias(
                "luogu:P2596:certified",
                failed,
                succeeded=False,
                expected_revision=1,
            )
        )
        current = self.db.stress_cache_alias("luogu:P2596:certified")
        self.assertEqual(current["revision"], 1)
        self.assertEqual(current["target_id"], valid["certification_key"])
        with self.assertRaises(StressCacheAliasRevisionConflict):
            self.store.publish_certification_alias(
                "luogu:P2596:certified",
                valid,
                succeeded=True,
                expected_revision=0,
            )


if __name__ == "__main__":
    unittest.main()
