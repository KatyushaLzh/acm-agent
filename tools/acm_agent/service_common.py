"""Small cross-domain constants and identifiers shared by service modules."""

from __future__ import annotations

RESULTS = ("AC", "WA", "TLE", "RE", "MLE", "ABANDONED")
FAILURE_MODES = (
    "none",
    "selection",
    "modeling",
    "invariant",
    "implementation",
    "complexity",
    "edge_case",
    "editorial",
)
PUBLIC_AI_SETTING_KEYS = (
    "recommendation_model",
    "coaching_model",
    "summary_model",
    "validation_model",
    "recommendation_thinking",
    "coaching_thinking",
    "summary_thinking",
    "validation_thinking",
    "reasoning_effort",
    "summary_reasoning_effort",
    "validation_reasoning_effort",
)
STRESS_AI_REQUEST_TIMEOUT_SECONDS = 300.0
AI_CHAT_SOURCE_MAX_BYTES = 128 * 1024
AI_CHAT_CONTEXT_BUDGET_BYTES = 256 * 1024
AI_SUMMARY_STATEMENT_MAX_BYTES = 128 * 1024
AI_SUMMARY_SOURCE_MAX_BYTES = 128 * 1024
AI_SUMMARY_CHAT_BUDGET_BYTES = 256 * 1024
AI_SUMMARY_SCHEMA_EXCERPT_MAX_BYTES = 64 * 1024


class AIConversationConflict(RuntimeError):
    """A conversation lifecycle conflict safe to expose as HTTP 409."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = str(code)


def _db_problem_id(platform: str, problem_id: str) -> str:
    problem_id = problem_id.upper()
    if platform == "codeforces" and problem_id.startswith("CF"):
        return problem_id[2:]
    return problem_id


def _display_problem_id(platform: str, problem_id: str) -> str:
    problem_id = str(problem_id).upper()
    if platform == "codeforces" and not problem_id.startswith("CF"):
        return f"CF{problem_id}"
    return problem_id


def _problem_key(platform: str, problem_id: str) -> str:
    return f"{platform}:{_display_problem_id(platform, problem_id)}"

__all__ = [
    "AIConversationConflict",
    "AI_CHAT_CONTEXT_BUDGET_BYTES",
    "AI_CHAT_SOURCE_MAX_BYTES",
    "AI_SUMMARY_CHAT_BUDGET_BYTES",
    "AI_SUMMARY_SCHEMA_EXCERPT_MAX_BYTES",
    "AI_SUMMARY_SOURCE_MAX_BYTES",
    "AI_SUMMARY_STATEMENT_MAX_BYTES",
    "FAILURE_MODES",
    "PUBLIC_AI_SETTING_KEYS",
    "RESULTS",
    "STRESS_AI_REQUEST_TIMEOUT_SECONDS",
    "_db_problem_id",
    "_display_problem_id",
    "_problem_key",
]
