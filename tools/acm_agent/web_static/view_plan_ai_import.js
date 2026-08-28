import {
  $, $$, api, asObject, cancelQueuedJob, escapeHtml, jobIdOf, jobProgressLabel,
  navigate, pollJob, state, toast,
} from "./core.js";
import { importErrors, submitPlanImport } from "./view_plans.js";
import { aiRequestSelection } from "./ai_model_controls.js";

function deepClone(value) { return JSON.parse(JSON.stringify(value)); }
function draftStages() {
  if (!state.aiPlanDraft) return [];
  if (!Array.isArray(state.aiPlanDraft.stages)) state.aiPlanDraft.stages = [];
  return state.aiPlanDraft.stages;
}
function stageTasks(stage) {
  if (!Array.isArray(stage.tasks)) stage.tasks = [];
  return stage.tasks;
}
function previewDuplicate(preview) {
  return Boolean(preview?.duplicate || preview?.exists || preview?.requires_confirmation);
}
function previewPlan(preview) {
  return preview?.plan || preview?.document || preview?.data?.plan || null;
}
function displayMessage(value) {
  if (typeof value === "string") return value;
  if (!value || typeof value !== "object") return String(value ?? "");
  const prefix = value.path || value.pointer || value.problem_key || value.problem_id || "";
  const message = value.message || value.reason || value.detail || JSON.stringify(value);
  return prefix ? `${prefix}: ${message}` : String(message);
}
function previewWarnings(preview) {
  const groups = [
    ["警告", preview?.warnings],
    ["警告", state.aiPlanMetadata?.warnings],
    ["假设", preview?.assumptions],
    ["假设", state.aiPlanMetadata?.assumptions],
    ["未解析", preview?.unresolved],
    ["未解析", state.aiPlanMetadata?.unresolved],
  ];
  const lines = [];
  const seen = new Set();
  for (const [label, values] of groups) {
    for (const value of Array.isArray(values) ? values : []) {
      const line = `${label}：${displayMessage(value)}`;
      if (!seen.has(line)) { seen.add(line); lines.push(line); }
    }
  }
  const fallback = state.aiPlanMetadata?.fallback;
  if (fallback) lines.push(`AI 降级：${displayMessage(fallback)}`);
  return lines;
}
function previewDiffText(preview) {
  const diff = asObject(preview?.diff);
  if (!Object.keys(diff).length) return preview?.diff_summary || "";
  return `阶段 ${diff.stages_before ?? 0} → ${diff.stages_after ?? 0}，题目 ${diff.tasks_before ?? 0} → ${diff.tasks_after ?? 0}${diff.title_changed ? "，标题有修改" : ""}`;
}

function draftTaskCount() {
  return draftStages().reduce((total, stage) => total + stageTasks(stage).length, 0);
}

function generatedDraftRequirement() {
  if (state.aiPlanMetadata?.mode !== "generate") return null;
  const requested = Number(state.aiPlanMetadata.requested_count);
  if (!Number.isInteger(requested) || requested < 1) return null;
  const current = draftTaskCount();
  return { requested, current, missing: Math.max(0, requested - current) };
}

function showAiPlanProgress(label, progress = null) {
  const root = $("#ai-plan-progress");
  const bar = $("progress", root);
  root.classList.remove("hidden");
  $("strong", root).textContent = progress?.message || label;
  const selecting = progress?.phase === "selecting";
  const step = Number(selecting ? progress?.round : progress?.step);
  const total = Number(selecting ? progress?.total_rounds : progress?.total);
  if (Number.isFinite(step) && Number.isFinite(total) && total > 0) {
    bar.max = total;
    bar.value = Math.min(total, Math.max(0, step));
  } else {
    bar.max = 100;
    bar.removeAttribute("value");
  }
  const round = Number(progress?.round);
  const totalRounds = Number(progress?.total_rounds);
  const accepted = Number(progress?.accepted_count);
  const requested = Number(progress?.requested_count);
  $("#ai-plan-progress-round").textContent = Number.isInteger(round) && Number.isInteger(totalRounds)
    ? `第${round}/${totalRounds}轮`
    : "";
  $("#ai-plan-progress-count").textContent = Number.isInteger(accepted) && Number.isInteger(requested)
    ? `${accepted}/${requested}题`
    : "";
}

