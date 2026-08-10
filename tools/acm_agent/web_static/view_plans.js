import {
  $, $$, api, asObject, escapeHtml, formatTime, jobIdOf, pollJob, safeHref,
  setBusy, state, toast,
} from "./core.js";
import { requestRecommendations } from "./view_today.js";

function planArray(payload) {
  if (Array.isArray(payload)) return payload;
  return payload?.plans || payload?.items || [];
}

function planDocument(payload) {
  return payload?.plan || payload?.document || payload?.data?.plan || payload;
}

function planRevision(payload, fallback = 0) {
  return Number(payload?.revision ?? payload?.current_revision ?? payload?.meta?.revision ?? fallback ?? 0);
}

function stagesOf(plan) {
  if (!Array.isArray(plan.stages)) plan.stages = [];
  return plan.stages;
}

function tasksOf(stage) {
  const tasks = stage.tasks || stage.problems || stage.items;
  if (!Array.isArray(tasks)) stage.tasks = [];
  else if (stage.tasks !== tasks) stage.tasks = tasks;
  return stage.tasks;
}

function stableKey(prefix) {
  const random = globalThis.crypto?.randomUUID?.().replaceAll("-", "").slice(0, 10) || `${Date.now()}${Math.random().toString(16).slice(2, 7)}`;
  return `${prefix}_${random}`;
}

function clonePlan() {
  return JSON.parse(JSON.stringify(state.selectedPlan));
}

function planSourceLabel(source) {
  return ({ builtin: "内置", built_in: "内置", managed: "托管", imported: "导入", override: "内置覆盖" })[String(source || "").toLowerCase()] || source || "本地";
}

function progressParts(summary) {
  const progress = asObject(summary.progress);
  const done = Number(progress.completed ?? progress.accepted ?? summary.completed_count ?? summary.completed_tasks ?? summary.ac_count ?? 0);
  const total = Number(progress.total ?? summary.total_tasks ?? summary.task_count ?? 0);
  const ratio = typeof summary.progress === "number" ? summary.progress * 100 : (total ? done / total * 100 : 0);
  return { done, total, ratio: Math.min(100, Math.max(0, ratio)) };
}

function renderRecommendationPlanOptions() {
  const enabled = state.plans.filter(item => item.enabled !== false);
  const root = $("#recommend-plan-options");
  const preserved = new Set($$("input:checked", root).map(input => input.value));
  root.innerHTML = enabled.length ? enabled.map(item => `<label class="check"><input type="checkbox" value="${escapeHtml(item.plan_id)}" ${preserved.has(item.plan_id) ? "checked" : ""}>${escapeHtml(item.title || item.plan_id)}</label>`).join("") : '<span class="subtle">暂无已启用题单</span>';
  const updateLabel = () => {
    const selected = $$("input:checked", root).length;
    $("#recommend-plan-filter summary").textContent = selected ? `已限定 ${selected} 个题单` : "全部已启用题单";
  };
  root.onchange = updateLabel;
  updateLabel();
}

function renderPlanList() {
  const root = $("#plan-list");
  $("#plan-count").textContent = state.plans.length;
  if (!state.plans.length) {
    root.className = "plan-list empty-state compact";
    root.innerHTML = "尚未导入题单";
    return;
  }
  root.className = "plan-list";
  root.innerHTML = state.plans.map(item => {
    const progress = progressParts(item);
    const active = item.plan_id === state.selectedPlanId;
    return `<button type="button" class="plan-list-item${active ? " active" : ""}" data-plan-id="${escapeHtml(item.plan_id)}">
      <span class="plan-list-top"><strong>${escapeHtml(item.title || item.plan_id)}</strong><span class="status-dot ${item.enabled === false ? "offline" : "online"}" title="${item.enabled === false ? "已停用" : "已启用"}"></span></span>
      <span class="plan-list-meta"><span>${escapeHtml(planSourceLabel(item.source))}</span><span>${progress.done}/${progress.total}</span></span>
      <progress class="plan-progress" max="100" value="${progress.ratio.toFixed(1)}" aria-label="完成进度 ${progress.ratio.toFixed(1)}%"></progress>
    </button>`;
  }).join("");
}

async function loadPlans({ select = state.selectedPlanId, forceDetail = false } = {}) {
  try {
    const data = await api("/api/plans");
    state.plans = planArray(data);
    renderPlanList();
    renderRecommendationPlanOptions();
    if (select && state.plans.some(item => item.plan_id === select)) await selectPlan(select, forceDetail);
    else if (!state.selectedPlan && state.plans[0]) await selectPlan(state.plans[0].plan_id);
    else if (!state.plans.length) renderPlanEditorEmpty("尚未导入题单", "可通过上方按钮导入 JSON，或先下载 v2 模板。");
  } catch (error) {
    $("#plan-list").className = "plan-list empty-state compact";
    $("#plan-list").textContent = `读取失败：${error.message}`;
    toast("题单读取失败", error.message, "error");
  }
}

function renderPlanEditorEmpty(title, description) {
  state.selectedPlan = null;
  state.selectedPlanMeta = null;
  state.editingPlanMeta = false;
  state.editingStageKey = "";
  $("#plan-editor").innerHTML = `<div class="empty-state plan-editor-empty"><div><strong>${escapeHtml(title)}</strong><p>${escapeHtml(description)}</p></div></div>`;
}

