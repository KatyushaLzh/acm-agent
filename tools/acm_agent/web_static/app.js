"use strict";

const state = {
  token: "",
  bootstrap: null,
  activeJobs: new Map(),
  recommendations: [],
  plans: [],
  selectedPlanId: "",
  selectedPlan: null,
  selectedPlanMeta: null,
  editingPlanMeta: false,
  editingStageKey: "",
  importContent: "",
  importPreview: null,
  tagPreview: null,
  tagPreviewPlanId: "",
  tagPreviewRevision: null,
  tagPreviewOverrideRevision: null,
  tagPreviewMode: "fill_missing",
  skipCandidate: null,
  skippedProblems: [],
  aiStatus: null,
  aiProblemKey: "",
  aiConversationId: "",
  aiConversationProblemKey: "",
  aiEpoch: 0,
  aiStreamController: null,
  aiContextHash: null,
  aiPatchProposalId: "",
  aiPatchProblemKey: "",
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

function captureToken() {
  const url = new URL(window.location.href);
  let token = url.searchParams.get("token") || "";
  if (!token && url.hash.includes("token=")) {
    token = new URLSearchParams(url.hash.replace(/^#/, "")).get("token") || "";
  }
  if (token) sessionStorage.setItem("acm-web-token", token);
  state.token = token || sessionStorage.getItem("acm-web-token") || "";
  if (url.searchParams.has("token") || url.hash.includes("token=")) {
    url.searchParams.delete("token");
    history.replaceState(null, "", `${url.pathname}${url.search}${url.hash.includes("token=") ? "" : url.hash}`);
  }
}

function unwrap(payload) {
  if (payload && payload.ok === false) {
    const detail = payload.error?.message || payload.error || payload.message || "请求失败";
    throw new Error(String(detail));
  }
  if (payload && Object.hasOwn(payload, "data")) return payload.data;
  return payload;
}

async function api(path, options = {}) {
  const method = options.method || (Object.hasOwn(options, "body") ? "POST" : "GET");
  const headers = { Accept: "application/json", "X-ACM-Token": state.token, ...(options.headers || {}) };
  const request = { method, headers };
  if (Object.hasOwn(options, "body")) {
    headers["Content-Type"] = "application/json";
    request.body = JSON.stringify(options.body);
  }
  const response = await fetch(path, request);
  let payload;
  try { payload = await response.json(); } catch { payload = { error: `服务返回了非 JSON 响应（HTTP ${response.status}）` }; }
  if (!response.ok) {
    const detail = payload?.error?.message || payload?.error || payload?.message || `HTTP ${response.status}`;
    const error = new Error(String(detail));
    error.status = response.status;
    error.payload = payload;
    throw error;
  }
  return unwrap(payload);
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, char => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" })[char]);
}

const CPP_KEYWORDS = new Set([
  "alignas", "alignof", "asm", "auto", "break", "case", "catch", "class", "concept", "const",
  "consteval", "constexpr", "constinit", "const_cast", "continue", "co_await", "co_return", "co_yield",
  "decltype", "default", "delete", "do", "dynamic_cast", "else", "enum", "explicit", "export", "extern",
  "for", "friend", "goto", "if", "inline", "mutable", "namespace", "new", "noexcept", "operator",
  "private", "protected", "public", "register", "reinterpret_cast", "requires", "return", "sizeof", "static",
  "static_assert", "static_cast", "struct", "switch", "template", "this", "thread_local", "throw", "try",
  "typedef", "typeid", "typename", "union", "using", "virtual", "volatile", "while",
]);
const CPP_TYPES = new Set([
  "bool", "char", "char8_t", "char16_t", "char32_t", "double", "float", "int", "long", "short", "signed",
  "unsigned", "void", "wchar_t", "size_t", "int8_t", "int16_t", "int32_t", "int64_t", "uint8_t",
  "uint16_t", "uint32_t", "uint64_t", "string", "vector", "array", "map", "set", "queue", "deque",
  "stack", "pair", "tuple",
]);
const CPP_LITERALS = new Set(["true", "false", "nullptr", "NULL"]);

function cppSpan(kind, value) {
  return `<span class="cpp-${kind}">${escapeHtml(value)}</span>`;
}

function highlightCpp(source) {
  const value = String(source ?? "");
  let output = ""; let index = 0; let lineStart = true;
  while (index < value.length) {
    const char = value[index]; const next = value[index + 1] || "";
    if (char === "\n") { output += "\n"; index += 1; lineStart = true; continue; }
    if (/\s/.test(char)) { output += escapeHtml(char); index += 1; continue; }
    if (lineStart && char === "#") {
      let end = index;
      while (end < value.length && value[end] !== "\n") end += 1;
      output += cppSpan("preprocessor", value.slice(index, end)); index = end; lineStart = false; continue;
    }
    lineStart = false;
    if (char === "/" && next === "/") {
      let end = index + 2;
      while (end < value.length && value[end] !== "\n") end += 1;
      output += cppSpan("comment", value.slice(index, end)); index = end; continue;
    }
    if (char === "/" && next === "*") {
      let end = value.indexOf("*/", index + 2);
      end = end < 0 ? value.length : end + 2;
      output += cppSpan("comment", value.slice(index, end));
      lineStart = value.slice(index, end).endsWith("\n"); index = end; continue;
    }
    if (char === '"' || char === "'") {
      const quote = char; let end = index + 1;
      while (end < value.length) {
        if (value[end] === "\\") { end += 2; continue; }
        if (value[end] === quote) { end += 1; break; }
        end += 1;
      }
      output += cppSpan("string", value.slice(index, end)); index = end; continue;
    }
    if (/[A-Za-z_]/.test(char)) {
      let end = index + 1;
      while (end < value.length && /[A-Za-z0-9_]/.test(value[end])) end += 1;
      const token = value.slice(index, end);
      const kind = CPP_KEYWORDS.has(token) ? "keyword" : CPP_TYPES.has(token) ? "type" : CPP_LITERALS.has(token) ? "literal" : "identifier";
      output += kind === "identifier" ? escapeHtml(token) : cppSpan(kind, token); index = end; continue;
    }
    if (/\d/.test(char) || (char === "." && /\d/.test(next))) {
      let end = index + 1;
      while (end < value.length && /[A-Za-z0-9_.'+-]/.test(value[end])) end += 1;
      output += cppSpan("number", value.slice(index, end)); index = end; continue;
    }
    if (/[{}()[\];,.?:~!%^&*+\-/|<>=]/.test(char)) {
      let end = index + 1;
      while (end < value.length && end < index + 3 && /[~!%^&*+\-/|<>=]/.test(value[end])) end += 1;
      output += cppSpan("operator", value.slice(index, end)); index = end; continue;
    }
    output += escapeHtml(char); index += 1;
  }
  return output;
}

function renderCppSource(node, source) {
  node.innerHTML = highlightCpp(source);
}

function safeHref(value) {
  try {
    const url = new URL(String(value));
    return ["http:", "https:"].includes(url.protocol) ? escapeHtml(url.href) : "#";
  } catch { return "#"; }
}

function toast(title, message = "", kind = "success") {
  const node = $("#toast-template").content.firstElementChild.cloneNode(true);
  node.classList.toggle("error", kind === "error");
  $("strong", node).textContent = title;
  $("p", node).textContent = message;
  $("#toast-region").append(node);
  window.setTimeout(() => node.remove(), 5200);
}

function showAlert(message = "") {
  const node = $("#global-alert");
  node.textContent = message;
  node.classList.toggle("hidden", !message);
}

function setBusy(button, busy, busyText = "处理中…") {
  if (!button) return;
  if (busy) {
    if (button.dataset.busy !== "true") {
      button.dataset.busyLabel = button.textContent;
      button.dataset.busyWasDisabled = button.disabled ? "true" : "false";
    }
    button.dataset.busy = "true";
    button.textContent = busyText;
    button.disabled = true;
  } else {
    if (button.dataset.busy !== "true") return;
    button.textContent = button.dataset.busyLabel ?? button.textContent;
    button.disabled = button.dataset.busyWasDisabled === "true";
    delete button.dataset.busy;
    delete button.dataset.busyLabel;
    delete button.dataset.busyWasDisabled;
  }
}

function asObject(value) { return value && typeof value === "object" && !Array.isArray(value) ? value : {}; }
function displayPlatform(platform) { return platform === "codeforces" ? "Codeforces" : platform === "luogu" ? "洛谷" : platform || "未知"; }
function freshnessBadge(value) {
  const normalized = String(value || "never").toLowerCase();
  const klass = normalized === "fresh" || normalized === "synced" ? "good" : normalized === "failed" ? "bad" : "warn";
  const label = { fresh: "fresh", stale: "stale", failed: "failed", never: "未同步" }[normalized] || normalized;
  return `<span class="badge ${klass}">${escapeHtml(label)}</span>`;
}

function navigate(view) {
  if (!$("#setup-view").classList.contains("hidden")) return;
  $$(".view").forEach(node => node.classList.toggle("active", node.id === `${view}-view`));
  $$(".nav-item").forEach(node => node.classList.toggle("active", node.dataset.view === view));
  const titles = { today: "今日训练", workbench: "做题工作台", plans: "题单管理", review: "训练复盘", settings: "设置" };
  $("#page-title").textContent = titles[view] || "ACM Agent";
  location.hash = view;
}

function formatTime(value) {
  if (!value) return "无";
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? String(value) : new Intl.DateTimeFormat("zh-CN", { month: "numeric", day: "numeric", hour: "2-digit", minute: "2-digit" }).format(date);
}

function findStatus(data) { return asObject(data.status || data.summary || data); }

function accountOf(status, platform) {
  return asObject(asObject(status.accounts)[platform]);
}

function renderBootstrap(data) {
  state.bootstrap = data;
  const configured = data.configured !== false;
  $("#setup-view").classList.toggle("hidden", configured);
  $$(".view").forEach(node => node.classList.toggle("hidden", !configured));
  $(".topbar").classList.toggle("hidden", !configured);
  $("#server-dot").className = "status-dot online";
  $("#server-label").textContent = "本地服务已连接";
  $("#service-version").textContent = data.web?.version || data.version || data.service?.version || "1.0.0";
  $("#service-address").textContent = data.address || `127.0.0.1:${data.web?.port || data.port || location.port}`;
  if (!configured) return;

  const status = findStatus(data);
  const accounts = asObject(status.accounts);
  const sources = asObject(status.sources);
  const cf = asObject(accounts.codeforces);
  const luogu = asObject(accounts.luogu);
  const counts = asObject(status.status_counts);
  const acceptedByPlatform = asObject(status.accepted_by_platform);
  const dueCount = Array.isArray(status.review_due) ? status.review_due.length : (status.review_due ?? 0);
  $("#account-stats").innerHTML = [
    statCard("Codeforces", cf.handle || cf.identifier || cf.account_id || cf.username || "未配置", `${acceptedByPlatform.codeforces ?? "—"} 道 AC${cf.rating != null ? ` · Rating ${cf.rating}` : ""}`, sources.codeforces?.freshness),
    statCard("洛谷", luogu.uid || luogu.identifier || luogu.account_id || luogu.username || "未配置", `${acceptedByPlatform.luogu ?? "—"} 道公开 AC`, sources.luogu?.freshness),
    statCard("训练状态", status.accepted ?? counts.accepted ?? 0, `到期复做 ${dueCount} · 本地文件 ${status.local_files ?? 0}`, "accepted"),
  ].join("");
  renderSources(sources);
  renderActive(data.active_session || data.active || status.active_session || (data.active_sessions || [])[0]);
  renderRecentSessions(data.recent_sessions || []);
  fillSettings(data.config || data.settings || {}, accounts);
}

function statCard(label, value, foot, fresh) {
  return `<article class="stat-card"><div class="stat-top"><span>${escapeHtml(label)}</span>${fresh ? freshnessBadge(fresh) : ""}</div><div class="stat-value">${escapeHtml(value)}</div><div class="stat-foot">${escapeHtml(foot)}</div></article>`;
}

function renderSources(sources) {
  const list = $("#source-list");
  list.innerHTML = ["codeforces", "luogu"].map(platform => {
    const item = asObject(sources[platform]);
    const detail = item.error || `最近成功：${formatTime(item.last_success_at)}`;
    return `<div class="source-row"><strong>${displayPlatform(platform)}</strong><small title="${escapeHtml(detail)}">${escapeHtml(detail)}</small>${freshnessBadge(item.freshness)}</div>`;
  }).join("");
}

function renderActive(session) {
  const node = $("#active-session");
  if (!session) {
    node.className = "empty-state compact";
    node.textContent = "没有 active session";
    return;
  }
  const id = session.problem_id || session.problem?.problem_id || session.id || "未知题目";
  const path = session.source || session.path || session.local_path || "";
  node.className = "session-card";
  node.innerHTML = `<div class="card-top"><h3>${escapeHtml(id)}</h3><span class="badge good">计时中</span></div><span class="subtle">开始于 ${escapeHtml(formatTime(session.started_at || session.start_time))}</span>${path ? `<div class="path-row"><code title="${escapeHtml(path)}">${escapeHtml(path)}</code><button class="text-button copy-path" data-path="${escapeHtml(path)}">复制路径</button></div>` : ""}`;
  ["#verify-form input[name=problem]", "#close-form input[name=problem]"].forEach(selector => { if (!$(selector).value) $(selector).value = id; });
  if (!$("#ai-problem").value) switchAiProblem(id);
}

function fillSettings(config, accounts) {
  const form = $("#settings-form");
  const cf = asObject(accounts.codeforces);
  const luogu = asObject(accounts.luogu);
  form.elements.codeforces.value = config.codeforces || config.codeforces_handle || cf.handle || cf.identifier || cf.account_id || "";
  form.elements.luogu.value = config.luogu || config.luogu_uid || luogu.uid || luogu.identifier || luogu.account_id || "";
  form.elements.target_rating.value = config.target_rating || config.recommendation?.target_rating || cf.target_rating || "";
}

function renderRecentSessions(sessions, filter = $("#session-filter").value) {
  const visible = sessions.filter(item => filter === "all" || (filter === "active" ? Boolean(item.active) : String(item.result || "").toUpperCase() === filter));
  const node = $("#recent-sessions");
  node.className = visible.length ? "session-list" : "session-list empty-state compact";
  node.innerHTML = visible.length ? visible.map(item => `<div class="session-row"><code>${escapeHtml(item.problem_id || item.id)}</code><span>${escapeHtml(displayPlatform(item.platform))}</span><span>${item.active ? '<b class="badge good">进行中</b>' : escapeHtml(item.result || "unknown")}</span><span>${escapeHtml(item.minutes != null ? `${item.minutes} 分钟` : formatTime(item.started_at))}</span></div>`).join("") : "当前筛选下暂无 session";
}

async function loadBootstrap() {
  try {
    const data = await api("/api/bootstrap");
    showAlert("");
    renderBootstrap(data || {});
  } catch (error) {
    $("#server-dot").className = "status-dot offline";
    $("#server-label").textContent = "本地服务连接失败";
    showAlert(`无法读取本地服务：${error.message}`);
  }
}

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

async function waitForJob(jobId, label = "AI 任务处理中…") {
  showJobProgress(label);
  try {
    for (;;) {
      const job = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
      const status = String(job.status || "running").toLowerCase();
      if (["failed", "error", "cancelled"].includes(status)) throw new Error(job.error?.message || job.error || "后台任务失败");
      if (["done", "success", "succeeded", "finished", "complete", "completed"].includes(status) || job.done === true) return jobResult(job) || job;
      await new Promise(resolve => window.setTimeout(resolve, 600));
    }
  } finally { $("#job-progress").classList.add("hidden"); }
}

async function requestAiRecommendations(button) {
  const form = $("#recommend-controls");
  setBusy(button, true, "DeepSeek 重排中…");
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
    } });
    const result = await waitForJob(started.job_id, "正在用最近尝试明细重排候选…");
    renderRecommendations(result || {});
    toast(result?.ai?.fallback ? "AI 已降级" : "AI 推荐完成", result?.ai?.fallback || "确定性资格过滤保持不变。");
  } catch (error) { toast("AI 推荐失败", error.message, "error"); }
  finally { setBusy(button, false); }
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
    form.elements.coaching_thinking.checked = Boolean(status.settings?.coaching_thinking);
    form.elements.reasoning_effort.value = status.settings?.reasoning_effort || "high";
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
      coaching_thinking: form.elements.coaching_thinking.checked,
      reasoning_effort: form.elements.reasoning_effort.value,
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

function resetAiWorkbenchUi(problem = "") {
  $("#ai-problem").value = problem;
  $("#ai-statement").value = "";
  $("#ai-context-source").textContent = problem ? "尚未读取" : "尚未选择题目";
  const messages = $("#ai-chat-messages");
  messages.className = "ai-chat-messages empty-state compact";
  messages.textContent = problem ? "正在读取本题的持久对话…" : "开始题目后可在这里请求分级提示或代码诊断。";
  $("#ai-patch-code").textContent = "";
  $("#ai-patch-box").classList.add("hidden");
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
  try {
    if (fetch) {
      const started = await api("/api/jobs/problems/context/fetch", { body: { problem, force } });
      data = await waitForJob(started.job_id, "正在读取公开题面…");
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
    const response = await fetch(`/api/ai/conversations/${encodeURIComponent(conversationId)}/messages`, {
      method: "POST",
      headers: { Accept: "text/event-stream", "Content-Type": "application/json", "X-ACM-Token": state.token },
      body: JSON.stringify({ message, mode, hint_level: hintLevel }),
      signal: controller.signal,
    });
    if (!response.ok) {
      let detail = `HTTP ${response.status}`;
      try { const payload = await response.json(); detail = payload.error?.message || payload.error || detail; } catch {}
      assistant.remove(); throw new Error(String(detail));
    }
    const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = ""; let completed = false;
    for (;;) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const blocks = buffer.split(/\r?\n\r?\n/); buffer = blocks.pop() || "";
      for (const block of blocks) {
        if (!aiOperationIsCurrent(problemKey, epoch) || state.aiStreamController !== controller) { controller.abort(); break; }
        const item = parseSseBlock(block); if (!item) continue;
        if (item.event === "delta") { assistant.textContent += item.data.content || ""; $("#ai-chat-messages").scrollTop = $("#ai-chat-messages").scrollHeight; }
        else if (item.event === "done") completed = true;
        else if (item.event === "error") throw new Error(item.data.message || "DeepSeek 流式请求失败");
      }
      if (completed) { await reader.cancel(); break; }
      if (done) break;
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
  return true;
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
    const data = await waitForJob(started.job_id, "正在生成带错误注释的候选源码…");
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
    const data = await waitForJob(started.job_id, action === "apply" ? "正在应用并验证…" : "正在安全回退…");
    if (!aiOperationIsCurrent(problemKey, epoch)) return;
    if (data.verify) renderVerify(data.verify);
    toast(action === "apply" ? "补丁已应用" : "补丁已回退", data.verify?.passed === false ? "本地验证未通过，备份仍可回退。" : "操作完成。");
  } catch (error) { if (aiOperationIsCurrent(problemKey, epoch)) toast(action === "apply" ? "补丁应用失败" : "补丁回退失败", error.message, "error"); }
  finally { setBusy(button, false); }
}

async function submitSetup(form, settings = false) {
  const body = {
    codeforces: form.elements.codeforces.value.trim(),
    luogu: form.elements.luogu.value.trim(),
    target_rating: form.elements.target_rating.value ? Number(form.elements.target_rating.value) : null,
    skip_validate: form.elements.skip_validate.checked,
  };
  const button = $("button[type=submit]", form);
  setBusy(button, true, body.skip_validate ? "保存中…" : "验证账号中…");
  try {
    await api("/api/setup", { body });
    toast(settings ? "设置已保存" : "初始化完成", body.skip_validate ? "当前为离线配置，请稍后同步平台状态。" : "账号已验证并保存。");
    await loadBootstrap();
    if (!settings) {
      await loadPlans();
      await requestRecommendations();
    }
  } catch (error) { toast("保存失败", error.message, "error"); }
  finally { setBusy(button, false); }
}

function jobResult(job) { return job.result ?? job.output ?? job.data ?? null; }

async function startJob(path, body, kind) {
  const data = await api(path, { body });
  const jobId = data.job_id || data.id || data.job?.id;
  if (!jobId) {
    if (kind === "verify") renderVerify(data);
    return data;
  }
  state.activeJobs.set(jobId, kind);
  showJobProgress(kind === "sync" ? "正在同步 Codeforces 与洛谷…" : "正在编译、运行样例与对拍…");
  pollJob(jobId, kind);
  return data;
}

function showJobProgress(label) {
  const box = $("#job-progress");
  box.classList.remove("hidden");
  $("p", box).textContent = label;
}

async function pollJob(jobId, kind) {
  try {
    const job = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
    const status = String(job.status || job.state || "running").toLowerCase();
    const message = job.message || job.progress?.message;
    if (message) $("#job-progress p").textContent = message;
    if (["failed", "error", "cancelled"].includes(status)) throw new Error(job.error?.message || job.error || "后台任务失败");
    if (job.done === true || ["done", "success", "succeeded", "finished", "complete", "completed"].includes(status)) {
      state.activeJobs.delete(jobId);
      $("#job-progress").classList.add("hidden");
      const result = jobResult(job);
      if (kind === "verify") renderVerify(result || job);
      else {
        toast("同步完成", "推荐和账号状态已更新。");
        await loadBootstrap();
        await requestRecommendations();
      }
      return;
    }
    window.setTimeout(() => pollJob(jobId, kind), 700);
  } catch (error) {
    state.activeJobs.delete(jobId);
    $("#job-progress").classList.add("hidden");
    if (kind === "verify") renderVerify({ ok: false, error: error.message });
    toast(kind === "sync" ? "同步失败" : "验证失败", error.message, "error");
  }
}

function renderVerify(result) {
  const passed = result?.passed ?? result?.ok === true;
  const badge = $("#verify-state");
  badge.className = `badge ${passed ? "good" : "bad"}`;
  badge.textContent = passed ? "验证通过" : "验证失败";
  const lines = [];
  if (result.problem_id) lines.push(`Problem: ${result.problem_id}`);
  if (result.compile?.command || result.compile_command) lines.push(`Compile: ${result.compile?.command || result.compile_command}`);
  if (result.compile?.stdout) lines.push(`\n[compiler stdout]\n${result.compile.stdout}`);
  if (result.compile?.stderr) lines.push(`\n[compiler stderr]\n${result.compile.stderr}`);
  if (result.compile_output) lines.push(`\n[compiler]\n${result.compile_output}`);
  const cases = result.cases || result.case_results || [];
  for (const test of cases) lines.push(`\n[${test.name || test.case || "case"}] ${test.passed ?? test.ok ? "PASS" : "FAIL"}${test.message ? ` — ${test.message}` : ""}`);
  if (result.sanitizer) lines.push(`\nSanitizer: ${result.sanitizer}`);
  if (result.stress) lines.push(`Stress: ${typeof result.stress === "object" ? JSON.stringify(result.stress, null, 2) : result.stress}`);
  if (result.failure_dir) lines.push(`\n失败资产: ${result.failure_dir}`);
  if (result.error) lines.push(`\nError: ${result.error.message || result.error}`);
  if (!lines.length) lines.push(JSON.stringify(result, null, 2));
  $("#verify-output").textContent = lines.join("\n");
  navigate("workbench");
}

function renderStart(data) {
  const box = $("#start-result");
  const id = data.problem_id || data.problem?.problem_id || data.problem || "题目";
  const path = data.source || data.path || data.local_path || "";
  box.className = "result-box success";
  box.innerHTML = `<strong>${escapeHtml(id)} 已开始</strong><p>${data.reused ? "已复用当日同名文件，不会覆盖原代码。" : "源码已创建。"}</p>${path ? `<div class="path-row"><code>${escapeHtml(path)}</code><button type="button" class="text-button copy-path" data-path="${escapeHtml(path)}">复制路径</button></div>` : ""}`;
  ["#verify-form input[name=problem]", "#close-form input[name=problem]"].forEach(selector => $(selector).value = id);
  switchAiProblem(id, { force: true });
  loadAiProblemState(id, { fetchContext: true });
  renderActive({ problem_id: id, source: path, started_at: data.started_at || new Date().toISOString() });
  navigate("workbench");
}

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
  return new Promise((resolve, reject) => {
    const poll = async () => {
      try {
        const job = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
        const status = String(job.status || job.state || "running").toLowerCase();
        if (["failed", "error", "cancelled"].includes(status)) { reject(tagJobError(job)); return; }
        if (job.done === true || ["done", "success", "succeeded", "finished", "complete", "completed"].includes(status)) {
          resolve(jobResult(job) || job);
          return;
        }
        window.setTimeout(poll, 700);
      } catch (error) { reject(error); }
    };
    poll();
  });
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
    const jobId = started.job_id || started.id || started.job?.id;
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

function setupEvents() {
  $$(".nav-item").forEach(button => button.addEventListener("click", () => {
    navigate(button.dataset.view);
    if (button.dataset.view === "review") loadReview();
    if (button.dataset.view === "plans") loadPlans();
  }));
  $$('[data-go]').forEach(button => button.addEventListener("click", () => navigate(button.dataset.go)));
  $("#theme-toggle").addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("acm-theme", next);
  });
  $("#setup-form").addEventListener("submit", event => { event.preventDefault(); submitSetup(event.currentTarget); });
  $("#settings-form").addEventListener("submit", event => { event.preventDefault(); submitSetup(event.currentTarget, true); });
  $("#ai-settings-form").addEventListener("submit", event => { event.preventDefault(); saveAiSettings(event.currentTarget); });
  $("#ai-credential-form").addEventListener("submit", event => { event.preventDefault(); saveAiCredential(event.currentTarget); });
  $("#ai-key-clear").addEventListener("click", event => clearAiCredential(event.currentTarget));
  $("#ai-test-button").addEventListener("click", async event => {
    const button = event.currentTarget;
    setBusy(button, true, "测试中…");
    try { const started = await api("/api/jobs/ai/test", { body: {} }); const result = await waitForJob(started.job_id, "正在验证 DeepSeek API…"); toast("DeepSeek 连接成功", result.model || "API 可用"); await loadAiStatus(); }
    catch (error) { toast("DeepSeek 测试失败", error.message, "error"); }
    finally { setBusy(button, false); }
  });
  $("#recommend-controls").addEventListener("submit", event => { event.preventDefault(); requestRecommendations(); });
  $("#ai-recommend-button").addEventListener("click", event => requestAiRecommendations(event.currentTarget));
  $("#recommend-controls [name=source_mode]").addEventListener("change", event => {
    $("#recommend-plan-filter").classList.toggle("hidden", event.currentTarget.value === "catalog_only");
  });
  $("#sync-button").addEventListener("click", async event => {
    const button = event.currentTarget;
    setBusy(button, true, "同步中…");
    try { await startJob("/api/jobs/sync", { platform: "all" }, "sync"); }
    catch (error) { toast("无法启动同步", error.message, "error"); }
    finally { setBusy(button, false); }
  });
  $("#recommendations").addEventListener("click", event => {
    const skipButton = event.target.closest(".skip-recommendation");
    if (skipButton) { openSkipDialog(skipButton.dataset.recommendationIndex); return; }
    const button = event.target.closest(".start-recommendation");
    if (!button) return;
    $("#start-form").elements.problem.value = button.dataset.problem;
    navigate("workbench");
    $("#start-form input[name=problem]").focus();
  });
  $("#start-form").addEventListener("submit", async event => {
    event.preventDefault(); const form = event.currentTarget; const button = $("button[type=submit]", form);
    setBusy(button, true);
    try { renderStart(await api("/api/sessions/start", { body: { problem: form.elements.problem.value.trim(), with_stress: form.elements.with_stress.checked } })); }
    catch (error) { toast("开始失败", error.message, "error"); }
    finally { setBusy(button, false); }
  });
  $("#verify-form").addEventListener("submit", async event => {
    event.preventDefault(); const form = event.currentTarget; const button = $("button[type=submit]", form);
    setBusy(button, true, "任务已提交");
    const seed = form.elements.seed.value;
    try { await startJob("/api/jobs/verify", { problem: form.elements.problem.value.trim() || null, exact: form.elements.compare.value === "exact", debug: form.elements.debug.checked, timeout: Number(form.elements.timeout.value), stress_iterations: Number(form.elements.stress_iterations.value), seed: seed ? Number(seed) : null }, "verify"); }
    catch (error) { renderVerify({ ok: false, error: error.message }); toast("无法启动验证", error.message, "error"); }
    finally { setBusy(button, false); }
  });
  $("#ai-context-button").addEventListener("click", async event => {
    const button = event.currentTarget;
    setBusy(button, true, "抓取中…");
    try { const data = await loadProblemContext({ fetch: true, force: true }); if (data) toast("题面已更新", "抓取成功或已使用人工版本。"); }
    catch (error) { $("#ai-context-source").textContent = `抓取失败：${error.message}`; toast("题面抓取失败", "可粘贴题面后保存。", "error"); }
    finally { setBusy(button, false); }
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
    event.preventDefault(); const form = event.currentTarget; const button = $("button[type=submit]", form);
    const message = form.elements.message.value.trim(); if (!message) return;
    setBusy(button, true, "回答中…");
    try { if (await streamAiChat(message, $("#ai-mode").value, Number($("#ai-hint-level").value))) form.reset(); }
    catch (error) { if (!isAbortError(error)) toast("AI 对话失败", error.message, "error"); }
    finally { setBusy(button, false); }
  });
  $("#ai-chat-clear").addEventListener("click", event => {
    clearAiConversation(event.currentTarget).catch(error => {
      if (!isAbortError(error)) toast("清空失败", error.message, "error");
    });
  });
  $("#ai-patch-preview").addEventListener("click", event => previewAiPatch(event.currentTarget));
  $("#ai-patch-apply").addEventListener("click", event => runPatchAction("apply", event.currentTarget));
  $("#ai-patch-revert").addEventListener("click", event => runPatchAction("revert", event.currentTarget));
  $("#close-form").addEventListener("submit", async event => {
    event.preventDefault(); const form = event.currentTarget; const button = $("button[type=submit]", form); setBusy(button, true);
    try {
      const data = await api("/api/sessions/close", { body: { problem: form.elements.problem.value.trim(), result: form.elements.result.value, minutes: Number(form.elements.minutes.value), hint_level: Number(form.elements.hint_level.value), failure: form.elements.failure.value, notes: form.elements.notes.value.trim() || null } });
      const box = $("#close-result"); box.className = "result-box success";
      box.innerHTML = `<strong>Session 已结束</strong><p>状态：${escapeHtml(data.status || data.close?.result || "已记录")}${data.review_due ? ` · 复做日期：${escapeHtml(data.review_due)}` : ""}</p><p>归档候选已保存，但尚未修改 <code>algorithms.md</code> 或 <code>tricks.md</code>。</p>`;
      renderActive(null); switchAiProblem("", { force: true }); toast("复盘已记录", data.review_due ? `已加入 ${data.review_due} 复做队列。` : "本次结果已影响后续推荐。");
      await loadBootstrap();
      if (currentAiProblem()) await loadAiProblemState(currentAiProblem());
      await requestRecommendations();
    } catch (error) { toast("结束失败", error.message, "error"); }
    finally { setBusy(button, false); }
  });
  $("#review-button").addEventListener("click", loadReview);
  $("#skip-confirm").addEventListener("click", event => confirmSkip(event.currentTarget));
  $("#skip-dialog").addEventListener("close", () => { state.skipCandidate = null; });
  $("#skipped-problems").addEventListener("click", event => {
    const button = event.target.closest(".unskip-button");
    if (button) unskipProblem(button.dataset.problem, button);
  });
  $("#session-filter").addEventListener("change", event => renderRecentSessions(state.bootstrap?.recent_sessions || [], event.currentTarget.value));
  $("#plan-check-button").addEventListener("click", async event => {
    const button = event.currentTarget;
    setBusy(button, true, "校验中…"); const box = $("#plan-result");
    try { const data = await api("/api/plan/check", { body: {} }); box.className = `result-box ${data.ok === false ? "error" : "success"}`; box.textContent = data.ok === false ? `校验失败：${(data.errors || []).join("；")}` : `题单校验通过${data.warnings?.length ? `，警告：${data.warnings.join("；")}` : ""}`; }
    catch (error) { box.className = "result-box error"; box.textContent = error.message; }
    finally { setBusy(button, false); }
  });
  $("#shutdown-button").addEventListener("click", async event => {
    if (!window.confirm("停止 ACM Agent 本地服务？当前页面随后将无法继续使用。")) return;
    const button = event.currentTarget;
    setBusy(button, true, "正在停止…");
    try { await api("/api/server/shutdown", { body: {} }); $("#server-dot").className = "status-dot offline"; $("#server-label").textContent = "本地服务已停止"; toast("服务已停止", "可以关闭此页面。"); }
    catch (error) { toast("停止失败", error.message, "error"); setBusy(button, false); }
  });
  $("#plan-list").addEventListener("click", event => {
    const button = event.target.closest("[data-plan-id]");
    if (button) selectPlan(button.dataset.planId);
  });
  $("#plan-refresh-button").addEventListener("click", event => {
    const button = event.currentTarget; setBusy(button, true, "…");
    loadPlans({ forceDetail: true }).finally(() => setBusy(button, false));
  });
  $("#plan-import-button").addEventListener("click", () => $("#plan-file-input").click());
  $("#plan-file-input").addEventListener("change", event => {
    const file = event.currentTarget.files?.[0];
    if (file) previewPlanFile(file).catch(error => toast("文件读取失败", error.message, "error"));
    event.currentTarget.value = "";
  });
  $("#plan-template-button").addEventListener("click", event => downloadPlanTemplate(event.currentTarget));
  $("#plan-replace-confirm input").addEventListener("change", event => {
    $("#plan-import-confirm").disabled = !event.currentTarget.checked || importErrors(state.importPreview).length > 0;
  });
  $("#plan-import-confirm").addEventListener("click", event => confirmPlanImport(event.currentTarget));
  $("#plan-tags-apply").addEventListener("click", event => applyTagPreview(event.currentTarget));
  $("#plan-tags-dialog").addEventListener("close", () => {
    state.tagPreview = null;
    state.tagPreviewPlanId = "";
    state.tagPreviewRevision = null;
    state.tagPreviewOverrideRevision = null;
    state.tagPreviewMode = "fill_missing";
  });
  $("#plan-editor").addEventListener("change", event => {
    const target = event.target;
    if (target.dataset.planField) mutatePlanField(target);
    else if (target.dataset.stageField) mutateStageField(target);
    else if (target.dataset.taskField) mutateTaskField(target);
  });
  $("#plan-editor").addEventListener("submit", event => {
    const form = event.target.closest("[data-add-task-form]");
    if (!form) return;
    event.preventDefault();
    addTaskFromForm(form);
  });
  $("#plan-editor").addEventListener("click", event => {
    const restore = event.target.closest("[data-restore-revision]");
    if (restore) { restoreRevision(restore.dataset.restoreRevision, restore); return; }
    const taskAction = event.target.closest("button[data-task-action]");
    if (taskAction) { handleTaskAction(taskAction); return; }
    const stageAction = event.target.closest("button[data-stage-action]");
    if (stageAction) { handleStageAction(stageAction); return; }
    const actionButton = event.target.closest("[data-plan-action]");
    if (!actionButton) return;
    const action = actionButton.dataset.planAction;
    if (action === "export" && state.selectedPlan) downloadJson(`${state.selectedPlan.plan_id}.json`, state.selectedPlan);
    else if (action === "revisions") loadRevisions();
    else if (action === "close-revisions") $("#plan-revisions").classList.add("hidden");
    else if (action === "toggle") toggleSelectedPlan();
    else if (action === "delete") deleteSelectedPlan();
    else if (action === "complete-tags") startTagPreview(actionButton, "fill_missing");
    else if (action === "cleanup-tags") startTagPreview(actionButton, "cleanup");
    else if (action === "reload") selectPlan(state.selectedPlanId, true);
    else if (action === "edit-meta") {
      state.editingPlanMeta = !state.editingPlanMeta;
      state.editingStageKey = "";
      renderPlanEditor();
    }
    else if (action === "add-stage") {
      const next = clonePlan();
      const stageKey = stableKey("stage");
      stagesOf(next).push({ stage_key: stageKey, topic: "新阶段", kind: "practice", unlock_at: null, due_date: null, tasks: [], replacements: [] });
      state.editingPlanMeta = false;
      state.editingStageKey = stageKey;
      savePlan(next, "阶段已添加");
    }
  });
  document.addEventListener("click", event => { const button = event.target.closest(".copy-path"); if (button) copyPath(button.dataset.path); });
}

async function boot() {
  captureToken();
  const storedTheme = localStorage.getItem("acm-theme");
  if (storedTheme) document.documentElement.dataset.theme = storedTheme;
  $("#today-label").textContent = new Intl.DateTimeFormat("zh-CN", { year: "numeric", month: "long", day: "numeric", weekday: "long" }).format(new Date());
  setupEvents();
  const initial = location.hash.replace(/^#/, "");
  if (["today", "workbench", "plans", "review", "settings"].includes(initial)) navigate(initial);
  await loadBootstrap();
  await loadAiStatus();
  if (currentAiProblem()) await loadAiProblemState(currentAiProblem(), { force: true });
  if (state.bootstrap?.configured !== false) {
    await loadPlans();
    await requestRecommendations();
  }
}

boot();
