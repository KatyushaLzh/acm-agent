import { $, $$, api, asObject, displayPlatform, escapeHtml, safeHref, setBusy, state, toast } from "./core.js";

function recommendationTitle(slot) {
  return ({ recovery: "恢复", main: "主练", stretch: "上探", "恢复": "恢复", "主练": "主练", "上探": "上探" })[slot] || slot || "推荐";
}

function renderRecommendations(data) {
  state.recommendations = data.recommendations || data.items || [];
  const basis = data.recommendation_basis || data.basis || "plan_only";
  const basisNode = $("#recommend-basis");
  basisNode.className = `basis-banner ${basis}`;
  basisNode.textContent = ({ synced: "已同步：推荐基于两个平台的最新状态", cached: "缓存模式：部分平台数据已过期，使用最后成功快照", plan_only: "本地计划模式：没有平台成功快照，不能据此判断平台 AC" })[basis] || basis;
  if ((data.warnings || []).length) basisNode.textContent += ` · ${data.warnings.join("；")}`;
  const container = $("#recommendations");
  if (!state.recommendations.length) {
    container.className = "recommend-grid empty-state";
    container.innerHTML = "<p>当前模式下没有符合条件的题目。</p>";
    return;
  }
  container.className = "recommend-grid";
  container.innerHTML = state.recommendations.map((item, recommendationIndex) => {
    const id = item.problem_id || item.id;
    const scoreParts = Object.entries(asObject(item.breakdown || item.score_breakdown));
    const tags = item.tags || (item.topic ? [item.topic] : []);
    const rawPlanSources = item.plan_sources || item.plans || [];
    const planSources = rawPlanSources.map(source => typeof source === "string" ? source : source.plan_title || source.title || source.plan_id).filter(Boolean);
    const dueDates = rawPlanSources.map(source => typeof source === "object" ? source.due_date : null).filter(Boolean).sort();
    const overdue = item.overdue || (dueDates[0] && dueDates[0] < new Date().toISOString().slice(0, 10));
    const urgency = item.urgency_label || item.urgency?.label || (overdue ? `已逾期 · ${dueDates[0]}` : dueDates[0] ? `截止 ${dueDates[0]}` : "");
    return `<article class="recommend-card" data-slot="${escapeHtml(item.slot)}">
      <div class="card-top"><span class="slot-label">${escapeHtml(recommendationTitle(item.slot))}</span><span class="score">score ${Number(item.score || 0).toFixed(1)}</span></div>
      <div class="problem-id">${escapeHtml(id)}</div><div class="problem-title">${escapeHtml(item.title || item.name || "")}</div>
      <div class="meta-row"><span class="tag">${escapeHtml(displayPlatform(item.platform))}</span><span class="tag">等效难度 ${escapeHtml(item.equivalent_rating ?? item.rating ?? item.difficulty ?? "未知")}</span></div>
      <div class="tags">${tags.slice(0, 4).map(tag => `<span class="tag">${escapeHtml(tag)}</span>`).join("")}</div>
      ${planSources.length || urgency ? `<div class="plan-source-row">${urgency ? `<span class="badge ${overdue ? "bad" : "warn"}">${escapeHtml(urgency)}</span>` : ""}${planSources.map(source => `<span class="tag">题单 · ${escapeHtml(source)}</span>`).join("")}</div>` : ""}
      <ul class="reasons">${(item.reasons || ["题库补充"]).slice(0, 3).map(reason => `<li>${escapeHtml(reason)}</li>`).join("")}</ul>
      ${item.ai_reason ? `<p><strong>AI：</strong>${escapeHtml(item.ai_reason)}</p>` : ""}
      ${item.training_focus ? `<p class="subtle">训练重点：${escapeHtml(item.training_focus)}</p>` : ""}
      ${scoreParts.length ? `<details class="score-details"><summary>查看分数分解</summary><div class="score-grid">${scoreParts.map(([key, value]) => `<span>${escapeHtml(key)}</span><b>${escapeHtml(value)}</b>`).join("")}</div></details>` : ""}
      <div class="card-actions"><button class="button primary start-recommendation" data-problem="${escapeHtml(id)}">开始这题</button><button class="button secondary skip-recommendation" data-recommendation-index="${recommendationIndex}">Skip</button>${item.url ? `<a href="${safeHref(item.url)}" target="_blank" rel="noopener noreferrer">打开题面 ↗</a>` : ""}</div>
    </article>`;
  }).join("");
}

async function requestRecommendations() {
  const form = $("#recommend-controls");
  const button = $("button[type=submit]", form);
  setBusy(button, true, "生成中…");
  try {
    const planIds = $$("input[type=checkbox]:checked", $("#recommend-plan-options")).map(input => input.value);
    const data = await api("/api/recommendations", { body: {
      mode: form.elements.mode.value,
      count: Number(form.elements.count.value),
      source_mode: form.elements.source_mode.value,
      plan_ids: planIds.length ? planIds : null,
    } });
    renderRecommendations(data || {});
  } catch (error) { toast("推荐失败", error.message, "error"); }
  finally { setBusy(button, false); }
}

export { renderRecommendations, requestRecommendations };