async function selectPlan(planId, force = false) {
  if (!force && state.selectedPlanId === planId && state.selectedPlan) return;
  state.editingPlanMeta = false;
  state.editingStageKey = "";
  state.selectedPlanId = planId;
  renderPlanList();
  $("#plan-editor").innerHTML = '<div class="empty-state plan-editor-empty">正在读取题单…</div>';
  try {
    const data = await api(`/api/plans/${encodeURIComponent(planId)}`);
    const document = planDocument(data);
    state.selectedPlan = JSON.parse(JSON.stringify(document));
    state.selectedPlanMeta = {
      revision: planRevision(data, document.revision),
      enabled: data.enabled ?? state.plans.find(item => item.plan_id === planId)?.enabled ?? true,
      source: data.source ?? state.plans.find(item => item.plan_id === planId)?.source,
      has_override: data.has_override ?? state.plans.find(item => item.plan_id === planId)?.has_override ?? false,
      platform_counts: data.platform_counts ?? state.plans.find(item => item.plan_id === planId)?.platform_counts,
      platform_ratio: data.platform_ratio ?? state.plans.find(item => item.plan_id === planId)?.platform_ratio,
      task_statuses: asObject(data.task_statuses),
    };
    renderPlanEditor();
  } catch (error) {
    renderPlanEditorEmpty("题单读取失败", error.message);
  }
}

function renderPlanEditor() {
  const plan = state.selectedPlan;
  if (!plan) return;
  const meta = state.selectedPlanMeta || {};
  const stages = stagesOf(plan);
  const editingMeta = state.editingPlanMeta;
  const isBuiltin = ["builtin", "built_in", "override"].includes(String(meta.source || "").toLowerCase());
  const deleteLabel = isBuiltin ? (meta.has_override ? "恢复内置版本" : "内置不可删除") : "删除";
  $("#plan-editor").innerHTML = `
    <div class="plan-editor-head">
      <div class="plan-title-block">
        <div class="plan-title-line"><span class="badge ${meta.enabled === false ? "warn" : "good"}">${meta.enabled === false ? "已停用" : "已启用"}</span><span class="subtle">${escapeHtml(planSourceLabel(meta.source))} · 修订 ${escapeHtml(meta.revision)}</span><span id="plan-save-state" class="save-state">已保存</span></div>
        ${editingMeta ? `<input class="plan-title-input" data-plan-field="title" value="${escapeHtml(plan.title || "")}" aria-label="题单标题" placeholder="题单标题">` : `<h1 class="plan-title-readonly">${escapeHtml(plan.title || "未命名题单")}</h1>`}
        <code>${escapeHtml(plan.plan_id)}</code>
      </div>
      <div class="plan-editor-actions">
        <button type="button" class="button ${editingMeta ? "primary" : "secondary"}" data-plan-action="edit-meta">${editingMeta ? "完成编辑" : "编辑题单信息"}</button>
        <button type="button" class="button secondary" data-plan-action="complete-tags">补全标签</button>
        <button type="button" class="button secondary" data-plan-action="cleanup-tags">清理标签</button>
        <button type="button" class="button secondary" data-plan-action="export">导出</button>
        <button type="button" class="button secondary" data-plan-action="revisions">历史版本</button>
        <button type="button" class="button secondary" data-plan-action="toggle">${meta.enabled === false ? "启用" : "停用"}</button>
        <button type="button" class="button danger" data-plan-action="delete" ${isBuiltin && !meta.has_override ? "disabled" : ""}>${deleteLabel}</button>
      </div>
    </div>
    <div id="plan-conflict" class="alert hidden"></div>
    ${editingMeta ? `<div class="plan-meta-grid">
      <label class="span-2">描述<textarea data-plan-field="description" rows="2" placeholder="训练目标与使用说明">${escapeHtml(plan.description || "")}</textarea></label>
      <label>排期模式<select data-plan-field="schedule_mode"><option value="dated" ${plan.schedule_mode !== "progressive" ? "selected" : ""}>按日期</option><option value="progressive" ${plan.schedule_mode === "progressive" ? "selected" : ""}>逐阶段解锁</option></select></label>
    </div>` : renderPlanMetaSummary(plan, meta)}
    <div class="stage-heading"><div><p class="eyebrow">STAGES</p><h2>${stages.length} 个阶段</h2></div>${editingMeta ? '<button type="button" class="button primary" data-plan-action="add-stage">新增阶段</button>' : ""}</div>
    <div id="stage-list" class="stage-list">${stages.map((stage, index) => renderStage(stage, index, stages.length)).join("") || '<div class="empty-state compact">当前题单还没有阶段</div>'}</div>
    <div id="plan-revisions" class="revision-panel hidden"></div>`;
}

function derivePlanPlatformStats(plan, meta) {
  const counts = { codeforces: 0, luogu: 0 };
  const canComputeFromTasks = Array.isArray(plan.stages);
  for (const stage of stagesOf(plan)) {
    for (const task of tasksOf(stage)) {
      const platform = String(task.platform || "").toLowerCase();
      if (Object.hasOwn(counts, platform)) counts[platform] += 1;
    }
  }
  let total = counts.codeforces + counts.luogu;
  if (!canComputeFromTasks) {
    const backendCounts = asObject(meta.platform_counts);
    counts.codeforces = Number(backendCounts.codeforces ?? backendCounts.cf ?? 0);
    counts.luogu = Number(backendCounts.luogu ?? 0);
    total = counts.codeforces + counts.luogu;
  }
  const backendRatio = asObject(meta.platform_ratio);
  const ratioFor = platform => {
    if (total) return counts[platform] / total;
    if (canComputeFromTasks) return 0;
    const value = Number(backendRatio[platform] ?? (platform === "codeforces" ? backendRatio.cf : 0));
    return Number.isFinite(value) ? (value > 1 ? value / 100 : value) : 0;
  };
  return { counts, ratios: { codeforces: ratioFor("codeforces"), luogu: ratioFor("luogu") } };
}

