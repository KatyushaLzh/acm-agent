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
from .config import Paths


LOOPBACK_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
LAST_PORT = 8775
MAX_REQUEST_BYTES = 1024 * 1024
MAX_JOBS = 100


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


class JobManager:
    """A bounded, single-worker in-memory background job registry."""

    def __init__(self, service_lock: threading.RLock, *, capacity: int = MAX_JOBS):
        self._service_lock = service_lock
        self._capacity = capacity
        self._jobs: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="acm-web-job")

    def submit(self, kind: str, function: Callable[[], Mapping[str, Any]]) -> dict[str, Any]:
        job_id = secrets.token_urlsafe(12)
        record: dict[str, Any] = {
            "job_id": job_id,
            "kind": kind,
            "status": "queued",
            "created_at": _utc_now(),
            "started_at": None,
            "finished_at": None,
            "result": None,
            "error": None,
        }
        with self._lock:
            self._jobs[job_id] = record
            self._trim_locked()
        self._executor.submit(self._run, job_id, function)
        return self.get(job_id) or record.copy()

    def _run(self, job_id: str, function: Callable[[], Mapping[str, Any]]) -> None:
        self._update(job_id, status="running", started_at=_utc_now())
        try:
            with self._service_lock:
                result = function()
        except Exception as exc:  # Job failures are data, not server failures.
            problem = _problem_from_exception(exc)
            self._update(
                job_id,
                status="failed",
                finished_at=_utc_now(),
                error={"code": problem.code, "message": problem.message},
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

    def _trim_locked(self) -> None:
        while len(self._jobs) > self._capacity:
            removable = next(
                (key for key, value in self._jobs.items() if value["status"] not in {"queued", "running"}),
                None,
            )
            if removable is None:
                removable = next(iter(self._jobs))
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
    if exc.__class__.__name__ in {
        "RevisionConflict",
        "PlanRevisionConflict",
        "TagOverrideRevisionConflict",
    }:
        return ApiProblem(HTTPStatus.CONFLICT, "revision_conflict", str(exc))
    if exc.__class__.__name__ == "DuplicatePlanError":
        return ApiProblem(HTTPStatus.CONFLICT, "duplicate_plan", str(exc))
    if isinstance(exc, KeyError):
        return ApiProblem(HTTPStatus.NOT_FOUND, "not_found", str(exc.args[0] if exc.args else exc))
    if isinstance(exc, FileNotFoundError):
        return ApiProblem(HTTPStatus.CONFLICT, "not_configured", str(exc))
    if isinstance(exc, (ValueError, TypeError, KeyError)):
        return ApiProblem(HTTPStatus.BAD_REQUEST, "invalid_request", str(exc))
    if isinstance(exc, TimeoutError):
        return ApiProblem(HTTPStatus.GATEWAY_TIMEOUT, "platform_timeout", str(exc))
    name = type(exc).__name__.lower()
    if any(marker in name for marker in ("platform", "sync", "response", "http")):
        return ApiProblem(HTTPStatus.BAD_GATEWAY, "platform_error", str(exc))
    return ApiProblem(HTTPStatus.INTERNAL_SERVER_ERROR, "internal_error", "Internal server error")


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
    ) -> None:
        self.service = service
        self.root = root.resolve()
        self.token = token
        self.static_dir = static_dir.resolve()
        self.max_request_bytes = max_request_bytes
        self.service_lock = threading.RLock()
        self.jobs = JobManager(self.service_lock)
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
    }
    _job_routes = {
        "/api/jobs/sync": "sync",
        "/api/jobs/verify": "verify",
        "/api/jobs/plans/tags/preview": "plan_tags_preview",
    }

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
        if path == "/api/bootstrap" or path.startswith("/api/jobs/") and path not in self._job_routes:
            self._send_error(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", "Method not allowed")
            return
        try:
            payload = self._read_json_object()
            if path in {"/api/problems/skip", "/api/problems/unskip"}:
                payload.setdefault("source", "web")
            if path in self._job_routes:
                method_name = self._job_routes[path]
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

    def do_OPTIONS(self) -> None:  # noqa: N802 - stdlib handler API
        self._send_error(HTTPStatus.METHOD_NOT_ALLOWED, "method_not_allowed", "Cross-origin requests are not supported")

    def _handle_api_get(self, path: str) -> None:
        if not self._authorize():
            return
        try:
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
            if path == "/api/problems/skipped":
                with self.server.service_lock:
                    result = self._invoke("skipped_problems", {})
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
            if path in self._post_routes or path in self._job_routes or path == "/api/server/shutdown":
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

    def _authorize(self) -> bool:
        try:
            host = self.headers.get("Host", "")
            if not _valid_host(host, self.server.port):
                raise ApiProblem(HTTPStatus.FORBIDDEN, "invalid_host", "Host must be loopback")
            origin = self.headers.get("Origin")
            if origin and not _valid_origin(origin, self.server.port):
                raise ApiProblem(HTTPStatus.FORBIDDEN, "invalid_origin", "Origin must match the local server")
            supplied = self.headers.get("X-ACM-Token", "")
            if not supplied or not hmac.compare_digest(supplied, self.server.token):
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
        media_type = mimetypes.guess_type(candidate.name)[0] or "application/octet-stream"
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", media_type + ("; charset=utf-8" if media_type.startswith("text/") else ""))
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store" if candidate.name == "index.html" else "public, max-age=300")
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
        if not suffix.startswith(":") or not suffix[1:].isdigit():
            return "", None
        return hostname, int(suffix[1:])
    if value.count(":") > 1:
        return "", None
    if ":" in value:
        hostname, port = value.rsplit(":", 1)
        return (hostname, int(port)) if port.isdigit() else ("", None)
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
    "JobManager",
    "create_server",
    "find_existing_instance",
    "serve",
]