function hideAiPlanProgress() {
  $("#ai-plan-progress").classList.add("hidden");
}

async function stopAiPlanWork({ cancelJob = true } = {}) {
  const jobId = state.aiPlanJobId;
  state.aiPlanJobId = "";
  state.aiPlanImportEpoch += 1;
  state.aiPlanValidationEpoch += 1;
  state.aiPlanImportController?.abort();
  state.aiPlanValidationController?.abort();
  state.aiPlanImportController = null;
  state.aiPlanValidationController = null;
  if (state.aiPlanValidationTimer) window.clearTimeout(state.aiPlanValidationTimer);
  state.aiPlanValidationTimer = null;
  if (cancelJob && jobId) {
    try { await cancelQueuedJob(jobId); } catch { /* A stale running job is safe to ignore here. */ }
  }
}

function resetAiPlanDialog() {
  stopAiPlanWork();
  state.aiPlanDraft = null;
  state.aiPlanPreview = null;
  state.aiPlanMetadata = null;
  $("#ai-plan-request-step").classList.remove("hidden");
  $("#ai-plan-draft-step").classList.add("hidden");
  $("#ai-plan-request-error").classList.add("hidden");
  $("#ai-plan-request-error").textContent = "";
  hideAiPlanProgress();
}

function openAiPlanImport() {
  resetAiPlanDialog();
  const dialog = $("#ai-plan-import-dialog");
  dialog.showModal();
  window.setTimeout(() => $("#ai-plan-text").focus(), 0);
}

function selectedMode() {
  return $("input[name=ai_plan_mode]:checked", $("#ai-plan-import-dialog"))?.value || "organize";
}

function syncGenerateControls() {
  const mode = selectedMode();
  $("#ai-plan-generate-controls").classList.toggle("hidden", mode !== "generate");
  $('[data-ai-model-picker="plan_organize"]').classList.toggle("hidden", mode !== "organize");
  $('[data-ai-model-picker="plan_generate"]').classList.toggle("hidden", mode !== "generate");
  $("#ai-plan-text").placeholder = mode === "generate"
    ? "例如：生成一份线段树与树状数组强化题单，先基础操作，再综合应用。"
    : "例如：第一阶段整理 CF1A、P3374；第二阶段做 P3372，并标记为提高。";
}

function aiPlanJobError(job) {
  const raw = job?.error;
  const error = new Error(raw?.message || raw || job?.message || "AI 题单生成失败");
  error.code = raw?.code || job?.code || "";
  return error;
}

