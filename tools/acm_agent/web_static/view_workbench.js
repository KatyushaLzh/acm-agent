import {
  $, api, asObject, escapeHtml, jobIdOf, loadBootstrap, navigate, pollJob,
  renderActive, renderCppSource, renderVerify, safeHref, setBusy, showJobProgress,
  state, toast, waitForJob,
} from "./core.js";
import {
  STRICT_VALIDATOR_FAILURE_MESSAGE, isStrictValidatorCertificationFailure,
  loadAiProblemState, switchAiProblem,
} from "./view_ai.js";
import { loadPlans } from "./view_plans.js";
import { requestRecommendations } from "./view_today.js";

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

function stressRows(payload) {
  if (Array.isArray(payload)) return payload;
  return payload?.runs || payload?.items || [];
}

function stressRunOf(payload) {
  return asObject(payload?.run || payload);
}

function stressRunId(run) {
  return String(run.id || run.run_id || "");
}

function stressBundleId(payload, run = stressRunOf(payload)) {
  const bundle = asObject(payload?.bundle);
  return String(run.bundle_id || bundle.id || bundle.bundle_id || "");
}

function stressBundleOf(payload) {
  const nested = asObject(payload?.bundle);
  return Object.keys(nested).length ? nested : asObject(payload);
}

function stressTerminal(status) {
  return ["mismatch", "oracle_conflict", "reference_mismatch_unconfirmed", "stopped", "interrupted", "fault", "error", "failed", "completed", "limit_reached"].includes(String(status || "").toLowerCase());
}

function stressResumable(status) {
  return ["interrupted", "stopped", "mismatch", "oracle_conflict", "reference_mismatch_unconfirmed", "fault"].includes(String(status || "").toLowerCase());
}

function stressFinishable(status) {
  return ["pending", "preparing", "running", "stop_requested", "stopped", "interrupted"].includes(String(status || "").toLowerCase());
}

function stressElapsedEnd(run, status) {
  const normalizedStatus = String(status || "").toLowerCase();
  const frozenAt = normalizedStatus === "stop_requested"
    ? run.updated_at
    : stressTerminal(normalizedStatus)
      ? (run.completed_at || run.updated_at)
      : null;
  const parsed = Date.parse(String(frozenAt || ""));
  return Number.isFinite(parsed) ? parsed : Date.now();
}

function stressSourceLink(url) {
  const value = String(url || "").trim();
  // safeHref, not escapeHtml: this URL comes from AI-selected external search
  // results, so the protocol must be whitelisted the same way every other
  // injected link is.  Escaping alone still permits javascript: navigation.
  return value
    ? `<a href="${safeHref(value)}" target="_blank" rel="noopener noreferrer">${escapeHtml(value)}</a>`
    : "无外部来源链接";
}

function renderStressPreparationFailure(message) {
  const panel = $("#ai-stress-run-panel");
  panel.classList.remove("hidden");
  const badge = $("#ai-stress-run-state");
  badge.className = "badge bad";
  badge.textContent = "准备失败";
  if (!state.stressRunId) {
    $("#ai-stress-run-summary").innerHTML = "";
    for (const selector of ["#ai-stress-stop", "#ai-stress-resume", "#ai-stress-finish", "#ai-stress-artifacts", "#ai-stress-revert"]) {
      $(selector).disabled = true;
    }
  }
  const detail = $("#ai-stress-detail");
  detail.dataset.pinned = "true";
  detail.classList.remove("hidden");
  detail.textContent = String(message || "AI 持续对拍准备失败");
}

