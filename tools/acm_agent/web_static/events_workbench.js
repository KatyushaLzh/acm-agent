import {
  $, api, escapeHtml, loadBootstrap, renderActive,
  renderVerify, setBusy, state, toast,
} from "./core.js";
import {
  currentAiProblem, loadAiProblemState, previewKnowledgeSummary, switchAiProblem,
} from "./view_ai.js";
import {
  controlStress, renderStart, renderTemplateHighlight, resetWorkspaceTemplate,
  revertStressBundle, showStressArtifacts, startAiStress, startJob, submitSetup,
} from "./view_workbench.js";
import { requestRecommendations } from "./view_today.js";

function bindWorkbenchEvents() {
  $("#setup-form").addEventListener("submit", event => {
    event.preventDefault();
    submitSetup(event.currentTarget);
  });
  $("#settings-form").addEventListener("submit", event => {
    event.preventDefault();
    submitSetup(event.currentTarget, true);
  });
  $("#sync-button").addEventListener("click", async event => {
    const button = event.currentTarget;
    setBusy(button, true, "正在同步中");
    button.classList.add("syncing");
    try {
      await startJob("/api/jobs/sync", { platform: "all" }, "sync");
    } catch (error) {
      toast("无法启动同步", error.message, "error");
    } finally {
      button.classList.remove("syncing");
      setBusy(button, false);
    }
  });
  $("#start-form").addEventListener("submit", async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const button = $("button[type=submit]", form);
    setBusy(button, true);
    try {
      const body = {
        problem: form.elements.problem.value.trim(),
        with_stress: form.elements.with_stress.checked,
      };
      if (state.templateDirty) body.template = $("#template-source").value;
      renderStart(await api("/api/sessions/start", { body }));
      if (state.templateDirty) {
        state.templateDirty = false;
        $("#template-state").textContent = "缺省源已保存";
      }
    } catch (error) {
      toast("开始失败", error.message, "error");
    } finally {
      setBusy(button, false);
    }
  });
  $("#template-source").addEventListener("input", () => {
    state.templateDirty = true;
    renderTemplateHighlight();
    $("#template-state").textContent = "修改后随「创建 / 复用源码并开始计时」保存";
  });
  $("#template-source").addEventListener("scroll", event => {
    $("#template-highlight").scrollTop = event.currentTarget.scrollTop;
    $("#template-highlight").scrollLeft = event.currentTarget.scrollLeft;
  });
  $("#template-reset").addEventListener("click", event => resetWorkspaceTemplate(event.currentTarget).catch(error => toast("恢复失败", error.message, "error")));
  $("#verify-form").addEventListener("submit", async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const button = $("button[type=submit]", form);
    setBusy(button, true, "任务已提交");
    const seed = form.elements.seed.value;
    try {
      await startJob("/api/jobs/verify", {
        problem: form.elements.problem.value.trim() || null,
        exact: form.elements.compare.value === "exact",
        debug: form.elements.debug.checked,
        timeout: Number(form.elements.timeout.value),
        stress_iterations: Number(form.elements.stress_iterations.value),
        seed: seed ? Number(seed) : null,
      }, "verify");
    } catch (error) {
      renderVerify({ ok: false, error: error.message });
      toast("无法启动验证", error.message, "error");
    } finally {
      setBusy(button, false);
    }
  });
  $("#ai-stress-form").elements.enabled.addEventListener("change", event => {
    $("#ai-stress-options").disabled = !event.currentTarget.checked;
  });
  $("#ai-stress-form").addEventListener("submit", async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const button = $("button[type=submit]", form);
    try {
      await startAiStress(form, button);
    } catch (error) {
      toast("无法启动 AI 持续对拍", error.message, "error");
    }
  });
  $("#ai-stress-stop").addEventListener("click", event => controlStress("stop", event.currentTarget).catch(error => toast("停止失败", error.message, "error")));
  $("#ai-stress-resume").addEventListener("click", event => controlStress("resume", event.currentTarget).catch(error => toast("继续失败", error.message, "error")));
  $("#ai-stress-finish").addEventListener("click", event => controlStress("finish", event.currentTarget).catch(error => toast("结束失败", error.message, "error")));
  $("#ai-stress-artifacts").addEventListener("click", event => showStressArtifacts(event.currentTarget).catch(error => toast("读取 helper 失败", error.message, "error")));
  $("#ai-stress-revert").addEventListener("click", event => revertStressBundle(event.currentTarget).catch(error => toast("回退失败", error.message, "error")));
  $("#close-form").addEventListener("submit", async event => {
    event.preventDefault();
    const form = event.currentTarget;
    const button = $("button[type=submit]", form);
    const summarize = form.elements.knowledge_enabled.checked;
    const knowledgeEpoch = ++state.knowledgeEpoch;
    setBusy(button, true, "记录中…");
    try {
      const data = await api("/api/sessions/close", { body: {
        problem: form.elements.problem.value.trim(),
        result: form.elements.result.value,
        minutes: Number(form.elements.minutes.value),
        hint_level: Number(form.elements.hint_level.value),
        failure: form.elements.failure.value,
        notes: form.elements.notes.value.trim() || null,
      } });
      const box = $("#close-result");
      box.className = "result-box success";
      box.innerHTML = `<strong>Session 已结束</strong><p>状态：${escapeHtml(data.status || data.close?.result || "已记录")}${data.review_due ? ` · 复做日期：${escapeHtml(data.review_due)}` : ""}</p><p>${summarize ? "正在生成可确认的 Markdown 预览；目标文件尚未修改。" : "归档候选已保存，未请求 Markdown 总结。"}</p>`;
      renderActive(null);
      switchAiProblem("", { force: true });
      toast("复盘已记录", data.review_due ? `已加入 ${data.review_due} 复做队列。` : "本次结果已影响后续推荐。");
      if (summarize) {
        setBusy(button, true, "生成总结中…");
        try {
          await previewKnowledgeSummary(data.attempt_id, knowledgeEpoch);
        } catch (error) {
          box.innerHTML += `<p class="warning-text">总结未生成：${escapeHtml(error.message)}</p>`;
          toast("Session 已结束，但总结未生成", error.message, "error");
        }
      }
      await loadBootstrap();
      if (currentAiProblem()) await loadAiProblemState(currentAiProblem());
      await requestRecommendations();
    } catch (error) {
      toast("结束失败", error.message, "error");
    } finally {
      setBusy(button, false);
    }
  });
}

export { bindWorkbenchEvents };
