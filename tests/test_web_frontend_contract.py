from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
STATIC = ROOT / "tools" / "acm_agent" / "web_static"


class WebFrontendConcurrencyContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.core = (STATIC / "core.js").read_text(encoding="utf-8")
        cls.app = (STATIC / "app.js").read_text(encoding="utf-8")
        cls.today = (STATIC / "view_today.js").read_text(encoding="utf-8")
        cls.ai = (STATIC / "view_ai.js").read_text(encoding="utf-8")
        cls.review = (STATIC / "view_review.js").read_text(encoding="utf-8")
        cls.workbench = (STATIC / "view_workbench.js").read_text(encoding="utf-8")
        cls.events_today = (STATIC / "events_today.js").read_text(encoding="utf-8")
        cls.events_ai = (STATIC / "events_ai.js").read_text(encoding="utf-8")
        cls.index = (STATIC / "index.html").read_text(encoding="utf-8")
        cls.plans = (STATIC / "view_plans.js").read_text(encoding="utf-8")
        cls.plan_ai_import = (STATIC / "view_plan_ai_import.js").read_text(encoding="utf-8")
        cls.events_plans = (STATIC / "events_plans.js").read_text(encoding="utf-8")
        cls.model_controls = (STATIC / "ai_model_controls.js").read_text(encoding="utf-8")
        cls.styles = (STATIC / "styles.css").read_text(encoding="utf-8")

    def test_ai_stream_requires_explicit_done_terminal_event(self) -> None:
        self.assertIn('if (done) throw new Error("模型流式响应在完成事件前中断")', self.ai)
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

    def test_luogu_tagless_count_is_visible_without_partial_status(self) -> None:
        self.assertIn("item.tagless", self.core)
        self.assertIn("无公开标签（tagless）", self.core)

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

    def test_stage2_ui_only_exposes_simplified_model_connections(self) -> None:
        self.assertIn('id="ai-connection-form"', self.index)
        for field in ('name="display_name"', 'name="base_url"', 'name="api_key"'):
            self.assertIn(field, self.index)
        self.assertIn('id="ai-secure-store-status"', self.index)
        self.assertIn('id="ai-secure-store-message"', self.index)
        self.assertIn("Windows DPAPI、macOS Keychain 或 Linux Secret Service", self.index)
        self.assertIn('<details class="environment-credential-help">', self.index)
        self.assertIn("临时环境变量回退", self.index)
        self.assertIn('export DEEPSEEK_API_KEY="你的 API Key"', self.index)
        self.assertIn("./start-acm-web.sh", self.index)
        self.assertIn("此回退不会持久化密钥", self.index)
        self.assertNotIn("Unix / macOS 接入方法", self.index)
        for backend in ("dpapi", "keychain", "secret_service", "unavailable"):
            self.assertIn(f'{backend}:', self.ai)
        self.assertIn("renderSecureStoreStatus(status)", self.ai)
        self.assertIn("secureStore.error_code", self.ai)
        self.assertIn("base_url.readOnly = Boolean(connection.builtin)", self.ai)
        self.assertIn("内置 DeepSeek 固定使用官方 Base URL", self.ai)
        for retired in (
            "模型与能力 JSON", "凭据槽位", "自定义 Header", "环境变量名（可选，仅保存名称）",
            'id="ai-provider-list"', 'id="ai-profile-list"', 'id="ai-named-credential-form"',
        ):
            self.assertNotIn(retired, self.index)
        for endpoint in (
            "/api/ai/connections", "/api/ai/connections/refresh", "/api/ai/connections/delete",
        ):
            self.assertIn(endpoint, self.ai)

    def test_user_facing_ai_profiles_share_model_and_reasoning_controls(self) -> None:
        for profile_id in (
            "recommendation", "plan_organize", "plan_generate", "coaching", "summary",
        ):
            self.assertIn(f'data-ai-model-picker="{profile_id}"', self.index)
        self.assertNotIn('data-ai-model-picker="patch"', self.index)
        for profile_id in ("recommendation", "coaching", "summary"):
            self.assertIn(f'aiRequestSelection("{profile_id}")', self.ai)
        self.assertIn('ensureSelectionVerified("patch", selection)', self.ai)
        self.assertNotIn('aiRequestSelection("patch")', self.ai)
        self.assertIn('profileId = mode === "generate" ? "plan_generate" : "plan_organize"', self.plan_ai_import)
        self.assertIn("aiRequestSelection(profileId)", self.plan_ai_import)
        for value in ("auto", "off", "low", "medium", "high"):
            self.assertIn(f'{value}:', self.model_controls)
        self.assertIn('auto: "Provider 默认"', self.model_controls)
        self.assertIn('off: "关闭"', self.model_controls)
        self.assertIn('api("/api/ai/profiles"', self.model_controls)
        self.assertIn('api("/api/jobs/ai/models/verify"', self.model_controls)
        self.assertIn('result?.ok !== true', self.model_controls)
        self.assertIn('模型能力验证未通过', self.model_controls)
        self.assertIn('/switch`', self.model_controls)

    def test_stage3_governance_and_cost_audit_are_visible(self) -> None:
        for marker in (
            'id="ai-governance-summary"', 'id="ai-route-policy-table"',
            'id="ai-policy-form"', 'id="ai-policy-dirty-state"',
            'id="ai-policy-save"', 'id="ai-policy-reset"',
            'id="ai-cost-summary"', 'id="ai-cost-runs"',
        ):
            self.assertIn(marker, self.index)
        self.assertNotIn('id="ai-policy-json"', self.index)
        self.assertIn("查看各项详情", self.index)
        self.assertNotIn('id="ai-route-policy-details" class="route-policy-details" open', self.index)
        for field in ("daily_cny", "monthly_cny"):
            self.assertIn(f'name="{field}"', self.index)
        for endpoint in ("/api/ai/policy", "/api/ai/costs/reprice"):
            self.assertIn(endpoint, self.ai)
        self.assertIn('/api/ai/costs', self.review)
        for marker in (
            'const POLICY_PROFILES = ["recommendation", "plan_organize", "plan_generate", "coaching", "patch", "summary"]',
            "max_output_tokens", "request_timeout_seconds", "max_retries",
            "max_requests", "max_total_tokens", "routes.length > 3",
            'profileId === "coaching"', "beforeunload", "setCustomValidity",
            "data-fallback-add", "data-fallback-remove",
            "包含 transport retry、JSON repair 和 fallback",
            "整个任务跨重试和 fallback 共享的总时限",
        ):
            self.assertIn(marker, self.ai)
        self.assertIn("备用路由不能重复主路由或前序备用路由", self.ai)
        self.assertIn("每月上限不能小于每日上限", self.ai)
        for wording in ("DeepSeek 30 天估算费用（人民币）", "全模型 30 天 Token", "全模型缓存命中率"):
            self.assertIn(wording, self.model_controls)
        self.assertIn("savedBudget.max_validation_repairs", self.ai)
        self.assertNotIn('name="coaching_delivery_mode"', self.index)
        self.assertNotIn('id="ai-delivery-mode"', self.index)
        self.assertIn("formatCny(cost.known_estimated_cny || 0)", self.model_controls)
        self.assertIn("按 DeepSeek 官方人民币单价核算", self.model_controls)
        self.assertNotIn("USD_TO_CNY", self.model_controls)
        self.assertNotIn(': `$${Number(cost.known_estimated_usd || 0).toFixed(6)}`', self.model_controls)
        self.assertIn(': formatInteger(tokens.total_tokens_known)', self.model_controls)
        self.assertIn("费用仅统计 DeepSeek 官方连接；Token 与缓存统计覆盖所有已记录模型", self.index)
        self.assertIn("缺失遥测不会按 0 处理", self.index)
        self.assertIn("item.deepseek_cost", self.review)
        self.assertIn("已固定", self.ai)

    def test_stage3_governance_has_narrow_screen_labels(self) -> None:
        self.assertIn('data-label="预算"', self.ai)
        self.assertIn('class="policy-profile-summary"', self.ai)
        self.assertIn('class="policy-budget-section"', self.ai)
        self.assertIn("grid-template-columns: repeat(5, minmax(0, 1fr))", self.styles)
        self.assertIn('data-label="DeepSeek 估算费用（人民币）"', self.review)
        self.assertIn('content: attr(data-label)', self.styles)
        self.assertIn(".fallback-route-editor { grid-template-columns: 1fr; }", self.styles)

    def test_stage4_cache_and_reliability_controls_stay_off_primary_ui(self) -> None:
        for marker in (
            'id="ai-cache-summary"', 'id="ai-cache-policy"',
            'id="ai-cache-clear"', 'id="ai-cache-prune"',
            'id="recommend-force-refresh"', 'id="ai-plan-force-refresh"',
            'id="knowledge-force-refresh"', 'name="coaching_delivery_mode"',
        ):
            self.assertNotIn(marker, self.index)
        for wording in (
            "Provider leg 成功率", "Provider 有效产物率", "业务修复恢复率",
            "完整业务成功率", "降级可用率", "部分 / 不可用率",
        ):
            self.assertNotIn(wording, self.model_controls)
        self.assertNotIn('from "./view_cache.js"', self.app)
        self.assertNotIn("force_refresh:", self.ai)
        self.assertNotIn("force_refresh:", self.plan_ai_import)
        self.assertNotIn("max_validation_repairs", self.index)

    def test_summary_preview_toggle_can_open_before_summary_profile_is_ready(self) -> None:
        self.assertIn("const canConfigureSummary = readyConnections.length > 0", self.ai)
        self.assertIn("knowledgeToggle.disabled = !canConfigureSummary", self.ai)
        self.assertIn('if (!canConfigureSummary) {', self.ai)
        self.assertIn('$("#knowledge-options").classList.toggle("hidden", !event.currentTarget.checked)', self.events_ai)

    def test_low_summary_confidence_is_a_visible_non_blocking_warning(self) -> None:
        self.assertIn('warnings.push("模型置信度低，需人工核对")', self.ai)
        self.assertIn("confidence < 0.75", self.ai)
        self.assertIn('warningIsBlocking ? "error" : "warning"', self.ai)
        self.assertIn('warningIsBlocking = status === "preview"', self.ai)
        self.assertIn(".result-box.warning", self.styles)
        self.assertNotIn(
            "proposal.apply_blocked_reason || Number(proposal.confidence",
            self.ai,
        )
        self.assertIn(
            '$("#knowledge-apply").disabled = status !== "preview" || blocked',
            self.ai,
        )

    def test_ai_layout_clamps_long_connection_and_model_text(self) -> None:
        self.assertIn("body { margin: 0; min-width: 320px; max-width: 100%; overflow-x: hidden;", self.styles)
        self.assertIn(".ai-settings-panel { grid-column: span 2; overflow: hidden; }", self.styles)
        self.assertIn(".ai-feature-routing", self.styles)
        self.assertIn(".ai-connection-identity strong, .ai-connection-identity span", self.styles)
        self.assertIn(".secure-store-status { display: grid", self.styles)
        self.assertIn(".secure-store-status-heading strong", self.styles)
        self.assertIn("text-overflow: ellipsis", self.styles)

    def test_verify_localizes_degraded_sanitizer_and_stress_statuses(self) -> None:
        self.assertIn("当前编译器未提供 ASan/UBSan，已按普通模式继续", self.core)
        self.assertIn("输出超过安全上限；具体阶段见下方警告", self.core)
        self.assertIn('for (const warning of result.warnings || [])', self.core)

    def test_local_stress_pickers_send_current_problem_for_initial_directory(self) -> None:
        self.assertIn(
            'const VERIFY_FILE_PICKERS = new Set(["generator_file", "reference_file", "user_file"]);',
            self.core,
        )
        self.assertIn('? String($("#verify-form input[name=problem]")?.value || "").trim()', self.core)
        self.assertIn('const body = problem ? { kind, problem } : { kind };', self.core)


if __name__ == "__main__":
    unittest.main()