function renderPlanMetaSummary(plan, meta) {
  const schedule = plan.schedule_mode === "progressive" ? "逐阶段解锁" : "按日期";
  const stats = derivePlanPlatformStats(plan, meta);
  const platformValue = platform => `${stats.counts[platform]}题 · ${(stats.ratios[platform] * 100).toFixed(1)}%`;
  return `<div class="plan-meta-summary">
    <div class="plan-description"><span>描述</span><p>${escapeHtml(plan.description || "暂无描述")}</p></div>
    <dl><div><dt>排期模式</dt><dd>${schedule}</dd></div><div><dt>Codeforces</dt><dd>${escapeHtml(platformValue("codeforces"))}</dd></div><div><dt>洛谷</dt><dd>${escapeHtml(platformValue("luogu"))}</dd></div></dl>
  </div>`;
}

function renderStage(stage, stageIndex, stageCount) {
  const tasks = tasksOf(stage);
  const editing = state.editingStageKey === stage.stage_key;
  return `<article class="stage-card" data-stage-index="${stageIndex}">
    <div class="stage-card-head">
      <div class="stage-identity"><span class="stage-number">${stageIndex + 1}</span><div><strong>${escapeHtml(stage.topic || stage.title || "未命名阶段")}</strong><code>${escapeHtml(stage.stage_key || "")}</code></div></div>
      <div class="row-actions">
        ${editing ? `<button type="button" class="mini-button" data-stage-action="up" ${stageIndex === 0 ? "disabled" : ""} aria-label="阶段上移">↑</button>
        <button type="button" class="mini-button" data-stage-action="down" ${stageIndex === stageCount - 1 ? "disabled" : ""} aria-label="阶段下移">↓</button>
        <button type="button" class="mini-button danger-text" data-stage-action="delete">删除阶段</button>` : ""}
        <button type="button" class="mini-button ${editing ? "active" : ""}" data-stage-action="edit">${editing ? "完成编辑" : "编辑阶段"}</button>
      </div>
    </div>
    ${editing ? `<div class="stage-fields">
      <label>专题<input data-stage-field="topic" value="${escapeHtml(stage.topic || stage.title || "")}" placeholder="例如 线段树"></label>
      <label>类型<input data-stage-field="kind" value="${escapeHtml(stage.kind || stage.type || "practice")}" placeholder="practice / review"></label>
      <label>解锁日期<input data-stage-field="unlock_at" type="date" value="${escapeHtml(stage.unlock_at || stage.unlock_date || "")}"></label>
      <label>截止日期<input data-stage-field="due_date" type="date" value="${escapeHtml(stage.due_date || stage.deadline || "")}"></label>
    </div>
    <details class="stage-replacements"><summary>高级设置 · 替换规则 ${Array.isArray(stage.replacements) ? stage.replacements.length : 0} 条</summary><label>JSON 数组<textarea data-stage-field="replacements" rows="4" placeholder='[{"condition":{"type":"ac","mode":"any","problem_keys":["codeforces:CF1A"]},"replace_task_keys":["task-1"],"task":{...}}]'>${escapeHtml(JSON.stringify(stage.replacements || [], null, 2))}</textarea></label></details>
    ${renderTaskTable(tasks, true)}
    <form class="add-task-form" data-add-task-form>
      <input name="problem_id" required autocomplete="off" placeholder="输入题号，如 CF1234A 或 P1000" aria-label="新题题号">
      <input name="name" required autocomplete="off" placeholder="题目名称" aria-label="新题名称">
      <input name="tags" autocomplete="off" placeholder="标签，使用逗号分隔" aria-label="新题标签">
      <button type="submit" class="add-task-button">＋ 添加题目</button>
    </form>` : `${renderStageSummary(stage, tasks)}${renderTaskTable(tasks, false)}`}
  </article>`;
}

function renderStageSummary(stage, tasks) {
  const replacements = Array.isArray(stage.replacements) ? stage.replacements.length : 0;
  return `<dl class="stage-summary"><div><dt>类型</dt><dd>${escapeHtml(stage.kind || stage.type || "practice")}</dd></div><div><dt>解锁日期</dt><dd>${displayDate(stage.unlock_at || stage.unlock_date)}</dd></div><div><dt>截止日期</dt><dd>${displayDate(stage.due_date || stage.deadline)}</dd></div><div><dt>题目</dt><dd>${tasks.length} 道</dd></div><div><dt>替换规则</dt><dd>${replacements} 条</dd></div></dl>`;
}

function displayDate(value) {
  return value ? escapeHtml(value) : '<span class="muted-value">未设置</span>';
}

function renderTaskTable(tasks, editing) {
  return `<div class="task-table-wrap"><table class="task-table ${editing ? "editing" : "readonly"}"><thead><tr><th>题目</th><th>标签</th><th>判题状态</th><th>Skip</th>${editing ? '<th><span class="sr-only">操作</span></th>' : ""}</tr></thead><tbody>${tasks.map((task, taskIndex) => editing ? renderEditableTask(task, taskIndex) : renderReadonlyTask(task)).join("") || `<tr><td colspan="${editing ? 5 : 4}" class="task-empty">本阶段还没有题目</td></tr>`}</tbody></table></div>`;
}

function taskStatusView(task) {
  const statuses = asObject(state.selectedPlanMeta?.task_statuses);
  const status = asObject(statuses[task.task_key]);
  const rawVerdict = String(status.judge_result || "").trim().toUpperCase();
  const workflow = String(status.workflow_status || "unknown").trim().toLowerCase();
  let verdict = ["", "UNKNOWN", "NONE", "PENDING", "-"].includes(rawVerdict) ? "" : (({ OK: "AC", ACCEPTED: "AC" })[rawVerdict] || rawVerdict);
  if (!verdict && workflow === "accepted") verdict = "AC";
  const workflowLabel = ({ active: "进行中", active_session: "进行中", in_progress: "进行中", working: "进行中", started: "进行中", attempted: "已尝试", skipped: "无判题记录", local: "仅本地", local_only: "仅本地", not_started: "未开始", unstarted: "未开始", idle: "未开始", unknown: "未知" })[workflow] || "未知";
  const accepted = verdict === "AC";
  const label = verdict || workflowLabel;
  const badgeClass = accepted ? "good" : ["WA", "RE", "MLE", "CE"].includes(verdict) ? "bad" : ["TLE", "ABANDONED"].includes(verdict) ? "warn" : "neutral";
  const skipped = !accepted && Boolean(status.skipped);
  const details = [status.evidence_source ? `来源：${status.evidence_source}` : "", status.updated_at ? `更新：${formatTime(status.updated_at)}` : ""].filter(Boolean).join(" · ");
  return { label, badgeClass, skipped, details };
}

