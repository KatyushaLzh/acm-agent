import { $, renderRecentSessions, state } from "./core.js";
import {
  confirmSkip, copyPath, loadReview, unskipProblem,
} from "./view_review.js";

function bindReviewEvents() {
  $("#review-button").addEventListener("click", loadReview);
  $("#skip-confirm").addEventListener("click", event => confirmSkip(event.currentTarget));
  $("#skip-dialog").addEventListener("close", () => { state.skipCandidate = null; });
  $("#skipped-problems").addEventListener("click", event => {
    const button = event.target.closest(".unskip-button");
    if (button) unskipProblem(button.dataset.problem, button);
  });
  $("#session-filter").addEventListener("change", event => {
    renderRecentSessions(state.bootstrap?.recent_sessions || [], event.currentTarget.value);
  });
  document.addEventListener("click", event => {
    const button = event.target.closest(".copy-path");
    if (button) copyPath(button.dataset.path);
  });
}

export { bindReviewEvents };
