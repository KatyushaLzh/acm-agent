import {
  $, $$, api, asObject, escapeHtml, formatTime, loadBootstrap, setBusy, statCard,
  state, toast,
} from "./core.js";
import { refreshPlanSummaries, selectPlan } from "./view_plans.js";
import { requestRecommendations } from "./view_today.js";

function renderReview(data) {
  const sessions = data.sessions ?? 0;
  const average = data.average_hint_level ?? 0;
  const due = data.review_due || [];
  $("#review-summary").innerHTML = [statCard("完成 session", sessions, `${data.window?.from || "—"} 至 ${data.window?.to || "—"}`), statCard("平均提示等级", average, "0 为完全独立，4 为完整代码"), statCard("到期复做", due.length, "优先进入下一组训练")].join("");
  const dueNode = $("#review-due");
  dueNode.className = due.length ? "list" : "list empty-state compact";
  dueNode.innerHTML = due.length ? due.map(item => `<div class="list-row"><strong>${escapeHtml(item.problem_id)}</strong><span>${escapeHtml(item.review_due)}</span></div>`).join("") : "暂无到期题目";
  const weak = Object.entries(asObject(data.weak_topics));
  const max = Math.max(1, ...weak.map(([, value]) => Number(value)));
  const weakNode = $("#weak-topics");
  weakNode.className = weak.length ? "bar-list" : "bar-list empty-state compact";
  weakNode.innerHTML = weak.length ? weak.sort((a, b) => b[1] - a[1]).slice(0, 10).map(([name, value]) => `<div class="bar-row"><span>${escapeHtml(name)}</span><progress class="bar-track" max="${escapeHtml(max)}" value="${escapeHtml(value)}"></progress><b>${escapeHtml(value)}</b></div>`).join("") : "暂无训练数据";
  renderChips("#result-distribution", data.results);
  renderChips("#failure-distribution", data.failure_modes);
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
  await loadSkippedProblems();
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

export { openSkipDialog, confirmSkip, unskipProblem, loadReview, copyPath };
