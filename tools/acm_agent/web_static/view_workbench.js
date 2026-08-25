import {
  $, api, asObject, displayPlatform, escapeHtml, jobIdOf, loadBootstrap, navigate, pollJob,
  renderActive, renderCppSource, renderVerify, setBusy, showJobProgress,
  state, toast,
} from "./core.js";
import {
  loadAiProblemState, switchAiProblem,
} from "./view_ai.js";
import { loadPlans } from "./view_plans.js";
import { requestRecommendations } from "./view_today.js";

const INITIAL_SYNC_JOB_KEY = "acm-initial-sync-job";

function setInitialSyncBusy(active) {
  [$("#settings-save-button"), $("#sync-button")].filter(Boolean).forEach(button => {
    button.classList.toggle("syncing", active);
    setBusy(button, active, "同步进行中…");
  });
}

const SYNC_PHASE_LABELS = {
  queued: "等待后台任务",
  preparing: "准备同步",
  account: "更新账号与提交",
  accepted: "更新公开 AC",
  codeforces: "同步 Codeforces",
  luogu: "同步洛谷",
  catalog: "更新题库",
  submissions: "更新做题状态",
  tags: "补全洛谷标签",
  tag_enrichment: "补全洛谷标签",
  finalizing: "整理同步结果",
  complete: "平台同步完成",
  completed: "同步完成",
};

function finiteNumber(...values) {
  return values.map(Number).find(Number.isFinite);
}

function formatElapsed(seconds) {
  const value = Math.max(0, Math.floor(Number(seconds) || 0));
  if (value < 60) return `${value} 秒`;
  const minutes = Math.floor(value / 60);
  const rest = value % 60;
  return rest ? `${minutes} 分 ${rest} 秒` : `${minutes} 分钟`;
}

function syncElapsedSeconds(job, progress, nowMs = Date.now()) {
  const explicit = finiteNumber(progress.elapsed_seconds, job.elapsed_seconds);
  if (Number.isFinite(explicit)) return explicit;
  const startedAt = progress.started_at || job.started_at || job.created_at;
  const startedMs = Date.parse(String(startedAt || ""));
  return Number.isFinite(startedMs) ? Math.max(0, (nowMs - startedMs) / 1000) : null;
}

function syncUsableLabel(progress, status) {
  if (progress.usable === true) return "主功能已可用";
  if (progress.usable === false) return "数据准备中";
  if (typeof progress.usable === "string" && progress.usable.trim()) return progress.usable.trim();
  return status === "queued" ? "已有快照仍可使用" : "基础功能可用，完整数据仍在更新";
}

function renderSyncProgress(job, status = "running") {
  const box = $("#sync-progress");
  if (!box) return;
  const progress = asObject(job?.progress);
  const normalizedStatus = String(status || job?.status || job?.state || "running").toLowerCase();
  const phase = String(progress.phase || "").toLowerCase();
  const platform = progress.platform ? displayPlatform(String(progress.platform).toLowerCase()) : "";
  const phaseLabel = SYNC_PHASE_LABELS[phase] || progress.phase || (normalizedStatus === "queued" ? SYNC_PHASE_LABELS.queued : "同步平台数据");
  const message = String(progress.message || progress.label || job?.message || phaseLabel);
  const step = finiteNumber(progress.step, progress.current);
  const total = finiteNumber(progress.total);
  const completed = finiteNumber(progress.completed);
  const failed = finiteNumber(progress.failed);
  const elapsed = syncElapsedSeconds(job || {}, progress);
  const facts = [];
  if (platform) facts.push(platform);
  if (Number.isFinite(step) && Number.isFinite(total) && total > 0) facts.push(`${Math.max(0, step)}/${total}`);
  if (Number.isFinite(completed)) facts.push(`完成 ${Math.max(0, completed)}`);
  if (Number.isFinite(failed) && failed > 0) facts.push(`失败 ${failed}`);
  if (Number.isFinite(elapsed)) facts.push(`已用时 ${formatElapsed(elapsed)}`);

  const bar = $("#sync-progress-bar", box);
  if (Number.isFinite(step) && Number.isFinite(total) && total > 0) {
    bar.max = total;
    bar.value = Math.min(total, Math.max(0, step));
  } else {
    bar.max = 1;
    bar.removeAttribute("value");
  }
  box.className = "sync-progress";
  box.dataset.status = normalizedStatus;
  $("#sync-progress-phase", box).textContent = normalizedStatus === "queued" ? "已排队" : String(phaseLabel);
  $("#sync-progress-message", box).textContent = message;
  $("#sync-progress-facts", box).textContent = facts.join(" · ") || "等待首个进度事件";
  $("#sync-progress-usable", box).textContent = syncUsableLabel(progress, normalizedStatus);
}

