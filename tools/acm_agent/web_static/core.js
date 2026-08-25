"use strict";

const state = {
  token: "",
  bootstrap: null,
  recommendations: [],
  recommendationEpoch: 0,
  recommendationController: null,
  plans: [],
  selectedPlanId: "",
  selectedPlan: null,
  selectedPlanMeta: null,
  planSelectionEpoch: 0,
  planSelectionController: null,
  editingPlanMeta: false,
  editingStageKey: "",
  importContent: "",
  importPreview: null,
  aiPlanImportEpoch: 0,
  aiPlanImportController: null,
  aiPlanJobId: "",
  aiPlanDraft: null,
  aiPlanPreview: null,
  aiPlanMetadata: null,
  aiPlanValidationEpoch: 0,
  aiPlanValidationController: null,
  aiPlanValidationTimer: null,
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
  knowledgeTargets: [],
  knowledgeEpoch: 0,
  knowledgeProposalId: "",
  knowledgeProposalRevision: null,
  knowledgeProposalDirty: false,
  knowledgeTargetInspection: null,
  templateDirty: false,
  initialSyncJobId: "",
  initialSyncEpoch: 0,
};

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];

let activeProblemHandler = () => {};

function setActiveProblemHandler(handler) {
  activeProblemHandler = typeof handler === "function" ? handler : () => {};
}

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
  if (options.signal) request.signal = options.signal;
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

function filePickerField(name) {
  return document.querySelector(`[data-file-picker-field="${name}"]`);
}

function resetKnowledgeTargetInspection() {
  state.knowledgeTargetInspection = null;
  const button = $("#knowledge-target-add");
  if (!button) return;
  delete button.dataset.inspectPhase;
  button.textContent = "检查并保存目标";
}

function setLocalFileSelection(name, path, { fromPicker = false } = {}) {
  const field = filePickerField(name);
  if (!field) return;
  const input = $(`input[name="${name}"]`, field);
  const status = $(".file-picker-status", field);
  const normalizedPath = String(path || "").trim();
  input.value = normalizedPath;
  status.textContent = normalizedPath || "未选择";
  status.title = normalizedPath;
  status.classList.toggle("selected", Boolean(normalizedPath));
  if (name === "knowledge_path") {
    resetKnowledgeTargetInspection();
    if (fromPicker) {
      $("#knowledge-target").value = "";
      $("#knowledge-target-name").value = "";
    }
  }
}

async function pickLocalFile(button) {
  const name = button.dataset.filePicker;
  const kind = button.dataset.kind;
  setBusy(button, true, "选择中…");
  try {
    const selected = await api("/api/local-files/pick", { body: { kind } });
    if (selected.cancelled) return;
    if (!selected.path) throw new Error("文件选择器未返回路径");
    setLocalFileSelection(name, selected.path, { fromPicker: true });
  } finally { setBusy(button, false); }
}

function clearLocalFileSelection(button) {
  const name = button.dataset.fileClear;
  setLocalFileSelection(name, "", { fromPicker: name === "knowledge_path" });
}

