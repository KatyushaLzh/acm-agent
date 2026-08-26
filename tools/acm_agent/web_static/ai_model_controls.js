import { $, $$, api, asObject, jobIdOf, statCard, state, toast, waitForJob } from "./core.js";

const PROFILE_LABELS = {
  recommendation: "题目推荐",
  plan_organize: "题单整理",
  plan_generate: "题单生成",
  coaching: "对话辅导",
  patch: "代码补丁",
  summary: "Markdown 总结",
};
const STRENGTH_LABELS = { auto: "Provider 默认", off: "关闭", low: "低", medium: "中", high: "高" };
const PROFILE_CAPABILITIES = {
  recommendation: ["text_chat", "json_object", "usage"],
  plan_organize: ["text_chat", "json_object", "usage"],
  plan_generate: ["text_chat", "json_object", "usage"],
  coaching: ["text_chat", "streaming", "usage", "stream_usage"],
  patch: ["text_chat", "json_object", "usage"],
  summary: ["text_chat", "json_object", "usage"],
};

function connectionRows(status = state.aiStatus) {
  if (Array.isArray(status?.connections)) return status.connections;
  return (Array.isArray(status?.providers) ? status.providers : []).map(provider => ({
    id: provider.id || provider.provider_id,
    display_name: provider.display_name || provider.name || provider.id,
    base_url: provider.base_url,
    builtin: provider.id === "deepseek",
    enabled: provider.enabled !== false,
    credential: provider.credential,
    models: provider.models,
  }));
}

function modelRows(connection) {
  if (Array.isArray(connection?.models)) return connection.models;
  return Object.entries(asObject(connection?.models)).map(([id, value]) => ({ id, ...asObject(value) }));
}

function profileSelection(profileId, status = state.aiStatus) {
  const profile = asObject(asObject(status?.profiles)[profileId]);
  return {
    model_ref: {
      provider_id: String(profile.model_ref?.provider_id || profile.provider_id || ""),
      model: String(profile.model_ref?.model || profile.model || ""),
    },
    reasoning_strength: String(profile.reasoning_strength || "auto"),
  };
}

function sameSelection(left, right) {
  return left?.model_ref?.provider_id === right?.model_ref?.provider_id
    && left?.model_ref?.model === right?.model_ref?.model
    && left?.reasoning_strength === right?.reasoning_strength;
}

function connectionFor(providerId) {
  return connectionRows().find(row => String(row.id) === String(providerId));
}

function modelFor(selection) {
  const connection = connectionFor(selection?.model_ref?.provider_id);
  return modelRows(connection).find(row => String(row.id) === String(selection?.model_ref?.model));
}

function availableStrengths(model) {
  const raw = Array.isArray(model?.reasoning_strengths) ? model.reasoning_strengths : [];
  const supported = new Set(raw.filter(value => Object.hasOwn(STRENGTH_LABELS, value)));
  supported.add("auto");
  return supported;
}

function selectableStrengths(model, connection) {
  const supported = new Set(["auto"]);
  if (model?.capabilities?.thinking) {
    for (const value of ["off", "low", "medium", "high"]) supported.add(value);
  }
  if (connection?.builtin) supported.delete("low");
  return supported;
}

function modelVerifiedForProfile(model, profileId, strength) {
  const verified = new Set(Array.isArray(model?.verified_capabilities) ? model.verified_capabilities : []);
  const capabilitiesReady = (PROFILE_CAPABILITIES[profileId] || []).every(value => verified.has(value));
  const strengthReady = strength === "auto" || availableStrengths(model).has(strength);
  return capabilitiesReady && strengthReady;
}

function formatInteger(value) {
  return value !== null && value !== undefined && Number.isFinite(Number(value)) ? Number(value).toLocaleString() : "未知";
}

function formatCny(value) {
  return `¥${Number(value).toFixed(6)}`;
}

function formatDeepSeekCost(cost) {
  const item = asObject(cost);
  const status = String(item.status || "");
  if (status === "out_of_scope") return "未计入费用统计";
  if (status === "unknown") return "未知";
  if (["known", "partial"].includes(status) && Number.isFinite(Number(item.amount))) {
    return `${status === "partial" ? "至少 " : ""}${formatCny(item.amount)}`;
  }
  return "未知";
}

