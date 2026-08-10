from __future__ import annotations

from datetime import date
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools.acm_agent.verify import outputs_equal, verify_problem
from tools.acm_agent.workspace import (
    DEFAULT_TEMPLATE,
    find_solution,
    load_default_template,
    parse_problem_ref,
    save_default_template,
    scan_local_solutions,
    start_problem,
    validate_default_template,
)


class WorkspaceTests(unittest.TestCase):
    def test_parse_problem_ids_and_urls(self) -> None:
        cases = {
            "cf1791c": ("codeforces", "CF1791C"),
            "https://codeforces.com/problemset/problem/1791/C": ("codeforces", "CF1791C"),
            "https://codeforces.com/contest/1313/problem/C1": ("codeforces", "CF1313C1"),
            "P3373": ("luogu", "P3373"),
            "https://www.luogu.com.cn/problem/P3373": ("luogu", "P3373"),
            "abc123": ("custom", "ABC123"),
            "my-test-1": ("custom", "MY-TEST-1"),
            "P1000x": ("custom", "P1000X"),
            "校内模拟/2026/round2": ("custom", "校内模拟_2026_ROUND2"),
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                parsed = parse_problem_ref(raw)
                self.assertEqual((parsed.platform, parsed.problem_id), expected)

    def test_rejects_non_problem_url_and_reserved_names(self) -> None:
        for value in (
            "https://codeforces.com/contest/1",
            "https://atcoder.jp/contests/abc123/tasks/abc123_a",
            "www.example.com/x",
        ):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    parse_problem_ref(value)
        with self.assertRaises(ValueError):
            parse_problem_ref("CON")
        with self.assertRaises(ValueError):
            parse_problem_ref("nul")

    def test_scan_only_dated_primary_solutions(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            day = root / "2026" / "8" / "3"
            day.mkdir(parents=True)
            (day / "CF1A.cpp").write_text("", encoding="utf-8")
            (day / "CF1A.bf.cpp").write_text("", encoding="utf-8")
            (day / "CF1A.gen.cpp").write_text("", encoding="utf-8")
            (day / "P1000.cpp").write_text("", encoding="utf-8")
            (day / "MYTEST.cpp").write_text("", encoding="utf-8")
            (day / "MYTEST.ref.cpp").write_text("", encoding="utf-8")
            (day / "notes.cpp").write_text("", encoding="utf-8")
            (day / "template.cpp").write_text("", encoding="utf-8")
            nested = day / "nested"
            nested.mkdir()
            (nested / "CF2A.cpp").write_text("", encoding="utf-8")
            found = scan_local_solutions(root)
            self.assertEqual(
                [item.problem.problem_id for item in found],
                ["CF1A", "MYTEST", "NOTES", "P1000"],
            )
            self.assertEqual(
                {item.problem.platform for item in found},
                {"codeforces", "custom", "luogu"},
            )
            self.assertTrue(all(item.to_dict()["status"] == "local_only" for item in found))

    def test_start_uses_unpadded_date_and_preserves_files(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            day = root / "2026" / "8" / "3"
            day.mkdir(parents=True)
            template = b"// exact-template\r\n"
            (day / "template.cpp").write_bytes(template)

            created = start_problem(root, "CF1A", today=date(2026, 8, 3), with_stress=True)
            self.assertEqual(created.source, (day / "CF1A.cpp").resolve())
            self.assertEqual(created.source.read_bytes(), template)
            self.assertTrue(created.brute_force and created.brute_force.exists())
            self.assertTrue(created.generator and created.generator.exists())

            created.source.write_text("// user edit", encoding="utf-8")
            assert created.brute_force is not None
            created.brute_force.write_text("// brute edit", encoding="utf-8")
            reused = start_problem(root, "CF1A", today=date(2026, 8, 3), with_stress=True)
            self.assertTrue(reused.reused)
            self.assertEqual(reused.source.read_text(encoding="utf-8"), "// user edit")
            self.assertEqual(reused.brute_force.read_text(encoding="utf-8"), "// brute edit")

    def test_start_builtin_template_when_day_has_no_template(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            result = start_problem(temp, "P1000", today=date(2026, 1, 2))
            self.assertEqual(result.source.read_text(encoding="utf-8"), DEFAULT_TEMPLATE)
            self.assertIsNone(result.template_source)

    def test_global_template_used_when_day_has_none(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            global_template = root / ".acm" / "template.cpp"
            global_template.parent.mkdir(parents=True)
            global_template.write_text("// global\n", encoding="utf-8")
            result = start_problem(root, "CF1A", today=date(2026, 1, 2))
            self.assertEqual(result.source.read_text(encoding="utf-8"), "// global\n")
            self.assertEqual(result.template_source, global_template.resolve())

    def test_day_template_beats_global_template(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            day = root / "2026" / "8" / "3"
            day.mkdir(parents=True)
            (day / "template.cpp").write_text("// day\n", encoding="utf-8")
            global_template = root / ".acm" / "template.cpp"
            global_template.parent.mkdir(parents=True)
            global_template.write_text("// global\n", encoding="utf-8")
            result = start_problem(root, "P1000", today=date(2026, 8, 3))
            self.assertEqual(result.source.read_text(encoding="utf-8"), "// day\n")

    def test_save_validate_and_load_default_template(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            path = save_default_template(root, "// hi\n")
            self.assertEqual(path.read_text(encoding="utf-8"), "// hi\n")
            self.assertEqual(load_default_template(root), "// hi\n")
            self.assertEqual(load_default_template(root / "missing"), DEFAULT_TEMPLATE)
            with self.assertRaises(ValueError):
                validate_default_template("a\x00b")
            with self.assertRaises(ValueError):
                validate_default_template("x" * (64 * 1024 + 1))
            self.assertEqual(validate_default_template("ok"), "ok")

    def test_find_solution_uses_latest_date_not_mtime(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            older = root / "2025" / "12" / "31" / "CF1A.cpp"
            newer = root / "2026" / "1" / "1" / "CF1A.cpp"
            older.parent.mkdir(parents=True)
            newer.parent.mkdir(parents=True)
            older.write_text("old", encoding="utf-8")
            newer.write_text("new", encoding="utf-8")
            self.assertEqual(find_solution(root, "CF1A"), newer.resolve())


class VerifyTests(unittest.TestCase):
    def test_output_comparison(self) -> None:
        self.assertTrue(outputs_equal(b"1  2\n", b"1\n2\n"))
        self.assertFalse(outputs_equal(b"1  2\n", b"1\n2\n", exact=True))
        self.assertTrue(outputs_equal(b"same\n", b"same\n", exact=True))

    @mock.patch("tools.acm_agent.verify.shutil.which", return_value=None)
    def test_missing_compiler_is_structured_failure(self, _: mock.Mock) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "2026" / "8" / "3" / "CF1A.cpp"
            source.parent.mkdir(parents=True)
            source.write_text(DEFAULT_TEMPLATE, encoding="utf-8")
            result = verify_problem(root, "CF1A")
            self.assertFalse(result.compiled)
            self.assertFalse(result.passed)
            self.assertIn("compiler not found", result.compile_output)

    def test_verify_samples_when_gpp_available(self) -> None:
        import shutil

        if shutil.which("g++") is None:
            self.skipTest("g++ is unavailable")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            day = root / "2026" / "8" / "3"
            day.mkdir(parents=True)
            source = day / "CF1A.cpp"
            source.write_text(
                "#include <iostream>\nint main(){long long a,b;std::cin>>a>>b;std::cout<<a+b<<'\\n';}\n",
                encoding="utf-8",
            )
            cases = root / ".acm" / "cases" / "CF1A"
            cases.mkdir(parents=True)
            (cases / "sample.in").write_text("2 3\n", encoding="utf-8")
            (cases / "sample.out").write_text("5   \n", encoding="utf-8")

            token_result = verify_problem(root, "CF1A")
            self.assertTrue(token_result.passed, token_result.to_dict())
            self.assertTrue(token_result.cases[0].passed)
            self.assertTrue(Path(token_result.compile_command[-1]).is_relative_to(root / ".acm" / "build"))

            exact_result = verify_problem(root, "CF1A", exact=True)
            self.assertFalse(exact_result.passed)
            self.assertEqual(exact_result.cases[0].reason, "wrong answer")

    def test_stress_mismatch_preserves_reproduction_assets(self) -> None:
        import json
        import shutil

        if shutil.which("g++") is None:
            self.skipTest("g++ is unavailable")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            day = root / "2026" / "8" / "3"
            day.mkdir(parents=True)
            (day / "CF1A.cpp").write_text(
                "#include <iostream>\nint main(){int x;std::cin>>x;std::cout<<0<<'\\n';}\n",
                encoding="utf-8",
            )
            (day / "CF1A.bf.cpp").write_text(
                "#include <iostream>\nint main(){int x;std::cin>>x;std::cout<<x<<'\\n';}\n",
                encoding="utf-8",
            )
            (day / "CF1A.gen.cpp").write_text(
                "#include <iostream>\nint main(){std::cout<<1<<'\\n';}\n",
                encoding="utf-8",
            )

            result = verify_problem(root, "CF1A", stress_iterations=3, seed=42)
            self.assertFalse(result.passed)
            self.assertEqual(result.stress, "failed")
            self.assertEqual(result.stress_iterations, 1)
            self.assertIsNotNone(result.failure_dir)
            failure = Path(result.failure_dir or "")
            self.assertTrue(failure.is_relative_to(root / ".acm" / "failures"))
            self.assertEqual((failure / "input.txt").read_text(), "1\n")
            self.assertEqual((failure / "actual.txt").read_text().strip(), "0")
            self.assertEqual((failure / "expected.txt").read_text().strip(), "1")
            metadata = json.loads((failure / "metadata.json").read_text(encoding="utf-8"))
            self.assertEqual(metadata["seed"], 42)
            self.assertIn("generator_command", metadata)
            self.assertEqual(set(metadata["commands"]), {"generator", "solution", "brute_force"})

    @mock.patch(
        "tools.acm_agent.verify.sanitizer_supported",
        return_value=(False, "probe runtime unavailable"),
    )
    def test_debug_does_not_claim_unsupported_sanitizers(
        self, _: mock.Mock
    ) -> None:
        import shutil

        if shutil.which("g++") is None:
            self.skipTest("g++ is unavailable")
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = root / "2026" / "8" / "3" / "P1000.cpp"
            source.parent.mkdir(parents=True)
            source.write_text("int main(){return 0;}\n", encoding="utf-8")
            result = verify_problem(root, "P1000", debug=True)
            self.assertTrue(result.compiled)
            self.assertEqual(result.sanitizer, "unsupported")
            self.assertTrue(result.warnings)
            self.assertNotIn("-fsanitize=address,undefined", result.compile_command)


if __name__ == "__main__":
    unittest.main()