function syncOutcome(result) {
  const rows = Array.isArray(result?.results) ? result.results : [];
  const statuses = rows.map(item => String(item?.status || "unknown").toLowerCase());
  const failed = statuses.filter(status => status === "failed").length;
  const partial = statuses.filter(status => status === "partial").length;
  const successful = statuses.filter(status => status === "fresh" || status === "partial").length;
  const details = rows.map(item => `${displayPlatform(item?.platform)} ${String(item?.status || "unknown")}`).join("；");
  const luogu = rows.find(item => item?.platform === "luogu") || {};
  const failedTags = Number(luogu?.tag_enrichment?.failed || 0);
  const tagDetail = failedTags > 0 ? `；${failedTags} 道已 AC 洛谷题标签仍未补全` : "";
  if (rows.length && failed === rows.length) {
    return { kind: "failed", title: "同步失败", message: `${details || "所有平台均同步失败"}${tagDetail}` };
  }
  if (failed > 0 || partial > 0 || result?.ok === false) {
    return { kind: successful > 0 ? "partial" : "failed", title: successful > 0 ? "同步部分完成" : "同步失败", message: `${details || "同步结果不完整"}${tagDetail}` };
  }
  if (rows.length && statuses.every(status => status === "fresh")) {
    return { kind: "fresh", title: "同步完成", message: `${details}${tagDetail}` };
  }
  return { kind: "unknown", title: "同步任务已结束", message: "正在使用最新的数据来源状态，未把后台任务结束等同于业务同步成功。" };
}

function renderSyncOutcome(outcome) {
  const box = $("#sync-progress");
  if (!box) return;
  const bar = $("#sync-progress-bar", box);
  box.className = `sync-progress terminal ${outcome.kind}`;
  box.dataset.status = outcome.kind;
  bar.max = 1;
  bar.value = outcome.kind === "failed" ? 0 : 1;
  $("#sync-progress-phase", box).textContent = outcome.title;
  $("#sync-progress-message", box).textContent = outcome.message;
  $("#sync-progress-facts", box).textContent = outcome.kind === "fresh" ? "fresh" : outcome.kind === "partial" ? "partial" : outcome.kind === "failed" ? "failed" : "状态已刷新";
  $("#sync-progress-usable", box).textContent = outcome.kind === "failed" ? "保留最后成功快照" : "最新可用数据已加载";
}

async function refreshAfterSync() {
  await loadBootstrap();
  await requestRecommendations();
}

async function monitorInitialSyncJob(jobId) {
  if (!jobId) return;
  const epoch = ++state.initialSyncEpoch;
  state.initialSyncJobId = jobId;
  sessionStorage.setItem(INITIAL_SYNC_JOB_KEY, jobId);
  setInitialSyncBusy(true);
  try {
    const result = await pollJob(jobId, {
      interval: 900,
      shouldCancel: () => state.initialSyncEpoch !== epoch || state.initialSyncJobId !== jobId,
      onPoll(job, status) {
        if (state.initialSyncEpoch === epoch) renderSyncProgress(job, status);
      },
    });
    if (!result || state.initialSyncEpoch !== epoch) return;
    const outcome = syncOutcome(result);
    renderSyncOutcome(outcome);
    toast(outcome.title, outcome.message, outcome.kind === "failed" ? "error" : "success");
    await refreshAfterSync();
    return result;
  } catch (error) {
    if (state.initialSyncEpoch === epoch) {
      const outcome = { kind: "failed", title: "后台同步失败", message: `${error.message}。已保留最后成功快照，可点击右上角“同步”重试。` };
      renderSyncOutcome(outcome);
      toast(outcome.title, outcome.message, "error");
      await refreshAfterSync();
    }
  } finally {
    if (state.initialSyncEpoch === epoch) {
      state.initialSyncJobId = "";
      sessionStorage.removeItem(INITIAL_SYNC_JOB_KEY);
      setInitialSyncBusy(false);
    }
  }
}

