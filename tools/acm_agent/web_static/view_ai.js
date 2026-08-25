import {
  $, $$, api, asObject, escapeHtml, jobIdOf, navigate, pollJob,
  renderCppSource, renderVerify, setBusy, setLocalFileSelection,
  showJobProgress, state, toast, waitForJob,
} from "./core.js";
import { postEventStream } from "./sse.js";
import { renderRecommendations } from "./view_today.js";

function knowledgeSchemaSelection() {
  const selected = $("#knowledge-schema-mode").value;
  let schema = null;
  if (selected === "custom") {
    const raw = $("#knowledge-custom-schema").value.trim();
    if (!raw) throw new Error("自定义 schema 不能为空");
    try { schema = JSON.parse(raw); }
    catch { throw new Error("自定义 schema 必须是合法 JSON"); }
  }
  return {
    schema_mode: selected === "infer" ? "auto" : selected,
    preset: selected === "custom" ? "custom" : null,
    schema,
  };
}

function knowledgeTargetRows(payload) {
  if (Array.isArray(payload)) return payload;
  return payload?.targets || payload?.items || [];
}

async function loadKnowledgeTargets(preferredId = "") {
  const select = $("#knowledge-target");
  const current = preferredId || select.value;
  try {
    const payload = await api("/api/knowledge/targets");
    state.knowledgeTargets = knowledgeTargetRows(payload).filter(item => item.enabled !== false);
    select.innerHTML = '<option value="">请选择或注册目标文件</option>' + state.knowledgeTargets.map(item => {
      const id = item.target_id || item.id;
      const label = item.name || item.display_name || item.path || id;
      return `<option value="${escapeHtml(id)}">${escapeHtml(label)}</option>`;
    }).join("");
    if (state.knowledgeTargets.some(item => String(item.target_id || item.id) === String(current))) select.value = current;
  } catch (error) {
    state.knowledgeTargets = [];
    select.innerHTML = '<option value="">知识归档服务暂不可用</option>';
    if ($("#knowledge-enabled").checked) toast("目标列表读取失败", error.message, "error");
  }
}

async function registerKnowledgeTarget(button) {
  const path = $("#knowledge-path").value.trim();
  if (!path) throw new Error("请先选择 Markdown 文件");
  const selection = knowledgeSchemaSelection();
  const inspectionKey = JSON.stringify({ path, preset: selection.preset, schema_mode: selection.schema_mode });
  let inspectionReady = false;
  setBusy(button, true, "检查中…");
  try {
    if (button.dataset.inspectPhase !== "confirm" || state.knowledgeTargetInspection?.key !== inspectionKey) {
      const inspected = await api("/api/knowledge/targets/inspect", { body: {
        path,
        allow_create: true,
        preset: selection.preset,
        schema_mode: selection.schema_mode,
        schema: selection.schema,
      } });
      state.knowledgeTargetInspection = { key: inspectionKey, inspected };
      if (inspected.schema) {
        $("#knowledge-custom-schema").value = JSON.stringify(inspected.schema, null, 2);
        $("#knowledge-custom-schema-wrap").classList.remove("hidden");
      }
      inspectionReady = true;
      toast("路径与 schema 检查通过", "请检查下方 schema，再次点击确认保存；目标文件尚未修改。", "success");
      return;
    }
    const inspected = state.knowledgeTargetInspection.inspected;
    let confirmedSchema = selection.schema || inspected.schema || null;
    if (!$("#knowledge-custom-schema-wrap").classList.contains("hidden") && $("#knowledge-custom-schema").value.trim()) {
      try { confirmedSchema = JSON.parse($("#knowledge-custom-schema").value); }
      catch { throw new Error("待保存 schema 必须是合法 JSON"); }
    }
    const target = await api("/api/knowledge/targets", { body: {
      path: inspected.normalized_path || inspected.path || path,
      name: $("#knowledge-target-name").value.trim() || null,
      allow_create: true,
      preset: selection.preset,
      schema_mode: selection.schema_mode,
      schema: confirmedSchema,
      expected_inspection_sha256: inspected.sha256 || inspected.baseline_sha256 || null,
      expected_existed: Boolean(inspected.exists),
    } });
    const targetId = target.target_id || target.id;
    await loadKnowledgeTargets(targetId);
    setLocalFileSelection("knowledge_path", target.path || inspected.normalized_path || path);
    $("#knowledge-schema-mode").value = "stored";
    $("#knowledge-custom-schema-wrap").classList.add("hidden");
    state.knowledgeTargetInspection = null;
    delete button.dataset.inspectPhase;
    toast("目标已保存", target.path || inspected.normalized_path || path);
  } finally {
    setBusy(button, false);
    if (inspectionReady) {
      button.dataset.inspectPhase = "confirm";
      button.textContent = "确认保存目标";
    } else if (!button.dataset.inspectPhase) button.textContent = "检查并保存目标";
  }
}