async function generateAiPlan(button) {
  const text = $("#ai-plan-text").value.trim();
  const mode = selectedMode();
  const profileId = mode === "generate" ? "plan_generate" : "plan_organize";
  const count = Number($("#ai-plan-task-count").value);
  if (!text) {
    $("#ai-plan-request-error").textContent = "请先输入题目列表或训练目标。";
    $("#ai-plan-request-error").classList.remove("hidden");
    $("#ai-plan-text").focus();
    return;
  }
  if (mode === "generate" && (!Number.isInteger(count) || count < 1 || count > 30)) {
    $("#ai-plan-request-error").textContent = "生成题数必须是 1 到 30 之间的整数。";
    $("#ai-plan-request-error").classList.remove("hidden");
    return;
  }
  if (state.aiStatus?.profiles?.[profileId]?.ready === false) {
    toast("题单模型不可用", "请先选择并验证此模式使用的模型。", "error");
    $("#ai-plan-import-dialog").close();
    navigate("settings");
    return;
  }
  await stopAiPlanWork();
  const epoch = ++state.aiPlanImportEpoch;
  const controller = new AbortController();
  state.aiPlanImportController = controller;
  state.aiPlanDraft = null;
  state.aiPlanPreview = null;
  state.aiPlanMetadata = null;
  $("#ai-plan-request-error").classList.add("hidden");
  $("#ai-plan-request-step").classList.add("hidden");
  $("#ai-plan-draft-step").classList.add("hidden");
  showAiPlanProgress(mode === "generate" ? "AI 正在分析目标并筛选本地题库…" : "AI 正在识别并整理题目…");
  button.disabled = true;
  try {
    const body = { mode, text, ...aiRequestSelection(profileId) };
    if (mode === "generate") {
      body.task_count = count;
      body.include_completed = $("#ai-plan-include-completed").checked;
    }
    const started = await api("/api/jobs/ai/plans/preview", { body, signal: controller.signal });
    if (state.aiPlanImportEpoch !== epoch || controller.signal.aborted) return;
    const jobId = jobIdOf(started);
    state.aiPlanJobId = jobId;
    const result = jobId ? await pollJob(jobId, {
      interval: 650,
      rejectWaiting: true,
      toError: aiPlanJobError,
      shouldCancel: () => state.aiPlanImportEpoch !== epoch || controller.signal.aborted,
      onPoll(job) {
        const progress = asObject(job.progress);
        showAiPlanProgress(progress.message || jobProgressLabel(progress, "AI 正在生成题单草稿…"), progress);
      },
    }) : started;
    if (!result || state.aiPlanImportEpoch !== epoch || controller.signal.aborted) return;
    const plan = previewPlan(result);
    if (!plan) {
      const error = Array.isArray(result.errors) ? result.errors[0] : result.error;
      throw new Error(displayMessage(error) || "AI 任务没有返回可编辑的题单草稿");
    }
    state.aiPlanDraft = deepClone(plan);
    state.aiPlanPreview = result;
    state.aiPlanMetadata = {
      ...asObject(result.ai),
      mode,
      requested_count: Number(result.ai?.requested_count ?? result.requested_count ?? (mode === "generate" ? count : 0)),
      warnings: Array.isArray(result.warnings) ? deepClone(result.warnings) : [],
      assumptions: Array.isArray(result.assumptions) ? deepClone(result.assumptions) : [],
      unresolved: Array.isArray(result.unresolved) ? deepClone(result.unresolved) : [],
    };
    state.aiPlanJobId = "";
    $("#ai-plan-request-step").classList.add("hidden");
    $("#ai-plan-draft-step").classList.remove("hidden");
    renderAiPlanDraft();
    renderAiPlanFeedback(result);
  } catch (error) {
    if (error.name === "AbortError" || state.aiPlanImportEpoch !== epoch) return;
    if (error.code === "missing_api_key") {
      toast("尚未启用 DeepSeek", "请先在设置页输入 API Key 并保存。", "error");
      $("#ai-plan-import-dialog").close();
      navigate("settings");
      return;
    }
    $("#ai-plan-request-step").classList.remove("hidden");
    const box = $("#ai-plan-request-error");
    box.textContent = error.message;
    box.classList.remove("hidden");
    toast("AI 快速导入失败", error.message, "error");
  } finally {
    if (state.aiPlanImportController === controller) state.aiPlanImportController = null;
    if (state.aiPlanImportEpoch === epoch) {
      state.aiPlanJobId = "";
      hideAiPlanProgress();
      button.disabled = false;
    }
  }
}

function nextStageKey() {
  const used = new Set(draftStages().map(stage => String(stage.stage_key || "")));
  for (let index = 1; index < 1000; index += 1) {
    const key = `stage-${String(index).padStart(2, "0")}`;
    if (!used.has(key)) return key;
  }
  return `stage-${Date.now()}`;
}

function nextTaskKey(stage) {
  const prefix = String(stage.stage_key || "stage");
  const used = new Set(draftStages().flatMap(item => stageTasks(item).map(task => String(task.task_key || ""))));
  for (let index = 1; index < 10000; index += 1) {
    const key = `${prefix}-task-${String(index).padStart(3, "0")}`;
    if (!used.has(key)) return key;
  }
  return `${prefix}-task-${Date.now()}`;
}

function taskLocation(stageIndex, taskIndex) {
  const stage = draftStages()[stageIndex];
  return { stage, task: stage ? stageTasks(stage)[taskIndex] : null };
}