function renderAiAuditCards(root, audit, windowLabel = "当前窗口") {
  const data = asObject(audit);
  const cost = asObject(data.deepseek_cost);
  const tokens = asObject(data.all_model_tokens);
  const cache = asObject(data.cache_metrics);
  const costValue = cost.runs && cost.unknown_cost_runs === cost.runs
    ? "未知"
    : formatCny(cost.known_estimated_cny || 0);
  const tokenValue = tokens.runs && tokens.unknown_runs === tokens.runs
    ? "未知"
    : formatInteger(tokens.total_tokens_known);
  const cacheValue = cache.hit_rate_percent !== null && cache.hit_rate_percent !== undefined
    && Number.isFinite(Number(cache.hit_rate_percent))
    ? `${Number(cache.hit_rate_percent).toFixed(1)}%`
    : "暂无可观测数据";
  root.innerHTML = [
    statCard("DeepSeek 30 天估算费用（人民币）", costValue, `${windowLabel} · 按 DeepSeek 官方人民币单价核算 · ${cost.runs ?? 0} runs · ${cost.partial_cost_runs ?? 0} 次部分 · ${cost.unknown_cost_runs ?? 0} 次未知`),
    statCard("全模型 30 天 Token", tokenValue, `输入 ${formatInteger(tokens.input_tokens_known)} · 输出 ${formatInteger(tokens.output_tokens_known)} · ${tokens.unknown_runs ?? 0} 次缺失`),
    statCard("全模型缓存命中率", cacheValue, `读取 ${formatInteger(cache.cache_read_tokens_known)} / 可观测输入 ${formatInteger(cache.eligible_input_tokens)} · ${cache.observed_runs ?? 0} 次可观测 · ${cache.unknown_runs ?? 0} 次缺失 · ${cache.invalid_runs ?? 0} 次无效`),
  ].join("");
}

function modelOptionValue(providerId, model) {
  return JSON.stringify({ provider_id: String(providerId), model: String(model) });
}

function parseModelOption(value) {
  try {
    const parsed = JSON.parse(value);
    return { provider_id: String(parsed.provider_id || ""), model: String(parsed.model || "") };
  } catch {
    return { provider_id: "", model: "" };
  }
}

function createOption(value, label, { disabled = false, title = "" } = {}) {
  const option = document.createElement("option");
  option.value = value;
  option.textContent = label;
  option.disabled = disabled;
  option.title = title || label;
  return option;
}

function pickerStatus(profileId, selection) {
  const profile = asObject(asObject(state.aiStatus?.profiles)[profileId]);
  const connection = connectionFor(selection.model_ref.provider_id);
  const model = modelFor(selection);
  if (!connection) return { className: "error", text: "连接已不存在，请重新选择。" };
  if (connection.enabled === false) return { className: "error", text: "此连接已停用。" };
  if (!connection.credential?.detected || connection.credential?.error) {
    return { className: "error", text: connection.credential?.error || "此连接缺少可用凭据。" };
  }
  if (!model || model.available === false) return { className: "error", text: "模型当前不可用，不会自动换模。" };
  if (sameSelection(profileSelection(profileId), selection) && profile.ready === true) {
    return { className: "ready", text: "已验证，可用于此功能。" };
  }
  if (modelVerifiedForProfile(model, profileId, selection.reasoning_strength)) {
    return { className: "ready", text: "能力证据已验证。" };
  }
  return { className: "pending", text: "首次使用前需验证；会产生一次小额模型调用。" };
}

function readPicker(root) {
  const modelRef = parseModelOption($("select[name=model_ref]", root)?.value || "");
  return {
    model_ref: modelRef,
    reasoning_strength: $("select[name=reasoning_strength]", root)?.value || "auto",
  };
}

function updatePickerHint(root) {
  const profileId = root.dataset.aiModelPicker;
  const selection = readPicker(root);
  const status = pickerStatus(profileId, selection);
  const hint = $(".ai-model-picker-status", root);
  hint.className = `ai-model-picker-status ${status.className}`;
  hint.textContent = status.text;
}

