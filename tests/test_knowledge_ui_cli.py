from __future__ import annotations

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools.acm_agent.cli import main
from tools.acm_agent.service import AcmService


REPO_ROOT = Path(__file__).resolve().parents[1]


class KnowledgeDashboardStaticTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.html = (REPO_ROOT / "tools/acm_agent/web_static/index.html").read_text(encoding="utf-8")
        cls.script = (REPO_ROOT / "tools/acm_agent/web_static/app.js").read_text(encoding="utf-8")
        cls.styles = (REPO_ROOT / "tools/acm_agent/web_static/styles.css").read_text(encoding="utf-8")

    def test_close_summary_is_explicitly_opt_in(self) -> None:
        self.assertIn('id="knowledge-enabled"', self.html)
        self.assertIn('type="checkbox"', self.html)
        self.assertIn('id="knowledge-options" class="knowledge-options span-2 hidden"', self.html)
        self.assertIn("默认关闭", self.html)
        self.assertIn("题面、最终源码、冻结标签", self.html)
        self.assertIn("不会发送账号、API Key、本机路径", self.html)
        self.assertIn("knowledgeToggle.disabled = !status.api_key_detected", self.script)
        self.assertIn('name="summary_model"', self.html)
        self.assertIn('name="summary_thinking"', self.html)
        self.assertIn('name="summary_reasoning_effort"', self.html)

    def test_close_finishes_before_starting_summary_job(self) -> None:
        start = self.script.index('$("#close-form").addEventListener')
        block = self.script[start : start + 2800]
        self.assertLess(block.index('api("/api/sessions/close"'), block.index("previewKnowledgeSummary(data.attempt_id"))
        self.assertIn("const knowledgeEpoch = ++state.knowledgeEpoch", block)
        self.assertIn("const button = $(\"button[type=submit]\", form)", block)

    def test_preview_requires_refresh_after_edit_and_is_safe_text(self) -> None:
        self.assertIn('id="knowledge-markdown-editor"', self.html)
        self.assertNotIn('id="knowledge-diff"', self.html)
        self.assertNotIn("Unified diff", self.html)
        self.assertIn('id="knowledge-apply"', self.html)
        self.assertIn("state.knowledgeProposalDirty = true", self.script)
        self.assertIn('$("#knowledge-apply").disabled = true', self.script)
        self.assertIn("renderSafeKnowledgeMarkdown(preview, markdown)", self.script)
        self.assertIn("node.textContent = text", self.script)
        self.assertIn("renderAssistantMath(container)", self.script)
        self.assertNotIn('$("#knowledge-rendered-preview").innerHTML', self.script)

    def test_saved_targets_use_stored_schema_without_preset_choices(self) -> None:
        self.assertIn('<option value="stored">使用目标已保存 schema</option>', self.html)
        self.assertNotIn('<option value="algorithms-v1">', self.html)
        self.assertNotIn('<option value="tricks-v1">', self.html)
        target_change = self.script[self.script.index('$("#knowledge-target").addEventListener("change"') :]
        self.assertIn('$("#knowledge-schema-mode").value = "stored"', target_change[:800])

    def test_duplicate_strategy_control_and_payload_are_removed(self) -> None:
        self.assertNotIn("knowledge-duplicate-action", self.html)
        self.assertNotIn("knowledge-duplicate-action", self.script)
        self.assertNotIn("duplicate_action:", self.script)
        warning_start = self.script.index("function knowledgeWarningItems")
        warning_block = self.script[warning_start : warning_start + 3000]
        self.assertIn('duplicateKind === "exact_source"', warning_block)
        self.assertIn('duplicateKind === "similar"', warning_block)
        self.assertIn('warningIsBlocking', warning_block)

    def test_recommendation_actions_align_to_card_bottom(self) -> None:
        self.assertIn(".recommend-card { position: relative; display: flex; flex-direction: column;", self.styles)
        self.assertIn(".recommend-card > .card-actions { margin-top: auto; }", self.styles)

    def test_target_inspection_precedes_registration_and_exposes_schema(self) -> None:
        start = self.script.index("async function registerKnowledgeTarget")
        block = self.script[start : start + 3300]
        self.assertLess(block.index('api("/api/knowledge/targets/inspect"'), block.index('api("/api/knowledge/targets"'))
        self.assertIn("state.knowledgeTargetInspection", block)
        self.assertIn("JSON.stringify(inspected.schema, null, 2)", block)
        self.assertIn('button.textContent = "确认保存目标"', block)
        self.assertIn("目标文件尚未修改", block)

    def test_jobs_are_epoch_guarded_and_cancel_never_calls_apply(self) -> None:
        wait_start = self.script.index("async function waitForKnowledgeJob")
        wait_block = self.script[wait_start : wait_start + 1500]
        self.assertGreaterEqual(wait_block.count("epoch !== state.knowledgeEpoch"), 2)
        cancel_start = self.script.index("function cancelKnowledgeProposal")
        cancel_block = self.script[cancel_start : cancel_start + 600]
        self.assertIn("state.knowledgeEpoch += 1", cancel_block)
        self.assertNotIn("/apply", cancel_block)
        self.assertIn("没有写入 Markdown 文件", cancel_block)


