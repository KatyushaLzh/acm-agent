import {
  $, $$, api, asObject, escapeHtml, jobIdOf, navigate, pollJob,
  renderCppSource, renderVerify, setBusy, setLocalFileSelection,
  showJobProgress, state, toast, waitForJob,
} from "./core.js";
import { postEventStream } from "./sse.js";
import { renderRecommendations } from "./view_today.js";
import {
  PROFILE_LABELS, aiRequestSelection, connectionRows, ensureSelectionVerified,
  modelFor, modelOptionValue,
  modelRows, modelVerifiedForProfile, parseModelOption, profileSelection,
  renderAiAuditCards, renderAiModelPickers, selectableStrengths,
} from "./ai_model_controls.js";

const POLICY_PROFILES = ["recommendation", "plan_organize", "plan_generate", "coaching", "patch", "summary"];
let savedAiPolicy = null;
let aiPolicyDirty = false;

function aiOutcome(payload) {
  return asObject(payload?.ai?.outcome || payload?.outcome);
}

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
  const confidence = Number(proposal.confidence ?? proposal.entry?.confidence ?? 1);
  if (Number.isFinite(confidence) && confidence < 0.75 && !warnings.includes("模型置信度低，需人工核对")) {
    warnings.push("模型置信度低，需人工核对");
  }
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
  const warningIsBlocking = status === "preview" && Boolean(proposal.can_apply === false || proposal.apply_allowed === false || proposal.apply_blocked || proposal.apply_blocked_reason);
  warningBox.className = `result-box ${warnings.length ? (warningIsBlocking ? "error" : "warning") : "hidden"}`;
  warningBox.textContent = warnings.join("；");
  const markdown = knowledgeMarkdown(proposal);
  $("#knowledge-markdown-editor").value = markdown;
  const preview = $("#knowledge-rendered-preview");
  renderSafeKnowledgeMarkdown(preview, markdown);
  const blocked = Boolean(proposal.can_apply === false || proposal.apply_allowed === false || proposal.apply_blocked || proposal.apply_blocked_reason);
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
  if (!profileCredentialReady(state.aiStatus, "summary")) throw new Error("Summary TaskProfile 的 Provider 凭据不可用；session 已结束，但未生成总结");
  const targetId = $("#knowledge-target").value;
  if (!targetId) throw new Error("请先选择或保存一个 Markdown 目标");
  const selection = knowledgeSchemaSelection();
  const started = await api("/api/jobs/ai/knowledge/preview", { body: {
    attempt_id: attemptId,
    target_id: targetId,
    schema_mode: selection.schema_mode,
    preset: selection.preset,
    schema: selection.schema,
    ...aiRequestSelection("summary"),
  } });
  const jobId = jobIdOf(started);
  const result = jobId
    ? await waitForKnowledgeJob(jobId, epoch, "正在生成 Markdown 总结预览…")
    : started;
  if (result && epoch === state.knowledgeEpoch) {
    const proposal = result.proposal && typeof result.proposal === "object" ? result.proposal : null;
    const outcome = aiOutcome(result);
    if (!proposal) {
      const message = result.error?.message || "模型未能生成通过业务校验的总结；目标文件未修改。";
      toast("总结当前不可用", message, "error");
      return;
    }
    renderKnowledgeProposal(proposal, epoch);
    const partial = outcome.business_outcome === "partial" || outcome.apply_ready === false;
    toast(
      partial ? "总结预览不可应用" : "总结预览已生成",
      partial ? "业务门禁未通过，只供检查。" : "目标文件尚未修改，请检查可编辑内容与安全预览。",
      partial ? "error" : "success",
    );
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
    if (!profileCredentialReady(state.aiStatus, "recommendation")) {
      toast("推荐 Provider 不可用", "请先为 recommendation TaskProfile 配置可用凭据。", "error");
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
      ...aiRequestSelection("recommendation"),
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

function renderSecureStoreStatus(status) {
  const container = $("#ai-secure-store-status");
  const title = $("#ai-secure-store-title");
  const message = $("#ai-secure-store-message");
  const code = $("#ai-secure-store-code");
  const secureStore = asObject(status?.secure_store);
  const backend = String(secureStore.backend || "unavailable");
  const backendLabels = {
    dpapi: "Windows DPAPI",
    keychain: "macOS Keychain",
    secret_service: "Linux Secret Service",
    unavailable: "系统安全存储",
  };
  const label = backendLabels[backend] || "系统安全存储";
  const reported = Object.prototype.hasOwnProperty.call(secureStore, "available") || Boolean(secureStore.backend);
  const available = secureStore.available === true;
  container.className = `secure-store-status ${reported ? (available ? "good" : "warn") : "pending"}`;
  title.textContent = reported ? `${label}${available ? "可用" : "不可用"}` : "正在检测系统安全存储";
  const diagnostic = typeof secureStore.message === "string" ? secureStore.message.trim() : "";
  if (diagnostic) {
    message.textContent = diagnostic;
  } else if (available) {
    message.textContent = "新保存的 API Key 会写入当前用户的系统安全存储，不会进入配置、SQLite 或浏览器存储。";
  } else if (backend === "secret_service") {
    message.textContent = "请确认当前桌面会话的 D-Bus 与 Secret Service 已启动并解锁；Dashboard 不会自动启动服务或代替你解锁。";
  } else if (reported) {
    message.textContent = "Dashboard 可以继续运行，但保存 API Key 会明确失败；可临时使用下方环境变量回退。";
  } else {
    message.textContent = "Dashboard 仍可在安全存储不可用时启动，但不会把 API Key 写入明文或弱加密文件。";
  }
  const errorCode = typeof secureStore.error_code === "string" ? secureStore.error_code.trim() : "";
  code.textContent = errorCode;
  code.classList.toggle("hidden", !errorCode);
}

async function loadAiStatus() {
  try {
    const status = await api("/api/ai/status");
    state.aiStatus = status;
    state.aiSelections = {};
    if (state.aiConversationId && state.aiConversationSelection) {
      state.aiSelections.coaching = state.aiConversationSelection;
    }
    renderSecureStoreStatus(status);
    const connections = connectionRows(status);
    const readyConnections = connections.filter(item => item.enabled !== false && item.credential?.detected && !item.credential?.error);
    const badge = $("#ai-key-state");
    badge.className = `badge ${readyConnections.length ? "good" : "warn"}`;
    badge.textContent = readyConnections.length ? `${readyConnections.length} 个可用` : "暂无可用连接";
    const coachingReady = profileCredentialReady(status, "coaching");
    const summaryReady = profileCredentialReady(status, "summary");
    const canConfigureSummary = readyConnections.length > 0;
    $("#ai-chat-state").className = `badge ${coachingReady ? "good" : "warn"}`;
    $("#ai-chat-state").textContent = coachingReady ? "可用" : "未配置";
    const knowledgeToggle = $("#knowledge-enabled");
    knowledgeToggle.disabled = !canConfigureSummary;
    knowledgeToggle.title = canConfigureSummary ? "" : "请先在设置页添加可用模型连接";
    $("#knowledge-key-hint").textContent = summaryReady
      ? "仅在勾选后调用 Summary TaskProfile；关闭 session 本身不会产生 AI 费用。"
      : canConfigureSummary
        ? "勾选后可选择并验证 Markdown 总结模型；关闭 session 本身不会产生 AI 费用。"
        : "需要先在设置页添加连接并选择可用模型。";
    if (!canConfigureSummary) {
      knowledgeToggle.checked = false;
      $("#knowledge-options").classList.add("hidden");
    }
    renderConnectionManagement(status);
    renderAiModelPickers(status);
    renderAiGovernance(status.governance);
  } catch (error) { toast("AI 状态读取失败", error.message, "error"); }
}

function clonePolicy(policy) {
  return JSON.parse(JSON.stringify(policy));
}

function fallbackModelOptions(profileId, selected) {
  const options = ['<option value="">不启用</option>'];
  let selectedFound = false;
  for (const connection of connectionRows()) {
    for (const model of modelRows(connection)) {
      const value = modelOptionValue(connection.id, model.id);
      const usable = connection.enabled !== false && model.available !== false
        && connection.credential?.detected && !connection.credential?.error
        && modelVerifiedForProfile(model, profileId, "auto");
      const isSelected = value === selected;
      selectedFound ||= isSelected;
      options.push(`<option value="${escapeHtml(value)}"${isSelected ? " selected" : ""}${usable ? "" : " disabled"}>${escapeHtml(`${connection.display_name || connection.id} / ${model.id}${usable ? "" : "（不可用）"}`)}</option>`);
    }
  }
  if (selected && !selectedFound) {
    const ref = parseModelOption(selected);
    options.push(`<option value="${escapeHtml(selected)}" selected>${escapeHtml(`${ref.provider_id} / ${ref.model}（已失效）`)}</option>`);
  }
  return options.join("");
}

function fallbackStrengthOptions(profileId, modelValue, selected = "auto") {
  const ref = parseModelOption(modelValue);
  const connection = connectionRows().find(item => String(item.id) === ref.provider_id);
  const model = modelFor({ model_ref: ref });
  const selectable = selectableStrengths(model, connection);
  return Object.entries({ auto: "Provider 默认", off: "关闭", low: "低", medium: "中", high: "高" }).map(([value, label]) => {
    const disabled = !modelValue || !selectable.has(value);
    return `<option value="${value}"${value === selected ? " selected" : ""}${disabled ? " disabled" : ""}>${label}${disabled && modelValue ? "（不支持）" : ""}</option>`;
  }).join("");
}

function budgetInput(profileId, name, label, explanation, value, { integer = true, min = 1, step = 1 } = {}) {
  return `<label>${label}<input name="${profileId}_${name}" data-budget="${name}" type="number" min="${min}" step="${step}" value="${escapeHtml(value)}" inputmode="${integer ? "numeric" : "decimal"}" required><small>${explanation}</small></label>`;
}

function fallbackEditor(profileId, index, route = {}) {
  const item = asObject(route);
  const modelValue = item.provider_id && item.model ? modelOptionValue(item.provider_id, item.model) : "";
  const ref = parseModelOption(modelValue);
  const connection = connectionRows().find(candidate => String(candidate.id) === ref.provider_id);
  const model = modelFor({ model_ref: ref });
  const valid = !modelValue || (connection && connection.enabled !== false && connection.credential?.detected
    && !connection.credential?.error && model?.available !== false
    && modelVerifiedForProfile(model, profileId, item.reasoning_strength || "auto"));
  return `<div class="fallback-route-editor${valid ? "" : " fallback-invalid"}" data-fallback-slot="${index}">
    <span>备用 ${index + 1}</span>
    <select data-fallback-model aria-label="${escapeHtml(PROFILE_LABELS[profileId])}备用模型 ${index + 1}">${fallbackModelOptions(profileId, modelValue)}</select>
    <select data-fallback-strength aria-label="${escapeHtml(PROFILE_LABELS[profileId])}备用推理强度 ${index + 1}"${modelValue ? "" : " disabled"}>${fallbackStrengthOptions(profileId, modelValue, item.reasoning_strength || "auto")}</select>
    <button class="button secondary fallback-remove" type="button" data-fallback-remove>删除</button>
    ${valid ? "" : "<small class=\"fallback-invalid-note\">历史路由已失效，请删除或替换后再保存。</small>"}
  </div>`;
}

function fallbackEditors(profileId, routes) {
  if (profileId === "coaching") {
    return '<div class="fallback-disabled"><span class="badge neutral">已禁用</span><small>会话路由固定，不能跨模型回退。</small><select disabled aria-label="Coaching fallback 已禁用"><option>不允许配置</option></select></div>';
  }
  const items = (Array.isArray(routes) ? routes : []).slice(0, 3);
  return `${items.map((route, index) => fallbackEditor(profileId, index, route)).join("")}
    <button class="button secondary fallback-add" type="button" data-fallback-add${items.length >= 3 ? " disabled" : ""}>添加备用路由</button>`;
}

function renderAiPolicyEditor(policy, { force = false } = {}) {
  if (aiPolicyDirty && !force) return;
  const normalized = clonePolicy(policy);
  savedAiPolicy = normalized;
  aiPolicyDirty = false;
  const form = $("#ai-policy-form");
  const limits = asObject(normalized.hard_limits);
  form.elements.daily_cny.value = limits.daily_cny ?? "";
  form.elements.monthly_cny.value = limits.monthly_cny ?? "";
  const profiles = asObject(state.aiStatus?.profiles);
  const rows = POLICY_PROFILES.map(profileId => {
    const profile = asObject(profiles[profileId]);
    const budget = asObject(asObject(normalized.budgets)[profileId]);
    const fallbacks = Array.isArray(asObject(normalized.fallbacks)[profileId]) ? normalized.fallbacks[profileId] : [];
    const provider = profile.provider_id || "?";
    const model = profile.model || "?";
    const strength = profile.reasoning_strength || "auto";
    return `<section class="governance-row policy-profile-row" data-policy-profile="${profileId}">
      <div class="policy-profile-summary">
        <div data-label="任务" class="policy-task"><span class="policy-field-label">任务</span><strong>${escapeHtml(PROFILE_LABELS[profileId] || profileId)}</strong><small>TaskProfile · ${escapeHtml(profileId)}</small></div>
        <div data-label="主路由" class="policy-primary-route"><span class="policy-field-label">主路由</span><strong>${escapeHtml(model)}</strong><small>${escapeHtml(provider)}</small><span class="badge neutral">推理强度 · ${escapeHtml(strength)}</span></div>
        <div data-label="能力" class="policy-capability"><span class="policy-field-label">能力</span><span class="badge ${profile.ready ? "good" : "warn"}">${profile.ready ? "已验证" : "不可用"}</span><small>${profile.ready ? "所需能力与推理证据完整" : escapeHtml(profile.error || "路由能力尚未满足")}</small></div>
        <div data-label="模型路由回退" class="policy-fallback-grid"><span class="policy-field-label">模型路由回退</span>${fallbackEditors(profileId, fallbacks)}</div>
      </div>
      <div data-label="预算" class="policy-budget-section">
        <span class="policy-field-label">预算</span>
        <div class="policy-budget-grid">
          ${budgetInput(profileId, "max_requests", "最大请求数", "包含 transport retry、JSON repair 和 fallback。", budget.max_requests)}
          ${budgetInput(profileId, "max_retries", "最大重试次数", "仅 transport retry，必须小于最大请求数。", budget.max_retries, { min: 0 })}
          ${budgetInput(profileId, "request_timeout_seconds", "任务超时（秒）", "整个任务跨重试和 fallback 共享的总时限。", budget.request_timeout_seconds, { integer: false, min: 0.1, step: 0.1 })}
          ${budgetInput(profileId, "max_output_tokens", "最大输出 Token", "单次 Provider 响应的输出上限。", budget.max_output_tokens)}
          ${budgetInput(profileId, "max_total_tokens", "最大总 Token", "整个任务跨重试和 fallback 的累计上限。", budget.max_total_tokens)}
        </div>
      </div>
    </section>`;
  });
  const table = $("#ai-route-policy-table");
  table.className = "governance-table";
  table.innerHTML = rows.join("");
  updateAiPolicyDirtyState();
}

function updateAiPolicyDirtyState() {
  const status = $("#ai-policy-dirty-state");
  status.className = `subtle${aiPolicyDirty ? " policy-dirty" : ""}`;
  status.textContent = aiPolicyDirty ? "有未保存修改；刷新或关闭页面前请先保存或放弃。" : "策略与已保存版本一致。";
  $("#ai-policy-save").disabled = !aiPolicyDirty;
  $("#ai-policy-reset").disabled = !aiPolicyDirty;
}

function markAiPolicyDirty() {
  aiPolicyDirty = true;
  updateAiPolicyDirtyState();
}

function resetAiPolicyDraft() {
  if (!savedAiPolicy) return;
  renderAiPolicyEditor(savedAiPolicy, { force: true });
  toast("已放弃未保存修改", "当前策略已恢复为服务端保存版本。");
}

function bindAiPolicyDraftEvents() {
  const form = $("#ai-policy-form");
  form.addEventListener("click", event => {
    const add = event.target.closest("[data-fallback-add]");
    if (add) {
      const grid = add.closest(".policy-fallback-grid");
      const row = add.closest("[data-policy-profile]");
      const count = $$("[data-fallback-slot]", grid).length;
      if (count < 3) add.insertAdjacentHTML("beforebegin", fallbackEditor(row.dataset.policyProfile, count));
      add.disabled = count + 1 >= 3;
      markAiPolicyDirty();
      return;
    }
    const remove = event.target.closest("[data-fallback-remove]");
    if (remove) {
      const grid = remove.closest(".policy-fallback-grid");
      const profileId = remove.closest("[data-policy-profile]").dataset.policyProfile;
      remove.closest("[data-fallback-slot]").remove();
      const routes = $$("[data-fallback-slot]", grid).map(editor => {
        const ref = parseModelOption($("[data-fallback-model]", editor).value);
        return { ...ref, reasoning_strength: $("[data-fallback-strength]", editor).value || "auto" };
      });
      grid.innerHTML = fallbackEditors(profileId, routes.filter(route => route.provider_id && route.model));
      markAiPolicyDirty();
    }
  });
  form.addEventListener("input", event => {
    event.target.setCustomValidity?.("");
    markAiPolicyDirty();
  });
  form.addEventListener("change", event => {
    if (event.target.matches("[data-fallback-model]")) {
      const editor = event.target.closest("[data-fallback-slot]");
      const strength = $("[data-fallback-strength]", editor);
      strength.innerHTML = fallbackStrengthOptions(editor.closest("[data-policy-profile]").dataset.policyProfile, event.target.value, "auto");
      strength.disabled = !event.target.value;
      editor.classList.remove("fallback-invalid");
      $(".fallback-invalid-note", editor)?.remove();
    }
    markAiPolicyDirty();
  });
  window.addEventListener("beforeunload", event => {
    if (!aiPolicyDirty) return;
    event.preventDefault();
    event.returnValue = "";
  });
}

function invalidPolicyField(input, message) {
  input.setCustomValidity(message);
  input.reportValidity();
  input.focus();
  throw new Error(message);
}

function readPolicyNumber(input, label, { integer = true, allowBlank = false, min = 0 } = {}) {
  const raw = input.value.trim();
  if (!raw && allowBlank) return null;
  const value = Number(raw);
  if (!Number.isFinite(value) || value <= min || (integer && !Number.isInteger(value))) {
    return invalidPolicyField(input, `${label}必须是${integer ? "整数" : "数字"}且大于 ${min}`);
  }
  return value;
}

function readAiPolicyDraft() {
  const form = $("#ai-policy-form");
  $$('[name], select', form).forEach(input => input.setCustomValidity?.(""));
  const policy = { budgets: {}, fallbacks: {}, hard_limits: {} };
  policy.hard_limits.daily_cny = readPolicyNumber(form.elements.daily_cny, "每日上限", { integer: false, allowBlank: true, min: 0 });
  policy.hard_limits.monthly_cny = readPolicyNumber(form.elements.monthly_cny, "每月上限", { integer: false, allowBlank: true, min: 0 });
  if (policy.hard_limits.daily_cny != null && policy.hard_limits.monthly_cny != null && policy.hard_limits.daily_cny > policy.hard_limits.monthly_cny) {
    invalidPolicyField(form.elements.monthly_cny, "每月上限不能小于每日上限");
  }
  for (const profileId of POLICY_PROFILES) {
    const row = $(`[data-policy-profile="${profileId}"]`, form);
    const value = name => $(`[data-budget="${name}"]`, row);
    const savedBudget = asObject(asObject(savedAiPolicy?.budgets)[profileId]);
    const budget = {
      max_output_tokens: readPolicyNumber(value("max_output_tokens"), `${PROFILE_LABELS[profileId]}最大输出`),
      request_timeout_seconds: readPolicyNumber(value("request_timeout_seconds"), `${PROFILE_LABELS[profileId]}单请求秒数`, { integer: false }),
      max_retries: readPolicyNumber(value("max_retries"), `${PROFILE_LABELS[profileId]}最多重试`, { min: -1 }),
      max_validation_repairs: Number.isInteger(savedBudget.max_validation_repairs)
        ? savedBudget.max_validation_repairs : 0,
      max_requests: readPolicyNumber(value("max_requests"), `${PROFILE_LABELS[profileId]}最多请求`),
      max_total_tokens: readPolicyNumber(value("max_total_tokens"), `${PROFILE_LABELS[profileId]}总 token`),
    };
    if (budget.max_retries >= budget.max_requests) invalidPolicyField(value("max_retries"), `${PROFILE_LABELS[profileId]}最多重试必须小于最多请求`);
    policy.budgets[profileId] = budget;
    const routes = [];
    for (const editor of $$('[data-fallback-slot]', row)) {
      const modelSelect = $("[data-fallback-model]", editor);
      if (!modelSelect.value) continue;
      const modelRef = parseModelOption(modelSelect.value);
      const strengthSelect = $("[data-fallback-strength]", editor);
      const route = { ...modelRef, reasoning_strength: strengthSelect.value || "auto" };
      const connection = connectionRows().find(item => String(item.id) === modelRef.provider_id);
      const model = modelFor({ model_ref: modelRef });
      const usable = connection && connection.enabled !== false && connection.credential?.detected
        && !connection.credential?.error && model?.available !== false;
      if (!usable || !model || !modelVerifiedForProfile(model, profileId, route.reasoning_strength)) {
        invalidPolicyField(modelSelect, `${PROFILE_LABELS[profileId]}备用模型尚未通过所需能力与推理强度验证`);
      }
      routes.push(route);
    }
    if (profileId === "coaching" && routes.length) throw new Error("Coaching 禁止配置跨模型回退");
    if (routes.length > 3) throw new Error(`${PROFILE_LABELS[profileId]}最多只能配置 3 条备用路由`);
    const primary = profileSelection(profileId);
    const identities = new Set([`${primary.model_ref.provider_id}\0${primary.model_ref.model}\0${primary.reasoning_strength}`]);
    for (const route of routes) {
      const identity = `${route.provider_id}\0${route.model}\0${route.reasoning_strength}`;
      if (identities.has(identity)) throw new Error(`${PROFILE_LABELS[profileId]}备用路由不能重复主路由或前序备用路由`);
      identities.add(identity);
    }
    policy.fallbacks[profileId] = routes;
  }
  return policy;
}

function renderAiGovernance(governance, options = {}) {
  const data = asObject(governance);
  const catalog = asObject(data.price_catalog);
  const policy = asObject(data.policy);
  $("#ai-price-version").textContent = catalog.version || "价格未知";
  renderAiAuditCards($("#ai-governance-summary"), data.audit, "近 30 天");
  if (policy.budgets && policy.fallbacks && policy.hard_limits) renderAiPolicyEditor(policy, { force: options.forcePolicy });
}

async function saveAiPolicy(button) {
  const policy = readAiPolicyDraft();
  setBusy(button, true, "校验中…");
  try {
    const result = await api("/api/ai/policy", { body: { policy } });
    aiPolicyDirty = false;
    if (result.governance) renderAiGovernance(result.governance, { forcePolicy: true });
    await loadAiStatus();
    toast("策略已保存", "六类预算、能力门禁与模型路由回退均已重新校验。");
  } finally {
    setBusy(button, false);
    updateAiPolicyDirtyState();
  }
}

async function repriceAiCosts(button) {
  setBusy(button, true, "重算中…");
  try {
    await api("/api/ai/costs/reprice", { body: {} });
    await loadAiStatus();
    toast("DeepSeek 费用已重算", "已追加当前 DeepSeek 价格版本；全模型历史 token 未修改。");
  } finally { setBusy(button, false); }
}

function profileCredentialReady(status, profileId) {
  const profile = asObject(asObject(status?.profiles)[profileId]);
  if (typeof profile.ready === "boolean") return profile.ready;
  const provider = connectionRows(status).find(item => item.id === profile.provider_id);
  return Boolean(provider?.enabled !== false && provider?.credential?.detected && !provider?.credential?.error);
}

function resetConnectionForm() {
  const form = $("#ai-connection-form");
  form.reset();
  form.elements.connection_id.value = "";
  form.elements.base_url.readOnly = false;
  form.elements.base_url.title = "";
  $("button[type=submit]", form).textContent = "添加连接";
  $("#ai-connection-cancel").classList.add("hidden");
}

function connectionModels(connection) {
  if (Array.isArray(connection?.models)) return connection.models;
  return Object.entries(asObject(connection?.models)).map(([id, value]) => ({ id, ...asObject(value) }));
}

function renderConnectionManagement(status) {
  const container = $("#ai-connection-list");
  container.replaceChildren();
  const connections = connectionRows(status);
  if (!connections.length) {
    const empty = document.createElement("p");
    empty.className = "empty-state compact";
    empty.textContent = "暂无模型连接。填写上方三个字段即可自动发现模型。";
    container.append(empty);
    return;
  }
  for (const connection of connections) {
    const card = document.createElement("article");
    card.className = "ai-connection-card";
    card.dataset.connectionId = connection.id;
    const heading = document.createElement("div");
    heading.className = "ai-connection-heading";
    const identity = document.createElement("div");
    identity.className = "ai-connection-identity";
    const name = document.createElement("strong");
    name.textContent = connection.display_name || connection.id;
    name.title = name.textContent;
    const url = document.createElement("span");
    url.textContent = connection.base_url || "";
    url.title = url.textContent;
    identity.append(name, url);
    const ready = connection.enabled !== false && connection.credential?.detected && !connection.credential?.error;
    const badge = document.createElement("span");
    badge.className = `badge ${ready ? "good" : "warn"}`;
    badge.textContent = ready ? "凭据可用" : (connection.credential?.error ? "凭据错误" : "缺少凭据");
    heading.append(identity, badge);
    const models = document.createElement("div");
    models.className = "ai-connection-models";
    for (const model of connectionModels(connection)) {
      const chip = document.createElement("span");
      chip.className = `chip${model.available === false ? " unavailable" : ""}`;
      chip.textContent = model.id;
      chip.title = model.id;
      models.append(chip);
    }
    if (!models.childElementCount) models.textContent = "尚未发现模型";
    const actions = document.createElement("div");
    actions.className = "card-actions ai-connection-actions";
    for (const [action, label, className] of [
      ["refresh", "刷新模型", "button secondary"],
      ["edit", "编辑", "button secondary"],
      ["delete", "删除", "button danger"],
    ]) {
      if (connection.builtin && action === "delete") continue;
      const button = document.createElement("button");
      button.type = "button";
      button.className = className;
      button.dataset.connectionAction = action;
      button.textContent = label;
      actions.append(button);
    }
    card.append(heading, models, actions);
    container.append(card);
  }
}

async function saveConnection(form) {
  const button = $("button[type=submit]", form);
  setBusy(button, true, "保存中…");
  const keyInput = form.elements.api_key;
  try {
    const connectionId = form.elements.connection_id.value.trim();
    if (!connectionId && !keyInput.value.trim()) throw new Error("新建连接时 API Key 不能为空");
    await api("/api/ai/connections", { body: {
      ...(connectionId ? { connection_id: connectionId } : {}),
      display_name: form.elements.display_name.value.trim(),
      base_url: form.elements.base_url.value.trim(),
      api_key: keyInput.value,
    } });
    toast(connectionId ? "连接已更新" : "连接已添加", "模型列表已从标准 /models 自动发现。");
    resetConnectionForm();
    await loadAiStatus();
  } finally {
    keyInput.value = "";
    setBusy(button, false);
  }
}

function editConnection(connectionId) {
  const connection = connectionRows().find(item => String(item.id) === String(connectionId));
  if (!connection) throw new Error("连接已不存在，请刷新页面");
  const form = $("#ai-connection-form");
  form.elements.connection_id.value = connection.id;
  form.elements.display_name.value = connection.display_name || connection.id;
  form.elements.base_url.value = connection.base_url || "";
  form.elements.base_url.readOnly = Boolean(connection.builtin);
  form.elements.base_url.title = connection.builtin ? "内置 DeepSeek 固定使用官方 Base URL" : "";
  form.elements.api_key.value = "";
  $("button[type=submit]", form).textContent = "保存更改";
  $("#ai-connection-cancel").classList.remove("hidden");
  form.elements.display_name.focus();
}

async function refreshConnection(connectionId, button) {
  setBusy(button, true, "刷新中…");
  try {
    await api("/api/ai/connections/refresh", { body: { connection_id: connectionId } });
    toast("模型列表已刷新", "已消失的模型会保留为不可用，不会自动换模。");
    await loadAiStatus();
  } finally { setBusy(button, false); }
}

async function deleteConnection(connectionId, button) {
  if (!window.confirm("删除此模型连接？正在引用它的功能会阻止删除，并明确列出引用。")) return;
  setBusy(button, true, "删除中…");
  try {
    await api("/api/ai/connections/delete", { body: { connection_id: connectionId } });
    toast("连接已删除", connectionId, "success");
    resetConnectionForm();
    await loadAiStatus();
  } finally { setBusy(button, false); }
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
  state.aiConversationSelection = null;
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
  try { data = await api("/api/ai/conversations", { body: { problem, ...aiRequestSelection("coaching") } }); }
  catch (error) { if (!aiOperationIsCurrent(problemKey, epoch)) return null; throw error; }
  if (!aiOperationIsCurrent(problemKey, epoch)) return null;
  state.aiConversationId = data.conversation_id;
  state.aiConversationProblemKey = problemKey;
  state.aiConversationSelection = data.model_ref?.provider_id && data.model_ref?.model
    ? { model_ref: data.model_ref, reasoning_strength: data.reasoning_strength || "auto" }
    : aiRequestSelection("coaching");
  state.aiSelections.coaching = state.aiConversationSelection;
  renderAiModelPickers(state.aiStatus);
  $("#ai-chat-state").className = "badge good";
  $("#ai-chat-state").textContent = data.model_ref?.provider_id && data.model_ref?.model
    ? `已固定 ${data.model_ref.provider_id}/${data.resolved_model || data.model_ref.model}`
    : "已连接";
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
      { message, mode, hint_level: hintLevel, ...aiRequestSelection("coaching") },
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
      else if (item.event === "done") {
        completed = true;
        const outcome = aiOutcome(item.data);
        const business = outcome.business_outcome || item.data.status || "complete";
        if (business === "partial") {
          assistant.classList.add("interrupted");
          $("#ai-chat-state").textContent = "部分结果";
        } else if (business === "unavailable") {
          assistant.classList.add("interrupted");
          if (!assistant.textContent.trim()) assistant.textContent = item.data.message || "本次辅导当前不可用，请稍后重试。";
          $("#ai-chat-state").textContent = "当前不可用";
        }
      }
      else if (item.event === "error") throw new Error(item.data.message || "模型流式请求失败");
    };
    for (;;) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const blocks = buffer.split(/\r?\n\r?\n/); buffer = blocks.pop() || "";
      for (const block of blocks) consumeBlock(block);
      if (done && buffer.trim()) { consumeBlock(buffer); buffer = ""; }
      if (completed) { await reader.cancel(); break; }
      if (done) throw new Error("模型流式响应在完成事件前中断");
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
    const selection = aiRequestSelection("coaching");
    if (!await ensureSelectionVerified("patch", selection)) return;
    const conversationId = state.aiConversationProblemKey === problemKey ? state.aiConversationId : null;
    const started = await api("/api/jobs/ai/patches/preview", { body: {
      problem, instruction, conversation_id: conversationId || null,
      ...selection,
    } });
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
  requestAiRecommendations, loadAiStatus,
  bindAiPolicyDraftEvents, resetAiPolicyDraft, saveAiPolicy, repriceAiCosts,
  saveConnection, editConnection, refreshConnection, deleteConnection, resetConnectionForm,
  currentAiProblem, isAbortError, updateAiProblemMode,
  switchAiProblem, loadAiProblemState, loadProblemContext, saveManualContext,
  restoreAutomaticContext, streamAiChat, clearAiConversation, previewAiPatch,
  runPatchAction,
};