function renderStressRun(payload) {
  const run = stressRunOf(payload);
  const id = stressRunId(run);
  if (!id) return;
  if (state.stressRunId && state.stressRunId !== id) {
    delete $("#ai-stress-detail").dataset.pinned;
  }
  state.stressRunId = id;
  state.stressBundleId = stressBundleId(payload, run) || state.stressBundleId;
  const status = String(run.status || "running").toLowerCase();
  const finishing = status === "stop_requested" && run.stop_reason === "user_finished";
  const retiredProfile = Number(run.profile_version ?? run.config?.profile_version ?? 1) < 2;
  const panel = $("#ai-stress-run-panel");
  panel.classList.remove("hidden");
  const badge = $("#ai-stress-run-state");
  const bad = ["mismatch", "oracle_conflict", "reference_mismatch_unconfirmed", "fault", "error", "failed"].includes(status);
  badge.className = `badge ${bad ? "bad" : stressTerminal(status) ? "neutral" : "good"}`;
  badge.textContent = finishing ? "正在结束" : status || "running";
  const elapsedEnd = stressElapsedEnd(run, status);
  const elapsedSeconds = run.started_at ? Math.max(0, (elapsedEnd - Date.parse(run.started_at)) / 1000) : 0;
  const total = Number(run.total_count ?? run.total_cases ?? 0);
  const rateBaseTotal = Number(run.config?.rate_base_total ?? 0);
  const segmentTotal = Math.max(0, total - rateBaseTotal);
  const metrics = [
    ["阶段", run.phase || "—"],
    ["下一 seed", run.next_seed ?? "—"],
    ["small", run.small_count ?? run.small_cases ?? 0],
    ["large", run.large_count ?? run.large_cases ?? 0],
    ["累计", total],
    ["本轮 / 速度", `${segmentTotal} / ${elapsedSeconds > 0 ? (segmentTotal / elapsedSeconds).toFixed(1) : "0.0"} case/s`],
  ];
  const preparationRoot = asObject(run.preparation || run.preparation_meta || run.config?.preparation);
  const preparation = { ...preparationRoot, ...asObject(preparationRoot.generation) };
  const preparationUsage = asObject(preparation.usage || run.usage);
  const completionDetails = asObject(preparationUsage.completion_tokens_details);
  const generationMode = run.generation_mode || preparation.generation_mode || run.config?.generation_mode;
  const fastFallback = preparation.fast_fallback
    ?? preparation.fast_fallback_used
    ?? preparation.fallback_to_fast;
  const generationAttempt = preparation.attempt ?? preparation.generation_attempt;
  const reasoningTokens = preparation.reasoning_tokens
    ?? preparationUsage.reasoning_tokens
    ?? completionDetails.reasoning_tokens;
  const usableRemaining = preparation.usable_remaining_seconds ?? preparation.usable_seconds;
  const reservedValidation = preparation.reserved_validation_seconds ?? preparation.validation_reserve_seconds;
  if (generationMode) metrics.push(["生成模式", generationMode]);
  if (fastFallback !== undefined) metrics.push(["Fast 降级", fastFallback ? "是" : "否"]);
  if (generationAttempt !== undefined) metrics.push(["生成尝试", generationAttempt]);
  if (reasoningTokens !== undefined) metrics.push(["推理 token", reasoningTokens]);
  if (usableRemaining !== undefined) metrics.push(["可用剩余", `${Number(usableRemaining).toFixed(1)}s`]);
  if (reservedValidation !== undefined) metrics.push(["验证预留", `${Number(reservedValidation).toFixed(1)}s`]);
  $("#ai-stress-run-summary").innerHTML = metrics.map(([label, value, html]) => `<div class="ai-stress-metric"><span>${escapeHtml(label)}</span><strong>${html ? value : escapeHtml(value)}</strong></div>`).join("");
  $("#ai-stress-stop").disabled = stressTerminal(status) || ["stopping", "stop_requested"].includes(status);
  $("#ai-stress-resume").disabled = retiredProfile || !stressResumable(status);
  $("#ai-stress-finish").disabled = retiredProfile || !stressFinishable(status) || finishing;
  $("#ai-stress-artifacts").disabled = !state.stressBundleId;
  $("#ai-stress-revert").disabled = !state.stressBundleId || !stressTerminal(status);
  const detail = $("#ai-stress-detail");
  const failure = run.failure_path || run.failure_dir;
  if (failure) {
    detail.classList.remove("hidden");
    const sourcePath = String(run.user_source_path || "").trim();
    const sourceDirectory = sourcePath.replace(/[\\/][^\\/]*$/, "") || sourcePath;
    const conflictEvidence = status === "oracle_conflict"
      ? "\n冲突输出：current.out / ref1.out / ref2.out"
      : "";
    detail.textContent = `相关数据已经保存到: ${sourceDirectory || failure}\n状态：${status}\nseed：${run.next_seed ?? "—"}${conflictEvidence}${retiredProfile ? "\n该运行协议已停用，不能继续。" : ""}`;
  } else if (retiredProfile) {
    detail.classList.remove("hidden");
    detail.textContent = "该运行协议已停用，历史状态仅供查看，不能继续。";
  } else if (!detail.dataset.pinned) {
    detail.classList.add("hidden");
    detail.textContent = "";
  }
  if (!stressTerminal(status)) scheduleStressPoll(id);
  else if (state.stressPollTimer) { clearTimeout(state.stressPollTimer); state.stressPollTimer = null; }
}

function scheduleStressPoll(runId) {
  if (state.stressPollTimer) clearTimeout(state.stressPollTimer);
  state.stressPollTimer = window.setTimeout(async () => {
    state.stressPollTimer = null;
    if (runId !== state.stressRunId) return;
    try { renderStressRun(await api(`/api/stress/runs/${encodeURIComponent(runId)}`)); }
    catch (error) { toast("持续对拍状态读取失败", error.message, "error"); }
  }, 1200);
}

