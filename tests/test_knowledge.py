from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest

from tools.acm_agent.knowledge import (
    EntryValidationError,
    MAX_MARKDOWN_BYTES,
    MarkdownContentError,
    MarkdownHashMismatch,
    MarkdownPathError,
    SchemaValidationError,
    build_markdown_candidate,
    build_markdown_candidate_from_rendered,
    diagnose_duplicates,
    find_exact_source_entries,
    get_builtin_schema,
    get_builtin_template,
    infer_summary_schema,
    inspect_markdown_path,
    list_builtin_templates,
    markdown_bytes_sha256,
    parse_rendered_entry,
    readback_markdown,
    render_structured_entry,
    schema_sha256,
    validate_summary_schema,
)


def algorithm_entry(**updates):
    entry = {
        "topic": "图论",
        "title": "树上路径分解",
        "aliases": ["路径分解"],
        "confidence": 0.91,
        "fields": {
            "source": "`CF1A`",
            "model": "把路径拆成若干区间。",
            "correctness": "每条边恰好属于一个区间。",
            "implementation": "维护父亲和端点。",
            "complexity": "$O(n \\log n)$。",
            "pitfalls": "固定端点是否重复计入。",
        },
        "rationale": "来自本题复盘。",
    }
    entry.update(updates)
    return entry


class BuiltinSchemaTests(unittest.TestCase):
    def test_builtins_are_packaged_and_valid(self) -> None:
        listed = list_builtin_templates()
        self.assertEqual([item["preset"] for item in listed], ["algorithms-v1", "tricks-v1"])
        algorithms = get_builtin_schema("algorithms-v1")
        tricks = get_builtin_schema("TRICKS-V1")
        self.assertEqual([field["key"] for field in algorithms["fields"]], [
            "source", "model", "correctness", "implementation", "complexity", "pitfalls"
        ])
        self.assertEqual(tricks["blank_lines_between_fields"], 1)
        self.assertIn("[TOC]", get_builtin_template("algorithms-v1"))
        self.assertNotIn("codeforces.com", get_builtin_template("tricks-v1"))

    def test_schema_is_strict_non_executable_and_hash_is_canonical(self) -> None:
        schema = get_builtin_schema("algorithms-v1")
        reordered = json.loads(json.dumps(schema, ensure_ascii=False))
        self.assertEqual(schema_sha256(schema), schema_sha256(reordered))
        invalid = dict(schema, template="{{ fields }}")
        with self.assertRaises(SchemaValidationError):
            validate_summary_schema(invalid)
        invalid = dict(schema, entry_heading_level=1)
        with self.assertRaises(SchemaValidationError):
            validate_summary_schema(invalid)
        invalid = json.loads(json.dumps(schema))
        invalid["fields"][0]["label"] = "<script>bad</script>"
        with self.assertRaises(SchemaValidationError):
            validate_summary_schema(invalid)


