from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import tempfile
import unittest

from tools.acm_agent.ai_context import (
    CONTEXT_MAX_BYTES,
    SOURCE_MAX_BYTES,
    ContextParseError,
    ContextValidationError,
    PatchConflictError,
    SourceValidationError,
    apply_source_patch,
    content_sha256,
    extract_codeforces_statement,
    extract_luogu_statement,
    extract_statement_samples,
    is_context_fresh,
    parse_problem_statement,
    revert_source_patch,
    unified_source_diff,
    validate_managed_cpp,
    validate_manual_context,
    validate_model_replacement,
    validate_patch_explanatory_comments,
)


class ProblemStatementParsingTests(unittest.TestCase):
    def test_codeforces_extracts_only_problem_statement(self) -> None:
        html = """
        <html><body>
          <div class="blog-entry">EDITORIAL SECRET</div>
          <div class="problem-statement">
            <div class="header"><div class="title">A. Fixture</div></div>
            <p>Find <span class="tex-span">$x+y$</span> &amp; print it.</p>
            <div class="input-specification">
              <div class="section-title">Input</div>
              <p>Two integers.<br>Second line.</p>
            </div>
            <script>INJECTED INSTRUCTION</script>
          </div>
          <div class="problem-statement-extra">NOT THE STATEMENT</div>
          <section class="tutorial">ANOTHER EDITORIAL</section>
        </body></html>
        """
        statement = extract_codeforces_statement(html)
        self.assertIn("A. Fixture", statement)
        self.assertIn("Find $x+y$ & print it.", statement)
        self.assertIn("## Input", statement)
        self.assertIn("Second line.", statement)
        for forbidden in (
            "EDITORIAL SECRET", "INJECTED INSTRUCTION", "NOT THE STATEMENT",
            "ANOTHER EDITORIAL",
        ):
            self.assertNotIn(forbidden, statement)
        self.assertEqual(parse_problem_statement("CF", html), statement)

    def test_codeforces_requires_exact_nonempty_statement_class(self) -> None:
        with self.assertRaises(ContextParseError):
            extract_codeforces_statement('<div class="problem-statement-extra">x</div>')
        with self.assertRaises(ContextParseError):
            extract_codeforces_statement('<div class="problem-statement"></div>')

    def test_luogu_extracts_whitelisted_content_and_samples_not_editorial(self) -> None:
        payload = {
            "currentData": {
                "problem": {
                    "pid": "P1000",
                    "content": {
                        "background": "背景 **Markdown**",
                        "description": "计算 $a+b$。",
                        "formatI": "输入两个整数。",
                        "formatO": "输出答案。",
                        "hint": "注意范围。",
                    },
                    "samples": [["1 2\n", "3\n"]],
                    "solution": {"description": "DO NOT LEAK SOLUTION"},
                    "solution_content": {"description": "DO NOT LEAK ALT SOLUTION"},
                    "editorial": "DO NOT LEAK EDITORIAL",
                }
            }
        }
        statement = extract_luogu_statement(payload)
        self.assertIn("## 题目描述", statement)
        self.assertIn("计算 $a+b$。", statement)
        self.assertIn("输入两个整数。", statement)
        self.assertIn("输出答案。", statement)
        self.assertIn("### 样例输入 1", statement)
        self.assertIn("1 2", statement)
        self.assertNotIn("DO NOT LEAK", statement)
        self.assertEqual(
            extract_statement_samples(statement),
            [{"name": "sample1", "input": "1 2\n", "output": "3\n"}],
        )

    def test_luogu_keeps_parent_samples_with_longer_translation(self) -> None:
        payload = {
            "problem": {
                "content": {
                    "description": "短题面",
                    "formatI": "输入",
                    "formatO": "输出",
                },
                "samples": [["1 2\n", "3\n"]],
            },
            "translations": {
                "en": {
                    "description": "A substantially longer translated statement.",
                    "formatI": "Read two integers from standard input.",
                    "formatO": "Print their sum to standard output.",
                }
            },
        }
        statement = extract_luogu_statement(payload)
        self.assertIn("substantially longer", statement)
        self.assertEqual(
            extract_statement_samples(statement),
            [{"name": "sample1", "input": "1 2\n", "output": "3\n"}],
        )

    def test_structured_samples_require_explicit_paired_fences(self) -> None:
        statement = """## Samples

### Sample Input 2
```text
4 5
```
### Sample Output 2
```text
9
```

### Sample Input 3
```cpp
int main() {}
```
"""
        self.assertEqual(
            extract_statement_samples(statement),
            [{"name": "sample2", "input": "4 5\n", "output": "9\n"}],
        )

    def test_luogu_accepts_json_and_lentille_html_with_standard_fields(self) -> None:
        context = {
            "data": {
                "problem": {
                    "content": {
                        "description": "描述",
                        "inputFormat": "输入",
                        "outputFormat": "输出",
                    }
                }
            }
        }
        expected = extract_luogu_statement(json.dumps(context, ensure_ascii=False))
        html = (
            '<script id="lentille-context" type="application/json">'
            + json.dumps(context, ensure_ascii=False)
            + "</script>"
        )
        self.assertEqual(extract_luogu_statement(html), expected)
        self.assertEqual(parse_problem_statement("luogu", context), expected)
        with self.assertRaises(ContextParseError):
            extract_luogu_statement({"editorial": {"description": "题解"}})
        with self.assertRaises(ContextParseError):
            parse_problem_statement("other", {})