function fillStrengthSelect(root, preferred) {
  const model = modelFor(readPicker(root));
  const connection = connectionFor(readPicker(root).model_ref.provider_id);
  const selectable = selectableStrengths(model, connection);
  const verified = availableStrengths(model);
  const select = $("select[name=reasoning_strength]", root);
  select.replaceChildren();
  for (const [value, label] of Object.entries(STRENGTH_LABELS)) {
    const disabled = !selectable.has(value);
    const suffix = disabled ? "（不支持）" : !verified.has(value) ? "（首次需验证）" : "";
    select.append(createOption(value, `${label}${suffix}`, { disabled }));
  }
  select.value = selectable.has(preferred) ? preferred : "auto";
}

function renderAiModelPicker(root, status = state.aiStatus) {
  const profileId = root.dataset.aiModelPicker;
  const selection = state.aiSelections[profileId] || profileSelection(profileId, status);
  const modelLabel = document.createElement("label");
  modelLabel.textContent = "模型";
  const modelSelect = document.createElement("select");
  modelSelect.name = "model_ref";
  modelSelect.setAttribute("aria-label", `${PROFILE_LABELS[profileId] || profileId}模型`);
  const options = [];
  for (const connection of connectionRows(status)) {
    for (const model of modelRows(connection)) {
      const unavailable = connection.enabled === false || model.available === false || !connection.credential?.detected || Boolean(connection.credential?.error);
      const label = `${connection.display_name || connection.id} / ${model.id}${unavailable ? "（不可用）" : ""}`;
      options.push(createOption(modelOptionValue(connection.id, model.id), label, { disabled: unavailable, title: label }));
    }
  }
  if (!options.length) options.push(createOption("", "请先添加模型连接", { disabled: true }));
  modelSelect.append(...options);
  const selectedValue = modelOptionValue(selection.model_ref.provider_id, selection.model_ref.model);
  if (![...modelSelect.options].some(option => option.value === selectedValue)) {
    const staleLabel = `${selection.model_ref.provider_id || "未知连接"} / ${selection.model_ref.model || "未选择"}（不可用）`;
    modelSelect.prepend(createOption(selectedValue, staleLabel, { disabled: false, title: staleLabel }));
  }
  modelSelect.value = selectedValue;
  modelSelect.title = modelSelect.selectedOptions[0]?.textContent || "";
  modelLabel.append(modelSelect);

  const strengthLabel = document.createElement("label");
  strengthLabel.textContent = "推理强度";
  const strengthSelect = document.createElement("select");
  strengthSelect.name = "reasoning_strength";
  strengthSelect.setAttribute("aria-label", `${PROFILE_LABELS[profileId] || profileId}推理强度`);
  strengthLabel.append(strengthSelect);

  const hint = document.createElement("p");
  hint.className = "ai-model-picker-status";
  hint.setAttribute("role", "status");
  root.replaceChildren(modelLabel, strengthLabel, hint);
  fillStrengthSelect(root, selection.reasoning_strength);
  updatePickerHint(root);
  state.aiSelections[profileId] = readPicker(root);
}

function renderAiModelPickers(status = state.aiStatus) {
  for (const root of $$('[data-ai-model-picker]')) renderAiModelPicker(root, status);
}

function aiRequestSelection(profileId) {
  const roots = $$(`[data-ai-model-picker="${profileId}"]`);
  const root = roots.find(item => !item.classList.contains("hidden")) || roots[0];
  const selection = root ? readPicker(root) : (state.aiSelections[profileId] || profileSelection(profileId));
  if (!selection.model_ref.provider_id || !selection.model_ref.model) {
    throw new Error(`${PROFILE_LABELS[profileId] || profileId}尚未选择可用模型`);
  }
  return selection;
}

async function verifySelection(profileId, selection) {
  const started = await api("/api/jobs/ai/models/verify", { body: {
    profile_id: profileId,
    ...selection,
  } });
  const jobId = jobIdOf(started);
  if (!jobId) throw new Error("服务未返回模型验证任务");
  const result = await waitForJob(jobId, `正在验证 ${PROFILE_LABELS[profileId] || profileId} 的模型能力…`);
  if (result?.ok !== true) {
    const failedCases = (Array.isArray(result?.report?.cases) ? result.report.cases : [])
      .filter(item => item?.ok !== true)
      .map(item => `${item.name || "unknown"}${item.error_code ? ` (${item.error_code})` : ""}`);
    const detail = failedCases.length ? failedCases.join("、") : "服务未返回通过证据";
    throw new Error(`模型能力验证未通过：${detail}`);
  }
}

