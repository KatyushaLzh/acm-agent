import {
  $, $$, api, asObject, escapeHtml, formatTime, loadBootstrap, setBusy, statCard,
  state, toast,
} from "./core.js";
import { refreshPlanSummaries, selectPlan } from "./view_plans.js";
import { requestRecommendations } from "./view_today.js";
import { formatDeepSeekCost, renderAiAuditCards } from "./ai_model_controls.js";

let reviewSnapshot = {};
let reviewQueueState = { items: [], counts: { total: 0, due: 0, overdue: 0, today: 0, future: 0 } };

function renderReviewSummary() {
  const sessions = reviewSnapshot.sessions ?? 0;
  const average = reviewSnapshot.average_hint_level ?? 0;
  const counts = reviewQueueState.counts;
  $("#review-summary").innerHTML = [
    statCard("完成 session", sessions, `${reviewSnapshot.window?.from || "—"} 至 ${reviewSnapshot.window?.to || "—"}`),
    statCard("平均提示等级", average, "0 为完全独立，4 为完整代码"),
    statCard("复做队列", counts.total, `${counts.future} 题尚未到期`),
    statCard("已到期", counts.due, `逾期 ${counts.overdue} 题 · 今天 ${counts.today} 题`),
  ].join("");
}

function renderReview(data) {
  reviewSnapshot = asObject(data);
  renderReviewSummary();
  const weak = Object.entries(asObject(data.weak_topics));
  const max = Math.max(1, ...weak.map(([, value]) => Number(value)));
  const weakNode = $("#weak-topics");
  weakNode.className = weak.length ? "bar-list" : "bar-list empty-state compact";
  weakNode.innerHTML = weak.length ? weak.sort((a, b) => b[1] - a[1]).slice(0, 10).map(([name, value]) => `<div class="bar-row"><span>${escapeHtml(name)}</span><progress class="bar-track" max="${escapeHtml(max)}" value="${escapeHtml(value)}"></progress><b>${escapeHtml(value)}</b></div>`).join("") : "暂无训练数据";
  renderChips("#result-distribution", data.results);
  renderChips("#failure-distribution", data.failure_modes);
}

