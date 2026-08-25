import {
  $, api, escapeHtml, jobIdOf, loadBootstrap, navigate, pollJob,
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
  const button = $("#settings-save-button");
  if (!button) return;
  button.classList.toggle("syncing", active);
  setBusy(button, active, "正在抓取…");
}

function initialSyncSummary(result) {
  const rows = Array.isArray(result?.results) ? result.results : [];
  const luogu = rows.find(item => item?.platform === "luogu") || {};
  const failedTags = Number(luogu?.tag_enrichment?.failed || 0);
  const problemCount = Number(luogu?.problems || 0);
  if (failedTags > 0) {
    return `洛谷目录已更新 ${problemCount} 道；仍有 ${failedTags} 道已 AC 题标签未补全。`;
  }
  return `两平台目录与做题状态已更新${problemCount ? `；洛谷写入 ${problemCount} 道` : ""}。`;
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
    });
    if (!result || state.initialSyncEpoch !== epoch) return;
    toast("后台抓取完成", initialSyncSummary(result));
    await loadBootstrap();
    await requestRecommendations();
  } catch (error) {
    if (state.initialSyncEpoch === epoch) {
      toast("后台抓取未完成", `${error.message}。可点击右上角“同步”重试。`, "error");
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
  const jobId = sessionStorage.getItem(INITIAL_SYNC_JOB_KEY) || "";
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