async function ensureSelectionVerified(profileId, selection) {
  const model = modelFor(selection);
  const currentProfile = asObject(asObject(state.aiStatus?.profiles)[profileId]);
  const alreadyReady = sameSelection(profileSelection(profileId), selection) && currentProfile.ready === true;
  if (alreadyReady || modelVerifiedForProfile(model, profileId, selection.reasoning_strength)) return true;
  const accepted = window.confirm(`首次将此模型用于${PROFILE_LABELS[profileId] || profileId}需要验证，会产生一次小额 API 调用。继续吗？`);
  if (!accepted) return false;
  await verifySelection(profileId, selection);
  return true;
}

async function switchConversationSelection(selection) {
  if (!state.aiConversationId) return;
  const data = await api(`/api/ai/conversations/${encodeURIComponent(state.aiConversationId)}/switch`, { body: selection });
  if (!data?.conversation_id) throw new Error("服务未返回新的 conversation_id");
  state.aiConversationId = data.conversation_id;
  state.aiConversationSelection = selection;
  const messages = $("#ai-chat-messages");
  messages.className = "ai-chat-messages empty-state compact";
  messages.textContent = "已切换模型，后续消息将写入新的空会话；旧会话仍保留审计记录。";
}

async function savePickerSelection(root, reloadStatus) {
  const profileId = root.dataset.aiModelPicker;
  const previous = state.aiSelections[profileId] || profileSelection(profileId);
  const selection = readPicker(root);
  if (!selection.model_ref.provider_id || !selection.model_ref.model) return;
  if (profileId === "coaching" && state.aiStreamController) {
    toast("暂时无法切换模型", "请等待当前流式回答结束后再切换。", "error");
    renderAiModelPicker(root);
    return;
  }
  if (!await ensureSelectionVerified(profileId, selection)) {
    renderAiModelPicker(root);
    return;
  }
  if (profileId === "coaching" && !sameSelection(previous, selection)) await switchConversationSelection(selection);
  await api("/api/ai/profiles", { body: { profile_id: profileId, ...selection } });
  state.aiSelections[profileId] = selection;
  toast(`${PROFILE_LABELS[profileId] || profileId}模型已保存`, `${selection.model_ref.provider_id} / ${selection.model_ref.model} · ${STRENGTH_LABELS[selection.reasoning_strength]}`);
  if (typeof reloadStatus === "function") await reloadStatus();
}

function bindAiModelPickerEvents(reloadStatus) {
  document.addEventListener("change", async event => {
    const root = event.target.closest?.("[data-ai-model-picker]");
    if (!root || !event.target.matches("select")) return;
    if (event.target.name === "model_ref") {
      event.target.title = event.target.selectedOptions[0]?.textContent || "";
      fillStrengthSelect(root, profileSelection(root.dataset.aiModelPicker).reasoning_strength);
      updatePickerHint(root);
    }
    if (state.aiSelectionBusy) return;
    state.aiSelectionBusy = true;
    $$('[data-ai-model-picker] select').forEach(select => { select.disabled = true; });
    try {
      await savePickerSelection(root, reloadStatus);
    } catch (error) {
      toast("模型选择保存失败", error.message, "error");
      renderAiModelPicker(root);
    } finally {
      state.aiSelectionBusy = false;
      $$('[data-ai-model-picker] select').forEach(select => { select.disabled = false; });
    }
  });
}

export {
  PROFILE_LABELS, aiRequestSelection, bindAiModelPickerEvents, connectionRows,
  ensureSelectionVerified,
  formatDeepSeekCost, modelFor, modelOptionValue, modelRows,
  modelVerifiedForProfile, parseModelOption, profileSelection, renderAiAuditCards,
  renderAiModelPickers, selectableStrengths,
};