function localDateValue(date = new Date()) {
  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${year}-${month}-${day}`;
}

function dayOrdinal(value) {
  const matched = /^(\d{4})-(\d{2})-(\d{2})$/.exec(String(value || ""));
  if (!matched) return null;
  const year = Number(matched[1]);
  const month = Number(matched[2]);
  const day = Number(matched[3]);
  const timestamp = Date.UTC(year, month - 1, day);
  const parsed = new Date(timestamp);
  if (parsed.getUTCFullYear() !== year || parsed.getUTCMonth() !== month - 1 || parsed.getUTCDate() !== day) return null;
  return timestamp / 86400000;
}

function queueDateStatus(reviewDue) {
  const dueDay = dayOrdinal(reviewDue);
  const today = dayOrdinal(localDateValue());
  if (dueDay == null || today == null) return { kind: "future", label: String(reviewDue || "日期未知") };
  const delta = dueDay - today;
  if (delta < 0) return { kind: "overdue", label: `逾期 ${Math.abs(delta)} 天` };
  if (delta === 0) return { kind: "today", label: "今天" };
  return { kind: "future", label: `${delta} 天后` };
}

function queueItems(payload) {
  return Array.isArray(payload?.items) ? payload.items : [];
}

function queueCounts(payload, items) {
  const supplied = asObject(payload?.counts);
  const computed = { total: items.length, due: 0, overdue: 0, today: 0, future: 0 };
  items.forEach(item => {
    const kind = queueDateStatus(item.review_due).kind;
    computed[kind] += 1;
    if (kind === "overdue" || kind === "today") computed.due += 1;
  });
  return Object.fromEntries(Object.entries(computed).map(([key, value]) => [key, Number.isFinite(Number(supplied[key])) ? Number(supplied[key]) : value]));
}

function queueTypeLabel(item) {
  const queueType = item.queue_type || item.type;
  if (queueType === "manual_once") return "一次性";
  const stage = item.review_stage ?? item.stage;
  return stage == null ? "自动" : `自动 · 阶段 ${stage}`;
}

function renderReviewQueue(payload) {
  const items = queueItems(payload);
  reviewQueueState = { items, counts: queueCounts(payload, items) };
  const { counts } = reviewQueueState;
  $("#review-queue-counts").textContent = `队列 ${counts.total} 题 · 已到期 ${counts.due} 题`;
  $("#review-clear-button").disabled = items.length === 0;
  const root = $("#review-due");
  if (!items.length) {
    root.className = "review-queue-list empty-state compact";
    root.textContent = "复做队列为空";
    renderReviewSummary();
    return;
  }
  root.className = "review-queue-list";
  root.innerHTML = items.map(item => {
    const problem = item.problem_id || item.problem || item.id || "未知题目";
    const status = queueDateStatus(item.review_due);
    const platform = item.platform ? `<span class="tag">${escapeHtml(item.platform)}</span>` : "";
    return `<div class="review-queue-row">
      <div class="review-queue-problem"><strong>${escapeHtml(problem)}</strong>${item.title ? `<small>${escapeHtml(item.title)}</small>` : ""}<span>${platform}<span class="tag">${escapeHtml(queueTypeLabel(item))}</span></span></div>
      <div class="review-queue-date"><time datetime="${escapeHtml(item.review_due)}">${escapeHtml(item.review_due)}</time><span class="badge review-date-${escapeHtml(status.kind)}">${escapeHtml(status.label)}</span></div>
      <button type="button" class="button danger review-remove-button" data-problem="${escapeHtml(problem)}">删除</button>
    </div>`;
  }).join("");
  renderReviewSummary();
}

async function loadReviewQueue() {
  try { renderReviewQueue(await api("/api/review/queue")); }
  catch (error) {
    const root = $("#review-due");
    root.className = "review-queue-list empty-state compact";
    root.textContent = `读取复做队列失败：${error.message}`;
    toast("复做队列读取失败", error.message, "error");
  }
}

function openReviewAddDialog() {
  const form = $("#review-add-form");
  form.reset();
  form.elements.review_due.value = localDateValue();
  $("#review-add-dialog").showModal();
  form.elements.problem.focus();
}

async function refreshAfterReviewQueueChange(hadRecommendations) {
  await loadReviewQueue();
  await loadBootstrap();
  if (hadRecommendations) await requestRecommendations();
}

async function addReviewQueueItem(button) {
  const form = $("#review-add-form");
  if (!form.reportValidity()) return;
  const problem = form.elements.problem.value.trim();
  const reviewDue = form.elements.review_due.value;
  const hadRecommendations = state.recommendations.length > 0;
  setBusy(button, true, "添加中…");
  try {
    await api("/api/review/queue/add", { body: { problem, review_due: reviewDue } });
    $("#review-add-dialog").close();
    toast("已加入复做队列", `${problem} · ${reviewDue}`);
    await refreshAfterReviewQueueChange(hadRecommendations);
  } catch (error) { toast("添加失败", error.message, "error"); }
  finally { setBusy(button, false); }
}

async function removeReviewQueueItem(problem, button) {
  if (!window.confirm(`确认从复做队列删除 ${problem}？\n训练历史与 AC 状态不会被删除。`)) return;
  const hadRecommendations = state.recommendations.length > 0;
  setBusy(button, true, "删除中…");
  try {
    await api("/api/review/queue/remove", { body: { problem } });
    toast("已删除复做日程", `${problem} 的训练历史保持不变。`);
    await refreshAfterReviewQueueChange(hadRecommendations);
  } catch (error) { toast("删除失败", error.message, "error"); }
  finally { setBusy(button, false); }
}

async function clearReviewQueue(button) {
  const total = reviewQueueState.items.length;
  if (!total || !window.confirm(`确认清空全部 ${total} 道复做题目？\n此操作不会删除训练历史或 AC 状态。`)) return;
  const hadRecommendations = state.recommendations.length > 0;
  setBusy(button, true, "清空中…");
  try {
    const result = await api("/api/review/queue/clear", { body: { confirm: true } });
    toast("复做队列已清空", `已删除 ${result?.removed_count ?? total} 条复做日程。`);
    await refreshAfterReviewQueueChange(hadRecommendations);
  } catch (error) { toast("清空失败", error.message, "error"); }
  finally {
    setBusy(button, false);
    button.disabled = reviewQueueState.items.length === 0;
  }
}

function renderAiCostAudit(payload) {
  const audit = asObject(payload?.audit || payload);
  renderAiAuditCards($("#ai-cost-summary"), audit, `近 ${audit.window_days || 30} 天`);
  const groups = Array.isArray(audit.groups) ? audit.groups : [];
  const groupNode = $("#ai-cost-groups");
  groupNode.className = groups.length ? "governance-table" : "governance-table empty-state compact";
  groupNode.innerHTML = groups.length ? `<div class="governance-row cost-head"><b>任务 / Provider / 模型</b><b>Runs</b><b>请求</b><b>全模型 Tokens / 缓存</b><b>DeepSeek 估算费用（人民币）</b></div>${groups.map(item => `<div class="governance-row cost-row"><strong data-label="任务 / 路由">${escapeHtml(item.profile_id)} · ${escapeHtml(item.provider_id)} / ${escapeHtml(item.model)}</strong><span data-label="Runs">${escapeHtml(item.runs)}</span><span data-label="请求">${item.provider_requests_known == null ? "未知" : escapeHtml(item.provider_requests_known)}</span><span data-label="全模型 Tokens / 缓存">${item.total_tokens_known == null ? "未知" : escapeHtml(item.total_tokens_known)} / ${item.cache_read_tokens_known == null ? "未知" : escapeHtml(item.cache_read_tokens_known)}</span><span data-label="DeepSeek 估算费用（人民币）">${escapeHtml(formatDeepSeekCost(item.deepseek_cost))}</span></div>`).join("")}` : "暂无 AI run";
  const runs = Array.isArray(audit.recent_runs) ? audit.recent_runs : [];
  const runNode = $("#ai-cost-runs");
  runNode.className = runs.length ? "governance-table recent-cost-runs" : "governance-table empty-state compact";
  runNode.innerHTML = runs.length ? `<div class="governance-row run-head"><b>时间 / 任务</b><b>请求 → 实际</b><b>请求数</b><b>全模型 Tokens / 缓存</b><b>DeepSeek 估算费用（人民币）</b></div>${runs.map(item => {
    const actual = `${item.resolved_provider_id || item.provider_id}/${item.resolved_model || "未知"}`;
    const requested = `${item.provider_id}/${item.requested_model}`;
    const cost = formatDeepSeekCost(item.deepseek_cost);
    return `<div class="governance-row run-row"><strong data-label="时间 / 任务">${escapeHtml(formatTime(item.created_at))} · ${escapeHtml(item.profile_id)}</strong><span data-label="请求 → 实际">${escapeHtml(requested)} → ${escapeHtml(actual)}${item.fallback_count ? ` · 模型路由回退 ${escapeHtml(item.fallback_count)}` : ""}</span><span data-label="请求数">${item.provider_requests == null ? "未知" : escapeHtml(item.provider_requests)}</span><span data-label="全模型 Tokens / 缓存">${item.total_tokens == null ? "未知" : escapeHtml(item.total_tokens)} / ${item.cache_read_tokens == null ? "未知" : escapeHtml(item.cache_read_tokens)}</span><span data-label="DeepSeek 估算费用（人民币）">${escapeHtml(cost)}${item.deepseek_cost?.unknown_reason ? ` · ${escapeHtml(item.deepseek_cost.unknown_reason)}` : ""}</span></div>`;
  }).join("")}` : "暂无 AI run";
}

async function repriceAiCostAudit(button) {
  setBusy(button, true, "重算中…");
  try {
    const data = await api("/api/ai/costs/reprice", { body: {} });
    renderAiCostAudit(data);
    toast("DeepSeek 费用已重算", "新 DeepSeek 价格版本已追加；全模型历史 token 与原估算记录均保留。");
  } finally { setBusy(button, false); }
}

function openSkipDialog(index) {
  const item = state.recommendations[Number(index)];
  if (!item) return;
  state.skipCandidate = item;
  const problemId = item.problem_id || item.id || "未知题目";
  $("#skip-problem-summary").innerHTML = `<strong>${escapeHtml(problemId)}</strong><span>${escapeHtml(item.title || item.name || "")}</span>`;
  $("#skip-note").value = "";
  $("#skip-dialog").showModal();
  $("#skip-note").focus();
}

function currentRecommendationContext(item) {
  const controls = $("#recommend-controls");
  const selectedPlanIds = $$("input[type=checkbox]:checked", $("#recommend-plan-options")).map(input => input.value);
  return {
    slot: item.slot || null,
    mode: controls.elements.mode.value,
    source_mode: controls.elements.source_mode.value,
    plan_ids: selectedPlanIds.length ? selectedPlanIds : null,
    plan_sources: item.plan_sources || [],
    reasons: item.reasons || [],
  };
}

async function refreshAfterSkipChange() {
  await loadBootstrap();
  await refreshPlanSummaries();
  if (state.selectedPlanId) await selectPlan(state.selectedPlanId, true);
  await requestRecommendations();
}

async function confirmSkip(button) {
  const item = state.skipCandidate;
  if (!item) return;
  const problem = item.problem_id || item.id;
  setBusy(button, true, "记录中…");
  try {
    await api("/api/problems/skip", { body: {
      problem,
      reason: "idea_clear_without_editorial",
      note: $("#skip-note").value.trim() || null,
      source: "web",
      context: currentRecommendationContext(item),
    } });
    $("#skip-dialog").close();
    toast("已 Skip", `${problem} 不计为 AC，也不会创建源码或复做记录。`);
    await refreshAfterSkipChange();
  } catch (error) { toast("Skip 失败", error.message, "error"); }
  finally { setBusy(button, false); }
}

function skippedArray(payload) {
  if (Array.isArray(payload)) return payload;
  return payload?.skipped || payload?.problems || payload?.items || payload?.records || [];
}

function renderSkippedProblems(payload) {
  state.skippedProblems = skippedArray(payload);
  const root = $("#skipped-problems");
  if (!state.skippedProblems.length) {
    root.className = "skipped-list empty-state compact";
    root.textContent = "暂无 Skip 记录";
    return;
  }
  root.className = "skipped-list";
  root.innerHTML = state.skippedProblems.map(item => {
    const problem = item.problem_id || item.problem || item.id || "未知题目";
    const note = item.note || item.notes || "无备注";
    return `<div class="skipped-row">
      <div class="skipped-problem"><strong>${escapeHtml(problem)}</strong><span>${escapeHtml(item.title || item.name || "")}</span></div>
      <div class="skipped-reason"><span>已有完整思路 · 未看题解</span><small>${escapeHtml(note)}</small></div>
      <time>${escapeHtml(formatTime(item.created_at || item.skipped_at || item.updated_at))}</time>
      <button type="button" class="button secondary unskip-button" data-problem="${escapeHtml(problem)}">撤销</button>
    </div>`;
  }).join("");
}

async function loadSkippedProblems() {
  try { renderSkippedProblems(await api("/api/problems/skipped")); }
  catch (error) {
    const root = $("#skipped-problems");
    root.className = "skipped-list empty-state compact";
    root.textContent = `读取 Skip 记录失败：${error.message}`;
  }
}

async function unskipProblem(problem, button) {
  setBusy(button, true, "撤销中…");
  try {
    await api("/api/problems/unskip", { body: { problem, source: "web" } });
    toast("已撤销 Skip", `${problem} 可以再次进入推荐池。`);
    await loadSkippedProblems();
    await refreshAfterSkipChange();
  } catch (error) { toast("撤销失败", error.message, "error"); }
  finally { setBusy(button, false); }
}

function renderChips(selector, object) {
  const entries = Object.entries(asObject(object));
  $(selector).innerHTML = entries.length ? entries.map(([key, value]) => `<span class="chip"><strong>${escapeHtml(key)}</strong>&nbsp; ${escapeHtml(value)}</span>`).join("") : '<span class="subtle">暂无数据</span>';
}

async function loadReview() {
  const button = $("#review-button");
  setBusy(button, true, "生成中…");
  try { renderReview(await api("/api/review/week", { body: {} })); }
  catch (error) { toast("复盘生成失败", error.message, "error"); }
  finally { setBusy(button, false); }
  await loadReviewQueue();
  await loadSkippedProblems();
  try { renderAiCostAudit(await api("/api/ai/costs")); }
  catch (error) { toast("AI 费用读取失败", error.message, "error"); }
}

async function copyPath(path) {
  try {
    await navigator.clipboard.writeText(path);
    toast("路径已复制", path);
  } catch {
    const area = document.createElement("textarea");
    area.value = path; document.body.append(area); area.select(); document.execCommand("copy"); area.remove();
    toast("路径已复制", path);
  }
}

export {
  openSkipDialog, confirmSkip, unskipProblem, loadReview, copyPath, repriceAiCostAudit,
  openReviewAddDialog, addReviewQueueItem, removeReviewQueueItem, clearReviewQueue,
};
