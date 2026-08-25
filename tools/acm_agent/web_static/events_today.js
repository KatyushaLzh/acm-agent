import { $, navigate } from "./core.js";
import { requestAiRecommendations } from "./view_ai.js";
import { openSkipDialog } from "./view_review.js";
import { requestRecommendations } from "./view_today.js";

function bindTodayEvents() {
  $("#recommend-controls").addEventListener("submit", event => {
    event.preventDefault();
    requestRecommendations();
  });
  $("#ai-gap-fill-button").addEventListener("click", event => requestAiRecommendations(event.currentTarget, "gap_fill"));
  $("#ai-specialization-button").addEventListener("click", event => requestAiRecommendations(event.currentTarget, "specialization"));
  $("#recommend-controls [name=source_mode]").addEventListener("change", event => {
    $("#recommend-plan-filter").classList.toggle("hidden", event.currentTarget.value === "catalog_only");
  });
  $("#recommendations").addEventListener("click", event => {
    const skipButton = event.target.closest(".skip-recommendation");
    if (skipButton) {
      openSkipDialog(skipButton.dataset.recommendationIndex);
      return;
    }
    const button = event.target.closest(".start-recommendation");
    if (!button) return;
    $("#start-form").elements.problem.value = button.dataset.problem;
    navigate("workbench");
    $("#start-form input[name=problem]").focus();
  });
}

export { bindTodayEvents };