function renderTaskStatusCells(task) {
  const status = taskStatusView(task);
  return `<td class="task-status-cell"><span class="badge ${status.badgeClass}"${status.details ? ` title="${escapeHtml(status.details)}"` : ""}>${escapeHtml(status.label)}</span></td><td class="task-skip-cell"><span class="task-skip ${status.skipped ? "yes" : "no"}">${status.skipped ? "是" : "否"}</span></td>`;
}

function renderEditableTask(task, taskIndex) {
  const url = taskProblemUrl(task);
  return `<tr class="task-row" data-task-index="${taskIndex}">
    <td><div class="problem-edit-fields"><input data-task-field="problem_id" value="${escapeHtml(task.problem_id || task.problem || task.id || "")}" aria-label="题号" placeholder="CF1234A"><input data-task-field="name" value="${escapeHtml(task.name || task.title || "")}" aria-label="题目名称" placeholder="题目名称"></div><div class="problem-edit-meta"><a href="${safeHref(url)}" target="_blank" rel="noopener noreferrer">打开题目 ↗</a><code title="${escapeHtml(task.task_key || "")}">${escapeHtml(task.task_key || "")}</code></div></td>
    <td><input data-task-field="tags" value="${escapeHtml((task.tags || []).join(", "))}" aria-label="标签" placeholder="树状数组, 前缀和"></td>
    ${renderTaskStatusCells(task)}
    <td><button type="button" class="mini-button danger-text" data-task-action="delete">删除</button></td>
  </tr>`;
}

function renderReadonlyTask(task) {
  const problemId = task.problem_id || task.problem || task.id || "未填写";
  const name = task.name || task.title || "题目名称待同步";
  const tags = Array.isArray(task.tags) && task.tags.length ? task.tags.join("、") : "—";
  return `<tr class="task-row readonly-task"><td><a class="problem-link" href="${safeHref(taskProblemUrl(task))}" target="_blank" rel="noopener noreferrer"><strong>${escapeHtml(problemId)}</strong><span>${escapeHtml(name)}</span><span class="external-mark" aria-hidden="true">↗</span></a></td><td>${escapeHtml(tags)}</td>${renderTaskStatusCells(task)}</tr>`;
}

function taskProblemUrl(task) {
  if (task.url) return String(task.url);
  const problemId = String(task.problem_id || task.problem || task.id || "").trim().toUpperCase();
  const cf = problemId.match(/^CF(\d+)([A-Z]\d*)$/);
  if (cf) return `https://codeforces.com/problemset/problem/${cf[1]}/${cf[2]}`;
  if (/^P\d+$/.test(problemId)) return `https://www.luogu.com.cn/problem/${problemId}`;
  return "#";
}

function markSaving(saving, message = "") {
  const node = $("#plan-save-state");
  if (!node) return;
  node.className = `save-state${saving ? " saving" : ""}`;
  node.textContent = saving ? "保存中…" : (message || "已保存");
  const editor = $("#plan-editor");
  editor.setAttribute("aria-busy", saving ? "true" : "false");
  $$("input, select, textarea, button", editor).forEach(control => {
    if (saving) {
      control.dataset.wasDisabled = control.disabled ? "1" : "0";
      control.disabled = true;
    } else {
      control.disabled = control.dataset.wasDisabled === "1";
      delete control.dataset.wasDisabled;
    }
  });
}

async function savePlan(nextPlan, successMessage = "修改已保存") {
  const meta = state.selectedPlanMeta || {};
  markSaving(true);
  try {
    const data = await api("/api/plans/edit", { body: { plan_id: nextPlan.plan_id, expected_revision: meta.revision, plan: nextPlan } });
    const responseHasStatuses = Object.hasOwn(asObject(data), "task_statuses");
    state.selectedPlan = JSON.parse(JSON.stringify(planDocument(data)?.plan_id ? planDocument(data) : nextPlan));
    meta.revision = planRevision(data, meta.revision + 1);
    if (data.source) meta.source = data.source;
    if (data.has_override != null) meta.has_override = data.has_override;
    if (responseHasStatuses) meta.task_statuses = asObject(data.task_statuses);
    state.selectedPlanMeta = meta;
    if (responseHasStatuses) renderPlanEditor();
    else {
      const editingMeta = state.editingPlanMeta;
      const editingStageKey = state.editingStageKey;
      await selectPlan(nextPlan.plan_id, true);
      state.editingPlanMeta = editingMeta;
      state.editingStageKey = state.selectedPlan && stagesOf(state.selectedPlan).some(stage => stage.stage_key === editingStageKey) ? editingStageKey : "";
      if (state.selectedPlan) renderPlanEditor();
    }
    await refreshPlanSummaries();
    toast(successMessage, `当前修订：${meta.revision}`);
    return true;
  } catch (error) {
    markSaving(false, "保存失败");
    if (error.status === 409) {
      const conflict = $("#plan-conflict");
      conflict.classList.remove("hidden");
      conflict.innerHTML = `检测到其他标签页已修改此题单。当前内容未覆盖服务器新版本。 <button type="button" class="text-button" data-plan-action="reload">重新加载</button>`;
      toast("版本冲突", "请重新加载题单后再编辑。", "error");
    } else toast("保存失败", error.message, "error");
    return false;
  }
}

