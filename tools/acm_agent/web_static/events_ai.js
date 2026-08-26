import { $, setBusy, setLocalFileSelection, state, toast } from "./core.js";
import {
  applyKnowledgeProposal, cancelKnowledgeProposal, clearAiConversation,
  deleteConnection, editConnection, isAbortError, loadAiProblemState, loadAiStatus,
  loadKnowledgeTargets, loadProblemContext, previewAiPatch,
  refreshKnowledgeProposal, registerKnowledgeTarget, removeKnowledgeTarget,
  renderSafeKnowledgeMarkdown, restoreAutomaticContext, revertKnowledgeProposal,
  refreshConnection, resetConnectionForm, runPatchAction, saveConnection,
  saveManualContext, streamAiChat,
  bindAiPolicyDraftEvents, resetAiPolicyDraft, saveAiPolicy, repriceAiCosts,
} from "./view_ai.js";
import { bindAiModelPickerEvents } from "./ai_model_controls.js";

function bindAiEvents() {
  bindAiModelPickerEvents(loadAiStatus);
  $("#ai-connection-form").addEventListener("submit", event => {
    event.preventDefault();
    saveConnection(event.currentTarget).catch(error => toast("连接保存失败", error.message, "error"));
  });
  $("#ai-connection-cancel").addEventListener("click", resetConnectionForm);
  $("#ai-policy-form").addEventListener("submit", event => {
    event.preventDefault();
    saveAiPolicy($("#ai-policy-save")).catch(error => toast("策略保存失败", error.message, "error"));
  });
  $("#ai-policy-reset").addEventListener("click", resetAiPolicyDraft);
  bindAiPolicyDraftEvents();
  $("#ai-cost-reprice").addEventListener("click", event => repriceAiCosts(event.currentTarget).catch(error => toast("费用重算失败", error.message, "error")));
  $("#ai-connection-list").addEventListener("click", event => {
    const button = event.target.closest("button[data-connection-action]");
    if (!button) return;
    const connectionId = button.closest("[data-connection-id]")?.dataset.connectionId || "";
    const action = button.dataset.connectionAction;
    const operation = action === "edit"
      ? Promise.resolve().then(() => editConnection(connectionId))
      : action === "refresh"
        ? refreshConnection(connectionId, button)
        : deleteConnection(connectionId, button);
    operation.catch(error => toast("连接操作失败", error.message, "error"));
  });
  $("#ai-context-button").addEventListener("click", async event => {
    const button = event.currentTarget;
    setBusy(button, true, "抓取中…");
    try {
      const data = await loadProblemContext({ fetch: true, force: true });
      if (data) toast("题面已更新", "抓取成功或已使用人工版本。");
    } catch (error) {
      $("#ai-context-source").textContent = `抓取失败：${error.message}`;
      toast("题面抓取失败", "可粘贴题面后保存。", "error");
    } finally {
      setBusy(button, false);
    }
  });
  $("#ai-statement-save").addEventListener("click", event => saveManualContext(event.currentTarget));
  $("#ai-statement-restore").addEventListener("click", event => restoreAutomaticContext(event.currentTarget));
  $("#ai-problem").addEventListener("change", event => {
    const problem = event.currentTarget.value.trim();
    loadAiProblemState(problem).catch(error => {
      if (!isAbortError(error)) toast("切换题目失败", error.message, "error");
    });
  });
  $("#ai-chat-form").addEventListener("submit", async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const button = $("button[type=submit]", form);
    const message = form.elements.message.value.trim();
    if (!message) return;
    setBusy(button, true, "回答中…");
    try {
      if (await streamAiChat(message, $("#ai-mode").value, Number($("#ai-hint-level").value))) form.reset();
    } catch (error) {
      if (!isAbortError(error)) toast("AI 对话失败", error.message, "error");
    } finally {
      setBusy(button, false);
    }
  });
  $("#ai-chat-clear").addEventListener("click", event => {
    clearAiConversation(event.currentTarget).catch(error => {
      if (!isAbortError(error)) toast("清空失败", error.message, "error");
    });
  });
  $("#ai-patch-preview").addEventListener("click", event => previewAiPatch(event.currentTarget));
  $("#ai-patch-apply").addEventListener("click", event => runPatchAction("apply", event.currentTarget));
  $("#ai-patch-revert").addEventListener("click", event => runPatchAction("revert", event.currentTarget));
  $("#knowledge-enabled").addEventListener("change", event => {
    $("#knowledge-options").classList.toggle("hidden", !event.currentTarget.checked);
    if (event.currentTarget.checked && !state.knowledgeTargets.length) loadKnowledgeTargets();
  });
  $("#knowledge-schema-mode").addEventListener("change", event => {
    $("#knowledge-custom-schema-wrap").classList.toggle("hidden", event.currentTarget.value !== "custom");
  });
  $("#knowledge-target").addEventListener("change", event => {
    const target = state.knowledgeTargets.find(item => String(item.target_id || item.id) === event.currentTarget.value);
    if (!target) {
      setLocalFileSelection("knowledge_path", "");
      return;
    }
    setLocalFileSelection("knowledge_path", target.path || "");
    $("#knowledge-target-name").value = target.name || target.display_name || "";
    $("#knowledge-schema-mode").value = "stored";
    $("#knowledge-custom-schema-wrap").classList.add("hidden");
  });
  $("#knowledge-target-add").addEventListener("click", event => {
    const button = event.currentTarget;
    registerKnowledgeTarget(button).catch(error => toast("目标保存失败", error.message, "error"));
  });
  $("#knowledge-target-remove").addEventListener("click", event => {
    const button = event.currentTarget;
    removeKnowledgeTarget(button).catch(error => toast("取消注册失败", error.message, "error"));
  });
  $("#knowledge-markdown-editor").addEventListener("input", () => {
    if (!state.knowledgeProposalId) return;
    state.knowledgeProposalDirty = true;
    $("#knowledge-proposal-state").className = "badge warn";
    $("#knowledge-proposal-state").textContent = "内容已编辑，需刷新";
    $("#knowledge-apply").disabled = true;
    renderSafeKnowledgeMarkdown($("#knowledge-rendered-preview"), $("#knowledge-markdown-editor").value);
  });
  $("#knowledge-refresh").addEventListener("click", event => refreshKnowledgeProposal(event.currentTarget).catch(error => toast("预览刷新失败", error.message, "error")));
  $("#knowledge-apply").addEventListener("click", event => applyKnowledgeProposal(event.currentTarget).catch(error => toast("Markdown 写入失败", error.message, "error")));
  $("#knowledge-revert").addEventListener("click", event => revertKnowledgeProposal(event.currentTarget).catch(error => toast("Markdown 回退失败", error.message, "error")));
  $("#knowledge-cancel").addEventListener("click", cancelKnowledgeProposal);
}

export { bindAiEvents };