async function waitForStressPause(runId, initialPayload) {
  let latest = initialPayload;
  const deadline = Date.now() + 10000;
  while (!stressTerminal(stressRunOf(latest).status) && Date.now() < deadline) {
    await new Promise(resolve => window.setTimeout(resolve, 100));
    latest = await api(`/api/stress/runs/${encodeURIComponent(runId)}`);
  }
  return latest;
}

async function loadAiStressStatus() {
  const badge = $("#ai-stress-isolation");
  try {
    const status = await api("/api/ai/stress/status");
    const capability = asObject(status.sandbox || status.isolation || status.capability);
    const available = capability.available ?? status.sandbox_available ?? status.available;
    badge.className = `badge ${available ? "good" : "bad"}`;
    badge.textContent = available ? `隔离可用${capability.backend ? ` · ${capability.backend}` : ""}` : `隔离不可用${capability.reason ? ` · ${capability.reason}` : ""}`;
    const model = status.model || status.validation_model || status.settings?.validation_model;
    if (model && !$("#ai-stress-form").elements.model.value) $("#ai-stress-form").elements.model.value = model;
    const settings = asObject(status.settings);
    const aiSettings = asObject(status.ai || settings.ai);
    const prepareTimeout = Number(
      status.stress_prepare_timeout_seconds
      ?? settings.stress_prepare_timeout_seconds
      ?? aiSettings.stress_prepare_timeout_seconds,
    );
    if (Number.isInteger(prepareTimeout) && prepareTimeout >= 60 && prepareTimeout <= 1800) {
      $("#ai-stress-form").elements.preparation_timeout_seconds.value = String(prepareTimeout);
    }
    const generationMode = String(
      status.stress_generation_mode
      ?? settings.stress_generation_mode
      ?? aiSettings.stress_generation_mode
      ?? "",
    );
    if (["fast", "hybrid", "full_thinking"].includes(generationMode)) {
      $("#ai-stress-form").elements.generation_mode.value = generationMode;
    }
  } catch (error) {
    badge.className = "badge bad";
    badge.textContent = `状态不可用 · ${error.message}`;
  }
  try {
    const recent = stressRows(await api("/api/stress/runs"));
    const selected = recent.find(item => !stressTerminal(item.status)) || recent[0];
    if (selected) {
      renderStressRun(selected);
      const bundleId = stressBundleId(selected);
      if (bundleId) {
        try {
          const bundle = stressBundleOf(await api(`/api/stress/bundles/${encodeURIComponent(bundleId)}`));
          renderStressRun({ run: selected, bundle });
        } catch { /* Run state remains usable even if source metadata is unavailable. */ }
      }
    }
  } catch { /* Older service builds may not expose stress history yet. */ }
}

async function startAiStress(form, button) {
  if (!form.elements.enabled.checked) throw new Error("请先勾选显式启用 AI 持续对拍");
  const problem = $("#verify-form").elements.problem.value.trim();
  if (!problem) throw new Error("请输入题号，或先开始一个 active session");
  const seed = form.elements.seed.value;
  const includeValidator = form.elements.include_validator.checked;
  const minimalVerification = !includeValidator;
  const largeProfile = form.elements.large_profile.checked;
  const preparationTimeout = Number(form.elements.preparation_timeout_seconds.value);
  if (!Number.isInteger(preparationTimeout) || preparationTimeout < 60 || preparationTimeout > 1800) {
    throw new Error("准备耗时上限必须是 60–1800 秒之间的整数");
  }
  const payload = {
    problem,
    generate_generator: form.elements.generate_generator.checked,
    prepare_reference_primary: form.elements.prepare_reference_primary.checked,
    prepare_reference_secondary: form.elements.prepare_reference_secondary.checked,
    large_profile: largeProfile,
    include_validator: includeValidator,
    minimal_verification: minimalVerification,
    unvalidated_large: minimalVerification && largeProfile,
    model: form.elements.model.value || null,
    seed: seed ? Number(seed) : null,
    timeout: Number(form.elements.timeout.value),
    reference_secondary_timeout: Number(form.elements.reference_secondary_timeout.value),
    compare: $("#verify-form").elements.compare.value,
    preparation_timeout_seconds: preparationTimeout,
    cache_mode: form.elements.cache_mode.value,
    generation_mode: form.elements.generation_mode.value,
    reference_primary_file: String(form.elements.reference_primary_file?.value || "").trim() || null,
    reference_secondary_file: String(form.elements.reference_secondary_file?.value || "").trim() || null,
    generator_file: String(form.elements.generator_file?.value || "").trim() || null,
  };
  if (includeValidator) {
    payload.allow_validator_degradation = false;
    payload.unvalidated_large = false;
  }
  const preparationDetail = $("#ai-stress-detail");
  delete preparationDetail.dataset.pinned;
  if (!state.stressRunId) {
    preparationDetail.classList.add("hidden");
    preparationDetail.textContent = "";
  }
  setBusy(button, true, "AI 准备中…");
  try {
    const started = await api("/api/jobs/ai/stress/start", { body: payload });
    const result = await waitForJob(
      jobIdOf(started),
      "AI 准备任务正在排队…",
      (_progress, _job, currentLabel) => setBusy(button, true, currentLabel),
    );
    renderStressRun(result);
    toast("持续对拍已启动", "刷新或切换题目不会终止运行。");
  } catch (error) {
    if (includeValidator && isStrictValidatorCertificationFailure(error)) {
      const strictFailure = new Error(STRICT_VALIDATOR_FAILURE_MESSAGE);
      renderStressPreparationFailure(strictFailure.message);
      throw strictFailure;
    }
    renderStressPreparationFailure(error.message);
    throw error;
  } finally { setBusy(button, false); }
}