class ContextPolicyTests(unittest.TestCase):
    def test_manual_context_size_nul_and_hash(self) -> None:
        text = "题面\n"
        self.assertEqual(validate_manual_context(text), text)
        self.assertEqual(content_sha256(text), content_sha256(text.encode("utf-8")))
        with self.assertRaises(ContextValidationError):
            validate_manual_context("  ")
        with self.assertRaises(ContextValidationError):
            validate_manual_context("bad\x00context")
        with self.assertRaises(ContextValidationError):
            validate_manual_context("a" * (CONTEXT_MAX_BYTES + 1))

    def test_context_freshness_is_timezone_safe_and_thirty_days_exclusive(self) -> None:
        now = datetime(2026, 8, 4, tzinfo=timezone.utc)
        self.assertTrue(is_context_fresh(now - timedelta(days=29, hours=23), now=now))
        self.assertTrue(is_context_fresh("2026-07-06T00:00:01Z", now=now))
        self.assertFalse(is_context_fresh(now - timedelta(days=30), now=now))
        self.assertFalse(is_context_fresh(now + timedelta(seconds=1), now=now))
        self.assertFalse(is_context_fresh("not-a-date", now=now))
        self.assertFalse(is_context_fresh(None, now=now))


class PatchSafetyTests(unittest.TestCase):
    def _source(self, root: Path, value: str = "int main() { return 0; }\n") -> Path:
        path = root / "2026" / "8" / "4" / "CF1A.cpp"
        path.parent.mkdir(parents=True)
        path.write_text(value, encoding="utf-8", newline="")
        return path

    def test_managed_path_and_replacement_validation(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self._source(root)
            self.assertEqual(validate_managed_cpp(root, source), source.resolve())
            self.assertEqual(
                validate_managed_cpp(root, "2026/8/4/CF1A.cpp"), source.resolve()
            )
            for invalid in (
                root / "CF1A.cpp",
                root / "2026/8/32/CF1A.cpp",
                root / "2026/8/4/nested/CF1A.cpp",
                root / "2026/8/4/CF1A.txt",
                root.parent / "outside.cpp",
            ):
                with self.subTest(path=invalid):
                    with self.assertRaises(SourceValidationError):
                        validate_managed_cpp(root, invalid)
            with self.assertRaises(SourceValidationError):
                validate_model_replacement("```cpp\nint main(){}\n```")
            with self.assertRaises(SourceValidationError):
                validate_model_replacement("int main(){}\x00")
            with self.assertRaises(SourceValidationError):
                validate_model_replacement("x" * (SOURCE_MAX_BYTES + 1))

    def test_diff_apply_backup_and_guarded_revert(self) -> None:
        original = "#include <iostream>\nint main() { return 0; }\n"
        replacement = "#include <iostream>\nint main() { return 1; }\n"
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self._source(root, original)
            baseline = content_sha256(source.read_bytes())
            preview = unified_source_diff(original, replacement, path="2026/8/4/CF1A.cpp")
            self.assertIn("--- a/2026/8/4/CF1A.cpp", preview)
            self.assertIn("+++ b/2026/8/4/CF1A.cpp", preview)

            applied = apply_source_patch(
                root,
                source,
                replacement,
                expected_sha256=baseline,
                backup_id="proposal-7",
            )
            self.assertEqual(source.read_text(encoding="utf-8"), replacement)
            self.assertEqual(applied.original_sha256, baseline)
            self.assertEqual(applied.applied_sha256, content_sha256(replacement))
            self.assertTrue(applied.backup_path.is_file())
            self.assertEqual(applied.backup_path.read_text(encoding="utf-8"), original)

            reverted = revert_source_patch(
                root,
                source,
                applied.backup_path,
                expected_applied_sha256=applied.applied_sha256,
                expected_baseline_sha256=baseline,
            )
            self.assertEqual(source.read_text(encoding="utf-8"), original)
            self.assertEqual(reverted.restored_sha256, baseline)

    def test_ai_replacement_requires_a_meaningful_new_comment(self) -> None:
        original = 'std::string url = "https://example.test"; // existing note\nint main(){return 1;}\n'
        with self.assertRaises(SourceValidationError):
            validate_patch_explanatory_comments(
                original,
                'std::string url = "https://changed.test"; // existing note\nint main(){return 0;}\n',
            )
        replacement = (
            'std::string url = "https://changed.test"; // existing note\n'
            'int main(){\n'
            '  // 原代码错误：返回 1 会表示程序异常结束；改为 0 表示正常结束。\n'
            '  return 0;\n'
            '}\n'
        )
        self.assertEqual(
            validate_patch_explanatory_comments(original, replacement), replacement
        )

    def test_apply_and_revert_reject_stale_hashes_and_unsafe_backup(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            source = self._source(root)
            baseline = content_sha256(source.read_bytes())
            source.write_text("int main() { return 2; }\n", encoding="utf-8", newline="")
            with self.assertRaises(PatchConflictError):
                apply_source_patch(
                    root,
                    source,
                    "int main() { return 1; }\n",
                    expected_sha256=baseline,
                )

            current_hash = content_sha256(source.read_bytes())
            applied = apply_source_patch(
                root,
                source,
                "int main() { return 3; }\n",
                expected_sha256=current_hash,
                backup_id="proposal-8",
            )
            source.write_text("// user edit\n", encoding="utf-8", newline="")
            with self.assertRaises(PatchConflictError):
                revert_source_patch(
                    root,
                    source,
                    applied.backup_path,
                    expected_applied_sha256=applied.applied_sha256,
                    expected_baseline_sha256=current_hash,
                )

            source.write_text("int main() { return 3; }\n", encoding="utf-8", newline="")
            applied.backup_path.write_text(
                "int main() { return 99; }\n", encoding="utf-8", newline=""
            )
            with self.assertRaises(PatchConflictError):
                revert_source_patch(
                    root,
                    source,
                    applied.backup_path,
                    expected_applied_sha256=applied.applied_sha256,
                    expected_baseline_sha256=current_hash,
                )
            outside = root / "outside.bak"
            outside.write_text("int main(){}\n", encoding="utf-8")
            user_hash = content_sha256(source.read_bytes())
            with self.assertRaises(SourceValidationError):
                revert_source_patch(
                    root,
                    source,
                    outside,
                    expected_applied_sha256=user_hash,
                    expected_baseline_sha256=current_hash,
                )


if __name__ == "__main__":
    unittest.main()