function renderAiPlanDraft() {
  const plan = state.aiPlanDraft;
  if (!plan) return;
  const stages = draftStages();
  $("#ai-plan-draft-editor").innerHTML = `
    <div class="ai-plan-meta-fields">
      <label>题单 ID<input data-ai-plan-field="plan_id" value="${escapeHtml(plan.plan_id || "")}" autocomplete="off"></label>
      <label>标题<input data-ai-plan-field="title" value="${escapeHtml(plan.title || "")}" autocomplete="off"></label>
      <label>排期<select data-ai-plan-field="schedule_mode"><option value="progressive" ${plan.schedule_mode !== "dated" ? "selected" : ""}>逐阶段解锁</option><option value="dated" ${plan.schedule_mode === "dated" ? "selected" : ""}>按日期</option></select></label>
      <label class="wide">描述<textarea data-ai-plan-field="description" rows="2">${escapeHtml(plan.description || "")}</textarea></label>
    </div>
    <div class="stage-heading"><div><p class="eyebrow">DRAFT STAGES</p><h3>${stages.length} 个阶段</h3></div><button type="button" class="button secondary" data-ai-plan-action="add-stage">新增阶段</button></div>
    <div class="ai-plan-stage-list">${stages.map((stage, stageIndex) => renderAiPlanStage(stage, stageIndex, stages.length)).join("") || '<div class="empty-state compact">草稿中还没有阶段，请先新增阶段。</div>'}</div>`;
  $("#ai-plan-json").textContent = `${JSON.stringify(plan, null, 2)}\n`;
}

function renderAiPlanStage(stage, stageIndex, stageCount) {
  const stages = draftStages();
  const tasks = stageTasks(stage);
  return `<article class="ai-plan-stage" data-ai-stage-index="${stageIndex}">
    <div class="ai-plan-stage-head">
      <span class="ai-plan-stage-number">${String(stageIndex + 1).padStart(2, "0")}</span>
      <label>阶段名称<input data-ai-stage-field="topic" value="${escapeHtml(stage.topic || stage.title || "")}"></label>
      <label>截止日期<input data-ai-stage-field="due_date" type="date" value="${escapeHtml(stage.due_date || "")}"></label>
      <div class="ai-plan-stage-actions"><button type="button" class="mini-button" data-ai-plan-action="stage-up" ${stageIndex === 0 ? "disabled" : ""} aria-label="阶段上移">↑</button><button type="button" class="mini-button" data-ai-plan-action="stage-down" ${stageIndex === stageCount - 1 ? "disabled" : ""} aria-label="阶段下移">↓</button><button type="button" class="mini-button danger-text" data-ai-plan-action="delete-stage">删除阶段</button></div>
    </div>
    <div class="ai-plan-task-list">${tasks.map((task, taskIndex) => `<div class="ai-plan-task-row" data-ai-task-index="${taskIndex}">
      <label>所属阶段<select data-ai-task-field="stage_index">${stages.map((option, optionIndex) => `<option value="${optionIndex}" ${optionIndex === stageIndex ? "selected" : ""}>${escapeHtml(option.topic || option.stage_key || `阶段 ${optionIndex + 1}`)}</option>`).join("")}</select></label>
      <label>题号<input data-ai-task-field="problem_id" value="${escapeHtml(task.problem_id || "")}" autocomplete="off"></label>
      <label>Level<input data-ai-task-field="level" value="${escapeHtml(task.level || "B")}" autocomplete="off"></label>
      <label>名称<input data-ai-task-field="name" value="${escapeHtml(task.name || "")}" autocomplete="off"></label>
      <label>标签<input data-ai-task-field="tags" value="${escapeHtml(Array.isArray(task.tags) ? task.tags.join(", ") : "")}" autocomplete="off"></label>
      <button type="button" class="mini-button danger-text" data-ai-plan-action="delete-task">删除</button>
    </div>`).join("") || '<div class="ai-plan-empty-stage">本阶段暂无题目</div>'}</div>
    <div class="ai-plan-stage-footer"><button type="button" class="mini-button" data-ai-plan-action="add-task">＋ 添加题目</button></div>
  </article>`;
}