async function removeKnowledgeTarget(button) {
  const targetId = $("#knowledge-target").value;
  const target = state.knowledgeTargets.find(item => String(item.target_id || item.id) === targetId);
  if (!target) throw new Error("请先选择一个已保存目标");
  if (!window.confirm("只取消注册此目标？Markdown 文件本身不会被删除。")) return;
  setBusy(button, true, "取消中…");
  try {
    await api(`/api/knowledge/targets/${encodeURIComponent(targetId)}`, {
      method: "DELETE",
      body: { expected_revision: target.revision },
    });
    setLocalFileSelection("knowledge_path", "");
    $("#knowledge-target-name").value = "";
    await loadKnowledgeTargets();
    toast("已取消注册", "Markdown 文件未删除。", "success");
  } finally { setBusy(button, false); }
}

function knowledgeMarkdown(proposal) {
  return proposal.entry_markdown ?? proposal.rendered_entry ?? proposal.rendered_markdown ?? proposal.markdown ?? proposal.entry?.markdown ?? "";
}

function renderSafeKnowledgeMarkdown(container, markdown) {
  container.replaceChildren();
  const lines = String(markdown || "").replace(/\r\n?/g, "\n").split("\n");
  let list = null;
  let fence = null;
  let codeLines = [];
  const appendTextNode = (tag, text, className = "") => {
    const node = document.createElement(tag);
    if (className) node.className = className;
    node.textContent = text;
    container.appendChild(node);
    return node;
  };
  const closeList = () => { list = null; };
  const closeFence = () => {
    if (fence === null) return;
    const pre = document.createElement("pre");
    const code = document.createElement("code");
    code.textContent = codeLines.join("\n");
    pre.appendChild(code);
    container.appendChild(pre);
    fence = null;
    codeLines = [];
  };
  for (const line of lines) {
    const fenceMatch = line.match(/^\s*(```|~~~)/);
    if (fenceMatch) {
      closeList();
      if (fence === null) fence = fenceMatch[1][0];
      else if (fence === fenceMatch[1][0]) closeFence();
      else codeLines.push(line);
      continue;
    }
    if (fence !== null) { codeLines.push(line); continue; }
    const heading = line.match(/^(#{1,6})\s+(.+)$/);
    if (heading) {
      closeList();
      appendTextNode(`h${heading[1].length}`, heading[2]);
      continue;
    }
    const bullet = line.match(/^\s*[-*+]\s+(.+)$/);
    if (bullet) {
      if (!list) {
        list = document.createElement("ul");
        container.appendChild(list);
      }
      const item = document.createElement("li");
      item.textContent = bullet[1];
      list.appendChild(item);
      continue;
    }
    closeList();
    if (!line.trim()) continue;
    appendTextNode("p", line);
  }
  closeFence();
  renderAssistantMath(container);
}

function knowledgeWarningItems(proposal) {
  const warnings = Array.isArray(proposal.warnings) ? [...proposal.warnings] : [];
  const duplicate = proposal.duplicate_diagnosis;
  const duplicateKind = String(duplicate?.kind || duplicate?.status || "none").toLowerCase();
  if (duplicateKind === "exact_source") warnings.push(duplicate?.message || "检测到题号完全相同的条目，已由 AI 合并内容。");
  else if (duplicateKind === "similar") warnings.push(duplicate?.message || "检测到相似条目；因题号不同，将按新条目处理。");
  else if (duplicate?.message) warnings.push(duplicate.message);
  else if (duplicateKind !== "none") warnings.push(`重复检测：${duplicateKind}`);
  if (proposal.apply_blocked_reason) warnings.push(proposal.apply_blocked_reason);
  return warnings.map(String);
}

function knowledgeProposalPayload(payload) {
  return payload?.proposal && typeof payload.proposal === "object" ? payload.proposal : payload;
}

function renderKnowledgeProposal(proposal, epoch = state.knowledgeEpoch) {
  proposal = knowledgeProposalPayload(proposal || {});
  if (epoch !== state.knowledgeEpoch) return;
  state.knowledgeProposalId = proposal.proposal_id || proposal.id || state.knowledgeProposalId;
  state.knowledgeProposalRevision = Number(proposal.revision ?? proposal.proposal_revision ?? 0);
  state.knowledgeProposalDirty = false;
  const box = $("#knowledge-preview-box");
  box.classList.remove("hidden");
  const status = String(proposal.status || "preview");
  const stateBadge = $("#knowledge-proposal-state");
  stateBadge.className = `badge ${status === "applied" ? "good" : status === "conflict" ? "bad" : "warn"}`;
  stateBadge.textContent = ({ preview: "等待确认", applied: "已写入", reverted: "已回退", conflict: "目标已变化" })[status] || status;
  const schema = proposal.schema || proposal.schema_snapshot || {};
  const fields = Array.isArray(schema.fields) ? schema.fields : [];
  $("#knowledge-schema-summary").innerHTML = [
    schema.version || "summary-schema-v1",
    proposal.topic || proposal.entry?.topic,
    ...fields.map(item => item.label || item.key).filter(Boolean),
  ].filter(Boolean).map(item => `<span class="tag">${escapeHtml(item)}</span>`).join("");
  const warnings = knowledgeWarningItems(proposal);
  const warningBox = $("#knowledge-warnings");
  const warningIsBlocking = Boolean(proposal.can_apply === false || proposal.apply_allowed === false || proposal.apply_blocked || proposal.apply_blocked_reason);
  warningBox.className = `result-box ${warnings.length ? (warningIsBlocking ? "error" : "") : "hidden"}`;
  warningBox.textContent = warnings.join("；");
  const markdown = knowledgeMarkdown(proposal);
  $("#knowledge-markdown-editor").value = markdown;
  const preview = $("#knowledge-rendered-preview");
  renderSafeKnowledgeMarkdown(preview, markdown);
  const blocked = Boolean(proposal.can_apply === false || proposal.apply_allowed === false || proposal.apply_blocked || proposal.apply_blocked_reason || Number(proposal.confidence ?? proposal.entry?.confidence ?? 1) < 0.75);
  $("#knowledge-refresh").disabled = status !== "preview";
  $("#knowledge-apply").disabled = status !== "preview" || blocked;
  $("#knowledge-revert").classList.toggle("hidden", status !== "applied");
}

async function waitForKnowledgeJob(jobId, epoch, label) {
  showJobProgress(label);
  try {
    return await pollJob(jobId, {
      shouldCancel: () => epoch !== state.knowledgeEpoch,
      toError: job => new Error(job.error?.message || job.error || "Markdown 总结任务失败"),
    });
  } finally {
    if (epoch === state.knowledgeEpoch) $("#job-progress").classList.add("hidden");
  }
}

async function previewKnowledgeSummary(attemptId, epoch) {
  if (!state.aiStatus?.api_key_detected) throw new Error("尚未配置 DeepSeek API Key；session 已结束，但未生成总结");
  const targetId = $("#knowledge-target").value;
  if (!targetId) throw new Error("请先选择或保存一个 Markdown 目标");
  const selection = knowledgeSchemaSelection();
  const started = await api("/api/jobs/ai/knowledge/preview", { body: {
    attempt_id: attemptId,
    target_id: targetId,
    schema_mode: selection.schema_mode,
    preset: selection.preset,
    schema: selection.schema,
  } });
  const jobId = jobIdOf(started);
  const result = jobId
    ? await waitForKnowledgeJob(jobId, epoch, "正在生成 Markdown 总结预览…")
    : started;
  if (result && epoch === state.knowledgeEpoch) {
    renderKnowledgeProposal(knowledgeProposalPayload(result), epoch);
    toast("总结预览已生成", "目标文件尚未修改，请检查可编辑内容与安全预览。", "success");
  }
}

async function refreshKnowledgeProposal(button) {
  if (!state.knowledgeProposalId) return;
  setBusy(button, true, "刷新中…");
  try {
    const proposal = await api(`/api/knowledge/proposals/${encodeURIComponent(state.knowledgeProposalId)}/refresh`, { body: {
      entry_markdown: $("#knowledge-markdown-editor").value,
      expected_revision: state.knowledgeProposalRevision,
    } });
    renderKnowledgeProposal(knowledgeProposalPayload(proposal));
  } finally { setBusy(button, false); }
}

async function applyKnowledgeProposal(button) {
  if (!state.knowledgeProposalId || state.knowledgeProposalDirty) return;
  setBusy(button, true, "写入中…");
  try {
    const epoch = state.knowledgeEpoch;
    const started = await api(`/api/jobs/knowledge/proposals/${encodeURIComponent(state.knowledgeProposalId)}/apply`, { body: { expected_revision: state.knowledgeProposalRevision } });
    const jobId = jobIdOf(started);
    let proposal = jobId ? await waitForKnowledgeJob(jobId, epoch, "正在备份并原子写入 Markdown…") : started;
    if (proposal) {
      proposal = await api(`/api/knowledge/proposals/${encodeURIComponent(state.knowledgeProposalId)}`);
      renderKnowledgeProposal(knowledgeProposalPayload(proposal), epoch);
      toast("Markdown 已写入", "已创建备份；只有文件仍是应用版本时才可回退。", "success");
    }
  } finally { setBusy(button, false); }
}

async function revertKnowledgeProposal(button) {
  if (!state.knowledgeProposalId) return;
  setBusy(button, true, "回退中…");
  try {
    const epoch = state.knowledgeEpoch;
    const started = await api(`/api/jobs/knowledge/proposals/${encodeURIComponent(state.knowledgeProposalId)}/revert`, { body: { expected_revision: state.knowledgeProposalRevision } });
    const jobId = jobIdOf(started);
    let proposal = jobId ? await waitForKnowledgeJob(jobId, epoch, "正在校验并恢复 Markdown 备份…") : started;
    if (proposal) {
      proposal = await api(`/api/knowledge/proposals/${encodeURIComponent(state.knowledgeProposalId)}`);
      renderKnowledgeProposal(knowledgeProposalPayload(proposal), epoch);
      toast("Markdown 已回退", "目标已恢复到写入前版本。", "success");
    }
  } finally { setBusy(button, false); }
}

function cancelKnowledgeProposal() {
  state.knowledgeEpoch += 1;
  state.knowledgeProposalId = "";
  state.knowledgeProposalRevision = null;
  state.knowledgeProposalDirty = false;
  $("#knowledge-preview-box").classList.add("hidden");
  $("#job-progress").classList.add("hidden");
  toast("已取消总结", "没有写入 Markdown 文件。", "success");
}

async function requestAiRecommendations(button, aiMode = "gap_fill") {
  const form = $("#recommend-controls");
  if (!["gap_fill", "specialization"].includes(aiMode)) throw new Error(`未知 AI 推荐模式：${aiMode}`);
  if (state.recommendationController) state.recommendationController.abort();
  const controller = new AbortController();
  const epoch = ++state.recommendationEpoch;
  state.recommendationController = controller;
  const modeLabel = aiMode === "specialization" ? "专项强化" : "查漏补缺";
  setBusy(button, true, `${modeLabel}中…`);
  try {
    if (!state.aiStatus?.api_key_detected) {
      toast("尚未启用 DeepSeek", "请先在设置页输入 API Key 并保存。", "error");
      navigate("settings");
      return;
    }
    const planIds = $$("input[type=checkbox]:checked", $("#recommend-plan-options")).map(input => input.value);
    const started = await api("/api/jobs/ai/recommendations", { body: {
      mode: form.elements.mode.value,
      count: Number(form.elements.count.value),
      source_mode: form.elements.source_mode.value,
      plan_ids: planIds.length ? planIds : null,
      ai_mode: aiMode,
    }, signal: controller.signal });
    const result = await waitForJob(
      jobIdOf(started),
      `正在按 AC 知识覆盖生成${modeLabel}推荐…`,
      null,
      { shouldCancel: () => state.recommendationEpoch !== epoch || state.recommendationController !== controller },
    );
    if (!result || state.recommendationEpoch !== epoch || state.recommendationController !== controller) return;
    renderRecommendations(result || {});
    const fallback = result?.ai?.fallback;
    const fallbackMessage = typeof fallback === "string" ? fallback : fallback?.message;
    toast(fallback ? `${modeLabel}已降级` : `${modeLabel}完成`, fallbackMessage || "确定性资格过滤保持不变。");
  } catch (error) {
    if (error.name !== "AbortError" && state.recommendationEpoch === epoch) toast("AI 推荐失败", error.message, "error");
  } finally {
    if (state.recommendationController === controller) state.recommendationController = null;
    setBusy(button, false);
  }
}

async function loadAiStatus() {
  try {
    const status = await api("/api/ai/status");
    state.aiStatus = status;
    const sourceLabels = {
      secure_store: "已安全保存",
      environment: "环境变量",
      injected: "测试凭据",
      none: "未配置",
    };
    const sourceLabel = sourceLabels[status.credential_source] || "状态未知";
    const badge = $("#ai-key-state");
    badge.className = `badge ${status.api_key_detected ? "good" : "warn"}`;
    badge.textContent = status.api_key_detected ? sourceLabel : "未配置";
    $("#ai-chat-state").className = `badge ${status.api_key_detected ? "good" : "warn"}`;
    $("#ai-chat-state").textContent = status.api_key_detected ? "可用" : "未配置";
    const knowledgeToggle = $("#knowledge-enabled");
    knowledgeToggle.disabled = !status.api_key_detected;
    knowledgeToggle.title = status.api_key_detected ? "" : "请先在设置页保存并启用 DeepSeek API Key";
    $("#knowledge-key-hint").textContent = status.api_key_detected
      ? "仅在勾选后调用 DeepSeek；关闭 session 本身不会产生 AI 费用。"
      : "需要先在设置页保存并启用 DeepSeek API Key。";
    if (!status.api_key_detected) {
      knowledgeToggle.checked = false;
      $("#knowledge-options").classList.add("hidden");
    }
    const detail = $("#ai-key-detail");
    detail.className = `credential-detail${status.credential_error ? " error" : ""}`;
    detail.textContent = status.credential_error
      ? `已保存凭据无法加载：${status.credential_error}`
      : status.credential_source === "secure_store"
        ? "已使用 Windows DPAPI 加密保存；重启本地服务后仍会自动启用。"
        : status.credential_source === "environment"
          ? "当前使用 DEEPSEEK_API_KEY 环境变量；可在上方输入新 Key 并改用加密存储。"
          : status.api_key_detected
            ? "当前测试服务已注入凭据。"
            : "尚未配置。输入 Key 后点击“保存并启用”。";
    const form = $("#ai-settings-form");
    form.elements.recommendation_model.value = status.settings?.recommendation_model || "deepseek-v4-flash";
    form.elements.coaching_model.value = status.settings?.coaching_model || "deepseek-v4-flash";
    form.elements.summary_model.value = status.settings?.summary_model || status.settings?.coaching_model || "deepseek-v4-flash";
    form.elements.coaching_thinking.checked = Boolean(status.settings?.coaching_thinking);
    form.elements.reasoning_effort.value = status.settings?.reasoning_effort || "high";
    form.elements.summary_thinking.checked = Boolean(status.settings?.summary_thinking);
    form.elements.summary_reasoning_effort.value = status.settings?.summary_reasoning_effort || status.settings?.reasoning_effort || "high";
  } catch (error) { toast("AI 状态读取失败", error.message, "error"); }
}

async function saveAiCredential(form) {
  const button = $("button[type=submit]", form);
  const input = form.elements.api_key;
  const key = input.value;
  if (!key.trim()) return toast("无法启用", "请输入 DeepSeek API Key。", "error");
  setBusy(button, true, "正在安全保存…");
  try {
    state.aiStatus = await api("/api/ai/credential", { body: { api_key: key } });
    toast("DeepSeek 已启用", "密钥已由 Windows DPAPI 加密保存，服务重启后无需重新输入。");
    await loadAiStatus();
  } catch (error) { toast("API Key 保存失败", error.message, "error"); }
  finally {
    input.value = "";
    setBusy(button, false);
  }
}

async function clearAiCredential(button) {
  if (!window.confirm("确认删除已加密保存的 DeepSeek API Key？")) return;
  setBusy(button, true, "正在清除…");
  try {
    state.aiStatus = await api("/api/ai/credential", { body: { clear: true } });
    toast("已清除保存的 Key", state.aiStatus.api_key_detected ? "环境变量中的 Key 仍然可用。" : "DeepSeek 已停用。");
    await loadAiStatus();
  } catch (error) { toast("API Key 清除失败", error.message, "error"); }
  finally { setBusy(button, false); }
}

async function saveAiSettings(form) {
  const button = $("button[type=submit]", form); setBusy(button, true);
  try {
    state.aiStatus = await api("/api/ai/settings", { body: {
      recommendation_model: form.elements.recommendation_model.value,
      coaching_model: form.elements.coaching_model.value,
      summary_model: form.elements.summary_model.value,
      coaching_thinking: form.elements.coaching_thinking.checked,
      reasoning_effort: form.elements.reasoning_effort.value,
      summary_thinking: form.elements.summary_thinking.checked,
      summary_reasoning_effort: form.elements.summary_reasoning_effort.value,
    } });
    toast("AI 设置已保存", "模型与 thinking 设置已更新。凭据由独立的 DPAPI 存储管理。");
    await loadAiStatus();
  } catch (error) { toast("AI 设置失败", error.message, "error"); }
  finally { setBusy(button, false); }
}

function currentAiProblem() {
  return $("#ai-problem").value.trim();
}

function aiProblemKey(problem) { return String(problem || "").trim().toUpperCase(); }
function aiOperationIsCurrent(problemKey, epoch) { return state.aiProblemKey === problemKey && state.aiEpoch === epoch; }
function isAbortError(error) { return error?.name === "AbortError"; }

function isScrapeableProblem(value) {
  const input = String(value || "").trim();
  if (!input) return false;
  if (/^CF\d+[A-Z][A-Z0-9]*$/i.test(input) || /^P\d+$/i.test(input)) return true;
  if (!/^https?:\/\//i.test(input)) return false;
  try {
    const host = new URL(input).hostname.toLowerCase();
    return host === "codeforces.com" || host.endsWith(".codeforces.com")
      || host === "luogu.com.cn" || host === "www.luogu.com.cn" || host === "luogu.org"
      || host.endsWith(".luogu.com.cn");
  } catch { return false; }
}

function updateAiProblemMode() {
  const problem = currentAiProblem();
  const scrapeable = Boolean(problem) && isScrapeableProblem(problem);
  const hint = $("#ai-problem-hint");
  if (hint) {
    hint.classList.toggle("hidden", !problem || scrapeable);
    hint.textContent = problem && !scrapeable ? "非 Codeforces / 洛谷题号，无法自动抓取题面，请粘贴题面后保存。" : "";
  }
  const button = $("#ai-context-button");
  button.disabled = !problem || !scrapeable;
  button.title = !problem || scrapeable ? "" : "该题号无法自动抓取，请手动粘贴题面";
}

function resetAiWorkbenchUi(problem = "") {
  $("#ai-problem").value = problem;
  $("#ai-statement").value = "";
  $("#ai-context-source").textContent = problem ? "尚未读取" : "尚未选择题目";
  const messages = $("#ai-chat-messages");
  messages.className = "ai-chat-messages empty-state compact";
  messages.textContent = problem ? "正在读取本题的持久对话…" : "开始题目后可在这里请求分级提示或代码诊断。";
  $("#ai-patch-code").textContent = "";
  $("#ai-patch-box").classList.add("hidden");
  updateAiProblemMode();
}

function switchAiProblem(problem, { force = false } = {}) {
  const value = String(problem || "").trim();
  const key = aiProblemKey(value);
  if (!force && key === state.aiProblemKey) return state.aiEpoch;
  if (state.aiStreamController) state.aiStreamController.abort();
  state.aiStreamController = null;
  $("#ai-problem").disabled = false;
  state.aiEpoch += 1;
  state.aiProblemKey = key;
  state.aiConversationId = "";
  state.aiConversationProblemKey = "";
  state.aiContextHash = null;
  state.aiPatchProposalId = "";
  state.aiPatchProblemKey = "";
  resetAiWorkbenchUi(value);
  return state.aiEpoch;
}

async function loadAiProblemState(problem, { force = false, fetchContext = false } = {}) {
  const value = String(problem || "").trim();
  switchAiProblem(value, { force });
  if (!value) return;
  const problemKey = aiProblemKey(value); const epoch = state.aiEpoch;
  const conversation = ensureAiConversation().catch(error => {
    if (!isAbortError(error) && aiOperationIsCurrent(problemKey, epoch)) {
      $("#ai-chat-state").textContent = `会话不可用：${error.message}`;
    }
  });
  const context = loadProblemContext({ fetch: fetchContext }).catch(error => {
    if (!isAbortError(error) && aiOperationIsCurrent(problemKey, epoch)) {
      $("#ai-context-source").textContent = `${fetchContext ? "自动抓取" : "题面读取"}失败：${error.message}`;
    }
  });
  await Promise.allSettled([conversation, context]);
}

async function loadProblemContext({ fetch = false, force = false } = {}) {
  const problem = currentAiProblem();
  if (!problem) throw new Error("请先填写题号或开始一个 session");
  if (state.aiProblemKey !== aiProblemKey(problem)) switchAiProblem(problem);
  const problemKey = aiProblemKey(problem); const epoch = state.aiEpoch;
  let data;
  if (fetch && !isScrapeableProblem(problem)) {
    $("#ai-context-source").textContent = "非 Codeforces / 洛谷题号，无法自动抓取；请粘贴题面后保存。";
    return null;
  }
  try {
    if (fetch) {
      const started = await api("/api/jobs/problems/context/fetch", { body: { problem, force } });
    data = await waitForJob(jobIdOf(started), "正在读取公开题面…");
    } else data = await api(`/api/problems/${encodeURIComponent(problem)}/context`);
  } catch (error) {
    if (!aiOperationIsCurrent(problemKey, epoch)) return null;
    throw error;
  }
  if (data.ok === false && data.error) throw new Error(data.error.message || String(data.error));
  if (!aiOperationIsCurrent(problemKey, epoch)) return null;
  $("#ai-statement").value = data.content || "";
  state.aiContextHash = data.content_hash || null;
  $("#ai-context-source").textContent = data.available === false ? (data.error || "暂无题面") : `来源：${data.source || "unknown"}`;
  return data;
}

async function saveManualContext(button) {
  const problem = currentAiProblem();
  if (!problem) return toast("无法保存题面", "请先填写题号。", "error");
  const problemKey = aiProblemKey(problem); const epoch = state.aiEpoch;
  setBusy(button, true);
  try {
    const data = await api("/api/problems/context", { body: { problem, content: $("#ai-statement").value, expected_hash: state.aiContextHash } });
    if (!aiOperationIsCurrent(problemKey, epoch)) return;
    state.aiContextHash = data.content_hash;
    $("#ai-context-source").textContent = "来源：manual";
    toast("题面已保存", "人工版本将优先于自动抓取。");
  } catch (error) { if (aiOperationIsCurrent(problemKey, epoch)) toast("题面保存失败", error.message, "error"); }
  finally { setBusy(button, false); }
}

async function restoreAutomaticContext(button) {
  const problem = currentAiProblem();
  if (!problem) return toast("无法恢复题面", "请先填写题号。", "error");
  if (!window.confirm("移除人工题面并恢复最近一次自动抓取版本？")) return;
  const problemKey = aiProblemKey(problem); const epoch = state.aiEpoch;
  setBusy(button, true);
  try {
    const data = await api("/api/problems/context", { body: { problem, restore_auto: true, expected_hash: state.aiContextHash } });
    if (!aiOperationIsCurrent(problemKey, epoch)) return;
    $("#ai-statement").value = data.content || "";
    state.aiContextHash = data.content_hash || null;
    $("#ai-context-source").textContent = data.available === false ? "暂无自动题面" : `来源：${data.source || "unknown"}`;
    toast("已恢复自动题面", data.available === false ? "可重新抓取公开题面。" : "人工版本已移除。");
  } catch (error) { if (aiOperationIsCurrent(problemKey, epoch)) toast("恢复失败", error.message, "error"); }
  finally { setBusy(button, false); }
}

function renderAssistantMath(node) {
  if (!node?.classList.contains("assistant") || !node.textContent) return;
  if (typeof window.renderMathInElement !== "function") return;
  const source = node.textContent;
  try {
    window.renderMathInElement(node, {
      delimiters: [
        { left: "$$", right: "$$", display: true },
        { left: "\\[", right: "\\]", display: true },
        { left: "\\(", right: "\\)", display: false },
        { left: "$", right: "$", display: false },
      ],
      throwOnError: false,
      trust: false,
    });
  } catch {
    // Auto-render should never make a saved answer unreadable.
    node.textContent = source;
  }
}

function appendAiMessage(role, content = "", status = "complete") {
  const root = $("#ai-chat-messages");
  if (root.classList.contains("empty-state")) { root.className = "ai-chat-messages"; root.innerHTML = ""; }
  const node = document.createElement("div");
  node.className = `ai-chat-message ${role}${status === "interrupted" ? " interrupted" : ""}`;
  node.textContent = content;
  root.append(node); root.scrollTop = root.scrollHeight;
  if (role === "assistant") renderAssistantMath(node);
  return node;
}

async function ensureAiConversation() {
  const problem = currentAiProblem();
  if (!problem) throw new Error("请先填写题号或开始一个 session");
  if (state.aiProblemKey !== aiProblemKey(problem)) switchAiProblem(problem);
  const problemKey = aiProblemKey(problem); const epoch = state.aiEpoch;
  if (state.aiConversationId && state.aiConversationProblemKey === problemKey) return state.aiConversationId;
  let data;
  try { data = await api("/api/ai/conversations", { body: { problem } }); }
  catch (error) { if (!aiOperationIsCurrent(problemKey, epoch)) return null; throw error; }
  if (!aiOperationIsCurrent(problemKey, epoch)) return null;
  state.aiConversationId = data.conversation_id;
  state.aiConversationProblemKey = problemKey;
  $("#ai-chat-state").className = "badge good";
  $("#ai-chat-state").textContent = "已连接";
  const root = $("#ai-chat-messages");
  root.className = (data.messages || []).length ? "ai-chat-messages" : "ai-chat-messages empty-state compact";
  root.innerHTML = (data.messages || []).length ? "" : "当前对话暂无消息。";
  for (const message of data.messages || []) appendAiMessage(message.role, message.content, message.status);
  return state.aiConversationId;
}

function parseSseBlock(block) {
  let event = "message"; const data = [];
  for (const line of block.split(/\r?\n/)) {
    if (line.startsWith("event:")) event = line.slice(6).trim();
    else if (line.startsWith("data:")) data.push(line.slice(5).trimStart());
  }
  if (!data.length) return null;
  try { return { event, data: JSON.parse(data.join("\n")) }; }
  catch { return { event, data: { content: data.join("\n") } }; }
}

async function streamAiChat(message, mode, hintLevel) {
  const conversationId = await ensureAiConversation();
  if (!conversationId) return false;
  const problemKey = state.aiProblemKey; const epoch = state.aiEpoch;
  if (state.aiStreamController) state.aiStreamController.abort();
  const controller = new AbortController();
  state.aiStreamController = controller;
  const problemInput = $("#ai-problem");
  problemInput.disabled = true;
  appendAiMessage("user", message);
  const assistant = appendAiMessage("assistant", "");
  try {
    const response = await postEventStream(
      `/api/ai/conversations/${encodeURIComponent(conversationId)}/messages`,
      { message, mode, hint_level: hintLevel },
      controller.signal,
    );
    if (!response.ok) {
      let detail = `HTTP ${response.status}`;
      try { const payload = await response.json(); detail = payload.error?.message || payload.error || detail; } catch {}
      assistant.remove(); throw new Error(String(detail));
    }
    const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = ""; let completed = false;
    const consumeBlock = block => {
      if (!aiOperationIsCurrent(problemKey, epoch) || state.aiStreamController !== controller) {
        controller.abort();
        return;
      }
      const item = parseSseBlock(block); if (!item) return;
      if (item.event === "delta") { assistant.textContent += item.data.content || ""; $("#ai-chat-messages").scrollTop = $("#ai-chat-messages").scrollHeight; }
      else if (item.event === "done") completed = true;
      else if (item.event === "error") throw new Error(item.data.message || "DeepSeek 流式请求失败");
    };
    for (;;) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const blocks = buffer.split(/\r?\n\r?\n/); buffer = blocks.pop() || "";
      for (const block of blocks) consumeBlock(block);
      if (done && buffer.trim()) { consumeBlock(buffer); buffer = ""; }
      if (completed) { await reader.cancel(); break; }
      if (done) throw new Error("DeepSeek 流式响应在完成事件前中断");
    }
  } catch (error) {
    assistant.classList.add("interrupted");
    if (isAbortError(error)) return false;
    throw error;
  } finally {
    renderAssistantMath(assistant);
    if (state.aiStreamController === controller) {
      state.aiStreamController = null;
      problemInput.disabled = false;
    }
  }
  return completed;
}

async function clearAiConversation(button) {
  if (state.aiStreamController) {
    toast("暂时无法清除", "请等待当前 AI 回答结束，或先切换题目以中断回答。", "error");
    return;
  }
  const conversationId = await ensureAiConversation();
  if (!conversationId) return;
  if (!window.confirm("清除本题当前显示的 AI 对话？旧记录会退出后续对话上下文，但仍保留提示等级与调用审计。")) return;
  const problemKey = state.aiProblemKey;
  const epoch = ++state.aiEpoch;
  setBusy(button, true, "清空中…");
  try {
    const data = await api(`/api/ai/conversations/${encodeURIComponent(conversationId)}/clear`, { body: {} });
    if (!aiOperationIsCurrent(problemKey, epoch)) return;
    if (!data.conversation_id) throw new Error("服务未返回新的 conversation_id");
    state.aiConversationId = data.conversation_id;
    state.aiConversationProblemKey = problemKey;
    const root = $("#ai-chat-messages");
    root.className = "ai-chat-messages empty-state compact";
    root.textContent = "当前对话暂无消息。";
    toast("本题对话已清除", "后续消息将写入新的持久会话。");
  } catch (error) { if (!isAbortError(error) && aiOperationIsCurrent(problemKey, epoch)) toast("清空失败", error.message, "error"); }
  finally { setBusy(button, false); }
}

async function previewAiPatch(button) {
  const problem = currentAiProblem(); const instruction = $("#ai-chat-form").elements.message.value.trim();
  if (!problem || !instruction) return toast("无法生成补丁", "需要题号和修复要求。", "error");
  const problemKey = aiProblemKey(problem); const epoch = state.aiEpoch;
  setBusy(button, true, "生成中…");
  try {
    const conversationId = state.aiConversationProblemKey === problemKey ? state.aiConversationId : null;
    const started = await api("/api/jobs/ai/patches/preview", { body: { problem, instruction, conversation_id: conversationId || null } });
    const data = await waitForJob(jobIdOf(started), "正在生成带错误注释的候选源码…");
    if (!aiOperationIsCurrent(problemKey, epoch)) return;
    if (typeof data.candidate_code !== "string" || !data.candidate_code.trim()) throw new Error("服务未返回修改后的 C++ 源码");
    state.aiPatchProposalId = data.proposal_id;
    state.aiPatchProblemKey = problemKey;
    renderCppSource($("#ai-patch-code"), data.candidate_code);
    $("#ai-patch-box").classList.remove("hidden");
  } catch (error) { if (aiOperationIsCurrent(problemKey, epoch)) toast("补丁预览失败", error.message, "error"); }
  finally { setBusy(button, false); }
}

async function runPatchAction(action, button) {
  if (!state.aiPatchProposalId) return toast("没有候选代码", "请先生成 AI 修改代码。", "error");
  if (state.aiPatchProblemKey !== state.aiProblemKey) return toast("候选代码已过期", "请为当前题目重新生成修改代码。", "error");
  if (action === "apply" && !window.confirm("应用当前修改后代码、备份原源码并运行本地验证？")) return;
  const problemKey = state.aiProblemKey; const epoch = state.aiEpoch;
  setBusy(button, true);
  try {
    const started = await api(`/api/jobs/ai/patches/${action}`, { body: { proposal_id: state.aiPatchProposalId } });
    const data = await waitForJob(jobIdOf(started), action === "apply" ? "正在应用并验证…" : "正在安全回退…");
    if (!aiOperationIsCurrent(problemKey, epoch)) return;
    if (data.verify) renderVerify(data.verify);
    toast(action === "apply" ? "补丁已应用" : "补丁已回退", data.verify?.passed === false ? "本地验证未通过，备份仍可回退。" : "操作完成。");
  } catch (error) { if (aiOperationIsCurrent(problemKey, epoch)) toast(action === "apply" ? "补丁应用失败" : "补丁回退失败", error.message, "error"); }
  finally { setBusy(button, false); }
}

export {
  loadKnowledgeTargets, registerKnowledgeTarget, removeKnowledgeTarget,
  renderSafeKnowledgeMarkdown, previewKnowledgeSummary, refreshKnowledgeProposal,
  applyKnowledgeProposal, revertKnowledgeProposal, cancelKnowledgeProposal,
  requestAiRecommendations, loadAiStatus, saveAiCredential, clearAiCredential,
  saveAiSettings, currentAiProblem, isAbortError, updateAiProblemMode,
  switchAiProblem, loadAiProblemState, loadProblemContext, saveManualContext,
  restoreAutomaticContext, streamAiChat, clearAiConversation, previewAiPatch,
  runPatchAction,
};
