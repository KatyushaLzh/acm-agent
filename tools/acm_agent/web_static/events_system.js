import {
  $, $$, api, clearLocalFileSelection, pickLocalFile, setBusy,
  syncLocalFileSelections, toast,
} from "./core.js";

function bindSystemEvents() {
  $$('[data-file-picker]').forEach(button => button.addEventListener("click", event => {
    pickLocalFile(event.currentTarget).catch(error => toast("文件选择失败", error.message, "error"));
  }));
  $$('[data-file-clear]').forEach(button => button.addEventListener("click", event => clearLocalFileSelection(event.currentTarget)));
  [$("#ai-stress-form"), $("#close-form")].forEach(form => form.addEventListener("reset", () => {
    window.setTimeout(() => syncLocalFileSelections(form), 0);
  }));
  $("#plan-check-button").addEventListener("click", async event => {
    const button = event.currentTarget;
    setBusy(button, true, "校验中…");
    const box = $("#plan-result");
    try {
      const data = await api("/api/plan/check", { body: {} });
      box.className = `result-box ${data.ok === false ? "error" : "success"}`;
      box.textContent = data.ok === false
        ? `校验失败：${(data.errors || []).join("；")}`
        : `题单校验通过${data.warnings?.length ? `，警告：${data.warnings.join("；")}` : ""}`;
    } catch (error) {
      box.className = "result-box error";
      box.textContent = error.message;
    } finally {
      setBusy(button, false);
    }
  });
  $("#shutdown-button").addEventListener("click", async event => {
    if (!window.confirm("停止 ACM Agent 本地服务？当前页面随后将无法继续使用。")) return;
    const button = event.currentTarget;
    setBusy(button, true, "正在停止…");
    try {
      await api("/api/server/shutdown", { body: {} });
      $("#server-dot").className = "status-dot offline";
      $("#server-label").textContent = "本地服务已停止";
      toast("服务已停止", "可以关闭此页面。");
    } catch (error) {
      toast("停止失败", error.message, "error");
      setBusy(button, false);
    }
  });
}

export { bindSystemEvents };