function syncLocalFileSelections(root = document) {
  $$('[data-file-picker-field]', root).forEach(field => {
    const input = $("input[type=hidden]", field);
    if (input?.name) setLocalFileSelection(input.name, input.value);
  });
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
  if (!$("#ai-problem").value) activeProblemHandler(id);
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

function jobProgressLabel(progress, fallback, nowMs = Date.now()) {
  const current = asObject(progress);
  const label = String(current.label || fallback || "后台任务处理中…");
  const step = Number(current.step);
  const total = Number(current.total);
  if (Number.isInteger(step) && step > 0 && Number.isInteger(total) && total > 0 && !label.includes(`${step}/${total}`)) {
    return `${step}/${total} ${label}`;
  }
  return label;
}

const JOB_FAILED = new Set(["failed", "error", "canceled", "cancelled"]);
const JOB_SUCCEEDED = new Set(["done", "success", "succeeded", "finished", "complete", "completed"]);

function jobIdOf(payload) {
  return payload?.job_id || payload?.id || payload?.job?.job_id || payload?.job?.id || "";
}

function jobResult(job) { return job.result ?? job.output ?? job.data ?? null; }

function defaultJobError(job) {
  const raw = job?.error;
  return new Error(raw?.message || raw || job?.message || "后台任务失败");
}

async function cancelQueuedJob(jobId) {
  try {
    await api(`/api/jobs/${encodeURIComponent(jobId)}`, { method: "DELETE" });
  } catch (error) {
    // A running or just-finished job cannot be cancelled.  The stale caller
    // still stops polling; only unexpected transport/server failures surface.
    if (error.status !== 404 && error.status !== 409) throw error;
  }
}

async function pollJob(jobId, {
  interval = 600,
  onPoll = null,
  shouldCancel = null,
  toError = defaultJobError,
  rejectWaiting = false,
} = {}) {
  for (;;) {
    if (typeof shouldCancel === "function" && shouldCancel()) {
      await cancelQueuedJob(jobId);
      return null;
    }
    const job = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
    if (typeof shouldCancel === "function" && shouldCancel()) {
      await cancelQueuedJob(jobId);
      return null;
    }
    const status = String(job.status || job.state || "running").toLowerCase();
    if (typeof onPoll === "function") onPoll(job, status);
    if (rejectWaiting && status.startsWith("waiting_")) {
      throw new Error("后台返回了已移除的人工等待状态；请重启本地服务后重试");
    }
    if (JOB_FAILED.has(status)) throw toError(job, status);
    if (job.done === true || JOB_SUCCEEDED.has(status)) return jobResult(job) || job;
    await new Promise(resolve => window.setTimeout(resolve, interval));
  }
}

function detailedJobError(job) {
  const error = asObject(job.error);
  const message = String(error.message || job.error || "后台任务失败");
  const failure = new Error(message);
  failure.jobError = error;
  return failure;
}

async function waitForJob(jobId, label = "AI 任务处理中…", onProgress = null, {
  interval = 600,
  shouldCancel = null,
} = {}) {
  showJobProgress(label);
  try {
    return await pollJob(jobId, {
      interval,
      shouldCancel,
      rejectWaiting: true,
      toError: detailedJobError,
      onPoll(job) {
        const progress = asObject(job.progress);
        const currentLabel = jobProgressLabel(progress, label);
        showJobProgress(currentLabel);
        if (typeof onProgress === "function") onProgress(progress, job, currentLabel);
      },
    });
  } finally { $("#job-progress").classList.add("hidden"); }
}

function showJobProgress(label) {
  const box = $("#job-progress");
  box.classList.remove("hidden");
  $("p", box).textContent = label;
}

function renderVerify(result) {
  const status = String(result?.verification_status || "").toLowerCase();
  const inconclusive = status === "inconclusive";
  const passed = !inconclusive && (status === "passed" || (result?.passed ?? result?.ok === true));
  const badge = $("#verify-state");
  badge.className = `badge ${inconclusive ? "warn" : passed ? "good" : "bad"}`;
  badge.textContent = inconclusive ? "证据不足" : passed ? "验证通过" : "验证失败";
  const lines = [];
  if (result.problem_id) lines.push(`Problem: ${result.problem_id}`);
  if (result.verification_level) lines.push(`Evidence: ${result.verification_level}`);
  if (inconclusive) lines.push("Conclusion: 仅编译成功；没有样例或对拍证据，不能判定正确性。");
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

export {
  state, $, $$, captureToken, api,
  setLocalFileSelection, pickLocalFile, clearLocalFileSelection, syncLocalFileSelections,
  escapeHtml, renderCppSource, safeHref, toast, setBusy,
  asObject, displayPlatform, navigate, formatTime, statCard, renderActive,
  renderRecentSessions, loadBootstrap, jobIdOf, jobProgressLabel, pollJob, waitForJob,
  cancelQueuedJob, showJobProgress, renderVerify, setActiveProblemHandler,
};