function renderAiPlanFeedback(preview) {
  const errors = importErrors(preview);
  const warnings = previewWarnings(preview);
  const duplicate = previewDuplicate(preview);
  const diff = previewDiffText(preview);
  const ai = state.aiPlanMetadata || {};
  const requirement = generatedDraftRequirement();
  const rounds = Number(ai.rounds);
  const modelParts = [
    ai.model || "",
    ai.thinking ? `${ai.reasoning_effort || "默认"}推理` : "",
    Number.isInteger(rounds) && rounds > 0 ? `${rounds} 轮` : "",
  ].filter(Boolean);
  const model = modelParts.length ? ` · ${modelParts.join(" · ")}` : "";
  const rejected = Number(ai.rejected_count ?? 0);
  const accepted = Number(ai.accepted_count ?? requirement?.current ?? 0);
  const partialLabel = ai.complete === false ? "部分草稿" : "当前";
  const selectionText = requirement
    ? `${partialLabel} ${requirement.current}/${requirement.requested} 题${Number.isInteger(accepted) && accepted !== requirement.current ? ` · AI 初始接受 ${accepted} 题` : ""} · 已过滤 ${Number.isInteger(rejected) && rejected > 0 ? rejected : 0} 题${requirement.missing ? ` · 仍缺 ${requirement.missing} 题` : " · 已达要求"}`
    : "";
  $("#ai-plan-summary").innerHTML = `<strong>${escapeHtml(state.aiPlanDraft?.title || state.aiPlanDraft?.plan_id || "AI 题单草稿")}</strong><span>${errors.length ? `${errors.length} 个错误` : "格式校验通过"}${duplicate ? " · 检测到同 ID 题单" : ""}${escapeHtml(model)}</span>${selectionText ? `<p>${escapeHtml(selectionText)}</p>` : ""}${diff ? `<p>${escapeHtml(diff)}</p>` : ""}`;
  const errorBox = $("#ai-plan-errors");
  errorBox.classList.toggle("hidden", !errors.length);
  errorBox.innerHTML = errors.map(error => `<div>${escapeHtml(error)}</div>`).join("");
  const warningBox = $("#ai-plan-warnings");
  if (requirement?.missing) warnings.push(`部分草稿：已保留 ${requirement.current} 题，已过滤 ${Number.isInteger(rejected) && rejected > 0 ? rejected : 0} 题，仍缺 ${requirement.missing} 题；补足到 ${requirement.requested} 题后才可导入。`);
  warningBox.classList.toggle("hidden", !warnings.length);
  warningBox.innerHTML = warnings.map(warning => `<div>${escapeHtml(warning)}</div>`).join("");
  const replace = $("#ai-plan-replace-confirm");
  replace.classList.toggle("hidden", !duplicate);
  if (!duplicate) $("input", replace).checked = false;
  syncAiPlanImportAvailability();
}

function syncAiPlanImportAvailability() {
  const preview = state.aiPlanPreview;
  const duplicate = previewDuplicate(preview);
  const confirmed = $("#ai-plan-replace-confirm input").checked;
  const requirement = generatedDraftRequirement();
  $("#ai-plan-import-confirm").disabled = !preview || importErrors(preview).length > 0 || (duplicate && !confirmed) || Boolean(requirement?.missing);
}

function markAiPlanDraftDirty() {
  state.aiPlanPreview = null;
  $("#ai-plan-replace-confirm input").checked = false;
  $("#ai-plan-replace-confirm").classList.add("hidden");
  $("#ai-plan-import-confirm").disabled = true;
  $("#ai-plan-errors").classList.add("hidden");
  $("#ai-plan-warnings").classList.add("hidden");
  const requirement = generatedDraftRequirement();
  const countText = requirement ? ` · 当前 ${requirement.current}/${requirement.requested} 题${requirement.missing ? `，仍缺 ${requirement.missing} 题` : "，已达要求"}` : "";
  $("#ai-plan-summary").innerHTML = `<strong>${escapeHtml(state.aiPlanDraft?.title || state.aiPlanDraft?.plan_id || "AI 题单草稿")}</strong><span>正在重新校验编辑内容${escapeHtml(countText)}…</span>`;
  $("#ai-plan-json").textContent = `${JSON.stringify(state.aiPlanDraft, null, 2)}\n`;
  if (state.aiPlanValidationTimer) window.clearTimeout(state.aiPlanValidationTimer);
  state.aiPlanValidationTimer = window.setTimeout(() => validateAiPlanDraft(), 550);
}

