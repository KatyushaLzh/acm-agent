from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "tools" / "acm_agent" / "web_static"


class WebFrontendConcurrencyContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.core = (STATIC / "core.js").read_text(encoding="utf-8")
        cls.today = (STATIC / "view_today.js").read_text(encoding="utf-8")
        cls.ai = (STATIC / "view_ai.js").read_text(encoding="utf-8")
        cls.workbench = (STATIC / "view_workbench.js").read_text(encoding="utf-8")
        cls.events_today = (STATIC / "events_today.js").read_text(encoding="utf-8")
        cls.index = (STATIC / "index.html").read_text(encoding="utf-8")
        cls.plans = (STATIC / "view_plans.js").read_text(encoding="utf-8")
        cls.plan_ai_import = (STATIC / "view_plan_ai_import.js").read_text(encoding="utf-8")
        cls.events_plans = (STATIC / "events_plans.js").read_text(encoding="utf-8")

    def test_ai_stream_requires_explicit_done_terminal_event(self) -> None:
        self.assertIn('if (done) throw new Error("DeepSeek 流式响应在完成事件前中断")', self.ai)
        self.assertIn("if (done && buffer.trim())", self.ai)
        self.assertIn("return completed", self.ai)

    def test_plan_selection_drops_stale_responses(self) -> None:
        self.assertIn("planSelectionEpoch", self.core)
        self.assertIn("planSelectionController", self.core)
        self.assertIn("state.planSelectionController.abort()", self.plans)
        self.assertIn("state.planSelectionEpoch !== epoch", self.plans)
        self.assertIn("state.selectedPlanId !== planId", self.plans)

    def test_plan_save_does_not_steal_a_newer_selection(self) -> None:
        self.assertIn("if (state.selectedPlanId !== planId)", self.plans)
        self.assertIn("当前题单保持不变", self.plans)

    def test_deterministic_and_ai_recommendations_share_one_epoch(self) -> None:
        self.assertIn("recommendationEpoch", self.core)
        self.assertIn("recommendationController", self.core)
        for source in (self.today, self.ai):
            self.assertIn("state.recommendationController.abort()", source)
            self.assertIn("++state.recommendationEpoch", source)
            self.assertIn("state.recommendationEpoch !== epoch", source)

    def test_api_can_abort_obsolete_fetches_and_job_polling(self) -> None:
        self.assertIn("request.signal = options.signal", self.core)
        self.assertIn("shouldCancel", self.core)
        self.assertIn('"canceled", "cancelled"', self.core)
        self.assertIn('method: "DELETE"', self.core)
        self.assertIn("await cancelQueuedJob(jobId)", self.core)

    def test_compiled_only_verification_is_rendered_as_inconclusive(self) -> None:
        self.assertIn('status === "inconclusive"', self.core)
        self.assertIn('badge.textContent = inconclusive ? "证据不足"', self.core)
        self.assertIn("仅编译成功；没有样例或对拍证据", self.core)

    def test_ai_recommendation_modes_have_independent_controls(self) -> None:
        self.assertIn('id="ai-gap-fill-button"', self.index)
        self.assertIn('id="ai-specialization-button"', self.index)
        self.assertNotIn('id="ai-recommend-button"', self.index)
        self.assertIn('requestAiRecommendations(event.currentTarget, "gap_fill")', self.events_today)
        self.assertIn('requestAiRecommendations(event.currentTarget, "specialization")', self.events_today)
        self.assertIn("ai_mode: aiMode", self.ai)

    def test_ai_recommendation_privacy_and_explanations_are_visible(self) -> None:
        for excluded in ("handle", "UID", "submission ID", "raw JSON", "源码", "本机路径", "运行 token"):
            self.assertIn(excluded, self.index)
        self.assertIn("ai.focus_topics", self.today)
        self.assertIn("ai.submission_coverage", self.today)
        self.assertIn("ai.taxonomy_version", self.today)
        self.assertIn("item.focus_topic", self.today)
        self.assertIn("item.ai_reason", self.today)
        self.assertIn("item.training_focus", self.today)
        self.assertIn('typeof fallback === "string" ? fallback : fallback?.message', self.ai)

    def test_validated_setup_enters_dashboard_and_tracks_background_sync(self) -> None:
        self.assertIn("正在验证账号", self.workbench)
        self.assertIn("result?.initial_sync_job", self.workbench)
        self.assertIn("同步进行中…", self.workbench)
        self.assertIn("acm-initial-sync-job", self.workbench)
        self.assertIn("resumeInitialSyncJob", self.workbench)
        self.assertIn('id="settings-save-button"', self.index)
        self.assertIn("离线配置，请稍后同步平台状态", self.workbench)

    def test_sync_progress_is_persistent_structured_and_accessible(self) -> None:
        self.assertIn('id="sync-progress"', self.index)
        self.assertIn('role="status" aria-live="polite" aria-atomic="true"', self.index)
        self.assertIn('id="sync-progress-bar"', self.index)
        self.assertIn("onPoll(job, status)", self.workbench)
        self.assertIn("renderSyncProgress(job, status)", self.workbench)
        for field in ("progress.phase", "progress.platform", "progress.step", "progress.total", "progress.completed", "progress.failed", "progress.started_at", "progress.usable"):
            self.assertIn(field, self.workbench)
        self.assertIn("sessionStorage.setItem(INITIAL_SYNC_JOB_KEY, jobId)", self.workbench)
        self.assertIn("sessionStorage.getItem(INITIAL_SYNC_JOB_KEY)", self.workbench)
        self.assertIn("jobIdOf(state.bootstrap?.active_sync_job)", self.workbench)
        self.assertIn("activeJobId || sessionStorage.getItem", self.workbench)
        self.assertIn('bar.removeAttribute("value")', self.workbench)
        self.assertIn("bar.value = Math.min(total", self.workbench)

    def test_sync_business_outcome_is_not_inferred_from_job_success(self) -> None:
        for status in ('status === "fresh"', 'status === "partial"', 'status === "failed"'):
            self.assertIn(status, self.workbench)
        self.assertIn("result?.ok === false", self.workbench)
        self.assertIn("同步部分完成", self.workbench)
        self.assertIn("未把后台任务结束等同于业务同步成功", self.workbench)
        self.assertIn("await refreshAfterSync()", self.workbench)
        self.assertIn('["partial", "failed"].includes(attemptStatus)', self.core)
        self.assertIn("freshnessBadge(displayStatus)", self.core)

    def test_ai_plan_import_has_two_modes_and_explicit_privacy_boundary(self) -> None:
        self.assertIn('id="ai-plan-import-button"', self.index)
        self.assertIn('value="organize"', self.index)
        self.assertIn('value="generate"', self.index)
        self.assertIn('id="ai-plan-task-count" type="number" min="1" max="30" value="12"', self.index)
        self.assertIn('id="ai-plan-include-completed"', self.index)
        self.assertIn("允许纳入 AC 或 Skip 题目", self.index)
        self.assertNotIn("允许纳入 AC、Skip 或进行中的题目", self.index)
        self.assertIn("进行中的题目始终在本地排除", self.index)
        for excluded in ("账号", "UID", "提交详情", "源码", "聊天", "现有题单", "用户标签覆盖", "本机路径", "API Key", "运行 token"):
            self.assertIn(excluded, self.index)
        for included in ("目标文本", "题数", "已接受", "需排除", "剩余数量"):
            self.assertIn(included, self.index)
        for forbidden in ("最多 120 个公共候选摘要", "粗粒度完成状态"):
            self.assertNotIn(forbidden, self.index)

    def test_ai_plan_import_uses_isolated_job_lifecycle(self) -> None:
        self.assertIn("aiPlanImportEpoch", self.core)
        self.assertIn("aiPlanImportController", self.core)
        self.assertIn("aiPlanValidationEpoch", self.core)
        self.assertIn("aiPlanValidationController", self.core)
        self.assertIn('api("/api/jobs/ai/plans/preview"', self.plan_ai_import)
        self.assertIn("state.aiPlanImportEpoch !== epoch", self.plan_ai_import)
        self.assertIn("await cancelQueuedJob(jobId)", self.plan_ai_import)
        self.assertIn('error.code === "missing_api_key"', self.plan_ai_import)
        self.assertIn('navigate("settings")', self.plan_ai_import)
        self.assertIn('id="ai-plan-progress"', self.index)
        self.assertIn('aria-live="polite"', self.index)
        self.assertIn('progress?.phase === "selecting"', self.plan_ai_import)
        for progress_field in ("progress?.round", "progress?.total_rounds", "progress?.accepted_count", "progress?.requested_count", "progress?.message"):
            self.assertIn(progress_field, self.plan_ai_import)
        self.assertIn('`第${round}/${totalRounds}轮`', self.plan_ai_import)
        self.assertIn('`${accepted}/${requested}题`', self.plan_ai_import)

    def test_ai_plan_draft_repreviews_and_reuses_guarded_import(self) -> None:
        self.assertIn('api("/api/plans/preview"', self.plan_ai_import)
        self.assertIn("state.aiPlanValidationTimer", self.plan_ai_import)
        self.assertIn("submitPlanImport({", self.plan_ai_import)
        self.assertIn("expected_revision: preview.current_revision", self.plans)
        self.assertIn("confirm_replace: confirmReplace", self.plans)
        self.assertIn("题单 revision 已变化，草稿仍保留", self.plan_ai_import)
        for result_field in ("ai.thinking", "ai.reasoning_effort", "ai.rounds", "result.ai?.requested_count", "ai.accepted_count", "ai.complete"):
            self.assertIn(result_field, self.plan_ai_import)
        self.assertIn("generatedDraftRequirement", self.plan_ai_import)
        self.assertIn("requirement?.missing", self.plan_ai_import)
        self.assertIn("仍缺", self.plan_ai_import)
        self.assertIn("已过滤", self.plan_ai_import)
        for control in ("data-ai-plan-field", "data-ai-stage-field", "data-ai-task-field", "stage_index", "level", "tags"):
            self.assertIn(control, self.plan_ai_import)
        self.assertIn("bindAiPlanImportEvents()", self.events_plans)


if __name__ == "__main__":
    unittest.main()
