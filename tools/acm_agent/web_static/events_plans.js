import { $, setBusy, state, toast } from "./core.js";
import {
  addTaskFromForm, applyTagPreview, clonePlan, confirmPlanImport,
  deleteSelectedPlan, downloadJson, downloadPlanTemplate, handleStageAction,
  handleTaskAction, importErrors, loadPlans, loadRevisions, mutatePlanField,
  mutateStageField, mutateTaskField, previewPlanFile, renderPlanEditor,
  restoreRevision, savePlan, selectPlan, stableKey, stagesOf, startTagPreview,
  toggleSelectedPlan,
} from "./view_plans.js";

function bindPlanEvents() {
  $("#plan-list").addEventListener("click", event => {
    const button = event.target.closest("[data-plan-id]");
    if (button) selectPlan(button.dataset.planId);
  });
  $("#plan-refresh-button").addEventListener("click", event => {
    const button = event.currentTarget;
    setBusy(button, true, "…");
    loadPlans({ forceDetail: true }).finally(() => setBusy(button, false));
  });
  $("#plan-import-button").addEventListener("click", () => $("#plan-file-input").click());
  $("#plan-file-input").addEventListener("change", event => {
    const file = event.currentTarget.files?.[0];
    if (file) previewPlanFile(file).catch(error => toast("文件读取失败", error.message, "error"));
    event.currentTarget.value = "";
  });
  $("#plan-template-button").addEventListener("click", event => downloadPlanTemplate(event.currentTarget));
  $("#plan-replace-confirm input").addEventListener("change", event => {
    $("#plan-import-confirm").disabled = !event.currentTarget.checked || importErrors(state.importPreview).length > 0;
  });
  $("#plan-import-confirm").addEventListener("click", event => confirmPlanImport(event.currentTarget));
  $("#plan-tags-apply").addEventListener("click", event => applyTagPreview(event.currentTarget));
  $("#plan-tags-dialog").addEventListener("close", () => {
    state.tagPreview = null;
    state.tagPreviewPlanId = "";
    state.tagPreviewRevision = null;
    state.tagPreviewOverrideRevision = null;
    state.tagPreviewMode = "fill_missing";
  });
  $("#plan-editor").addEventListener("change", event => {
    const target = event.target;
    if (target.dataset.planField) mutatePlanField(target);
    else if (target.dataset.stageField) mutateStageField(target);
    else if (target.dataset.taskField) mutateTaskField(target);
  });
  $("#plan-editor").addEventListener("submit", event => {
    const form = event.target.closest("[data-add-task-form]");
    if (!form) return;
    event.preventDefault();
    addTaskFromForm(form);
  });
  $("#plan-editor").addEventListener("click", event => {
    const restore = event.target.closest("[data-restore-revision]");
    if (restore) {
      restoreRevision(restore.dataset.restoreRevision, restore);
      return;
    }
    const taskAction = event.target.closest("button[data-task-action]");
    if (taskAction) {
      handleTaskAction(taskAction);
      return;
    }
    const stageAction = event.target.closest("button[data-stage-action]");
    if (stageAction) {
      handleStageAction(stageAction);
      return;
    }
    const actionButton = event.target.closest("[data-plan-action]");
    if (!actionButton) return;
    const action = actionButton.dataset.planAction;
    if (action === "export" && state.selectedPlan) downloadJson(`${state.selectedPlan.plan_id}.json`, state.selectedPlan);
    else if (action === "revisions") loadRevisions();
    else if (action === "close-revisions") $("#plan-revisions").classList.add("hidden");
    else if (action === "toggle") toggleSelectedPlan();
    else if (action === "delete") deleteSelectedPlan();
    else if (action === "complete-tags") startTagPreview(actionButton, "fill_missing");
    else if (action === "cleanup-tags") startTagPreview(actionButton, "cleanup");
    else if (action === "reload") selectPlan(state.selectedPlanId, true);
    else if (action === "edit-meta") {
      state.editingPlanMeta = !state.editingPlanMeta;
      state.editingStageKey = "";
      renderPlanEditor();
    } else if (action === "add-stage") {
      const next = clonePlan();
      const stageKey = stableKey("stage");
      stagesOf(next).push({
        stage_key: stageKey,
        topic: "新阶段",
        kind: "practice",
        unlock_at: null,
        due_date: null,
        tasks: [],
        replacements: [],
      });
      state.editingPlanMeta = false;
      state.editingStageKey = stageKey;
      savePlan(next, "阶段已添加");
    }
  });
}

export { bindPlanEvents };
