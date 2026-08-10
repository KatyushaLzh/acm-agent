import {
  $, api, jobIdOf, setBusy, setLocalFileSelection, state, toast, waitForJob,
} from "./core.js";
import {
  applyKnowledgeProposal, cancelKnowledgeProposal, clearAiConversation,
  clearAiCredential, isAbortError, loadAiProblemState, loadAiStatus,
  loadKnowledgeTargets, loadProblemContext, previewAiPatch,
  refreshKnowledgeProposal, registerKnowledgeTarget, removeKnowledgeTarget,
  renderSafeKnowledgeMarkdown, restoreAutomaticContext, revertKnowledgeProposal,
  runPatchAction, saveAiCredential, saveAiSettings, saveManualContext,
  streamAiChat,
} from "./view_ai.js";

function bindAiEvents() {
  $("#ai-settings-form").addEventListener("submit", event => {
    event.preventDefault();
    saveAiSettings(event.currentTarget);
  });
  $("#ai-credential-form").addEventListener("submit", event => {
    event.preventDefault();
    saveAiCredential(event.currentTarget);
  });
  $("#ai-key-clear").addEventListener("click", event => clearAiCredential(event.currentTarget));
  $("#ai-test-button").addEventListener("click", async event => {
    const button = event.currentTarget;
    setBusy(button, true, "测试中…");
    try {
      const started = await api("/api/jobs/ai/test", { body: {} });
      const result = await waitForJob(jobIdOf(started), "正在验证 DeepSeek API…");
      toast("DeepSeek 连接成功", result.model || "API 可用");
      await loadAiStatus();
    } catch (error) {
      toast("DeepSeek 测试失败", error.message, "error");
    } finally {
      setBusy(button, false);
    }
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