async function refreshPlanSummaries() {
  try {
    state.plans = planArray(await api("/api/plans"));
    renderPlanList();
    renderRecommendationPlanOptions();
  } catch { /* The saved document remains usable even if the summary refresh fails. */ }
}

function downloadJson(filename, value) {
  const blob = new Blob([`${JSON.stringify(value, null, 2)}\n`], { type: "application/json;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url; anchor.download = filename; document.body.append(anchor); anchor.click(); anchor.remove();
  URL.revokeObjectURL(url);
}

async function toggleSelectedPlan() {
  const meta = state.selectedPlanMeta;
  if (!meta || !state.selectedPlan) return;
  try {
    const data = await api("/api/plans/state", { body: { plan_id: state.selectedPlan.plan_id, enabled: meta.enabled === false, expected_revision: meta.revision } });
    meta.enabled = data.enabled ?? meta.enabled === false;
    meta.revision = planRevision(data, meta.revision + 1);
    renderPlanEditor(); await refreshPlanSummaries();
    toast(meta.enabled ? "题单已启用" : "题单已停用", "下一次推荐将立即使用新状态。");
  } catch (error) { toast("状态修改失败", error.message, "error"); }
}

async function deleteSelectedPlan() {
  const plan = state.selectedPlan;
  const meta = state.selectedPlanMeta;
  if (!plan || !meta) return;
  const restore = ["builtin", "built_in", "override"].includes(String(meta.source || "").toLowerCase()) && meta.has_override;
  const message = restore ? "移除托管覆盖并恢复仓库内置题单？训练历史不会删除。" : "删除此题单？只会解除题单关联，已有 AC、session、尝试、复做和失败记录都不会删除。";
  if (!window.confirm(message)) return;
  try {
    await api("/api/plans/delete", { body: { plan_id: plan.plan_id, expected_revision: meta.revision } });
    toast(restore ? "已恢复内置版本" : "题单已删除", "训练历史没有删除。");
    state.selectedPlanId = ""; state.selectedPlan = null; state.selectedPlanMeta = null;
    await loadPlans();
  } catch (error) { toast(restore ? "恢复失败" : "删除失败", error.message, "error"); }
}

async function loadRevisions() {
  if (!state.selectedPlan) return;
  const root = $("#plan-revisions");
  root.className = "revision-panel";
  root.innerHTML = '<div class="empty-state compact">正在读取历史版本…</div>';
  try {
    const data = await api(`/api/plans/${encodeURIComponent(state.selectedPlan.plan_id)}/revisions`);
    const revisions = data.revisions || data.items || (Array.isArray(data) ? data : []);
    root.innerHTML = `<div class="revision-head"><strong>最近版本</strong><button type="button" class="mini-button" data-plan-action="close-revisions">关闭</button></div>${revisions.length ? revisions.map(item => `<div class="revision-row"><div><strong>修订 ${escapeHtml(item.revision ?? item.id)}</strong><small>${escapeHtml(formatTime(item.created_at || item.timestamp))}${item.summary ? ` · ${escapeHtml(item.summary)}` : ""}</small></div><button type="button" class="button secondary" data-restore-revision="${escapeHtml(item.revision ?? item.id)}">恢复</button></div>`).join("") : '<div class="empty-state compact">暂无可恢复版本</div>'}`;
  } catch (error) { root.innerHTML = `<div class="result-box error">${escapeHtml(error.message)}</div>`; }
}

async function restoreRevision(revision, button) {
  if (!window.confirm(`恢复修订 ${revision}？恢复操作会生成一个新的修订。`)) return;
  setBusy(button, true, "恢复中…");
  try {
    const data = await api("/api/plans/restore", { body: { plan_id: state.selectedPlan.plan_id, revision: Number(revision), expected_revision: state.selectedPlanMeta.revision } });
    toast("历史版本已恢复", "恢复结果已作为新修订保存。");
    await selectPlan(state.selectedPlan.plan_id, true); await refreshPlanSummaries();
  } catch (error) { toast("恢复失败", error.message, "error"); }
  finally { setBusy(button, false); }
}

function tagJobError(job) {
  const raw = job?.error;
  const error = new Error(raw?.message || raw || job?.message || "标签预览任务失败");
  const code = String(raw?.code || job?.code || "").toLowerCase();
  if (job?.status_code === 409 || code.includes("revision") || code.includes("conflict")) error.status = 409;
  return error;
}

function waitForTagJob(jobId) {
  return pollJob(jobId, { interval: 700, toError: tagJobError });
}

function tagList(value) {
  if (Array.isArray(value)) return value.map(tag => String(tag).trim()).filter(Boolean);
  if (typeof value === "string") return value.split(/[,，]/).map(tag => tag.trim()).filter(Boolean);
  return [];
}

function tagDifference(left, right) {
  const rightKeys = new Set(tagList(right).map(tag => tag.toLocaleLowerCase()));
  return tagList(left).filter(tag => !rightKeys.has(tag.toLocaleLowerCase()));
}

function renderTagChips(tags, className = "") {
  const values = tagList(tags);
  return values.length
    ? values.map(tag => `<b class="tag ${className}">${escapeHtml(tag)}</b>`).join("")
    : "<em>无</em>";
}

function tagSourceLabel(value) {
  if (Array.isArray(value)) return value.map(tagSourceLabel).filter(Boolean).join("、");
  if (value && typeof value === "object") return String(value.label || value.name || value.provider || value.type || "");
  const text = String(value || "自动分析");
  return ({ sqlite_catalog: "本地平台缓存", codeforces_problemset: "Codeforces 官方题库", luogu_problem: "洛谷公开题面", agent: "Agent 生成", unresolved: "待人工或 Agent 补充" })[text] || text;
}

function formatCoverage(value, total) {
  if (value == null || value === "") return "—";
  const number = Number(value);
  if (!Number.isFinite(number)) return "—";
  if (number >= 0 && number <= 1) return `${Math.round(number * 100)}%`;
  if (total && Number.isInteger(number) && number <= total) return `${number}/${total}`;
  return `${Math.round(number)}%`;
}

function proposalFallback(taskKey) {
  for (const stage of stagesOf(state.selectedPlan || {})) {
    const task = tasksOf(stage).find(item => item.task_key === taskKey);
    if (task) return task;
  }
  return {};
}

function renderTagPreview(payload) {
  const preview = payload?.preview || payload;
  const proposalValue = preview?.proposals || preview?.items || preview?.suggestions || [];
  const errorValue = preview?.failures || preview?.errors || [];
  const warningValue = preview?.warnings || [];
  const proposals = Array.isArray(proposalValue) ? proposalValue : [];
  const failures = [
    ...(Array.isArray(errorValue) ? errorValue : (errorValue ? [errorValue] : [])),
    ...(Array.isArray(warningValue) ? warningValue : (warningValue ? [warningValue] : [])),
  ];
  const coverage = asObject(preview?.coverage || preview?.summary);
  const failedCount = Number(preview?.failed_count ?? preview?.failure_count ?? coverage.failed ?? coverage.unresolved ?? failures.length);
  const eligible = Number(coverage.eligible ?? preview?.eligible_tasks ?? proposals.length);
  const skippedNonempty = Number(coverage.skipped_nonempty ?? preview?.tagged_tasks ?? preview?.tagged_before ?? 0);
  const suggestedCount = Number(coverage.suggested ?? preview?.suggested_count ?? proposals.filter(item => tagList(item.suggested_tags ?? item.tags).length).length);
  const total = Number(preview?.total_tasks ?? coverage.total_tasks ?? coverage.total ?? eligible + skippedNonempty);
  const taggedBefore = Number(preview?.tagged_tasks ?? preview?.tagged_before ?? coverage.tagged_before ?? coverage.tagged ?? NaN);
  const before = preview?.coverage_before ?? coverage.coverage_before ?? coverage.before ?? coverage.current ?? (total ? skippedNonempty / total : (Number.isFinite(taggedBefore) ? taggedBefore : null));
  const after = preview?.coverage_after ?? coverage.coverage_after ?? coverage.after ?? coverage.projected ?? (total ? Math.min(total, skippedNonempty + suggestedCount) / total : coverage.ratio);
  state.tagPreview = { ...preview, proposals };
  const revision = preview?.base_revision ?? preview?.revision;
  const overrideRevision = preview?.override_revision;
  if (revision != null) state.tagPreviewRevision = Number(revision);
  if (overrideRevision != null) state.tagPreviewOverrideRevision = Number(overrideRevision);
  state.tagPreviewMode = preview?.mode === "cleanup" ? "cleanup" : state.tagPreviewMode;
  const cleanup = state.tagPreviewMode === "cleanup";
  $("#plan-tags-title").textContent = cleanup ? "确认清理标签" : "确认补全标签";

  $("#plan-tags-summary").innerHTML = `
    <div><span>当前覆盖</span><strong>${escapeHtml(formatCoverage(before, total))}</strong></div>
    <div><span>预计覆盖</span><strong>${escapeHtml(formatCoverage(after, total))}</strong></div>
    <div><span>建议</span><strong>${escapeHtml(suggestedCount)}</strong></div>
    <div><span>失败</span><strong class="${failedCount ? "danger-text" : ""}">${escapeHtml(Number.isFinite(failedCount) ? failedCount : failures.length)}</strong></div>`;
  const failureBox = $("#plan-tags-failures");
  failureBox.classList.toggle("hidden", !failures.length);
  failureBox.innerHTML = failures.map(item => {
    const detail = typeof item === "string" ? item : `${item.problem_id || item.task_key || "未知题目"}：${item.message || item.error || "无法生成标签"}`;
    return `<div>${escapeHtml(detail)}</div>`;
  }).join("");
  const root = $("#plan-tags-proposals");
  root.innerHTML = proposals.length ? proposals.map((item, index) => {
    const fallback = proposalFallback(item.task_key);
    const problemId = item.problem_id || fallback.problem_id || "未知题目";
    const name = item.name || item.problem_name || item.title || fallback.name || fallback.title || "题目名称待同步";
    const current = tagList(item.current_tags ?? item.existing_tags ?? item.original_tags ?? item.old_tags ?? fallback.tags);
    const suggested = tagList(item.suggested_tags ?? item.new_tags ?? item.tags ?? item.proposed_tags);
    const raw = tagList(item.raw_tags);
    const added = tagList(item.added_tags ?? tagDifference(suggested, current));
    const removed = tagList(item.removed_tags ?? tagDifference(current, suggested));
    const ignored = tagList(item.ignored_meta_tags);
    const source = tagSourceLabel(item.source ?? item.sources ?? item.tag_source ?? item.evidence);
    return `<article class="tag-proposal" data-proposal-index="${index}">
      <div class="tag-proposal-problem"><strong>${escapeHtml(problemId)}</strong><span>${escapeHtml(name)}</span></div>
      <div class="tag-proposal-original"><span>当前有效标签</span><p>${renderTagChips(current)}</p>${raw.length ? `<span>平台原始标签</span><p>${renderTagChips(raw)}</p>` : ""}</div>
      <label>期望有效标签<input data-tag-task-key="${escapeHtml(item.task_key || fallback.task_key || "")}" value="${escapeHtml(suggested.join(", "))}" placeholder="可留空；使用逗号分隔" aria-label="${escapeHtml(problemId)} 的期望有效标签"></label>
      <div class="tag-proposal-diff">
        <span>新增</span><p>${renderTagChips(added, "tag-added")}</p>
        <span>删除</span><p>${renderTagChips(removed, "tag-removed")}</p>
        <span>已忽略元标签</span><p>${renderTagChips(ignored, "tag-ignored")}</p>
      </div>
      <div class="tag-proposal-source"><span>来源</span><strong>${escapeHtml(source)}</strong></div>
    </article>`;
  }).join("") : '<div class="empty-state compact">没有可应用的标签建议</div>';
  $("#plan-tags-apply").disabled = !proposals.length;
  $("#plan-tags-dialog").showModal();
}

async function reloadAfterTagConflict(message) {
  if ($("#plan-tags-dialog").open) $("#plan-tags-dialog").close();
  toast("标签状态已变化", message || "题单或全局标签已被修改，请基于最新版本重新预览。", "error");
  await selectPlan(state.selectedPlanId, true);
  await refreshPlanSummaries();
}

async function startTagPreview(button, mode = "fill_missing") {
  if (!state.selectedPlan || !state.selectedPlanMeta) return;
  const planId = state.selectedPlan.plan_id;
  const revision = state.selectedPlanMeta.revision;
  state.tagPreviewPlanId = planId;
  state.tagPreviewRevision = revision;
  state.tagPreviewOverrideRevision = null;
  state.tagPreviewMode = mode;
  setBusy(button, true, "分析中…");
  try {
    const started = await api("/api/jobs/plans/tags/preview", { body: { plan_id: planId, expected_revision: revision, mode, overwrite: false } });
    const jobId = jobIdOf(started);
    const result = jobId ? await waitForTagJob(jobId) : started;
    if (state.selectedPlanId !== planId) {
      toast("标签预览已完成", "当前题单已切换，本次预览未打开。请在目标题单中重新运行。", "error");
      return;
    }
    renderTagPreview(result || {});
  } catch (error) {
    if (error.status === 409) await reloadAfterTagConflict(error.message);
    else toast("标签预览失败", error.message, "error");
  } finally { setBusy(button, false); }
}

async function applyTagPreview(button) {
  if (!state.tagPreview || !state.tagPreviewPlanId) return;
  const planId = state.tagPreviewPlanId;
  const proposals = $$("[data-tag-task-key]", $("#plan-tags-proposals")).map(input => ({
    task_key: input.dataset.tagTaskKey,
    tags: tagList(input.value),
  })).filter(item => item.task_key);
  setBusy(button, true, "应用中…");
  try {
    const applied = await api("/api/plans/tags/apply", { body: {
      plan_id: state.tagPreviewPlanId,
      expected_revision: state.tagPreviewRevision,
      expected_override_revision: state.tagPreviewOverrideRevision,
      proposals,
    } });
    const cleanup = state.tagPreviewMode === "cleanup";
    $("#plan-tags-dialog").close();
    state.tagPreview = null;
    toast(cleanup ? "标签已清理" : "标签已补全", `已更新 ${applied.updated ?? proposals.length} 道题，推荐结果正在刷新。`);
    await selectPlan(planId, true);
    await refreshPlanSummaries();
    await requestRecommendations();
  } catch (error) {
    if (error.status === 409) await reloadAfterTagConflict(error.message);
    else toast("应用标签失败", error.message, "error");
  } finally { setBusy(button, false); }
}

function importErrors(preview) {
  const errors = preview?.errors || preview?.validation?.errors || [];
  return errors.map(item => typeof item === "string" ? item : `${item.path || item.pointer || "$"}: ${item.message || JSON.stringify(item)}`);
}

async function previewPlanFile(file) {
  const dialog = $("#plan-import-dialog");
  state.importContent = await file.text();
  state.importPreview = null;
  $("#plan-import-summary").textContent = `${file.name} · ${(file.size / 1024).toFixed(1)} KB`;
  $("#plan-import-preview").textContent = state.importContent.slice(0, 12000);
  $("#plan-import-errors").classList.add("hidden");
  $("#plan-replace-confirm").classList.add("hidden");
  $("#plan-replace-confirm input").checked = false;
  $("#plan-import-confirm").disabled = true;
  dialog.showModal();
  try {
    const preview = await api("/api/plans/preview", { body: { content: state.importContent } });
    state.importPreview = preview;
    const errors = importErrors(preview);
    const duplicate = Boolean(preview.duplicate || preview.exists || preview.requires_confirmation);
    const diff = asObject(preview.diff);
    const diffText = Object.keys(diff).length ? `阶段 ${diff.stages_before ?? 0} → ${diff.stages_after ?? 0}，题目 ${diff.tasks_before ?? 0} → ${diff.tasks_after ?? 0}${diff.title_changed ? "，标题有修改" : ""}` : preview.diff_summary || "";
    $("#plan-import-summary").innerHTML = `<strong>${escapeHtml(preview.plan?.title || preview.title || preview.plan_id || file.name)}</strong><span>${errors.length ? `${errors.length} 个错误` : "格式校验通过"}${duplicate ? " · 检测到同 ID 题单" : ""}</span>${diffText ? `<p>${escapeHtml(diffText)}</p>` : ""}`;
    if (errors.length) {
      const box = $("#plan-import-errors"); box.classList.remove("hidden"); box.innerHTML = errors.map(error => `<div>${escapeHtml(error)}</div>`).join("");
    }
    $("#plan-replace-confirm").classList.toggle("hidden", !duplicate);
    $("#plan-import-confirm").disabled = Boolean(errors.length || duplicate);
  } catch (error) {
    const box = $("#plan-import-errors"); box.classList.remove("hidden"); box.textContent = error.message;
  }
}

async function confirmPlanImport(button) {
  if (!state.importPreview || !state.importContent) return;
  const confirmReplace = $("#plan-replace-confirm input").checked;
  setBusy(button, true, "导入中…");
  try {
    const data = await api("/api/plans/import", { body: {
      content: state.importContent,
      confirm_replace: confirmReplace,
      expected_revision: state.importPreview.current_revision ?? null,
    } });
    $("#plan-import-dialog").close();
    const planId = data.plan_id || data.plan?.plan_id || state.importPreview.plan_id || state.importPreview.plan?.plan_id;
    toast("题单已导入", planId || "导入内容已保存");
    state.selectedPlanId = ""; state.selectedPlan = null;
    await loadPlans({ select: planId });
  } catch (error) { toast("导入失败", error.message, "error"); }
  finally { setBusy(button, false); }
}

async function downloadPlanTemplate(button) {
  setBusy(button, true, "准备中…");
  try { downloadJson("plan-v2-template.json", planDocument(await api("/api/plans/template"))); }
  catch (error) { toast("模板下载失败", error.message, "error"); }
  finally { setBusy(button, false); }
}

function mutatePlanField(target) {
  const next = clonePlan();
  if (!target.dataset.planField) return;
  next[target.dataset.planField] = target.value;
  savePlan(next);
}

function mutateStageField(target) {
  const row = target.closest("[data-stage-index]");
  if (!row) return;
  const next = clonePlan();
  const stage = stagesOf(next)[Number(row.dataset.stageIndex)];
  if (target.dataset.stageField === "replacements") {
    try {
      const value = JSON.parse(target.value || "[]");
      if (!Array.isArray(value)) throw new Error("must be array");
      stage.replacements = value;
    } catch { toast("替换规则不是合法 JSON 数组", "当前修改未保存。", "error"); return; }
  } else stage[target.dataset.stageField] = target.value || null;
  savePlan(next);
}

function mutateTaskField(target) {
  const stageRow = target.closest("[data-stage-index]");
  const taskRow = target.closest("[data-task-index]");
  if (!stageRow || !taskRow) return;
  const next = clonePlan();
  const task = tasksOf(stagesOf(next)[Number(stageRow.dataset.stageIndex)])[Number(taskRow.dataset.taskIndex)];
  const field = target.dataset.taskField;
  if (field === "tags") task[field] = target.value.split(",").map(tag => tag.trim()).filter(Boolean);
  else if (field === "problem_id") {
    const problemId = target.value.trim().toUpperCase();
    task.problem_id = problemId;
    if (problemId.startsWith("P")) task.platform = "luogu";
    else if (problemId.startsWith("CF")) task.platform = "codeforces";
    delete task.url;
  } else task[field] = target.value || null;
  savePlan(next);
}

async function handleStageAction(button) {
  const row = button.closest("[data-stage-index]");
  if (!row) return;
  const index = Number(row.dataset.stageIndex);
  const action = button.dataset.stageAction;
  const currentStage = stagesOf(state.selectedPlan)[index];
  if (action === "edit") {
    state.editingPlanMeta = false;
    state.editingStageKey = state.editingStageKey === currentStage.stage_key ? "" : currentStage.stage_key;
    renderPlanEditor();
    return;
  }
  const next = clonePlan();
  const stages = stagesOf(next);
  if (action === "delete") {
    const count = tasksOf(stages[index]).length;
    if (!window.confirm(`删除阶段“${stages[index].topic || stages[index].stage_key}”？其中 ${count} 道题只会解除题单关联，训练历史不会删除。`)) return;
    stages.splice(index, 1);
    state.editingStageKey = "";
  } else {
    const other = action === "up" ? index - 1 : index + 1;
    if (other < 0 || other >= stages.length) return;
    [stages[index], stages[other]] = [stages[other], stages[index]];
  }
  await savePlan(next, "阶段已更新");
}

function addTaskFromForm(form) {
  const stageRow = form.closest("[data-stage-index]");
  const problemId = form.elements.problem_id.value.trim().toUpperCase();
  if (!stageRow || !problemId) return;
  const next = clonePlan();
  const stage = stagesOf(next)[Number(stageRow.dataset.stageIndex)];
  if (next.schedule_mode === "dated" && !stage.due_date) {
    toast("请先设置阶段截止日期", "按日期题单统一使用阶段的解锁日期和截止日期。", "error");
    return;
  }
  const platform = problemId.startsWith("P") ? "luogu" : problemId.startsWith("CF") ? "codeforces" : "";
  if (!platform) {
    toast("题号格式无效", "请输入 CF1234A 或 P1000 形式的题号。", "error");
    return;
  }
  tasksOf(stage).push({
    task_key: stableKey("task"), platform, problem_id: problemId,
    name: form.elements.name.value.trim(),
    tags: form.elements.tags.value.split(",").map(tag => tag.trim()).filter(Boolean),
  });
  savePlan(next, "题目已添加");
}

function handleTaskAction(target) {
  const stageRow = target.closest("[data-stage-index]");
  const taskRow = target.closest("[data-task-index]");
  if (!stageRow || !taskRow) return;
  const stageIndex = Number(stageRow.dataset.stageIndex);
  const taskIndex = Number(taskRow.dataset.taskIndex);
  const action = target.dataset.taskAction;
  if (action !== "delete") return;
  const next = clonePlan();
  const stages = stagesOf(next);
  const tasks = tasksOf(stages[stageIndex]);
  if (!window.confirm("从题单删除此题？这只会解除题单关联，已有 AC、session、尝试、复做和失败记录不会删除。")) return;
  tasks.splice(taskIndex, 1);
  savePlan(next, "题目已移除");
}

export {
  stagesOf, stableKey, clonePlan, loadPlans,
  selectPlan, renderPlanEditor, savePlan, refreshPlanSummaries, downloadJson,
  toggleSelectedPlan, deleteSelectedPlan, loadRevisions, restoreRevision,
  startTagPreview, applyTagPreview, importErrors, previewPlanFile,
  confirmPlanImport, downloadPlanTemplate, mutatePlanField, mutateStageField,
  mutateTaskField, handleStageAction, addTaskFromForm, handleTaskAction,
};