function resumeInitialSyncJob() {
  const activeJobId = jobIdOf(state.bootstrap?.active_sync_job);
  const jobId = activeJobId || sessionStorage.getItem(INITIAL_SYNC_JOB_KEY) || "";
  if (jobId && state.initialSyncJobId !== jobId) void monitorInitialSyncJob(jobId);
}

async function submitSetup(form, settings = false) {
  const body = {
    codeforces: form.elements.codeforces.value.trim(),
    luogu: form.elements.luogu.value.trim(),
    target_rating: form.elements.target_rating.value ? Number(form.elements.target_rating.value) : null,
    skip_validate: form.elements.skip_validate.checked,
  };
  const button = $("button[type=submit]", form);
  setBusy(button, true, body.skip_validate ? "保存中…" : "正在验证账号…");
  let result = null;
  try {
    result = await api("/api/setup", { body });
  } catch (error) {
    toast("保存失败", error.message, "error");
  } finally {
    setBusy(button, false);
  }
  if (!result) return;

  const jobId = jobIdOf(result?.initial_sync_job);
  const jobError = result?.initial_sync_error;
  const detail = body.skip_validate
    ? "当前为离线配置，请稍后同步平台状态。"
    : jobId
      ? "账号已保存，完整题库正在后台抓取。"
      : `账号已保存，但后台抓取未启动${jobError?.message ? `：${jobError.message}` : ""}。`;
  toast(settings ? "设置已保存" : "初始化完成", detail, jobId || body.skip_validate ? "success" : "error");
  await loadBootstrap();
  if (jobId) void monitorInitialSyncJob(jobId);
  if (!settings) {
    await loadPlans();
    await requestRecommendations();
  }
}

async function startJob(path, body, kind) {
  const data = await api(path, { body });
  const jobId = jobIdOf(data);
  if (!jobId) {
    if (kind === "verify") renderVerify(data);
    return data;
  }
  if (kind === "sync") return monitorInitialSyncJob(jobId);
  showJobProgress(kind === "sync" ? "正在同步 Codeforces 与洛谷…" : "正在编译、运行样例与对拍…");
  await settleJob(jobId, kind);
  return data;
}

async function settleJob(jobId, kind) {
  try {
    const result = await pollJob(jobId, {
      interval: 700,
      onPoll(job) {
        const message = job.message || job.progress?.message;
        if (message) $("#job-progress p").textContent = message;
      },
    });
    if (kind === "verify") renderVerify(result);
    else {
      toast("同步完成", "推荐和账号状态已更新。");
      await loadBootstrap();
      await requestRecommendations();
    }
  } catch (error) {
    if (kind === "verify") renderVerify({ ok: false, error: error.message });
    toast(kind === "sync" ? "同步失败" : "验证失败", error.message, "error");
  } finally {
    $("#job-progress").classList.add("hidden");
  }
}

function renderTemplateHighlight() {
  renderCppSource($("#template-highlight"), $("#template-source").value);
}

async function loadWorkspaceTemplate() {
  const input = $("#template-source");
  if (!input) return;
  try {
    const data = await api("/api/workspace/template");
    input.value = data.template || "";
    renderTemplateHighlight();
    state.templateDirty = false;
    $("#template-state").textContent = data.source === "global" ? "已启用自定义缺省源" : "使用内置缺省源";
  } catch (error) {
    $("#template-state").textContent = `缺省源读取失败：${error.message}`;
  }
}

async function resetWorkspaceTemplate(button) {
  if (!window.confirm("恢复为内置缺省源？保存仍发生在下次点击「创建 / 复用源码并开始计时」时。")) return;
  setBusy(button, true, "…");
  try {
    const data = await api("/api/workspace/template");
    $("#template-source").value = data.builtin || "";
    renderTemplateHighlight();
    state.templateDirty = true;
    $("#template-state").textContent = "已恢复内置内容，随开始做题保存";
  } catch (error) { toast("恢复失败", error.message, "error"); }
  finally { setBusy(button, false); }
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

export {
  submitSetup, startJob, resumeInitialSyncJob,
  renderTemplateHighlight, loadWorkspaceTemplate,
  resetWorkspaceTemplate, renderStart,
};
