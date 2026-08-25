"""Loopback-only web server for the ACM practice dashboard.

The module intentionally uses only the Python standard library.  It exposes a
small JSON API over :class:`AcmService` and serves the static dashboard assets
from ``tools/acm_agent/web_static``.
"""

from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import Future, ThreadPoolExecutor
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
import secrets
import stat
import subprocess
import tempfile
import threading
from typing import Any, Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit
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
    TagOverrideRevisionConflict,
)

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


class JobManager:
    """A bounded, single-worker in-memory background job registry."""

    def __init__(self, service_lock: threading.RLock, *, capacity: int = MAX_JOBS):
        self._service_lock = service_lock
        self._capacity = capacity
        self._jobs: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._futures: dict[str, Future[Any]] = {}
        self._lock = threading.Lock()
        self._accepting = True
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="acm-web-job")

    def submit(
        self,
        kind: str,
        function: Callable[[], Mapping[str, Any]],
        *,
        job_id: str | None = None,
        use_service_lock: bool = True,
        metadata: Mapping[str, Any] | None = None,
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
            "metadata": _json_safe(dict(metadata or {})),
        }
        with self._lock:
            if not self._accepting:
                raise ApiProblem(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "job_manager_stopped",
                    "The background job manager is shutting down",
                )
            self._trim_locked(self._capacity - 1)
            if len(self._jobs) >= self._capacity:
                raise ApiProblem(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "job_capacity_exhausted",
                    "All background job slots are currently active",
                )
            self._jobs[job_id] = record
            try:
                self._futures[job_id] = self._executor.submit(
                    self._run, job_id, function, use_service_lock
                )
            except RuntimeError:
                self._jobs.pop(job_id, None)
                raise ApiProblem(
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    "job_manager_stopped",
                    "The background job manager is shutting down",
                ) from None
        return self.get(job_id) or record.copy()

    def _run(
        self,
        job_id: str,
        function: Callable[[], Mapping[str, Any]],
        use_service_lock: bool,
    ) -> None:
        with self._lock:
            record = self._jobs.get(job_id)
            if record is None or record["status"] != "queued":
                return
            record.update(status="running", started_at=_utc_now())
        try:
            if use_service_lock:
                with self._service_lock:
                    result = function()
            else:
                result = function()
        except Exception as exc:  # Job failures are data, not server failures.
            self._update(
                job_id,
                status="failed",
                finished_at=_utc_now(),
                error=_public_error_payload(exc),
            )
        else:
            self._update(
                job_id,
                status="succeeded",
                finished_at=_utc_now(),
                result=_json_safe(result),
            )

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
            self._futures.pop(removable, None)

    def get(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._jobs.get(job_id)
            return _json_safe(record) if record is not None else None

    def active(
        self,
        kind: str,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """Return the newest queued/running job of one kind, if present."""

        with self._lock:
            for record in reversed(self._jobs.values()):
                if (
                    record["kind"] == kind
                    and record["status"] in {"queued", "running"}
                    and (metadata is None or record.get("metadata") == _json_safe(dict(metadata)))
                ):
                    return _json_safe(record)
        return None

    def active_any(self, kinds: set[str]) -> dict[str, Any] | None:
        with self._lock:
            for record in reversed(self._jobs.values()):
                if record["kind"] in kinds and record["status"] in {"queued", "running"}:
                    return _json_safe(record)
        return None

    def report_progress(self, job_id: str, progress: Mapping[str, Any]) -> None:
        """Publish JSON-safe progress for a running background job."""

        with self._lock:
            record = self._jobs.get(job_id)
            if record is not None and record["status"] == "running":
                record["progress"] = _json_safe(dict(progress))

    def cancel(self, job_id: str) -> dict[str, Any]:
        """Cancel a queued job without interrupting work that already started."""

        with self._lock:
            record = self._jobs.get(job_id)
            if record is None:
                raise ApiProblem(HTTPStatus.NOT_FOUND, "not_found", "Job not found")
            if record["status"] == "canceled":
                return _json_safe(record)
            future = self._futures.get(job_id)
            if record["status"] != "queued" or future is None or not future.cancel():
                raise ApiProblem(
                    HTTPStatus.CONFLICT,
                    "job_not_cancelable",
                    "Only queued background jobs can be canceled",
                )
            record.update(
                status="canceled",
                finished_at=_utc_now(),
                error={
                    "code": "job_canceled",
                    "message": "Background job was canceled before it started",
                },
            )
            return _json_safe(record)

    def close(self, *, wait: bool = True) -> None:
        with self._lock:
            self._accepting = False
            stamp = _utc_now()
            for job_id, record in self._jobs.items():
                future = self._futures.get(job_id)
                if (
                    record["status"] == "queued"
                    and future is not None
                    and future.cancel()
                ):
                    record.update(
                        status="canceled",
                        finished_at=stamp,
                        error={
                            "code": "job_canceled",
                            "message": "Background job was canceled during server shutdown",
                        },
                    )
        self._executor.shutdown(wait=wait, cancel_futures=True)


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


def _public_error_payload(exc: Exception) -> dict[str, Any]:
    problem = _problem_from_exception(exc)
    return {
        "code": problem.code,
        "message": problem.message,
    }


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
        # Stop accepting paid/background work immediately and discard anything
        # that has not started before the HTTP server begins shutting down.
        self.jobs.close(wait=False)
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
        "/api/jobs/ai/plans/preview": "ai_plan_preview",
        "/api/jobs/problems/context/fetch": "problem_context_fetch",
        "/api/jobs/ai/patches/preview": "ai_patch_preview",
        "/api/jobs/ai/patches/apply": "ai_patch_apply",
        "/api/jobs/ai/patches/revert": "ai_patch_revert",
        "/api/jobs/ai/knowledge/preview": "knowledge_preview",
    }

    @staticmethod
    def _is_retired_stress_route(path: str) -> bool:
        return path == "/api/ai/stress/status" or path.startswith(
            (
                "/api/jobs/ai/stress/",
                "/api/stress/",
                "/api/jobs/stress/",
            )
        )

    @staticmethod
    def _match_dynamic_post_route(path: str) -> tuple[str, str, str] | None:
        """Return ``(kind, action, raw identifier)`` for dynamic POST routes."""

        route_specs = (
            ("knowledge_proposal", "/api/knowledge/proposals/", ("refresh",)),
            ("knowledge_job", "/api/jobs/knowledge/proposals/", ("apply", "revert")),
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
        if self._is_retired_stress_route(path):
            self._send_error(HTTPStatus.NOT_FOUND, "not_found", "API endpoint not found")
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
            if path == "/api/setup":
                unsupported_fields = set(payload) - {
                    "codeforces",
                    "luogu",
                    "target_rating",
                    "skip_validate",
                }
                if unsupported_fields:
                    field = sorted(unsupported_fields)[0]
                    raise ApiProblem(
                        HTTPStatus.BAD_REQUEST,
                        "invalid_request",
                        f"Unknown request field: {field}",
                    )
                # Web setup must enter the dashboard as soon as account
                # validation and configuration persistence finish.  The
                # catalog crawl uses independent SQLite connections and only
                # takes write locks for its atomic commits, so it deliberately
                # does not hold the process-wide service lock for the whole
                # network-bound operation.
                with self.server.service_lock:
                    result = dict(
                        self._invoke("setup", {**payload, "defer_sync": True})
                    )
                result["initial_sync_job"] = None
                if result.get("validated"):
                    try:
                        sync_metadata = {"accounts": dict(result.get("accounts") or {})}
                        record = self.server.jobs.active(
                            "initial_sync",
                            metadata=sync_metadata,
                        )
                        if record is None:
                            job_id = secrets.token_urlsafe(12)
                            record = self.server.jobs.submit(
                                "initial_sync",
                                lambda job_id=job_id: self._invoke(
                                    "sync",
                                    {
                                        "platform": "all",
                                        "full_catalog": True,
                                        "_progress_callback": lambda progress: self.server.jobs.report_progress(
                                            job_id, progress
                                        ),
                                    },
                                ),
                                job_id=job_id,
                                use_service_lock=False,
                                metadata=sync_metadata,
                            )
                    except ApiProblem as exc:
                        result["initial_sync_error"] = {
                            "code": exc.code,
                            "message": exc.message,
                        }
                    else:
                        result["initial_sync_job"] = {
                            "job_id": record["job_id"],
                            "status": record["status"],
                        }
                self._send_success(result)
                return
            if dynamic_route is not None:
                route_kind, action, raw_identifier = dynamic_route
                not_found = {
                    "knowledge_proposal": "Markdown proposal not found",
                    "knowledge_job": "Markdown proposal not found",
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
                if route_kind == "knowledge_job":
                    method_name = f"knowledge_{action}"
                    record = self.server.jobs.submit(
                        method_name,
                        lambda method_name=method_name,
                        identifier=identifier, payload=payload: self._invoke(
                            method_name, {"proposal_id": identifier, **payload}
                        ),
                    )
                    self._send_success(
                        {"job_id": record["job_id"], "job": record},
                        status=HTTPStatus.ACCEPTED,
                    )
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
                if method_name == "sync":
                    unsupported_fields = set(payload) - {"platform", "force", "full_catalog"}
                    if unsupported_fields:
                        field = sorted(unsupported_fields)[0]
                        raise ApiProblem(
                            HTTPStatus.BAD_REQUEST,
                            "invalid_request",
                            f"Unknown request field: {field}",
                        )
                if method_name in {"ai_plan_preview", "sync"}:
                    job_id = secrets.token_urlsafe(12)
                    record = self.server.jobs.submit(
                        method_name,
                        lambda method_name=method_name, payload=payload, job_id=job_id: self._invoke(
                            method_name,
                            {
                                **payload,
                                "_progress_callback": lambda progress: self.server.jobs.report_progress(
                                    job_id, progress
                                ),
                            },
                        ),
                        job_id=job_id,
                        metadata=(
                            {"platform": str(payload.get("platform") or "all")}
                            if method_name == "sync"
                            else None
                        ),
                    )
                    self._send_success(
                        {"job_id": record["job_id"], "job": record},
                        status=HTTPStatus.ACCEPTED,
                    )
                    return
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
            job_prefix = "/api/jobs/"
            if path.startswith(job_prefix):
                job_id = unquote(path[len(job_prefix) :])
                if not job_id or "/" in job_id:
                    raise ApiProblem(HTTPStatus.NOT_FOUND, "not_found", "Job not found")
                self._send_success(self.server.jobs.cancel(job_id))
                return
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
            if self._is_retired_stress_route(path):
                raise ApiProblem(
                    HTTPStatus.NOT_FOUND,
                    "not_found",
                    "API endpoint not found",
                )
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
                data["active_sync_job"] = self.server.jobs.active_any(
                    {"initial_sync", "sync"}
                )
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
        iterator = iter(stream)
        headers_committed = False
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Connection", "close")
            self.end_headers()
            headers_committed = True
            self.close_connection = True
            for item in iterator:
                event = str(item.get("event") or "message")
                data = json.dumps(item.get("data") or {}, ensure_ascii=False, default=str)
                self.wfile.write(f"event: {event}\ndata: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as exc:
            if not headers_committed:
                raise
            # Headers are already committed as 200 at this point.  Preserve the
            # SSE protocol and expose only the same sanitized error shape used
            # by JSON endpoints, so clients always receive a terminal event.
            try:
                data = json.dumps(
                    _public_error_payload(exc),
                    ensure_ascii=False,
                    separators=(",", ":"),
                    default=str,
                )
                self.wfile.write(f"event: error\ndata: {data}\n\n".encode("utf-8"))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
        finally:
            close = getattr(iterator, "close", None)
            if close is not None:
                try:
                    close()
                except Exception:
                    # A terminal event or disconnected socket cannot carry a
                    # second failure. Lifecycle cleanup has already been tried.
                    pass

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
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data: blob:; base-uri 'none'; frame-ancestors 'none'")
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
