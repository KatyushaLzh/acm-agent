import { $, $$, captureToken, loadBootstrap, navigate, setActiveProblemHandler, state } from "./core.js";
import { bindAiEvents } from "./events_ai.js";
import { bindPlanEvents } from "./events_plans.js";
import { bindReviewEvents } from "./events_review.js";
import { bindSystemEvents } from "./events_system.js";
import { bindTodayEvents } from "./events_today.js";
import { bindWorkbenchEvents } from "./events_workbench.js";
import {
  currentAiProblem, loadAiProblemState, loadAiStatus, loadKnowledgeTargets,
  switchAiProblem, updateAiProblemMode,
} from "./view_ai.js";
import { loadPlans } from "./view_plans.js";
import { loadReview } from "./view_review.js";
import { requestRecommendations } from "./view_today.js";
import { loadWorkspaceTemplate, resumeInitialSyncJob } from "./view_workbench.js";
import { bindAppearanceEvents, loadAppearance } from "./view_appearance.js";

setActiveProblemHandler(switchAiProblem);

function bindShellEvents() {
  $$(".nav-item").forEach(button => button.addEventListener("click", () => {
    navigate(button.dataset.view);
    if (button.dataset.view === "review") loadReview();
    if (button.dataset.view === "plans") loadPlans();
  }));
  $$("[data-go]").forEach(button => button.addEventListener("click", () => navigate(button.dataset.go)));
  $("#theme-toggle").addEventListener("click", () => {
    const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    document.documentElement.dataset.theme = next;
    localStorage.setItem("acm-theme", next);
  });
}

async function boot() {
  captureToken();
  const storedTheme = localStorage.getItem("acm-theme");
  if (storedTheme) document.documentElement.dataset.theme = storedTheme;
  $("#today-label").textContent = new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "long",
    day: "numeric",
    weekday: "long",
  }).format(new Date());

  bindShellEvents();
  bindSystemEvents();
  bindTodayEvents();
  bindAiEvents();
  bindWorkbenchEvents();
  bindReviewEvents();
  bindPlanEvents();
  bindAppearanceEvents();

  const initial = location.hash.replace(/^#/, "");
  if (["today", "workbench", "plans", "review", "settings"].includes(initial)) navigate(initial);
  await loadAppearance();
  await loadBootstrap();
  resumeInitialSyncJob();
  await loadAiStatus();
  await loadKnowledgeTargets();
  loadWorkspaceTemplate();
  updateAiProblemMode();
  if (currentAiProblem()) await loadAiProblemState(currentAiProblem(), { force: true });
  if (state.bootstrap?.configured !== false) {
    await loadPlans();
    await requestRecommendations();
  }
}

boot();