class MarkdownInspectionTests(unittest.TestCase):
    def test_inspect_preserves_bom_crlf_trailing_and_hash(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "notes.md"
            raw = b"\xef\xbb\xbf# TOC\r\n\r\n[TOC]\r\n"
            path.write_bytes(raw)
            document = inspect_markdown_path(path)
            self.assertTrue(document.exists)
            self.assertTrue(document.has_bom)
            self.assertEqual(document.newline, "\r\n")
            self.assertTrue(document.ends_with_newline)
            self.assertEqual(document.baseline_sha256, hashlib.sha256(raw).hexdigest())
            self.assertEqual(document.text, "# TOC\r\n\r\n[TOC]\r\n")

    def test_rejects_bad_path_encoding_nul_and_size(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            relative = "notes.md"
            with self.assertRaises(MarkdownPathError):
                inspect_markdown_path(relative)
            wrong_suffix = root / "notes.txt"
            wrong_suffix.write_text("x", encoding="utf-8")
            with self.assertRaises(MarkdownPathError):
                inspect_markdown_path(wrong_suffix)
            with self.assertRaises(MarkdownPathError):
                inspect_markdown_path(r"\\server\share\notes.md")
            invalid = root / "invalid.md"
            invalid.write_bytes(b"\xff")
            with self.assertRaises(MarkdownContentError):
                inspect_markdown_path(invalid)
            nul = root / "nul.md"
            nul.write_bytes(b"a\x00b")
            with self.assertRaises(MarkdownContentError):
                inspect_markdown_path(nul)
            huge = root / "huge.md"
            huge.write_bytes(b"x" * (MAX_MARKDOWN_BYTES + 1))
            with self.assertRaises(MarkdownContentError):
                inspect_markdown_path(huge)

    def test_new_file_requires_confirmed_initial_text(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "new.md"
            with self.assertRaises(MarkdownContentError):
                inspect_markdown_path(path, allow_create=True)
            document = inspect_markdown_path(path, allow_create=True, initial_text="# TOC\n\n[TOC]\n")
            self.assertFalse(path.exists())
            self.assertFalse(document.exists)
            self.assertEqual(document.path, path)

    def test_readback_hash_helper(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "notes.md"
            raw = b"# Notes\r\n"
            path.write_bytes(raw)
            expected = markdown_bytes_sha256(raw)
            self.assertEqual(readback_markdown(path, expected_sha256=expected).baseline_sha256, expected)
            with self.assertRaises(MarkdownHashMismatch):
                readback_markdown(path, expected_sha256="0" * 64)
            with self.assertRaises(MarkdownHashMismatch):
                readback_markdown(path, expected_sha256="not-a-hash")

    def test_rejects_symlink_when_supported(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            actual = root / "actual.md"
            actual.write_text("x", encoding="utf-8")
            link = root / "link.md"
            try:
                link.symlink_to(actual)
            except (OSError, NotImplementedError):
                self.skipTest("symlink creation is not available")
            with self.assertRaises(MarkdownPathError):
                inspect_markdown_path(link)


class InferenceAndRenderingTests(unittest.TestCase):
    def test_infers_builtin_style_and_field_spacing(self) -> None:
        algorithms = infer_summary_schema(get_builtin_template("algorithms-v1"))
        self.assertTrue(algorithms.stable)
        self.assertGreaterEqual(algorithms.confidence, 0.75)
        self.assertEqual(algorithms.schema["category_heading_level"], 1)
        self.assertEqual(algorithms.schema["entry_heading_level"], 2)
        self.assertEqual(algorithms.schema["fields"][0]["key"], "source")

        tricks = infer_summary_schema(get_builtin_template("tricks-v1"))
        self.assertTrue(tricks.stable)
        self.assertEqual(tricks.schema["blank_lines_between_fields"], 1)
        self.assertEqual([field["key"] for field in tricks.schema["fields"]], [
            "source", "trigger", "conclusion", "implementation", "pitfalls"
        ])

    def test_low_information_file_returns_unstable_fallback(self) -> None:
        inferred = infer_summary_schema("# Notes\n\nplain text\n")
        self.assertFalse(inferred.stable)
        self.assertLess(inferred.confidence, 0.75)
        self.assertTrue(inferred.warnings)

    def test_render_respects_schema_order_and_rejects_html_or_missing_required(self) -> None:
        schema = get_builtin_schema("algorithms-v1")
        rendered = render_structured_entry(algorithm_entry(), schema)
        self.assertTrue(rendered.startswith("## 树上路径分解\n\n- Source:"))
        self.assertLess(rendered.index("- Model:"), rendered.index("- Complexity:"))
        missing = algorithm_entry()
        missing["fields"] = dict(missing["fields"], model="")
        with self.assertRaises(EntryValidationError):
            render_structured_entry(missing, schema)
        html = algorithm_entry()
        html["fields"] = dict(html["fields"], model="<script>alert(1)</script>")
        with self.assertRaises(EntryValidationError):
            render_structured_entry(html, schema)

    def test_subheading_layout(self) -> None:
        schema = get_builtin_schema("algorithms-v1")
        schema["fields"] = [dict(schema["fields"][1], layout="subheading")]
        rendered = render_structured_entry(
            {"topic": "图论", "title": "条目", "confidence": 1, "fields": {"model": "第一行\n第二行"}},
            schema,
        )
        self.assertIn("### Model\n\n第一行\n第二行", rendered)

    def test_user_edited_rendered_entry_is_parsed_and_refreshed(self) -> None:
        schema = get_builtin_schema("algorithms-v1")
        rendered = render_structured_entry(algorithm_entry(), schema)
        edited = rendered.replace("把路径拆成若干区间。", "把路径拆成若干互不重叠的区间。")
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "notes.md"
            path.write_text("# 图论\n", encoding="utf-8")
            result = build_markdown_candidate_from_rendered(
                inspect_markdown_path(path),
                schema,
                topic="图论",
                rendered_markdown=edited,
                aliases=["路径分解"],
                confidence=0.91,
            )
            self.assertIn("互不重叠", result.candidate_bytes.decode("utf-8"))
            self.assertEqual(result.rendered_entry.replace("\r\n", "\n"), edited)

    def test_user_edited_rendered_entry_rejects_extra_entry_or_field_reorder(self) -> None:
        schema = get_builtin_schema("algorithms-v1")
        rendered = render_structured_entry(algorithm_entry(), schema)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "notes.md"
            path.write_text("# 图论\n", encoding="utf-8")
            document = inspect_markdown_path(path)
            with self.assertRaises(EntryValidationError):
                build_markdown_candidate_from_rendered(
                    document,
                    schema,
                    topic="图论",
                    rendered_markdown=rendered + "\n\n## 注入条目\n",
                )
            reordered = rendered.replace(
                "- Model: 把路径拆成若干区间。\n- Invariant / correctness: 每条边恰好属于一个区间。",
                "- Invariant / correctness: 每条边恰好属于一个区间。\n- Model: 把路径拆成若干区间。",
            )
            with self.assertRaises(EntryValidationError):
                build_markdown_candidate_from_rendered(
                    document, schema, topic="图论", rendered_markdown=reordered
                )
            unknown = rendered.replace("- Model:", "- Unknown model:")
            with self.assertRaises(EntryValidationError):
                parse_rendered_entry(unknown, schema, topic="图论")

    def test_user_edited_rendered_entry_is_limited_to_64_kib(self) -> None:
        schema = get_builtin_schema("algorithms-v1")
        rendered = render_structured_entry(algorithm_entry(), schema)
        huge = rendered.replace("把路径拆成若干区间。", "x" * (64 * 1024))
        with self.assertRaises(EntryValidationError):
            parse_rendered_entry(huge, schema, topic="图论")

    def test_nested_bullet_with_colon_remains_field_content(self) -> None:
        schema = get_builtin_schema("algorithms-v1")
        rendered = render_structured_entry(algorithm_entry(), schema)
        rendered = rendered.replace(
            "- Pitfalls: 固定端点是否重复计入。",
            "- Pitfalls: 固定端点是否重复计入。\n  - 边界: 空路径单独处理。",
        )
        parsed = parse_rendered_entry(rendered, schema, topic="图论", confidence=0.91)
        self.assertIn("- 边界: 空路径单独处理。", parsed["fields"]["pitfalls"])


class CandidateTests(unittest.TestCase):
    def _document(self, root: Path, text: str, *, bom: bool = False):
        path = root / "notes.md"
        path.write_bytes((b"\xef\xbb\xbf" if bom else b"") + text.encode("utf-8"))
        return inspect_markdown_path(path)

    def test_insert_existing_topic_preserves_bom_crlf_and_trailing_newline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            document = self._document(
                Path(tmp), "# TOC\r\n\r\n[TOC]\r\n\r\n# 图论\r\n\r\n## 旧条目\r\n\r\n- Model: old\r\n", bom=True
            )
            result = build_markdown_candidate(document, get_builtin_schema("algorithms-v1"), algorithm_entry())
            self.assertTrue(result.candidate_bytes.startswith(b"\xef\xbb\xbf"))
            decoded = result.candidate_bytes[3:].decode("utf-8")
            self.assertIn("\r\n## 树上路径分解\r\n", decoded)
            self.assertTrue(decoded.endswith("\r\n"))
            self.assertIn("+## 树上路径分解", result.unified_diff)
            self.assertEqual(result.action, "new")
            self.assertTrue(result.target_existed)
            self.assertEqual(result.baseline_sha256, document.baseline_sha256)

    def test_insert_new_topic_preserves_no_trailing_newline(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            document = self._document(Path(tmp), "# TOC\n\n[TOC]")
            result = build_markdown_candidate(document, get_builtin_schema("algorithms-v1"), algorithm_entry())
            text = result.candidate_bytes.decode("utf-8")
            self.assertIn("# 图论\n\n## 树上路径分解", text)
            self.assertFalse(text.endswith("\n"))

    def test_only_exact_source_defaults_to_merge(self) -> None:
        schema = get_builtin_schema("algorithms-v1")
        with tempfile.TemporaryDirectory() as tmp:
            text = get_builtin_template("algorithms-v1")
            document = self._document(Path(tmp), text)
            same_title = algorithm_entry(title="树上路径分解示例")
            result = build_markdown_candidate(document, schema, same_title)
            self.assertEqual(result.action, "new")
            self.assertEqual(result.candidate_bytes.decode("utf-8").count("## 树上路径分解示例"), 2)

            source_entry = algorithm_entry(title="完全不同标题")
            source_entry["fields"] = dict(source_entry["fields"], source="`OJ000A`")
            diagnosis = diagnose_duplicates(document, schema, source_entry)
            self.assertEqual(diagnosis.exact[0].kind, "source")
            merged = build_markdown_candidate(document, schema, source_entry)
            self.assertEqual(merged.action, "merge")

    def test_exact_source_lookup_accepts_problem_id_markdown_link(self) -> None:
        schema = get_builtin_schema("algorithms-v1")
        with tempfile.TemporaryDirectory() as tmp:
            document = self._document(
                Path(tmp),
                "# 图论\n\n## 旧条目\n\n- Source: [CF1A](https://codeforces.com/problemset/problem/1/A)\n- Model: old\n",
            )
            matches = find_exact_source_entries(document, schema, "CF1A")
            self.assertEqual(len(matches), 1)
            self.assertEqual(matches[0].title, "旧条目")
            self.assertIn("Model: old", matches[0].markdown)

    def test_merge_keeps_separator_before_following_entry(self) -> None:
        schema = get_builtin_schema("algorithms-v1")
        with tempfile.TemporaryDirectory() as tmp:
            document = self._document(
                Path(tmp),
                "# 图论\n\n## 树上路径分解\n\n- Source: `CF1A`\n\n## 后续条目\n\n- Model: keep\n",
            )
            result = build_markdown_candidate(document, schema, algorithm_entry())
            text = result.candidate_bytes.decode("utf-8")
            self.assertIn("- Pitfalls: 固定端点是否重复计入。\n\n## 后续条目", text)

    def test_multiple_exact_anchors_block_ambiguous_merge(self) -> None:
        schema = get_builtin_schema("algorithms-v1")
        with tempfile.TemporaryDirectory() as tmp:
            document = self._document(
                Path(tmp),
                "# 图论\n\n## 旧一\n\n- Source: `CF1A`\n\n## 旧二\n\n- Source: `CF1A`\n",
            )
            with self.assertRaises(EntryValidationError):
                build_markdown_candidate(document, schema, algorithm_entry())

    def test_fuzzy_duplicate_defaults_to_new_but_explicit_merge_remains_compatible(self) -> None:
        schema = get_builtin_schema("algorithms-v1")
        with tempfile.TemporaryDirectory() as tmp:
            document = self._document(
                Path(tmp), "# 图论\n\n## 树上路径分解方法\n\n- Model: old\n"
            )
            entry = algorithm_entry()
            entry["fields"] = dict(entry["fields"], source="")
            created = build_markdown_candidate(document, schema, entry)
            self.assertEqual(created.action, "new")
            merged = build_markdown_candidate(
                document, schema, entry, duplicate_action="merge", merge_title="树上路径分解方法"
            )
            self.assertEqual(merged.action, "merge")

    def test_low_confidence_is_not_applicable(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            document = self._document(Path(tmp), "# 图论\n")
            with self.assertRaises(EntryValidationError):
                build_markdown_candidate(
                    document, get_builtin_schema("algorithms-v1"), algorithm_entry(confidence=0.74)
                )


if __name__ == "__main__":
    unittest.main()