async function validateAiPlanDraft() {
  if (!state.aiPlanDraft || !$("#ai-plan-import-dialog").open) return;
  state.aiPlanValidationController?.abort();
  const controller = new AbortController();
  const epoch = ++state.aiPlanValidationEpoch;
  state.aiPlanValidationController = controller;
  state.aiPlanValidationTimer = null;
  const content = JSON.stringify(state.aiPlanDraft);
  try {
    const preview = await api("/api/plans/preview", { body: { content }, signal: controller.signal });
    if (state.aiPlanValidationEpoch !== epoch || controller.signal.aborted) return;
    state.aiPlanPreview = preview;
    renderAiPlanFeedback(preview);
  } catch (error) {
    if (error.name === "AbortError" || state.aiPlanValidationEpoch !== epoch) return;
    state.aiPlanPreview = null;
    const box = $("#ai-plan-errors");
    box.textContent = error.message;
    box.classList.remove("hidden");
    $("#ai-plan-summary").innerHTML = `<strong>${escapeHtml(state.aiPlanDraft?.title || "AI 题单草稿")}</strong><span>校验请求失败</span>`;
  } finally {
    if (state.aiPlanValidationController === controller) state.aiPlanValidationController = null;
  }
}

function normalizeTaskProblem(task, value) {
  const problemId = String(value || "").trim().toUpperCase();
  task.problem_id = problemId;
  const cf = problemId.match(/^CF(\d+)([A-Z]\d*)$/);
  if (cf) {
    task.platform = "codeforces";
    task.url = `https://codeforces.com/problemset/problem/${cf[1]}/${cf[2]}`;
  } else if (/^P\d+$/.test(problemId)) {
    task.platform = "luogu";
    task.url = `https://www.luogu.com.cn/problem/${problemId}`;
  } else {
    delete task.platform;
    delete task.url;
  }
}

function mutateAiPlanDraft(target) {
  if (!state.aiPlanDraft) return;
  if (target.dataset.aiPlanField) {
    state.aiPlanDraft[target.dataset.aiPlanField] = target.value;
    markAiPlanDraftDirty();
    return;
  }
  const stageRow = target.closest("[data-ai-stage-index]");
  if (!stageRow) return;
  const stageIndex = Number(stageRow.dataset.aiStageIndex);
  const stage = draftStages()[stageIndex];
  if (target.dataset.aiStageField) {
    stage[target.dataset.aiStageField] = target.value || null;
    markAiPlanDraftDirty();
    return;
  }
  const taskRow = target.closest("[data-ai-task-index]");
  if (!taskRow) return;
  const taskIndex = Number(taskRow.dataset.aiTaskIndex);
  const task = stageTasks(stage)[taskIndex];
  const field = target.dataset.aiTaskField;
  if (field === "stage_index") {
    const destination = Number(target.value);
    if (destination !== stageIndex && draftStages()[destination]) {
      stageTasks(stage).splice(taskIndex, 1);
      stageTasks(draftStages()[destination]).push(task);
      renderAiPlanDraft();
    }
  } else if (field === "tags") task.tags = target.value.split(/[,，]/).map(tag => tag.trim()).filter(Boolean);
  else if (field === "problem_id") normalizeTaskProblem(task, target.value);
  else task[field] = target.value;
  markAiPlanDraftDirty();
}