async function controlStress(action, button) {
  if (!state.stressRunId) return;
  if (action === "finish" && !window.confirm("结束后该 run 将不能继续；helper 与历史记录会保留。确认结束对拍？")) return;
  const busyLabel = action === "stop" ? "暂停中…" : action === "finish" ? "结束中…" : "继续中…";
  setBusy(button, true, busyLabel);
  let result;
  try {
    result = await api(`/api/stress/runs/${encodeURIComponent(state.stressRunId)}/${action}`, { body: {} });
    if (action === "stop" && !stressTerminal(stressRunOf(result).status)) {
      renderStressRun(result);
      if (state.stressPollTimer) {
        clearTimeout(state.stressPollTimer);
        state.stressPollTimer = null;
      }
      try {
        result = await waitForStressPause(state.stressRunId, result);
      } catch (error) {
        scheduleStressPoll(state.stressRunId);
        throw error;
      }
    }
  } finally { setBusy(button, false); }
  renderStressRun(result);
  if (action === "stop") toast("暂停请求已发送", "当前隔离进程树将安全退出，稍后可继续。");
  else if (action === "finish") toast("结束请求已发送", "进程树退出后将释放持续对拍运行锁，helper 与历史记录会保留。");
  else toast("持续对拍已继续", "复用现有 helper，从保存的 next seed 继续；速度从本轮重新计算。");
}

async function showStressArtifacts(button) {
  if (!state.stressBundleId) return;
  setBusy(button, true, "读取中…");
  try {
    const bundle = stressBundleOf(await api(`/api/stress/bundles/${encodeURIComponent(state.stressBundleId)}`));
    const artifacts = Array.isArray(bundle.artifacts) ? bundle.artifacts : [];
    rememberStressReferenceLink(artifacts);
    const detail = $("#ai-stress-detail");
    detail.classList.remove("hidden");
    detail.dataset.pinned = "true";
    detail.innerHTML = artifacts.map(item => {
      const validation = asObject(item.validation);
      const audit = asObject(validation.ai_audit || item.static_audit);
      const role = String(item.kind || "unknown");
      const source = String(item.source_kind || "unknown");
      const hash = String(item.source_hash || item.source_content_hash || "—");
      const auditState = audit.accepted === true ? "accepted" : audit.accepted === false ? "rejected" : "not_applicable";
      const url = role === "validator" ? "" : String(item.source_url || "").trim();
      return `${escapeHtml(role)} · source=${escapeHtml(source)} · sha256=${escapeHtml(hash)} · audit=${escapeHtml(auditState)}${url ? `\n${stressSourceLink(url)}` : ""}`;
    }).join("\n\n") || "无 helper 记录";
  } finally { setBusy(button, false); }
}

async function revertStressBundle(button) {
  if (!state.stressBundleId || !window.confirm("回退本次 AI 写入的 generator、validator 和两份 reference？如果文件已被你修改，服务会拒绝覆盖。")) return;
  setBusy(button, true, "回退中…");
  try {
    const started = await api(`/api/jobs/stress/bundles/${encodeURIComponent(state.stressBundleId)}/revert`, { body: {} });
    await waitForJob(jobIdOf(started), "正在进行 hash 校验并回退 helper…");
    toast("helper 已回退", "用户后续修改未被覆盖。");
  } finally { setBusy(button, false); }
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
  submitSetup, startJob,
  loadAiStressStatus, startAiStress, controlStress, showStressArtifacts,
  revertStressBundle, renderTemplateHighlight, loadWorkspaceTemplate,
  resetWorkspaceTemplate, renderStart,
};
