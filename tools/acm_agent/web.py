"""Loopback-only web server for the ACM practice dashboard.

The module intentionally uses only the Python standard library.  It exposes a
small JSON API over :class:`AcmService` and serves the static dashboard assets
from ``tools/acm_agent/web_static``.
"""

from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
import csv
from dataclasses import dataclass
from datetime import datetime, timezone
import hmac
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import tempfile
import threading
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, quote, unquote, urlsplit
from urllib.request import Request, urlopen

from . import __version__
from .ai_context import PatchConflictError
from .config import Paths
from .credentials import CredentialStoreError
from .deepseek import DeepSeekError
from .knowledge_io import MarkdownWriteConflict
from .plan_manager import DuplicatePlanError
from .service_common import AIConversationConflict
from .storage_common import (
    MarkdownSummaryProposalRevisionConflict,
    MarkdownSummaryTargetRevisionConflict,
    PlanRevisionConflict,
    ProblemContextConflict,
    StressArtifactBundleRevisionConflict,
    StressArtifactCandidateConflict,
    StressArtifactProofConflict,
    StressBundleCertificationConflict,
    StressCacheAliasRevisionConflict,
    StressPreparationCacheConflict,
    StressRunRevisionConflict,
    StressSetupSlotConflict,
    TagOverrideRevisionConflict,
)
from .stress import BundleConflictError, SandboxUnavailableError, SourceSafetyError
from .stress_ai_core import StressPreparationError
from .stress_budget import PreparationBudgetExhausted
from .stress_runtime import StressRuntimeError