function handleAiPlanDraftAction(button) {
  if (!state.aiPlanDraft) return;
  const action = button.dataset.aiPlanAction;
  const stageRow = button.closest("[data-ai-stage-index]");
  const taskRow = button.closest("[data-ai-task-index]");
  const stages = draftStages();
  const stageIndex = stageRow ? Number(stageRow.dataset.aiStageIndex) : -1;
  if (action === "add-stage") {
    stages.push({ stage_key: nextStageKey(), topic: "新阶段", kind: "practice", unlock_at: null, due_date: null, tasks: [], replacements: [] });
  } else if (action === "delete-stage" && stageIndex >= 0) stages.splice(stageIndex, 1);
  else if (action === "stage-up" && stageIndex > 0) [stages[stageIndex - 1], stages[stageIndex]] = [stages[stageIndex], stages[stageIndex - 1]];
  else if (action === "stage-down" && stageIndex >= 0 && stageIndex < stages.length - 1) [stages[stageIndex + 1], stages[stageIndex]] = [stages[stageIndex], stages[stageIndex + 1]];
  else if (action === "add-task" && stageIndex >= 0) {
    const stage = stages[stageIndex];
    stageTasks(stage).push({ task_key: nextTaskKey(stage), platform: "", problem_id: "", name: "", level: "B", tags: [] });
  } else if (action === "delete-task" && stageIndex >= 0 && taskRow) {
    stageTasks(stages[stageIndex]).splice(Number(taskRow.dataset.aiTaskIndex), 1);
  } else return;
  renderAiPlanDraft();
  markAiPlanDraftDirty();
}

async function cancelAiPlanGeneration() {
  await stopAiPlanWork();
  hideAiPlanProgress();
  $("#ai-plan-request-step").classList.remove("hidden");
  $("#ai-plan-generate").disabled = false;
  toast("已取消生成", "已停止等待，并尝试取消仍在队列中的任务。");
}

async function importAiPlanDraft(button) {
  const preview = state.aiPlanPreview;
  if (!preview || !state.aiPlanDraft) return;
  const content = JSON.stringify(state.aiPlanDraft);
  try {
    await submitPlanImport({
      button,
      content,
      preview,
      confirmReplace: $("#ai-plan-replace-confirm input").checked,
      dialog: $("#ai-plan-import-dialog"),
    });
  } catch (error) {
    if (error.status === 409) {
      state.aiPlanPreview = null;
      $("#ai-plan-replace-confirm input").checked = false;
      $("#ai-plan-import-confirm").disabled = true;
      const box = $("#ai-plan-errors");
      box.textContent = "题单 revision 已变化，草稿仍保留；已重新预览，请再次确认替换。";
      box.classList.remove("hidden");
      await validateAiPlanDraft();
    }
  }
}

function bindAiPlanImportEvents() {
  $("#ai-plan-import-button").addEventListener("click", openAiPlanImport);
  $$("input[name=ai_plan_mode]", $("#ai-plan-import-dialog")).forEach(input => input.addEventListener("change", syncGenerateControls));
  $("#ai-plan-generate").addEventListener("click", event => generateAiPlan(event.currentTarget));
  $("#ai-plan-cancel-job").addEventListener("click", cancelAiPlanGeneration);
  $("#ai-plan-back").addEventListener("click", () => {
    stopAiPlanWork();
    state.aiPlanDraft = null;
    state.aiPlanPreview = null;
    $("#ai-plan-draft-step").classList.add("hidden");
    $("#ai-plan-request-step").classList.remove("hidden");
  });
  $("#ai-plan-replace-confirm input").addEventListener("change", syncAiPlanImportAvailability);
  $("#ai-plan-import-confirm").addEventListener("click", event => importAiPlanDraft(event.currentTarget));
  const editor = $("#ai-plan-draft-editor");
  editor.addEventListener("input", event => {
    if (!event.target.matches("select, input[type=date]")) mutateAiPlanDraft(event.target);
  });
  editor.addEventListener("change", event => {
    if (event.target.matches("select, input[type=date]")) mutateAiPlanDraft(event.target);
  });
  editor.addEventListener("click", event => {
    const action = event.target.closest("button[data-ai-plan-action]");
    if (action) handleAiPlanDraftAction(action);
  });
  $("#ai-plan-import-dialog").addEventListener("close", () => stopAiPlanWork());
  syncGenerateControls();
}

export { bindAiPlanImportEvents };