class KnowledgeCliSurfaceTests(unittest.TestCase):
    def run_json(self, root: Path, *args: str) -> dict:
        output = io.StringIO()
        with redirect_stdout(output):
            code = main([*args, "--json"], root=root)
        self.assertEqual(code, 0, output.getvalue())
        return json.loads(output.getvalue())

    def test_templates_targets_and_inspect_forward_arguments(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            with mock.patch.object(
                AcmService,
                "knowledge_templates",
                create=True,
                return_value={"templates": [{"preset": "algorithms-v1", "name": "Algorithms"}]},
            ):
                payload = self.run_json(root, "knowledge", "templates")
            self.assertEqual(payload["templates"][0]["preset"], "algorithms-v1")

            with mock.patch.object(
                AcmService,
                "knowledge_targets",
                create=True,
                return_value={"targets": []},
            ):
                self.assertEqual(self.run_json(root, "knowledge", "targets")["targets"], [])

            with mock.patch.object(
                AcmService,
                "knowledge_target_inspect",
                create=True,
                return_value={"normalized_path": str(root / "notes.md")},
            ) as inspect:
                self.run_json(root, "knowledge", "inspect", str(root / "notes.md"), "--allow-create")
            self.assertTrue(inspect.call_args.kwargs["allow_create"])
            self.assertEqual(inspect.call_args.kwargs["schema_mode"], "auto")

    def test_summary_ai_settings_are_independent(self) -> None:
        with tempfile.TemporaryDirectory() as temp, mock.patch.object(
            AcmService,
            "ai_settings",
            return_value={"ok": True, "settings": {}},
        ) as settings:
            self.run_json(
                Path(temp),
                "ai", "settings",
                "--summary-model", "deepseek-v4-pro",
                "--summary-thinking",
                "--summary-reasoning-effort", "max",
            )
        self.assertEqual(settings.call_args.kwargs["summary_model"], "deepseek-v4-pro")
        self.assertTrue(settings.call_args.kwargs["summary_thinking"])
        self.assertEqual(settings.call_args.kwargs["summary_reasoning_effort"], "max")

    def test_preview_refresh_apply_and_revert_are_revision_guarded(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            edited = root / "entry.md"
            edited.write_text("## 单调栈\n", encoding="utf-8")
            with mock.patch.object(
                AcmService,
                "knowledge_preview",
                create=True,
                return_value={"proposal_id": "p1", "revision": 1, "entry_markdown": "## preview"},
            ) as preview:
                self.run_json(root, "knowledge", "preview", "7", "target-1", "--schema-mode", "stored")
            self.assertEqual(preview.call_args.kwargs["attempt_id"], 7)
            self.assertEqual(preview.call_args.kwargs["target_id"], "target-1")

            with mock.patch.object(
                AcmService,
                "knowledge_refresh",
                create=True,
                return_value={"proposal_id": "p1", "revision": 2, "entry_markdown": "## refreshed"},
            ) as refresh:
                self.run_json(root, "knowledge", "refresh", "p1", "--entry-file", str(edited), "--expected-revision", "1")
            self.assertEqual(refresh.call_args.kwargs["entry_markdown"], "## 单调栈\n")
            self.assertEqual(refresh.call_args.kwargs["expected_revision"], 1)

            with mock.patch.object(
                AcmService,
                "knowledge_apply",
                create=True,
                return_value={"proposal_id": "p1", "status": "applied"},
            ) as apply:
                self.run_json(root, "knowledge", "apply", "p1", "--expected-revision", "2")
            apply.assert_called_once_with("p1", expected_revision=2)

            with mock.patch.object(
                AcmService,
                "knowledge_revert",
                create=True,
                return_value={"proposal_id": "p1", "status": "reverted"},
            ) as revert:
                self.run_json(root, "knowledge", "revert", "p1", "--expected-revision", "3")
            revert.assert_called_once_with("p1", expected_revision=3)


if __name__ == "__main__":
    unittest.main()