LOOPBACK_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
LAST_PORT = 8775
MAX_REQUEST_BYTES = 1024 * 1024
MAX_JOBS = 100
STATIC_MEDIA_TYPES = {
    ".css": "text/css",
    ".html": "text/html",
    ".js": "text/javascript",
    ".json": "application/json",
    ".svg": "image/svg+xml",
    ".ttf": "font/ttf",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _json_safe(value: Any) -> Any:
    """Round-trip values into the subset accepted by the JSON encoder."""

    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


@dataclass(frozen=True)
class ApiProblem(Exception):
    status: int
    code: str
    message: str


_REVISION_CONFLICT_TYPES = (
    PlanRevisionConflict,
    TagOverrideRevisionConflict,
    ProblemContextConflict,
    PatchConflictError,
    MarkdownSummaryTargetRevisionConflict,
    MarkdownSummaryProposalRevisionConflict,
    MarkdownWriteConflict,
    StressArtifactBundleRevisionConflict,
    StressRunRevisionConflict,
    StressCacheAliasRevisionConflict,
    BundleConflictError,
)
_STRESS_PERSISTENCE_CONFLICT_TYPES = (
    StressSetupSlotConflict,
    StressPreparationCacheConflict,
    StressArtifactCandidateConflict,
    StressArtifactProofConflict,
    StressBundleCertificationConflict,
)


_FAILURE_IDENTIFIER_RE = re.compile(r"^[a-z0-9_-]{1,32}$", re.IGNORECASE)
_FAILURE_PATH_RE = re.compile(
    r"^\$?(?:[A-Za-z_][A-Za-z0-9_-]*)(?:(?:\[[0-9]{1,4}\])|(?:\.[A-Za-z_][A-Za-z0-9_-]*))*$"
)
_SENSITIVE_FAILURE_RE = re.compile(
    r"api[-_ ]?key|authorization|bearer\s+|access[-_ ]?token|secret|password|"
    r"raw[-_ ]?reasoning|reasoning[-_ ]?content|思维链|推理原文",
    re.IGNORECASE,
)

class FileDialogUnavailable(RuntimeError):
    """Raised when the native file dialog cannot be created or displayed."""


def _default_local_file_picker(kind: str, initial_dir: Path) -> str | None:
    """Open a native Tk file dialog and return its selection, if any."""

    try:
        import tkinter as tk
        from tkinter import filedialog
    except (ImportError, RuntimeError) as exc:
        raise FileDialogUnavailable("Native file dialog is unavailable") from exc

    window = None
    try:
        window = tk.Tk()
        window.withdraw()
        window.attributes("-topmost", True)
        common = {
            "initialdir": str(initial_dir),
            "parent": window,
        }
        if kind == "cpp":
            selected = filedialog.askopenfilename(
                **common,
                title="选择 C++ 源文件",
                filetypes=(("C++ 源文件", "*.cpp"),),
            )
        else:
            selected = filedialog.asksaveasfilename(
                **common,
                title="选择 Markdown 文件",
                defaultextension=".md",
                filetypes=(("Markdown 文件", "*.md"),),
            )
    except (tk.TclError, RuntimeError, OSError) as exc:
        raise FileDialogUnavailable("Native file dialog is unavailable") from exc
    finally:
        if window is not None:
            try:
                window.destroy()
            except Exception:
                pass
    return str(selected) if selected else None


def _validate_local_file_selection(kind: str, selected: object) -> dict[str, str]:
    if not isinstance(selected, (str, os.PathLike)):
        raise ApiProblem(
            HTTPStatus.BAD_REQUEST,
            "invalid_file_selection",
            "File dialog returned an invalid path",
        )
    path_value = os.fspath(selected)
    if not isinstance(path_value, str):
        raise ApiProblem(
            HTTPStatus.BAD_REQUEST,
            "invalid_file_selection",
            "File dialog returned an invalid path",
        )
    raw_path = path_value.strip()
    if not raw_path or raw_path.startswith(("\\\\", "//")):
        raise ApiProblem(
            HTTPStatus.BAD_REQUEST,
            "invalid_file_selection",
            "Selected path must be an absolute local path",
        )
    path = Path(raw_path)
    if not path.is_absolute():
        raise ApiProblem(
            HTTPStatus.BAD_REQUEST,
            "invalid_file_selection",
            "Selected path must be an absolute local path",
        )

    expected_suffix = ".cpp" if kind == "cpp" else ".md"
    if path.suffix.lower() != expected_suffix:
        raise ApiProblem(
            HTTPStatus.BAD_REQUEST,
            "invalid_file_selection",
            f"Selected file must use the {expected_suffix} extension",
        )
    if kind == "cpp":
        if path.is_symlink() or not path.is_file():
            raise ApiProblem(
                HTTPStatus.BAD_REQUEST,
                "invalid_file_selection",
                "Selected C++ file must be an existing regular non-symlink file",
            )
    else:
        parent = path.parent
        if not parent.is_dir():
            raise ApiProblem(
                HTTPStatus.BAD_REQUEST,
                "invalid_file_selection",
                "Selected Markdown parent directory must exist",
            )
        if path.is_symlink() or (path.exists() and not path.is_file()):
            raise ApiProblem(
                HTTPStatus.BAD_REQUEST,
                "invalid_file_selection",
                "Selected Markdown target must be absent or a regular non-symlink file",
            )
    return {"path": str(path), "name": path.name}


def _safe_failure_identifier(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized if _FAILURE_IDENTIFIER_RE.fullmatch(normalized) else None


def _safe_failure_path(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized if _FAILURE_PATH_RE.fullmatch(normalized) else None


def _safe_failure_attempts(value: Any) -> int | dict[str, int] | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 0 <= value <= 100:
        return value
    if not isinstance(value, Mapping):
        return None
    result: dict[str, int] = {}
    for role, count in list(value.items())[:3]:
        safe_role = _safe_failure_identifier(role)
        if safe_role is None or isinstance(count, bool) or not isinstance(count, int):
            continue
        if 0 <= count <= 100:
            result[safe_role] = count
    return result or None


def _safe_failure_message(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.split())
    if not normalized:
        return None
    if _SENSITIVE_FAILURE_RE.search(normalized):
        return "结构化诊断包含敏感内容，已隐藏"
    # Messages may name the failed blueprint field, but never expose a raw
    # blueprint, prompt, reasoning trace, local path or credential material.
    fence = normalized.find("```")
    if fence >= 0:
        normalized = normalized[:fence].rstrip() + " [结构化内容已隐藏]"
    brace = normalized.find("{")
    if brace >= 0:
        normalized = normalized[:brace].rstrip(" :=") + " [结构化内容已隐藏]"
    if normalized.lstrip().startswith("["):
        return "结构化诊断已隐藏"
    normalized = re.sub(r"\b[A-Za-z]:[\\/]\S+", "[路径已隐藏]", normalized)
    normalized = re.sub(
        r"(?<!\w)/(?:home|Users|tmp|var|etc)/\S+",
        "[路径已隐藏]",
        normalized,
        flags=re.IGNORECASE,
    )
    return normalized[:300] or None


def _safe_role_failure(role_key: Any, value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    role = _safe_failure_identifier(value.get("role"))
    if role is None:
        role = _safe_failure_identifier(role_key)
    result: dict[str, Any] = {}
    for key, safe_value in (
        ("role", role),
        ("substage", _safe_failure_identifier(value.get("substage"))),
        ("path", _safe_failure_path(value.get("path"))),
        ("attempts", _safe_failure_attempts(value.get("attempts"))),
        ("message", _safe_failure_message(value.get("message"))),
    ):
        if safe_value is not None:
            result[key] = safe_value
    stage = _safe_failure_identifier(value.get("stage"))
    if stage is not None:
        result["stage"] = stage
    code = _safe_failure_identifier(value.get("code"))
    if code is not None:
        result["code"] = code
    elapsed = value.get("elapsed")
    if not isinstance(elapsed, bool) and isinstance(elapsed, (int, float)):
        result["elapsed"] = max(0.0, min(float(elapsed), 86400.0))
    return result or None


def _root_cause_from_details(
    details: Mapping[str, Any], safe_roles: Mapping[str, Mapping[str, Any]]
) -> tuple[str, str] | None:
    primary = details.get("primary_failure")
    selected = _safe_role_failure(None, primary) if isinstance(primary, Mapping) else None
    if selected is None and safe_roles:
        role_order = {
            "generator": 0,
            "reference_primary": 1,
            "reference_secondary": 2,
            # Legacy bundles remain viewable.
            "brute": 3,
            "reference": 4,
        }
        selected = min(
            safe_roles.values(),
            key=lambda item: role_order.get(str(item.get("role") or ""), 99),
        )
    if selected is None:
        return None
    label_parts = [
        str(selected[key])
        for key in ("role", "substage", "path")
        if selected.get(key)
    ]
    attempts = selected.get("attempts")
    if isinstance(attempts, int):
        label_parts.append(f"attempt {attempts}")
    elif isinstance(attempts, Mapping):
        counts = ", ".join(f"{role}={count}" for role, count in attempts.items())
        if counts:
            label_parts.append(f"attempts {counts}")
    role = str(selected.get("role") or "helper")
    substage = str(selected.get("substage") or selected.get("stage") or "prepare")
    message = str(selected.get("message") or f"{role} 的 {substage} 阶段失败")
    return " · ".join(label_parts) or role, message


class JobManager:
    """A bounded, single-worker in-memory background job registry."""

    def __init__(self, service_lock: threading.RLock, *, capacity: int = MAX_JOBS):
        self._service_lock = service_lock
        self._capacity = capacity
        self._jobs: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="acm-web-job")

    def submit(
        self,
        kind: str,
        function: Callable[..., Mapping[str, Any]],
        *,
        with_progress: bool = False,
        job_id: str | None = None,
    ) -> dict[str, Any]:
        job_id = job_id or secrets.token_urlsafe(12)
        record: dict[str, Any] = {
            "job_id": job_id,
            "kind": kind,
            "status": "queued",
            "created_at": _utc_now(),
            "started_at": None,
            "finished_at": None,
            "result": None,
            "error": None,
            "progress": None,
        }
        with self._lock:
            self._trim_locked(self._capacity - 1)
            if len(self._jobs) >= self._capacity:
                raise ApiProblem(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "job_capacity_exhausted",
                    "All background job slots are currently active",
                )
            self._jobs[job_id] = record
        self._executor.submit(self._run, job_id, function, with_progress)
        return self.get(job_id) or record.copy()

    def _run(
        self,
        job_id: str,
        function: Callable[..., Mapping[str, Any]],
        with_progress: bool,
    ) -> None:
        self._update(job_id, status="running", started_at=_utc_now())
        try:
            with self._service_lock:
                if with_progress:
                    result = function(self._progress_callback(job_id))
                else:
                    result = function()
        except Exception as exc:  # Job failures are data, not server failures.
            current = self.get(job_id) or {}
            details = _exception_details(exc)
            if (
                current.get("kind") == "ai_stress_start"
                and details.get("validator_strict_certification_failed") is True
            ):
                self._update(
                    job_id,
                    status="failed",
                    finished_at=_utc_now(),
                    error={
                        "code": "validator_strict_certification_failed",
                        "message": (
                            "validator 严格认证未通过，已终止 AI 对拍；"
                            "helper 未应用，run 未创建。"
                        ),
                        "validator_strict_certification_failed": True,
                        "helpers_unchanged": True,
                        "run_created": False,
                    },
                )
                return
            self._update(
                job_id,
                status="failed",
                finished_at=_utc_now(),
                error=_public_error_payload(
                    exc,
                    job_kind=str(current.get("kind") or ""),
                    progress=current.get("progress"),
                ),
            )
        else:
            self._update(
                job_id,
                status="succeeded",
                finished_at=_utc_now(),
                result=_json_safe(result),
            )

    def _progress_callback(
        self, job_id: str
    ) -> Callable[[str, str, int, int], None]:
        def report(
            stage: str,
            label: str,
            step: int,
            total: int,
            preparation: Mapping[str, Any] | None = None,
        ) -> None:
            # Progress reporting must never be able to fail the actual job.  It
            # is also normalized before crossing the HTTP boundary so a model
            # or provider error cannot inject an unbounded label into the UI.
            try:
                safe_stage = str(stage).strip()[:64] or "working"
                safe_label = str(label).strip()[:200] or safe_stage
                safe_total = max(1, min(int(total), 10_000))
                safe_step = max(0, min(int(step), safe_total))
            except (TypeError, ValueError, OverflowError):
                return
            progress = {
                "stage": safe_stage,
                "label": safe_label,
                "step": safe_step,
                "total": safe_total,
                "updated_at": _utc_now(),
            }
            if isinstance(preparation, Mapping):
                for key in (
                    "configured_timeout_seconds",
                    "elapsed_seconds",
                    "remaining_seconds",
                    "stage_elapsed_seconds",
                    "soft_budget_seconds",
                    "deadline_at",
                    "absolute_deadline",
                ):
                    value = preparation.get(key)
                    if isinstance(value, (int, float, str)) and not isinstance(value, bool):
                        progress[key] = value
            self._update(
                job_id,
                progress=progress,
            )

        return report

    def _update(self, job_id: str, **changes: Any) -> None:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is not None:
                record.update(changes)

    def _trim_locked(self, target: int) -> None:
        while len(self._jobs) > target:
            removable = next(
                (key for key, value in self._jobs.items() if value["status"] not in {"queued", "running"}),
                None,
            )
            if removable is None:
                return
            self._jobs.pop(removable, None)

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._jobs.get(job_id)
            return _json_safe(record) if record is not None else None

    def close(self, *, wait: bool = True) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=False)


def _problem_from_exception(exc: Exception) -> ApiProblem:
    if isinstance(exc, ApiProblem):
        return exc
    if isinstance(exc, AIConversationConflict):
        return ApiProblem(
            HTTPStatus.CONFLICT,
            str(getattr(exc, "code", "conversation_conflict")),
            str(exc),
        )
    if isinstance(exc, _REVISION_CONFLICT_TYPES):
        return ApiProblem(HTTPStatus.CONFLICT, "revision_conflict", str(exc))
    if isinstance(exc, _STRESS_PERSISTENCE_CONFLICT_TYPES):
        return ApiProblem(
            HTTPStatus.CONFLICT,
            str(getattr(exc, "code", "stress_conflict")),
            str(exc),
        )
    if isinstance(exc, SandboxUnavailableError):
        return ApiProblem(HTTPStatus.CONFLICT, "sandbox_unavailable", str(exc))
    if isinstance(
        exc,
        (SourceSafetyError, StressPreparationError, PreparationBudgetExhausted),
    ):
        code = str(getattr(exc, "code", "invalid_stress_artifact"))
        return ApiProblem(
            HTTPStatus.GATEWAY_TIMEOUT
            if code == "stress_prepare_budget_exhausted"
            else HTTPStatus.BAD_REQUEST,
            code,
            str(exc),
        )
    if isinstance(exc, StressRuntimeError):
        code = str(getattr(exc, "code", "stress_error"))
        status = (
            HTTPStatus.GATEWAY_TIMEOUT
            if code == "stress_prepare_budget_exhausted"
            else HTTPStatus.CONFLICT
            if code in {
                "stress_run_active",
                "stress_source_changed",
                "stress_bundle_active",
                "stress_setup_active",
            }
            else HTTPStatus.BAD_REQUEST
        )
        return ApiProblem(status, code, str(exc))
    if isinstance(exc, DeepSeekError):
        code = str(getattr(exc, "code", "deepseek_error"))
        if code in {"missing_api_key", "invalid_model", "invalid_messages", "invalid_reasoning_effort"}:
            return ApiProblem(HTTPStatus.BAD_REQUEST, code, str(exc))
        if code == "timeout":
            return ApiProblem(HTTPStatus.GATEWAY_TIMEOUT, code, str(exc))
        if code == "rate_limited":
            return ApiProblem(HTTPStatus.SERVICE_UNAVAILABLE, code, str(exc))
        return ApiProblem(HTTPStatus.BAD_GATEWAY, code, str(exc))
    if isinstance(exc, CredentialStoreError):
        return ApiProblem(HTTPStatus.CONFLICT, "credential_store_error", str(exc))
    if isinstance(exc, DuplicatePlanError):
        return ApiProblem(HTTPStatus.CONFLICT, "duplicate_plan", str(exc))
    if isinstance(exc, KeyError):
        return ApiProblem(HTTPStatus.NOT_FOUND, "not_found", str(exc.args[0] if exc.args else exc))
    if isinstance(exc, FileNotFoundError):
        return ApiProblem(HTTPStatus.CONFLICT, "not_configured", str(exc))
    if isinstance(exc, (ValueError, TypeError)):
        return ApiProblem(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
    if isinstance(exc, TimeoutError):
        return ApiProblem(HTTPStatus.GATEWAY_TIMEOUT, "platform_timeout", str(exc))
    name = type(exc).__name__.lower()
    if any(marker in name for marker in ("platform", "sync", "response", "http")):
        return ApiProblem(HTTPStatus.BAD_GATEWAY, "platform_error", str(exc))
    return ApiProblem(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error", "Internal server error")


def _exception_details(exc: Exception) -> Mapping[str, Any]:
    details = getattr(exc, "details", None)
    if not isinstance(details, Mapping):
        details = getattr(exc, "context", None)
    return details if isinstance(details, Mapping) else {}


def _public_error_payload(
    exc: Exception,
    *,
    job_kind: str = "",
    progress: object = None,
) -> dict[str, Any]:
    """Build the bounded public error shape shared by background jobs."""

    problem = _problem_from_exception(exc)
    details = _exception_details(exc)
    error: dict[str, Any] = {
        "code": problem.code,
        "message": _safe_failure_message(problem.message) or "后台任务失败",
    }
    if job_kind == "ai_stress_start":
        error["helpers_unchanged"] = True
        error["run_created"] = False

    # Stress preflight failures deliberately expose only bounded,
    # non-sensitive coordinates.  They are enough to reproduce the rejected
    # case without leaking source, paths, provider payloads, or model prompts.
    for key in ("artifact", "profile", "case_kind", "seed"):
        value = getattr(exc, key, None)
        if value is None:
            value = details.get(key)
        if value is None or isinstance(value, bool):
            continue
        if key == "seed":
            try:
                error[key] = int(value)
            except (TypeError, ValueError, OverflowError):
                error[key] = str(value).strip()[:128]
        else:
            error[key] = str(value).strip()[:128]
    for key in (
        "configured_timeout_seconds",
        "elapsed_seconds",
        "last_stage",
        "active_run_id",
    ):
        value = details.get(key)
        if value is not None and not isinstance(value, bool):
            error[key] = value if isinstance(value, (int, float)) else str(value)[:128]

    roles = details.get("roles")
    safe_roles: dict[str, dict[str, Any]] = {}
    if isinstance(roles, Mapping):
        for role, item in list(roles.items())[:3]:
            safe_item = _safe_role_failure(role, item)
            safe_role = _safe_failure_identifier(role)
            if safe_item is not None and safe_role is not None:
                safe_roles[safe_role] = safe_item
        if safe_roles:
            error["roles"] = safe_roles
    root_cause = _root_cause_from_details(details, safe_roles)
    if root_cause is not None:
        error["root_cause_label"], error["root_cause_message"] = root_cause
    if isinstance(progress, Mapping):
        error["stage"] = progress.get("stage")
        error["stage_label"] = progress.get("label")
    return error


class AcmHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(
        self,
        address: tuple[str, int],
        service: Any,
        root: Path,
        token: str,
        static_dir: Path,
        *,
        max_request_bytes: int = MAX_REQUEST_BYTES,
        file_picker: Callable[[str, Path], str | os.PathLike[str] | None] | None = None,
    ) -> None:
        self.service = service
        self.root = root.resolve()
        self.token = token
        self.static_dir = static_dir.resolve()
        self.max_request_bytes = max_request_bytes
        self.service_lock = threading.RLock()
        self.jobs = JobManager(self.service_lock)
        self.file_picker = file_picker or _default_local_file_picker
        self.file_dialog_lock = threading.Lock()
        self.runtime_path = Paths.for_root(self.root).state_dir / "web-runtime.json"
        self._shutdown_requested = threading.Event()
        super().__init__(address, AcmRequestHandler)

    @property
    def port(self) -> int:
        return int(self.server_address[1])

    @property
    def public_url(self) -> str:
        return f"http://{LOOPBACK_HOST}:{self.port}/?token={quote(self.token)}"

    def request_shutdown(self) -> None:
        if self._shutdown_requested.is_set():
            return
        self._shutdown_requested.set()
        threading.Thread(target=self.shutdown, name="acm-web-shutdown", daemon=True).start()

    def cleanup(self) -> None:
        self.jobs.close(wait=True)
        shutdown = getattr(self.service, "shutdown", None)
        if callable(shutdown):
            shutdown()
        _remove_runtime_if_owned(self.runtime_path, os.getpid(), self.token)


class AcmRequestHandler(BaseHTTPRequestHandler):
    server: AcmHTTPServer
    protocol_version = "HTTP/1.1"

    _post_routes = {
        "/api/setup": "setup",
        "/api/recommendations": "recommendations",
        "/api/sessions/start": "start",
        "/api/sessions/close": "close",
        "/api/review/week": "weekly_review",
        "/api/plan/check": "plan_check",
        "/api/plans/preview": "plan_preview",
        "/api/plans/import": "plan_import",
        "/api/plans/edit": "plan_edit",
        "/api/plans/state": "plan_state",
        "/api/plans/delete": "plan_delete",
        "/api/plans/restore": "plan_restore",
        "/api/plans/tags/apply": "plan_tags_apply",
        "/api/problems/skip": "problem_skip",
        "/api/problems/unskip": "problem_unskip",
        "/api/problems/context": "problem_context_save",
        "/api/ai/settings": "ai_settings",
        "/api/ai/credential": "ai_credential",
        "/api/ai/conversations": "ai_conversation_start",
        "/api/knowledge/targets": "knowledge_target_create",
        "/api/knowledge/targets/inspect": "knowledge_target_inspect",
    }
    _job_routes = {
        "/api/jobs/sync": "sync",
        "/api/jobs/verify": "verify",
        "/api/jobs/plans/tags/preview": "plan_tags_preview",
        "/api/jobs/ai/test": "ai_test",
        "/api/jobs/ai/recommendations": "ai_recommendations",
        "/api/jobs/problems/context/fetch": "problem_context_fetch",
        "/api/jobs/ai/patches/preview": "ai_patch_preview",
        "/api/jobs/ai/patches/apply": "ai_patch_apply",
        "/api/jobs/ai/patches/revert": "ai_patch_revert",
        "/api/jobs/ai/knowledge/preview": "knowledge_preview",
        "/api/jobs/ai/stress/start": "ai_stress_start",
    }

    @staticmethod
    def _match_dynamic_post_route(path: str) -> tuple[str, str, str] | None:
        """Return ``(kind, action, raw identifier)`` for dynamic POST routes."""

        route_specs = (
            ("knowledge_proposal", "/api/knowledge/proposals/", ("refresh",)),
            ("knowledge_job", "/api/jobs/knowledge/proposals/", ("apply", "revert")),
            ("stress_bundle_job", "/api/jobs/stress/bundles/", ("revert",)),
            ("stress_run", "/api/stress/runs/", ("stop", "resume", "finish")),
            ("conversation", "/api/ai/conversations/", ("clear", "messages")),
        )
        for kind, prefix, actions in route_specs:
            if not path.startswith(prefix):
                continue
            for action in actions:
                suffix = f"/{action}"
                if path.endswith(suffix):
                    return kind, action, path[len(prefix) : -len(suffix)]
        return None

    @classmethod
    def _is_post_route(cls, path: str) -> bool:
        return (
            path in cls._post_routes
            or path in cls._job_routes
            or path in {"/api/local-files/pick", "/api/server/shutdown"}
            or cls._match_dynamic_post_route(path) is not None
        )

    @staticmethod
    def _dynamic_route_identifier(raw_identifier: str, not_found: str) -> str:
        identifier = unquote(raw_identifier)
        if not identifier or "/" in identifier:
            raise ApiProblem(HTTPStatus.NOT_FOUND, "not_found", not_found)
        return identifier

    def log_message(self, format: str, *args: Any) -> None:
        return

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        path = urlsplit(self.path).path
        if path.startswith("/api/"):
            self._handle_api_get(path)
        else:
            self._serve_static(path, head_only=False)

    def do_HEAD(self) -> None:  # noqa: N802 - stdlib handler API
        path = urlsplit(self.path).path
        if path.startswith("/api/"):
            self._send_error(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", "Method not allowed")
        else:
            self._serve_static(path, head_only=True)

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        path = urlsplit(self.path).path
        if not path.startswith("/api/"):
            self._send_error(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", "Method not allowed")
            return
        if not self._authorize():
            return
        dynamic_route = self._match_dynamic_post_route(path)
        if path == "/api/bootstrap" or (
            path.startswith("/api/jobs/")
            and path not in self._job_routes
            and dynamic_route is None
        ):
            self._send_error(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", "Method not allowed")
            return
        try:
            payload = self._read_json_object()
            if path == "/api/local-files/pick":
                kind = payload.get("kind")
                if kind not in {"cpp", "markdown"}:
                    raise ApiProblem(
                        HTTPStatus.BAD_REQUEST,
                        "invalid_request",
                        "kind must be 'cpp' or 'markdown'",
                    )
                if set(payload) != {"kind"}:
                    raise ApiProblem(
                        HTTPStatus.BAD_REQUEST,
                        "invalid_request",
                        "File picker only accepts the kind field",
                    )
                if not self.server.file_dialog_lock.acquire(blocking=False):
                    raise ApiProblem(
                        HTTPStatus.CONFLICT,
                        "file_dialog_busy",
                        "Another native file dialog is already open",
                    )
                try:
                    try:
                        selected = self.server.file_picker(kind, self.server.root)
                    except FileDialogUnavailable as exc:
                        raise ApiProblem(
                            HTTPStatus.SERVICE_UNAVAILABLE,
                            "file_dialog_unavailable",
                            str(exc) or "Native file dialog is unavailable",
                        ) from exc
                finally:
                    self.server.file_dialog_lock.release()
                if selected is None or selected == "":
                    self._send_success({"cancelled": True})
                    return
                result = _validate_local_file_selection(kind, selected)
                self._send_success({"cancelled": False, **result})
                return
            if dynamic_route is not None:
                route_kind, action, raw_identifier = dynamic_route
                not_found = {
                    "knowledge_proposal": "Markdown proposal not found",
                    "knowledge_job": "Markdown proposal not found",
                    "stress_bundle_job": "Stress bundle not found",
                    "stress_run": "Stress run not found",
                    "conversation": "Conversation not found",
                }[route_kind]
                identifier = self._dynamic_route_identifier(raw_identifier, not_found)
                if route_kind == "knowledge_proposal":
                    with self.server.service_lock:
                        result = self._invoke(
                            "knowledge_refresh",
                            {"proposal_id": identifier, **payload},
                        )
                    self._send_success(result)
                    return
                if route_kind in {"knowledge_job", "stress_bundle_job"}:
                    method_name = (
                        f"knowledge_{action}"
                        if route_kind == "knowledge_job"
                        else "stress_bundle_revert"
                    )
                    argument_name = (
                        "proposal_id" if route_kind == "knowledge_job" else "bundle_id"
                    )
                    record = self.server.jobs.submit(
                        method_name,
                        lambda method_name=method_name, argument_name=argument_name,
                        identifier=identifier, payload=payload: self._invoke(
                            method_name, {argument_name: identifier, **payload}
                        ),
                    )
                    self._send_success(
                        {"job_id": record["job_id"], "job": record},
                        status=HTTPStatus.ACCEPTED,
                    )
                    return
                if route_kind == "stress_run":
                    with self.server.service_lock:
                        result = self._invoke(
                            f"stress_{action}", {"run_id": identifier}
                        )
                    self._send_success(result)
                    return
                if action == "clear":
                    if payload:
                        raise ApiProblem(
                            HTTPStatus.BAD_REQUEST,
                            "invalid_request",
                            "Conversation clear does not accept a request payload",
                        )
                    with self.server.service_lock:
                        result = self._invoke(
                            "ai_conversation_clear", {"conversation_id": identifier}
                        )
                    self._send_success(result)
                    return
                with self.server.service_lock:
                    stream = self.server.service.ai_chat_stream(identifier, **payload)
                self._send_sse(stream)
                return
            if path in {"/api/problems/skip", "/api/problems/unskip"}:
                payload.setdefault("source", "web")
            if path in self._job_routes:
                method_name = self._job_routes[path]
                if path == "/api/jobs/ai/stress/start":
                    unsupported_fields = {
                        field
                        for field in ("medium_profile", "review_contract", "no_hints")
                        if field in payload
                    }
                    if unsupported_fields:
                        field = sorted(unsupported_fields)[0]
                        raise ApiProblem(
                            HTTPStatus.BAD_REQUEST,
                            "invalid_request",
                            f"Unknown request field: {field}",
                        )

                    def run_stress(
                        progress_callback: Callable[..., None],
                        method_name: str = method_name,
                        payload: Mapping[str, Any] = payload,
                    ) -> Mapping[str, Any]:
                        return self._invoke(
                            method_name,
                            {
                                **payload,
                                "progress_callback": progress_callback,
                            },
                        )

                    record = self.server.jobs.submit(
                        method_name,
                        run_stress,
                        with_progress=True,
                    )
                else:
                    record = self.server.jobs.submit(
                        method_name,
                        lambda method_name=method_name, payload=payload: self._invoke(method_name, payload),
                    )
                self._send_success({"job_id": record["job_id"], "job": record}, status=HTTPStatus.ACCEPTED)
                return
            if path == "/api/server/shutdown":
                self._send_success({"shutting_down": True})
                self.server.request_shutdown()
                return
            method_name = self._post_routes.get(path)
            if method_name is None:
                self._send_error(HTTPStatus.NOT_FOUND, "not_found", "API endpoint not found")
                return
            with self.server.service_lock:
                result = self._invoke(method_name, payload)
            self._send_success(result)
        except Exception as exc:
            self._send_problem(_problem_from_exception(exc))

    def do_PATCH(self) -> None:  # noqa: N802 - stdlib handler API
        path = urlsplit(self.path).path
        if not self._authorize():
            return
        try:
            prefix = "/api/knowledge/targets/"
            if not path.startswith(prefix):
                raise ApiProblem(HTTPStatus.NOT_FOUND, "not_found", "API endpoint not found")
            target_id = unquote(path[len(prefix) :])
            if not target_id or "/" in target_id:
                raise ApiProblem(HTTPStatus.NOT_FOUND, "not_found", "Markdown target not found")
            payload = self._read_json_object()
            with self.server.service_lock:
                result = self._invoke(
                    "knowledge_target_update", {"target_id": target_id, **payload}
                )
            self._send_success(result)
        except Exception as exc:
            self._send_problem(_problem_from_exception(exc))

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib handler API
        path = urlsplit(self.path).path
        if not self._authorize():
            return
        try:
            prefix = "/api/knowledge/targets/"
            if not path.startswith(prefix):
                raise ApiProblem(HTTPStatus.NOT_FOUND, "not_found", "API endpoint not found")
            target_id = unquote(path[len(prefix) :])
            if not target_id or "/" in target_id:
                raise ApiProblem(HTTPStatus.NOT_FOUND, "not_found", "Markdown target not found")
            payload = self._read_json_object()
            with self.server.service_lock:
                result = self._invoke(
                    "knowledge_target_delete", {"target_id": target_id, **payload}
                )
            self._send_success(result)
        except Exception as exc:
            self._send_problem(_problem_from_exception(exc))

    def do_OPTIONS(self) -> None:  # noqa: N802 - stdlib handler API
        self._send_error(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", "Cross-origin requests are not supported")

    def _handle_api_get(self, path: str) -> None:
        if not self._authorize():
            return
        try:
            if self._match_dynamic_post_route(path) is not None:
                raise ApiProblem(
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    "method_not_allowed",
                    "Method not allowed",
                )
            if path == "/api/bootstrap":
                with self.server.service_lock:
                    result = self._invoke("bootstrap", {})
                data = dict(result)
                data["web"] = {
                    "pid": os.getpid(),
                    "port": self.server.port,
                    "version": __version__,
                }
                self._send_success(data)
                return
            if path == "/api/plans":
                with self.server.service_lock:
                    result = self._invoke("plans", {})
                self._send_success(result)
                return
            if path == "/api/plans/template":
                with self.server.service_lock:
                    result = self._invoke("plan_template", {})
                self._send_success(result)
                return
            if path == "/api/workspace/template":
                with self.server.service_lock:
                    result = self._invoke("workspace_template", {})
                self._send_success(result)
                return
            if path == "/api/problems/skipped":
                with self.server.service_lock:
                    result = self._invoke("skipped_problems", {})
                self._send_success(result)
                return
            if path == "/api/ai/status":
                with self.server.service_lock:
                    result = self._invoke("ai_status", {})
                self._send_success(result)
                return
            if path == "/api/ai/stress/status":
                with self.server.service_lock:
                    result = self._invoke("ai_stress_status", {})
                self._send_success(result)
                return
            if path == "/api/stress/runs":
                query = parse_qs(urlsplit(self.path).query, keep_blank_values=False)
                problem = query.get("problem", [None])[-1]
                with self.server.service_lock:
                    result = self._invoke("stress_runs", {"problem": problem})
                self._send_success(result)
                return
            stress_run_prefix = "/api/stress/runs/"
            if path.startswith(stress_run_prefix):
                run_id = unquote(path[len(stress_run_prefix) :])
                if not run_id or "/" in run_id:
                    raise ApiProblem(HTTPStatus.NOT_FOUND, "not_found", "Stress run not found")
                with self.server.service_lock:
                    result = self._invoke("stress_run", {"run_id": run_id})
                self._send_success(result)
                return
            stress_bundle_prefix = "/api/stress/bundles/"
            if path.startswith(stress_bundle_prefix):
                bundle_id = unquote(path[len(stress_bundle_prefix) :])
                if not bundle_id or "/" in bundle_id:
                    raise ApiProblem(HTTPStatus.NOT_FOUND, "not_found", "Stress bundle not found")
                with self.server.service_lock:
                    result = self._invoke("stress_bundle", {"bundle_id": bundle_id})
                self._send_success(result)
                return
            if path == "/api/knowledge/templates":
                with self.server.service_lock:
                    result = self._invoke("knowledge_templates", {})
                self._send_success(result)
                return
            if path == "/api/knowledge/targets":
                with self.server.service_lock:
                    result = self._invoke("knowledge_targets", {})
                self._send_success(result)
                return
            proposal_prefix = "/api/knowledge/proposals/"
            if path.startswith(proposal_prefix):
                proposal_id = unquote(path[len(proposal_prefix) :])
                if not proposal_id or "/" in proposal_id:
                    raise ApiProblem(HTTPStatus.NOT_FOUND, "not_found", "Markdown proposal not found")
                with self.server.service_lock:
                    result = self._invoke("knowledge_proposal", {"proposal_id": proposal_id})
                self._send_success(result)
                return
            attempt_prefix = "/api/attempts/"
            if path.startswith(attempt_prefix) and path.endswith("/knowledge"):
                attempt_id = unquote(path[len(attempt_prefix) : -len("/knowledge")])
                if not attempt_id.isdigit():
                    raise ApiProblem(HTTPStatus.NOT_FOUND, "not_found", "Attempt not found")
                with self.server.service_lock:
                    result = self._invoke(
                        "knowledge_attempt_proposals", {"attempt_id": int(attempt_id)}
                    )
                self._send_success(result)
                return
            context_prefix = "/api/problems/"
            if path.startswith(context_prefix) and path.endswith("/context"):
                problem = unquote(path[len(context_prefix) : -len("/context")])
                if not problem or "/" in problem:
                    raise ApiProblem(HTTPStatus.NOT_FOUND, "not_found", "Problem context not found")
                with self.server.service_lock:
                    result = self._invoke("problem_context", {"problem": problem})
                self._send_success(result)
                return
            conversation_prefix = "/api/ai/conversations/"
            if path.startswith(conversation_prefix):
                conversation_id = unquote(path[len(conversation_prefix) :])
                if not conversation_id or "/" in conversation_id:
                    raise ApiProblem(HTTPStatus.NOT_FOUND, "not_found", "Conversation not found")
                with self.server.service_lock:
                    result = self._invoke("ai_conversation", {"conversation_id": conversation_id})
                self._send_success(result)
                return
            plans_prefix = "/api/plans/"
            if path.startswith(plans_prefix):
                suffix = unquote(path[len(plans_prefix) :])
                if not suffix or suffix.startswith("/") or "//" in suffix:
                    raise ApiProblem(HTTPStatus.NOT_FOUND, "not_found", "Plan not found")
                if suffix.endswith("/revisions"):
                    plan_id = suffix[: -len("/revisions")]
                    if not plan_id or "/" in plan_id:
                        raise ApiProblem(HTTPStatus.NOT_FOUND, "not_found", "Plan not found")
                    with self.server.service_lock:
                        result = self._invoke("plan_revisions", {"plan_id": plan_id})
                else:
                    if "/" in suffix:
                        raise ApiProblem(HTTPStatus.NOT_FOUND, "not_found", "Plan not found")
                    with self.server.service_lock:
                        result = self._invoke("plan_detail", {"plan_id": suffix})
                self._send_success(result)
                return
            prefix = "/api/jobs/"
            if path.startswith(prefix) and len(path) > len(prefix):
                job_id = unquote(path[len(prefix) :])
                if "/" in job_id or not job_id:
                    raise ApiProblem(HTTPStatus.NOT_FOUND, "not_found", "Job not found")
                record = self.server.jobs.get(job_id)
                if record is None:
                    raise ApiProblem(HTTPStatus.NOT_FOUND, "not_found", "Job not found")
                self._send_success(record)
                return
            if self._is_post_route(path):
                raise ApiProblem(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", "Method not allowed")
            raise ApiProblem(HTTPStatus.NOT_FOUND, "not_found", "API endpoint not found")
        except Exception as exc:
            self._send_problem(_problem_from_exception(exc))

    def _invoke(self, method_name: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        method = getattr(self.server.service, method_name)
        result = method(**dict(payload))
        if not isinstance(result, Mapping):
            raise TypeError(f"Service method {method_name} must return a mapping")
        return result

    def _send_sse(self, stream: Any) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        iterator = iter(stream)
        try:
            for item in iterator:
                event = str(item.get("event") or "message")
                data = json.dumps(item.get("data") or {}, ensure_ascii=False, default=str)
                self.wfile.write(f"event: {event}\ndata: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            close = getattr(iterator, "close", None)
            if close is not None:
                close()

    def _authorize(self) -> bool:
        try:
            host = self.headers.get("Host", "")
            if not _valid_host(host, self.server.port):
                raise ApiProblem(HTTPStatus.FORBIDDEN, "invalid_host", "Host must be loopback")
            origin = self.headers.get("Origin")
            if origin and not _valid_origin(origin, self.server.port):
                raise ApiProblem(HTTPStatus.FORBIDDEN, "invalid_origin", "Origin must match the local server")
            supplied = self.headers.get("X-ACM-Token", "")
            # Compared as bytes, not str: http.server decodes headers as
            # latin-1, and hmac.compare_digest raises TypeError on a str
            # holding any non-ASCII character.  _authorize() runs outside the
            # request try/except, so that TypeError escaped the handler and
            # dropped the connection with no response instead of answering
            # 401.  latin-1 round-trips every byte the client actually sent,
            # so a malformed token simply fails to match.
            if not supplied or not hmac.compare_digest(
                supplied.encode("latin-1", "replace"),
                self.server.token.encode("latin-1", "replace"),
            ):
                raise ApiProblem(HTTPStatus.UNAUTHORIZED, "unauthorized", "Missing or invalid access token")
        except ApiProblem as problem:
            self._send_problem(problem)
            return False
        return True

    def _read_json_object(self) -> dict[str, Any]:
        content_type = self.headers.get("Content-Type", "")
        media_type = content_type.split(";", 1)[0].strip().lower()
        if media_type != "application/json":
            raise ApiProblem(
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                "unsupported_media_type",
                "Content-Type must be application/json",
            )
        if self.headers.get("Transfer-Encoding"):
            raise ApiProblem(HTTPStatus.BAD_REQUEST, "unsupported_transfer_encoding", "Chunked request bodies are not supported")
        raw_length = self.headers.get("Content-Length")
        if raw_length is None:
            raise ApiProblem(HTTPStatus.LENGTH_REQUIRED, "length_required", "Content-Length is required")
        try:
            length = int(raw_length)
        except ValueError as exc:
            raise ApiProblem(HTTPStatus.BAD_REQUEST, "invalid_content_length", "Invalid Content-Length") from exc
        if length < 0:
            raise ApiProblem(HTTPStatus.BAD_REQUEST, "invalid_content_length", "Invalid Content-Length")
        if length > self.server.max_request_bytes:
            raise ApiProblem(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "request_too_large", "Request body is too large")
        body = self.rfile.read(length)
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiProblem(HTTPStatus.BAD_REQUEST, "invalid_json", "Request body must contain valid UTF-8 JSON") from exc
        if not isinstance(value, dict):
            raise ApiProblem(HTTPStatus.BAD_REQUEST, "invalid_json_type", "JSON request body must be an object")
        return value

    def _serve_static(self, request_path: str, *, head_only: bool) -> None:
        if request_path in {"", "/"}:
            relative = "index.html"
        elif request_path.startswith("/static/"):
            relative = unquote(request_path[len("/static/") :])
        else:
            relative = unquote(request_path.lstrip("/"))
        try:
            candidate = (self.server.static_dir / relative).resolve()
            candidate.relative_to(self.server.static_dir)
        except (ValueError, OSError):
            self._send_error(HTTPStatus.NOT_FOUND, "not_found", "Static asset not found")
            return
        if not candidate.is_file():
            self._send_error(HTTPStatus.NOT_FOUND, "not_found", "Static asset not found")
            return
        try:
            content = candidate.read_bytes()
        except OSError:
            self._send_error(HTTPStatus.INTERNAL_SERVER_ERROR, "static_read_error", "Unable to read static asset")
            return
        media_type = STATIC_MEDIA_TYPES.get(candidate.suffix.lower())
        if media_type is None:
            media_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", media_type + ("; charset=utf-8" if media_type.startswith("text/") else ""))
        self.send_header("Content-Length", str(len(content)))
        if candidate.name == "index.html":
            self.send_header("Cache-Control", "no-store")
        elif candidate.relative_to(self.server.static_dir).parts[0] == "vendor":
            # Version-pinned third-party assets may be cached briefly.
            self.send_header("Cache-Control", "public, max-age=300")
        else:
            # Mutable dashboard assets are never cached so UI updates land
            # immediately; localhost serving makes the re-fetch cost trivial.
            self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        if not head_only:
            self.wfile.write(content)

    def _send_success(self, data: Mapping[str, Any], *, status: int = HTTPStatus.OK) -> None:
        self._send_json(status, {"ok": True, "data": _json_safe(data)})

    def _send_problem(self, problem: ApiProblem) -> None:
        self._send_json(problem.status, {"ok": False, "error": {"code": problem.code, "message": problem.message}})

    def _send_error(self, status: int, code: str, message: str) -> None:
        self._send_problem(ApiProblem(status, code, message))

    def _send_json(self, status: int, payload: Mapping[str, Any]) -> None:
        content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
        try:
            self.send_response(int(status))
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(content)
        except ConnectionError:
            pass


def _ascii_port(value: str) -> int | None:
    """Return the port only for a pure ASCII-digit string.

    ``str.isdigit()`` is True for characters ``int()`` rejects -- the Latin-1
    superscripts ``²³¹`` among them -- and ``http.server`` decodes headers as
    Latin-1, so those characters reach here from the wire.  Guarding ``int()``
    with ``isdigit()`` alone let ``Host: 127.0.0.1:²`` raise ValueError out of
    ``_valid_host``, out of ``_authorize`` (which only catches ApiProblem) and
    out of the request handler, dropping the connection with no response
    instead of answering 403.  A port is ASCII digits by definition, so this
    narrows the predicate rather than swallowing the error.
    """

    return int(value) if value.isascii() and value.isdigit() else None


def _split_host_header(value: str) -> tuple[str, int | None]:
    value = value.strip().lower()
    if value.startswith("["):
        end = value.find("]")
        if end < 0:
            return "", None
        hostname = value[1:end]
        suffix = value[end + 1 :]
        if not suffix:
            return hostname, None
        if not suffix.startswith(":"):
            return "", None
        port = _ascii_port(suffix[1:])
        return (hostname, port) if port is not None else ("", None)
    if value.count(":") > 1:
        return "", None
    if ":" in value:
        hostname, raw_port = value.rsplit(":", 1)
        port = _ascii_port(raw_port)
        return (hostname, port) if port is not None else ("", None)
    return value, None


def _valid_host(value: str, expected_port: int) -> bool:
    hostname, port = _split_host_header(value)
    return hostname in {"127.0.0.1", "localhost", "::1"} and port == expected_port


def _valid_origin(value: str, expected_port: int) -> bool:
    try:
        parsed = urlsplit(value)
        port = parsed.port
    except ValueError:
        return False
    return (
        parsed.scheme == "http"
        and parsed.hostname in {"127.0.0.1", "localhost", "::1"}
        and port == expected_port
        and not parsed.username
        and not parsed.password
        and parsed.path in {"", "/"}
        and not parsed.query
        and not parsed.fragment
    )


def _write_runtime(server: AcmHTTPServer) -> None:
    paths = Paths.for_root(server.root)
    paths.ensure()
    payload = {
        "version": 1,
        "pid": os.getpid(),
        "host": LOOPBACK_HOST,
        "port": server.port,
        "token": server.token,
        "started_at": _utc_now(),
    }
    fd, temporary = tempfile.mkstemp(prefix="web-runtime-", suffix=".json", dir=paths.state_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, server.runtime_path)
        try:
            _restrict_runtime_permissions(server.runtime_path)
        except Exception:
            server.runtime_path.unlink(missing_ok=True)
            raise
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _restrict_runtime_permissions(path: Path) -> None:
    """Make the bearer-token runtime file private to this OS user.

    ``chmod(0600)`` is sufficient on POSIX.  On Windows, chmod only toggles the
    read-only attribute, so remove inherited ACLs and grant access solely to
    the current user and LocalSystem.  Failure is fatal: starting without a
    private token file would turn a local multi-user machine into a privilege
    boundary bypass.
    """

    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    if os.name != "nt":
        return

    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    whoami = subprocess.run(
        ["whoami", "/user", "/fo", "csv", "/nh"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        timeout=10,
        creationflags=creation_flags,
        check=False,
    )
    try:
        identity = f"*{next(csv.reader([whoami.stdout.strip()]))[1]}"
    except (IndexError, StopIteration, csv.Error) as exc:
        detail = whoami.stdout.strip() or f"exit code {whoami.returncode}"
        raise PermissionError(f"Unable to determine current Windows SID: {detail}") from exc
    completed = subprocess.run(
        [
            "icacls",
            str(path),
            "/inheritance:r",
            "/grant:r",
            f"{identity}:(F)",
            "/grant:r",
            "*S-1-5-18:(F)",
        ],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        errors="replace",
        timeout=10,
        creationflags=creation_flags,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stdout.strip() or f"exit code {completed.returncode}"
        raise PermissionError(f"Unable to protect web-runtime.json ACL: {detail}")


def _remove_runtime_if_owned(path: Path, pid: int, token: str) -> None:
    try:
        current = json.loads(path.read_text(encoding="utf-8"))
        if current.get("pid") == pid and hmac.compare_digest(str(current.get("token", "")), token):
            path.unlink(missing_ok=True)
    except (OSError, ValueError, TypeError):
        return


def find_existing_instance(root: Path, *, timeout: float = 0.5) -> dict[str, Any] | None:
    runtime_path = Paths.for_root(Path(root)).state_dir / "web-runtime.json"
    try:
        runtime = json.loads(runtime_path.read_text(encoding="utf-8"))
        host = runtime.get("host")
        port = int(runtime.get("port"))
        token = str(runtime.get("token"))
        if host != LOOPBACK_HOST or not 1 <= port <= 65535 or not token:
            return None
        request = Request(
            f"http://{LOOPBACK_HOST}:{port}/api/bootstrap",
            headers={"X-ACM-Token": token},
        )
        with urlopen(request, timeout=timeout) as response:
            payload = json.load(response)
        if response.status == HTTPStatus.OK and payload.get("ok") is True:
            return runtime
    except (OSError, ValueError, TypeError, HTTPError, URLError, json.JSONDecodeError):
        return None
    return None


def create_server(
    root: Path,
    *,
    service: Any | None = None,
    host: str = LOOPBACK_HOST,
    port: int = DEFAULT_PORT,
    max_port: int = LAST_PORT,
    token: str | None = None,
    static_dir: Path | None = None,
    max_request_bytes: int = MAX_REQUEST_BYTES,
    write_runtime: bool = True,
    file_picker: Callable[[str, Path], str | os.PathLike[str] | None] | None = None,
) -> AcmHTTPServer:
    if host != LOOPBACK_HOST:
        raise ValueError("ACM web server may only bind to 127.0.0.1")
    if port < 0 or max_port < port:
        raise ValueError("Invalid port range")
    root = Path(root).resolve()
    if service is None:
        from .service import AcmService

        service = AcmService(root)
    static_dir = static_dir or Path(__file__).with_name("web_static")
    last_error: OSError | None = None
    for candidate in range(port, max_port + 1):
        try:
            server = AcmHTTPServer(
                (host, candidate),
                service,
                root,
                token or secrets.token_urlsafe(32),
                Path(static_dir),
                max_request_bytes=max_request_bytes,
                file_picker=file_picker,
            )
        except OSError as exc:
            last_error = exc
            continue
        if write_runtime:
            try:
                _write_runtime(server)
            except Exception:
                server.server_close()
                server.jobs.close(wait=False)
                raise
        return server
    if last_error is not None:
        raise OSError(f"No available loopback port in {port}-{max_port}") from last_error
    raise OSError("No available loopback port")


def serve(
    root: Path,
    *,
    service: Any | None = None,
    host: str = LOOPBACK_HOST,
    open_browser: bool = True,
    port: int = DEFAULT_PORT,
    max_port: int | None = None,
) -> dict[str, Any]:
    root = Path(root).resolve()
    if host != LOOPBACK_HOST:
        raise ValueError("ACM web server may only bind to 127.0.0.1")
    existing = find_existing_instance(root)
    if existing is not None:
        url = f"http://{LOOPBACK_HOST}:{existing['port']}/?token={quote(str(existing['token']))}"
        if open_browser:
            import webbrowser

            webbrowser.open(url)
        return {**existing, "url": url, "existing": True}

    if max_port is None:
        max_port = min(int(port) + (LAST_PORT - DEFAULT_PORT), 65535)
    server = create_server(
        root, service=service, host=host, port=port, max_port=max_port
    )
    runtime = json.loads(server.runtime_path.read_text(encoding="utf-8"))
    result = {**runtime, "url": server.public_url, "existing": False}
    if open_browser:
        import webbrowser

        webbrowser.open(server.public_url)
    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        server.cleanup()
    return result


__all__ = [
    "AcmHTTPServer",
    "ApiProblem",
    "FileDialogUnavailable",
    "JobManager",
    "create_server",
    "find_existing_instance",
    "serve",
]
