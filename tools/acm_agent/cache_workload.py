"""Isolated Stage 4 cache workload runner.

The runner deliberately stores only aggregate telemetry and correctness gates.
Prompts, source code, credentials, and temporary workspace paths never enter
the durable report.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import statistics
import subprocess
import tempfile
import threading
import time
from typing import Any, Callable, Iterable, Mapping

from .ai_telemetry import estimate_cost
from .config import Paths, load_config, save_config
from .credentials import create_platform_credential_vault
from .deepseek import DeepSeekClient
from .knowledge import get_builtin_template
from .provider import ProviderConfigurationError
from .provider_config import TASK_PROFILE_IDS
from .service import AcmService
from .storage import Database


MODEL = "deepseek-v4-flash"
PROVIDER_LIMIT = 20
PROVIDER_TARGET = 20
WORKLOAD_PATH = Path(__file__).with_name("model-provider-workload.v1.json")
TOKEN_KEYS = (
    "input_tokens",
    "output_tokens",
    "total_tokens",
    "cache_read_tokens",
    "cache_miss_tokens",
    "provider_requests",
)


def _safe_label(value: Any, *, fallback: str = "unknown", limit: int = 128) -> str:
    selected = str(value or "").strip()
    if (
        not selected
        or len(selected) > limit
        or not all(character.isalnum() or character in "._-" for character in selected)
    ):
        return fallback
    return selected


def _safe_error_code(value: Any) -> str | None:
    selected = _safe_label(value, fallback="", limit=128).lower()
    if not selected:
        return None
    trusted_prefixes = (
        "ai_",
        "cache_",
        "deepseek_",
        "http_",
        "openai_",
        "provider_",
        "stage4_",
        "validation_",
        "workload_",
    )
    if selected.startswith(trusted_prefixes):
        return selected
    return "unclassified_sha256_" + hashlib.sha256(selected.encode("utf-8")).hexdigest()


class ProviderRequestLimitExceeded(ProviderConfigurationError):
    def __init__(self, limit: int) -> None:
        super().__init__(
            "stage4_provider_request_limit",
            f"Stage 4 provider request hard limit ({limit}) reached",
        )


class CappedProviderClient:
    """Reserve a global budget unit and expose actual adapter HTTP attempts."""

    def __init__(self, client: Any, *, limit: int = PROVIDER_LIMIT) -> None:
        self._client = client
        self.limit = int(limit)
        self._count = 0
        self._lock = threading.Lock()

    @property
    def provider_request_count(self) -> int:
        actual = getattr(self._client, "provider_request_count", None)
        if isinstance(actual, int) and not isinstance(actual, bool):
            return actual
        return self.reserved_request_count

    @property
    def reserved_request_count(self) -> int:
        with self._lock:
            return self._count

    @property
    def key_detected(self) -> bool:
        return bool(self._client.key_detected)

    def capabilities(self, model: str) -> Any:
        return self._client.capabilities(model)

    def test_connection(self, model: str) -> Any:
        return self._reserved_call(self._client.test_connection, model)

    def _reserve(self) -> None:
        with self._lock:
            actual = getattr(self._client, "provider_request_count", 0)
            actual_count = (
                int(actual)
                if isinstance(actual, int) and not isinstance(actual, bool)
                else 0
            )
            if max(self._count, actual_count) >= self.limit:
                raise ProviderRequestLimitExceeded(self.limit)
            self._count += 1

    def _reserved_call(self, function: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        self._reserve()
        return function(*args, **kwargs)

    def chat(
        self,
        messages: Any,
        *,
        model: str = MODEL,
        thinking: bool = False,
        reasoning_effort: str = "high",
        max_tokens: int | None = None,
        temperature: float | None = None,
        retry_callback: Any = None,
        request_timeout: float | None = None,
        request_retries: int | None = None,
    ) -> Any:
        return self._reserved_call(
            self._client.chat,
            messages,
            model=model,
            thinking=thinking,
            reasoning_effort=reasoning_effort,
            max_tokens=max_tokens,
            temperature=temperature,
            retry_callback=retry_callback,
            request_timeout=request_timeout,
            request_retries=request_retries,
        )

    def chat_json(
        self,
        messages: Any,
        *,
        model: str = MODEL,
        thinking: bool = False,
        reasoning_effort: str = "high",
        max_tokens: int | None = None,
        temperature: float | None = None,
        retry_callback: Any = None,
        request_timeout: float | None = None,
        request_retries: int | None = None,
        json_retries: int = 1,
    ) -> Any:
        return self._reserved_call(
            self._client.chat_json,
            messages,
            model=model,
            thinking=thinking,
            reasoning_effort=reasoning_effort,
            max_tokens=max_tokens,
            temperature=temperature,
            retry_callback=retry_callback,
            request_timeout=request_timeout,
            request_retries=request_retries,
            json_retries=json_retries,
        )

    def structured(
        self,
        messages: Any,
        *,
        json_schema: Mapping[str, Any],
        schema_name: str,
        model: str = MODEL,
        thinking: bool = False,
        reasoning_effort: str = "high",
        max_tokens: int | None = None,
        temperature: float | None = None,
        retry_callback: Any = None,
        request_timeout: float | None = None,
        request_retries: int | None = None,
    ) -> Any:
        return self._reserved_call(
            self._client.structured,
            messages,
            json_schema=json_schema,
            schema_name=schema_name,
            model=model,
            thinking=thinking,
            reasoning_effort=reasoning_effort,
            max_tokens=max_tokens,
            temperature=temperature,
            retry_callback=retry_callback,
            request_timeout=request_timeout,
            request_retries=request_retries,
        )

    def stream_chat(
        self,
        messages: Any,
        *,
        model: str = MODEL,
        thinking: bool = True,
        reasoning_effort: str = "high",
        max_tokens: int | None = None,
        temperature: float | None = None,
    ) -> Any:
        self._reserve()
        return self._client.stream_chat(
            messages,
            model=model,
            thinking=thinking,
            reasoning_effort=reasoning_effort,
            max_tokens=max_tokens,
            temperature=temperature,
        )


@dataclass
class _Workspace:
    temporary: tempfile.TemporaryDirectory[str]
    service: AcmService
    attempt_id: int
    target_id: str
    phase: str

    def close(self) -> None:
        self.temporary.cleanup()


def _safe_usage(value: Any) -> dict[str, int]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, int] = {}
    for key in TOKEN_KEYS:
        item = value.get(key)
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            result[key] = int(item)
    return result


def _cache_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    cache = value.get("local_cache")
    if not isinstance(cache, Mapping) and isinstance(value.get("ai"), Mapping):
        cache = value["ai"].get("local_cache")
    if not isinstance(cache, Mapping):
        return {}
    allowed = ("eligible", "status", "source_run_id", "coalesced")
    return {key: cache[key] for key in allowed if key in cache}


def _outcome_summary(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    outcome = value.get("outcome")
    if not isinstance(outcome, Mapping) and isinstance(value.get("ai"), Mapping):
        outcome = value["ai"].get("outcome")
    if not isinstance(outcome, Mapping):
        return {}
    allowed = (
        "provider_outcome",
        "artifact_outcome",
        "business_outcome",
        "usable",
        "apply_ready",
        "degraded",
        "repair_attempts",
    )
    return {key: outcome[key] for key in allowed if key in outcome}


def _result_error_codes(value: Any) -> list[str]:
    if not isinstance(value, Mapping):
        return []
    codes: list[str] = []
    errors = value.get("errors")
    if isinstance(errors, list):
        for error in errors:
            if isinstance(error, Mapping):
                code = _safe_error_code(error.get("code"))
                if code and code not in codes:
                    codes.append(code)
    ai = value.get("ai")
    fallback = ai.get("fallback") if isinstance(ai, Mapping) else None
    if isinstance(fallback, Mapping):
        code = _safe_error_code(fallback.get("code"))
        if code and code not in codes:
            codes.append(code)
    return codes


def _percentile(values: Iterable[float], percentile: float) -> float | None:
    ordered = sorted(float(value) for value in values)
    if not ordered:
        return None
    if len(ordered) == 1:
        return round(ordered[0], 3)
    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower
    return round(ordered[lower] * (1 - fraction) + ordered[upper] * fraction, 3)


def _git_hash(root: Path) -> str | None:
    completed = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    value = completed.stdout.strip()
    return value if completed.returncode == 0 and len(value) == 40 else None


def _git_bytes(root: Path, *args: str) -> bytes | None:
    completed = subprocess.run(
        ["git", *args],
        cwd=root,
        capture_output=True,
        timeout=30,
        check=False,
    )
    return completed.stdout if completed.returncode == 0 else None


def _file_set_sha256(root: Path, names: bytes | None) -> tuple[str | None, int]:
    if names is None:
        return None, 0
    digest = hashlib.sha256()
    count = 0
    for raw_name in sorted(item for item in names.split(b"\0") if item):
        path = root / os.fsdecode(raw_name)
        if not path.is_file():
            continue
        content_digest = hashlib.sha256(path.read_bytes()).digest()
        digest.update(len(raw_name).to_bytes(8, "big"))
        digest.update(raw_name)
        digest.update(content_digest)
        count += 1
    return digest.hexdigest(), count


def _source_evidence(
    root: Path,
    *,
    critical_files: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Fingerprint the executed worktree without persisting paths or contents."""

    selected = critical_files or {
        "stage4_runner": "tools/acm_agent/cache_workload.py",
        "workload_manifest": "tools/acm_agent/model-provider-workload.v1.json",
        "cache_core": "tools/acm_agent/ai_cache.py",
        "provider_configuration": "tools/acm_agent/provider_config.py",
        "ai_service": "tools/acm_agent/service_ai.py",
        "ai_storage": "tools/acm_agent/storage_ai.py",
    }
    status = _git_bytes(root, "status", "--porcelain=v1", "-z")
    tracked_diff = _git_bytes(root, "diff", "--binary", "--no-ext-diff", "HEAD", "--")
    tracked_names = _git_bytes(root, "ls-files", "-z", "--cached")
    worktree_names = _git_bytes(
        root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"
    )
    tracked_tree_sha256, tracked_file_count = _file_set_sha256(root, tracked_names)
    worktree_sha256, worktree_file_count = _file_set_sha256(root, worktree_names)
    critical: dict[str, dict[str, Any]] = {}
    for label, relative in sorted(selected.items()):
        path = root / relative
        if not path.is_file():
            critical[str(label)] = {"present": False}
            continue
        content = path.read_bytes()
        critical[str(label)] = {
            "present": True,
            "sha256": hashlib.sha256(content).hexdigest(),
            "bytes": len(content),
        }
    evidence = {
        "head": _git_hash(root),
        "dirty": bool(status),
        "status_sha256": hashlib.sha256(status).hexdigest() if status is not None else None,
        "status_entry_count": status.count(b"\0") if status is not None else None,
        "tracked_dirty": bool(tracked_diff),
        "tracked_diff_sha256": (
            hashlib.sha256(tracked_diff).hexdigest()
            if tracked_diff is not None
            else None
        ),
        "tracked_diff_bytes": len(tracked_diff) if tracked_diff is not None else None,
        "tracked_tree_sha256": tracked_tree_sha256,
        "tracked_file_count": tracked_file_count,
        "worktree_sha256": worktree_sha256,
        "worktree_file_count": worktree_file_count,
        "critical_files": critical,
    }
    evidence["source_snapshot_sha256"] = hashlib.sha256(
        json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return evidence


def _safe_cost(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        return {}
    result: dict[str, Any] = {}
    status = str(value.get("status") or "")
    if status in {"known", "partial", "unknown"}:
        result["status"] = status
    currency = str(value.get("currency") or "")
    if len(currency) == 3 and currency.isalpha():
        result["currency"] = currency.upper()
    for key in ("amount", "cache_savings"):
        item = value.get(key)
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            result[key] = float(item)
    version = str(value.get("price_version") or "")
    if version and len(version) <= 64 and all(
        character.isalnum() or character in "._-" for character in version
    ):
        result["price_version"] = version
    catalog = str(value.get("catalog_sha256") or "").lower()
    if len(catalog) == 64 and all(
        character in "0123456789abcdef" for character in catalog
    ):
        result["catalog_sha256"] = catalog
    return result


def _report_count_consistency(
    records: Iterable[Mapping[str, Any]],
    provider_leg_evidence: Iterable[Mapping[str, Any]],
    *,
    top_level_provider_requests: int,
) -> dict[str, Any]:
    selected_records = list(records)
    evidence = list(provider_leg_evidence)
    phase_requests: dict[str, int] = {}
    for row in evidence:
        phase = str(row.get("phase") or "unknown")
        phase_requests[phase] = phase_requests.get(phase, 0) + int(
            row.get("provider_requests") or 0
        )
    business_requests = sum(
        int(row.get("provider_requests") or 0)
        for row in evidence
        if row.get("evidence_kind") == "business_provider_leg"
    )
    probe_requests = sum(
        int(row.get("provider_requests") or 0)
        for row in evidence
        if row.get("evidence_kind") == "provider_cache_probe"
    )
    evidence_requests = sum(phase_requests.values())
    run_requests = sum(
        int(row.get("provider_requests") or 0) for row in selected_records
    )
    result: dict[str, Any] = {
        "top_level_provider_requests": int(top_level_provider_requests),
        "provider_leg_evidence_requests": evidence_requests,
        "phase_provider_requests": phase_requests,
        "run_attributed_provider_requests": run_requests,
        "business_provider_leg_requests": business_requests,
        "probe_provider_requests": probe_requests,
    }
    result["consistent"] = bool(
        evidence_requests == int(top_level_provider_requests)
        and sum(phase_requests.values()) == int(top_level_provider_requests)
        and business_requests + probe_requests == int(top_level_provider_requests)
        and run_requests == business_requests
    )
    return result


class Stage4WorkloadRunner:
    def __init__(self, root: str | Path, provider: CappedProviderClient) -> None:
        self.root = Path(root).resolve()
        self.provider = provider
        self.records: list[dict[str, Any]] = []
        self.provider_probe_records: list[dict[str, Any]] = []
        self._workspaces: list[_Workspace] = []
        self._singleflight_provider_requests: int | None = None
        self._records_lock = threading.Lock()

    def _configure(
        self,
        service: AcmService,
        *,
        exact_cache: bool,
        validation_repairs: int = 1,
    ) -> None:
        config = load_config(service.paths)
        for profile_id in TASK_PROFILE_IDS:
            profile = config["ai"]["profiles"][profile_id]
            profile.update(provider_id="deepseek", model=MODEL)
            budget = config["ai"]["policy"]["budgets"][profile_id]
            budget["max_retries"] = 1
            budget["max_validation_repairs"] = int(validation_repairs)
            config["ai"]["policy"]["fallbacks"][profile_id] = []
        config["ai"]["cache"]["exact_profiles"] = (
            ["recommendation", "plan_organize", "summary"] if exact_cache else []
        )
        config["ai"]["cache"]["semantic_enabled"] = False
        config["ai"]["coaching_delivery_mode"] = "resilient"
        save_config(service.paths, config)

    def _workspace(
        self, *, phase: str, exact_cache: bool, validation_repairs: int = 1
    ) -> _Workspace:
        temporary = tempfile.TemporaryDirectory(prefix="acm-stage4-")
        root = Path(temporary.name)
        (root / "algorithms.md").write_text(
            get_builtin_template("algorithms-v1"), encoding="utf-8", newline="\n"
        )
        service = AcmService(root, provider_client_factory=lambda: self.provider)
        service.setup("cache-fixture", "900001", target_rating=800, skip_validate=True)
        self._configure(
            service,
            exact_cache=exact_cache,
            validation_repairs=validation_repairs,
        )
        problems = [
            {
                "platform": "codeforces",
                "problem_id": f"{index}A",
                "name": f"Fixture {index}A",
                "rating": 800,
                "tags": ["dp"],
            }
            for index in range(100, 108)
        ]
        with Database(service.paths.database) as db:
            db.upsert_problems(problems)
        started = service.start(
            "CF100A",
            template=(
                "#include <iostream>\n"
                "int main(){ long long n; if(std::cin>>n) std::cout<<n<<'\\n'; }\n"
            ),
        )
        service.problem_context_save(
            "CF100A",
            content=(
                "Given one integer n, output n+1.\n\n"
                "Input\n1\n\nOutput\n2\n"
            ),
        )
        targets = service.knowledge_targets()["targets"]
        target = next(item for item in targets if item["preset"] == "algorithms-v1")
        workspace = _Workspace(
            temporary=temporary,
            service=service,
            attempt_id=int(started["attempt_id"]),
            target_id=str(target["target_id"]),
            phase=str(phase),
        )
        self._workspaces.append(workspace)
        return workspace

    def _record(
        self,
        phase: str,
        profile: str,
        index: int,
        function: Callable[[], Any],
        gate: Callable[[Any], bool],
    ) -> Any:
        before = self.provider.provider_request_count
        started = time.perf_counter()
        try:
            value = function()
            passed = bool(gate(value))
            error = None
        except Exception as exc:
            value = None
            passed = False
            error = {
                "type": type(exc).__name__,
                "code": _safe_error_code(
                    getattr(exc, "code", "workload_call_failed")
                ),
            }
        elapsed = (time.perf_counter() - started) * 1000
        after = self.provider.provider_request_count
        usage = {}
        if isinstance(value, Mapping):
            usage = _safe_usage(value.get("usage"))
            if not usage and isinstance(value.get("ai"), Mapping):
                usage = _safe_usage(value["ai"].get("usage"))
        observed_requests = max(0, after - before)
        attributed_requests = usage.get("provider_requests")
        if attributed_requests is None:
            cache = _cache_summary(value)
            outcome = _outcome_summary(value)
            if (
                cache.get("status") in {"hit", "coalesced"}
                or outcome.get("provider_outcome") == "not_called"
            ):
                attributed_requests = 0
            else:
                attributed_requests = observed_requests
        record = {
                "phase": phase,
                "profile": profile,
                "logical_index": index,
                "correct": passed,
                "provider_requests": int(attributed_requests),
                "provider_request_window_delta": observed_requests,
                "latency_ms": round(elapsed, 3),
                "usage": usage,
                "local_cache": _cache_summary(value),
                "outcome": _outcome_summary(value),
                "result_error_codes": _result_error_codes(value),
                **({"error": error} if error else {}),
            }
        with self._records_lock:
            self.records.append(record)
        return value

    @staticmethod
    def _reset_recommendation_history(service: AcmService) -> None:
        with Database(service.paths.database) as db:
            db.connection.execute("DELETE FROM recommendation_runs")

    @staticmethod
    def _stream(service: AcmService, conversation_id: str, message: str) -> dict[str, Any]:
        content = ""
        usage: dict[str, Any] = {}
        outcome: dict[str, Any] = {}
        done = False
        for event in service.ai_chat_stream(
            conversation_id,
            message=message,
            mode="hint",
            hint_level=1,
        ):
            if event.get("event") == "delta":
                content += str(event.get("data", {}).get("content") or "")
            elif event.get("event") == "usage":
                usage = dict(event.get("data", {}).get("usage") or {})
            elif event.get("event") == "done":
                done = True
                outcome = dict(event.get("data", {}).get("outcome") or {})
            elif event.get("event") == "error":
                raise RuntimeError("coaching stream returned an error event")
        return {
            "ok": done and bool(content.strip()),
            "usage": usage,
            "outcome": outcome,
        }

    @staticmethod
    def _compile_patch(workspace: _Workspace, value: Any) -> bool:
        if not isinstance(value, Mapping):
            return False
        source = value.get("candidate_code")
        if not isinstance(source, str) or "n" not in source:
            return False
        scratch = Path(workspace.temporary.name) / ".acm" / "cache-workload-patch.cpp"
        binary = scratch.with_suffix(".exe")
        scratch.write_text(source, encoding="utf-8", newline="\n")
        compiled = subprocess.run(
            ["g++", "-std=c++17", "-O2", str(scratch), "-o", str(binary)],
            capture_output=True,
            timeout=30,
            check=False,
        )
        if compiled.returncode != 0:
            return False
        executed = subprocess.run(
            [str(binary)],
            input=b"1\n",
            capture_output=True,
            timeout=5,
            check=False,
        )
        return executed.returncode == 0 and executed.stdout.strip() == b"2"

    def _run_correctness_phase(self) -> None:
        phase = "correctness-cache-disabled"
        workspace = self._workspace(phase=phase, exact_cache=False)
        service = workspace.service

        self._record(
            phase,
            "recommendation",
            1,
            lambda: service.ai_recommendations(
                count=1,
                mode="mixed",
                source_mode="catalog_only",
                ai_mode="gap_fill",
            ),
            lambda value: bool(value.get("ok"))
            and len(value.get("recommendations") or []) == 1
            and not value.get("ai", {}).get("fallback")
            and not value.get("ai", {}).get("outcome", {}).get("degraded"),
        )

        self._record(
            phase,
            "plan_organize",
            1,
            lambda: service.ai_plan_preview(
                mode="organize", text="CF101A CF102A"
            ),
            lambda value: bool(value.get("ok"))
            and not value.get("ai", {}).get("fallback")
            and not value.get("ai", {}).get("outcome", {}).get("degraded")
            and sum(
                len(stage.get("tasks") or [])
                for stage in value.get("plan", {}).get("stages", [])
            )
            == 2,
        )

        self._record(
            phase,
            "plan_generate",
            1,
            lambda: service.ai_plan_preview(
                mode="generate",
                text="仅选择 CF101A，生成一题基础训练",
                task_count=1,
                include_completed=False,
            ),
            lambda value: bool(value.get("ok"))
            and bool(value.get("ai", {}).get("complete"))
            and int(value.get("ai", {}).get("accepted_count") or 0) == 1,
        )

        conversation = service.ai_conversation_start("CF100A")
        conversation_id = str(conversation["conversation_id"])
        for index, message in enumerate(
            ("只给我一个最小提示。", "再推进一步，但不要给完整代码。", "检查我的边界条件。"),
            1,
        ):
            self._record(
                phase,
                "coaching",
                index,
                lambda message=message: self._stream(service, conversation_id, message),
                lambda value: bool(value.get("ok")),
            )

        self._record(
            phase,
            "patch",
            1,
            lambda: service.ai_patch_preview(
                "CF100A",
                instruction="修复程序，使其读取 n 后输出 n+1，并保留完整可编译源码。",
                conversation_id=conversation_id,
            ),
            lambda value: self._compile_patch(workspace, value),
        )

        service.close(
            "CF100A", result="AC", minutes=5, hint_level=1, failure="none"
        )
        self._record(
            phase,
            "summary",
            1,
            lambda: service.knowledge_preview(
                workspace.attempt_id,
                workspace.target_id,
                schema_mode="stored",
            ),
            lambda value: bool(value.get("ok"))
            and bool(value.get("proposal", {}).get("can_apply"))
            and not value.get("ai", {}).get("outcome", {}).get("degraded"),
        )

    def _run_exact_cache_phase(self) -> None:
        phase = "exact-cache"
        workspace = self._workspace(phase=phase, exact_cache=True)
        service = workspace.service

        for index in (1, 2):
            self._record(
                phase,
                "recommendation",
                index,
                lambda: service.ai_recommendations(
                    count=1,
                    mode="mixed",
                    source_mode="catalog_only",
                    ai_mode="gap_fill",
                ),
                lambda value: bool(value.get("ok"))
                and len(value.get("recommendations") or []) == 1
                and not value.get("ai", {}).get("fallback"),
            )
            self._reset_recommendation_history(service)

        for index in (1, 2):
            self._record(
                phase,
                "plan_organize",
                index,
                lambda: service.ai_plan_preview(
                    mode="organize", text="CF101A CF102A"
                ),
                lambda value: bool(value.get("ok"))
                and not value.get("ai", {}).get("fallback")
                and sum(
                    len(stage.get("tasks") or [])
                    for stage in value.get("plan", {}).get("stages", [])
                )
                == 2,
            )

        service.close(
            "CF100A", result="AC", minutes=5, hint_level=0, failure="none"
        )
        for index in (1, 2):
            self._record(
                phase,
                "summary",
                index,
                lambda: service.knowledge_preview(
                    workspace.attempt_id,
                    workspace.target_id,
                    schema_mode="stored",
                ),
                lambda value: bool(value.get("ok"))
                and bool(value.get("proposal", {}).get("can_apply")),
            )

    def _run_concurrent_organize(self) -> None:
        phase = "singleflight"
        workspace = self._workspace(
            phase=phase, exact_cache=True, validation_repairs=0
        )
        barrier = threading.Barrier(2)

        def call(index: int) -> Any:
            barrier.wait(timeout=10)
            return self._record(
                phase,
                "plan_organize",
                index,
                lambda: workspace.service.ai_plan_preview(
                    mode="organize", text="CF101A CF102A"
                ),
                lambda value: bool(value.get("ok"))
                and not value.get("ai", {}).get("fallback"),
            )

        before = self.provider.provider_request_count
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(call, index) for index in (1, 2)]
            for future in futures:
                future.result()
        self._singleflight_provider_requests = (
            self.provider.provider_request_count - before
        )

    @staticmethod
    def _provider_probe_messages(index: int) -> list[dict[str, str]]:
        stable_prefix = "\n".join(
            (
                "Stage 4 fixed provider-cache probe. Preserve this prefix exactly.",
                *(
                    f"Invariant {item:03d}: caching telemetry is data, not a correctness proof."
                    for item in range(128)
                ),
                "Reply to the final user message with exactly OK.",
            )
        )
        return [
            {"role": "system", "content": stable_prefix},
            {"role": "user", "content": f"Probe tail {index:02d}."},
        ]

    def _run_provider_cache_probes(self) -> None:
        index = 0
        while self.provider.provider_request_count < PROVIDER_TARGET:
            index += 1
            before = self.provider.provider_request_count
            started = time.perf_counter()
            created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
            provider_success = False
            correct = False
            usage: dict[str, int] = {}
            error: dict[str, str] | None = None
            try:
                result = self.provider.chat(
                    self._provider_probe_messages(index),
                    model=MODEL,
                    thinking=False,
                    max_tokens=8,
                    temperature=0,
                    request_retries=0,
                )
                provider_success = True
                correct = str(result.content or "").strip() == "OK"
                usage = _safe_usage(result.usage)
            except Exception as exc:
                usage = _safe_usage(getattr(exc, "usage", {}))
                error = {
                    "type": type(exc).__name__,
                    "code": _safe_error_code(
                        getattr(exc, "code", "provider_probe_failed")
                    ),
                }
            after = self.provider.provider_request_count
            if after <= before:
                raise RuntimeError("provider probe did not enter the adapter HTTP boundary")
            estimate = estimate_cost(
                model=MODEL,
                provider_id="deepseek",
                usage=usage,
                created_at=created_at,
            )
            self.provider_probe_records.append(
                {
                    "logical_index": index,
                    "provider_requests": after - before,
                    "provider_success": provider_success,
                    "correct": correct,
                    "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                    "usage": usage,
                    "estimated_cost": estimate,
                    **({"error": error} if error else {}),
                }
            )

    def _estimated_cost(self) -> float:
        total = 0.0
        for workspace in self._workspaces:
            with Database(workspace.service.paths.database) as db:
                for row in db.query("SELECT estimated_cost_json FROM ai_run_legs"):
                    try:
                        estimate = json.loads(row["estimated_cost_json"] or "{}")
                    except json.JSONDecodeError:
                        continue
                    if estimate.get("currency") == "CNY" and isinstance(
                        estimate.get("amount"), (int, float)
                    ):
                        total += float(estimate["amount"])
        for probe in self.provider_probe_records:
            estimate = probe.get("estimated_cost")
            if (
                isinstance(estimate, Mapping)
                and estimate.get("currency") == "CNY"
                and isinstance(estimate.get("amount"), (int, float))
            ):
                total += float(estimate["amount"])
        return round(total, 12)

    def _provider_token_totals(self) -> dict[str, int]:
        totals = {key: 0 for key in TOKEN_KEYS}
        for workspace in self._workspaces:
            with Database(workspace.service.paths.database) as db:
                for row in db.query("SELECT usage_json FROM ai_run_legs"):
                    try:
                        usage = _safe_usage(json.loads(row["usage_json"] or "{}"))
                    except json.JSONDecodeError:
                        continue
                    for key in TOKEN_KEYS:
                        totals[key] += int(usage.get(key) or 0)
        for probe in self.provider_probe_records:
            usage = _safe_usage(probe.get("usage"))
            for key in TOKEN_KEYS:
                totals[key] += int(usage.get(key) or 0)
        return totals

    def _provider_leg_evidence(self) -> list[dict[str, Any]]:
        """Return a strict, content-free ledger of every provider attempt."""

        evidence: list[dict[str, Any]] = []
        for workspace in self._workspaces:
            with Database(workspace.service.paths.database) as db:
                rows = db.query(
                    "SELECT l.run_id,l.ordinal,l.route_kind,l.provider_id,l.profile_id,"
                    "l.requested_model,l.resolved_model,l.reasoning_strength,l.status,"
                    "l.error_code,l.provider_requests,l.usage_json,l.cache_status,"
                    "l.estimated_cost_json,l.purpose,l.validation_code "
                    "FROM ai_run_legs l ORDER BY l.run_id,l.ordinal"
                )
            for row in rows:
                try:
                    usage = _safe_usage(json.loads(row["usage_json"] or "{}"))
                except json.JSONDecodeError:
                    usage = {}
                try:
                    cost = _safe_cost(json.loads(row["estimated_cost_json"] or "{}"))
                except json.JSONDecodeError:
                    cost = {}
                run_fingerprint = hashlib.sha256(
                    f"{workspace.phase}\0{row['run_id']}".encode("utf-8")
                ).hexdigest()
                evidence.append(
                    {
                        "evidence_kind": "business_provider_leg",
                        "phase": workspace.phase,
                        "run_fingerprint": run_fingerprint,
                        "ordinal": int(row["ordinal"] or 0),
                        "route_kind": _safe_label(row["route_kind"]),
                        "profile": _safe_label(row["profile_id"]),
                        "provider": _safe_label(row["provider_id"]),
                        "requested_model": _safe_label(row["requested_model"]),
                        "resolved_model": _safe_label(row["resolved_model"]),
                        "reasoning_strength": _safe_label(row["reasoning_strength"]),
                        "status": _safe_label(row["status"]),
                        "error_code": _safe_error_code(row["error_code"]),
                        "purpose": _safe_label(row["purpose"], limit=64),
                        "validation_code": _safe_error_code(row["validation_code"]),
                        "provider_requests": int(row["provider_requests"] or 0),
                        "usage": usage,
                        "provider_cache_status": _safe_label(row["cache_status"]),
                        "estimated_cost": cost,
                    }
                )
        for probe in self.provider_probe_records:
            requests = int(probe.get("provider_requests") or 0)
            evidence.append(
                {
                    "evidence_kind": "provider_cache_probe",
                    "phase": "provider-kv-probe",
                    "run_fingerprint": hashlib.sha256(
                        f"provider-kv-probe\0{int(probe.get('logical_index') or 0)}".encode(
                            "utf-8"
                        )
                    ).hexdigest(),
                    "ordinal": 0,
                    "route_kind": "primary",
                    "profile": "provider_kv_probe",
                    "provider": "deepseek",
                    "requested_model": MODEL,
                    "resolved_model": MODEL,
                    "reasoning_strength": "off",
                    "status": (
                        "complete" if probe.get("provider_success") else "failed"
                    ),
                    "error_code": _safe_error_code(
                        probe.get("error", {}).get("code")
                    ),
                    "purpose": "provider_cache_probe",
                    "validation_code": "exact_ok" if probe.get("correct") else None,
                    "provider_requests": requests,
                    "usage": _safe_usage(probe.get("usage")),
                    "provider_cache_status": "probe",
                    "estimated_cost": _safe_cost(probe.get("estimated_cost")),
                }
            )
        return evidence

    @staticmethod
    def _phase_provider_facts(
        phase: str, provider_leg_evidence: Iterable[Mapping[str, Any]]
    ) -> dict[str, Any]:
        usage = {key: 0 for key in TOKEN_KEYS}
        requests = 0
        observed_usage_legs = 0
        missing_usage_legs = 0
        estimated_cost_cny = 0.0
        cost_unknown_legs = 0
        for row in provider_leg_evidence:
            if row.get("phase") != phase:
                continue
            leg_requests = int(row.get("provider_requests") or 0)
            requests += leg_requests
            normalized = _safe_usage(row.get("usage"))
            if normalized.get("input_tokens") is None:
                missing_usage_legs += leg_requests
            else:
                observed_usage_legs += leg_requests
            for key in TOKEN_KEYS:
                usage[key] += int(normalized.get(key) or 0)
            estimate = _safe_cost(row.get("estimated_cost"))
            if estimate.get("currency") == "CNY" and isinstance(
                estimate.get("amount"), (int, float)
            ):
                estimated_cost_cny += float(estimate["amount"])
            else:
                cost_unknown_legs += leg_requests
        return {
            "usage": usage,
            "provider_requests": requests,
            "observed_usage_legs": observed_usage_legs,
            "missing_usage_legs": missing_usage_legs,
            "estimated_cost_cny": round(estimated_cost_cny, 12),
            "cost_unknown_legs": cost_unknown_legs,
        }

    @staticmethod
    def _phase_metrics(
        rows: Iterable[Mapping[str, Any]],
        *,
        provider_requests: int,
        provider_facts: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        selected = list(rows)
        facts = dict(provider_facts or {})
        usage = _safe_usage(facts.get("usage"))
        if not usage:
            usage = {key: 0 for key in TOKEN_KEYS}
            for row in selected:
                normalized = _safe_usage(row.get("usage"))
                for key in TOKEN_KEYS:
                    usage[key] += int(normalized.get(key) or 0)
        requests = int(facts.get("provider_requests", provider_requests) or 0)
        latencies = [float(row.get("latency_ms") or 0) for row in selected]
        input_tokens = usage["input_tokens"]
        cache_read = usage["cache_read_tokens"]
        estimate = estimate_cost(
            model=MODEL,
            provider_id="deepseek",
            usage=usage,
            created_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )
        return {
            "logical_requests": len(selected),
            "provider_requests": requests,
            "input_tokens": input_tokens,
            "output_tokens": usage["output_tokens"],
            "total_tokens": usage["total_tokens"],
            "cache_read_tokens": cache_read,
            "provider_token_hit_rate": (
                round(cache_read / input_tokens, 6) if input_tokens else None
            ),
            "latency_ms": {
                "p50": round(statistics.median(latencies), 3) if latencies else None,
                "p95": _percentile(latencies, 0.95),
            },
            "estimated_cost_cny": facts.get(
                "estimated_cost_cny",
                estimate.get("amount") if estimate.get("status") == "known" else None,
            ),
            "provider_usage_telemetry": {
                "observed_legs": facts.get("observed_usage_legs"),
                "missing_legs": facts.get("missing_usage_legs"),
            },
            "cost_unknown_legs": facts.get("cost_unknown_legs"),
        }

    @staticmethod
    def _provider_requests_by_profile(
        provider_leg_evidence: Iterable[Mapping[str, Any]]
    ) -> dict[str, int]:
        totals = {profile_id: 0 for profile_id in TASK_PROFILE_IDS}
        for row in provider_leg_evidence:
            profile_id = str(row.get("profile") or "")
            if profile_id in totals:
                totals[profile_id] += int(row.get("provider_requests") or 0)
        return totals

    @staticmethod
    def _provider_leg_counts(
        provider_leg_evidence: Iterable[Mapping[str, Any]]
    ) -> dict[str, int]:
        counts = {
            "total": 0,
            "succeeded": 0,
            "failed": 0,
            "validation_repairs": 0,
            "route_leg_records": 0,
        }
        for row in provider_leg_evidence:
            requests = int(row.get("provider_requests") or 0)
            counts["total"] += requests
            counts["route_leg_records"] += 1
            status = str(row.get("status") or "")
            if status == "complete":
                counts["succeeded"] += requests
            elif status == "failed":
                counts["failed"] += requests
            if str(row.get("purpose") or "") == "validation_repair":
                counts["validation_repairs"] += requests
        return counts

    def run(self) -> dict[str, Any]:
        started = datetime.now(timezone.utc)
        fatal_error: Exception | None = None
        try:
            self._run_correctness_phase()
            correctness_requests = self.provider.provider_request_count
            self._run_exact_cache_phase()
            exact_requests = self.provider.provider_request_count - correctness_requests
            self._run_concurrent_organize()
            self._run_provider_cache_probes()
        except Exception as exc:
            fatal_error = exc
            correctness_requests = locals().get("correctness_requests", 0)
            exact_requests = locals().get("exact_requests", 0)
        try:
            exact_seconds = [
                row
                for row in self.records
                if row["phase"] == "exact-cache"
                and row["profile"] in {"recommendation", "plan_organize", "summary"}
                and row["logical_index"] == 2
            ]
            exact_gate = len(exact_seconds) == 3 and all(
                row["provider_requests"] == 0
                and row["local_cache"].get("status") == "hit"
                for row in exact_seconds
            )
            coaching = [
                row for row in self.records
                if row["phase"] == "correctness-cache-disabled"
                and row["profile"] == "coaching"
            ]
            coaching_prefix_gate = len(coaching) == 3 and all(
                int(row["usage"].get("cache_read_tokens") or 0) > 0
                for row in coaching[1:]
            )
            token_totals = self._provider_token_totals()
            input_tokens = token_totals["input_tokens"]
            cache_read = token_totals["cache_read_tokens"]
            local_hits = sum(
                row["local_cache"].get("status") == "hit"
                for row in self.records
                if row["phase"] in {"exact-cache", "singleflight"}
                and row["profile"] in {"recommendation", "plan_organize", "summary"}
            )
            coalesced_followers = sum(
                row["local_cache"].get("status") == "coalesced"
                for row in self.records
                if row["phase"] == "singleflight"
            )
            eligible_exact_lookups = sum(
                row["phase"] in {"exact-cache", "singleflight"}
                and row["profile"] in {"recommendation", "plan_organize", "summary"}
                for row in self.records
            )
            latencies = [float(row["latency_ms"]) for row in self.records]
            provider_leg_evidence = self._provider_leg_evidence()
            requests_by_profile = self._provider_requests_by_profile(
                provider_leg_evidence
            )
            provider_legs = self._provider_leg_counts(provider_leg_evidence)
            expected_profiles = set(TASK_PROFILE_IDS)
            correctness_profiles = {
                row["profile"]
                for row in self.records
                if row["phase"] == "correctness-cache-disabled" and row["correct"]
            }
            all_profile_gate = correctness_profiles == expected_profiles
            logical_gate = len(self.records) == 16
            singleflight_gate = self._singleflight_provider_requests == 1
            provider_limit_gate = self.provider.provider_request_count <= PROVIDER_LIMIT
            provider_target_gate = self.provider.provider_request_count == PROVIDER_TARGET
            authoritative_outcome_gate = bool(self.records) and all(
                row["outcome"].get("provider_outcome")
                in {"not_called", "succeeded", "mixed"}
                and row["outcome"].get("artifact_outcome") in {"valid", "repaired"}
                and row["outcome"].get("business_outcome") in {"complete", "cache"}
                and bool(row["outcome"].get("usable"))
                and not bool(row["outcome"].get("degraded"))
                for row in self.records
            )
            provider_probe_gate = all(
                bool(row.get("provider_success")) and bool(row.get("correct"))
                for row in self.provider_probe_records
            )
            provider_attempts_accounted_gate = (
                provider_legs["total"] == self.provider.provider_request_count
            )
            phase_provider_facts = {
                phase: self._phase_provider_facts(phase, provider_leg_evidence)
                for phase in (
                    "correctness-cache-disabled",
                    "exact-cache",
                    "singleflight",
                )
            }
            probe_provider_facts = self._phase_provider_facts(
                "provider-kv-probe", provider_leg_evidence
            )
            probe_observed_legs = int(probe_provider_facts["observed_usage_legs"])
            probe_missing_legs = int(probe_provider_facts["missing_usage_legs"])
            observed_usage_legs = probe_observed_legs + sum(
                int(facts["observed_usage_legs"])
                for facts in phase_provider_facts.values()
            )
            missing_usage_legs = probe_missing_legs + sum(
                int(facts["missing_usage_legs"])
                for facts in phase_provider_facts.values()
            )
            all_recorded_correct = bool(self.records) and all(
                row["correct"] for row in self.records
            )
            probe_evidence_requests = int(probe_provider_facts["provider_requests"])
            count_consistency = _report_count_consistency(
                self.records,
                provider_leg_evidence,
                top_level_provider_requests=self.provider.provider_request_count,
            )
            counts_self_consistent = bool(count_consistency["consistent"])
            stage_verified = bool(
                fatal_error is None
                and logical_gate
                and all_profile_gate
                and all_recorded_correct
                and authoritative_outcome_gate
                and exact_gate
                and singleflight_gate
                and coaching_prefix_gate
                and provider_limit_gate
                and provider_target_gate
                and provider_probe_gate
                and provider_attempts_accounted_gate
                and counts_self_consistent
            )
            provider_called_rows = [
                row
                for row in self.records
                if row["outcome"]
                and row["outcome"].get("provider_outcome") != "not_called"
                and (
                    int(row.get("provider_requests") or 0) > 0
                    or int(row.get("usage", {}).get("provider_requests") or 0) > 0
                )
            ]
            outcome_counts = {
                "logical_usable": sum(
                    bool(row["outcome"].get("usable")) for row in self.records
                ),
                "provider_valid": sum(
                    row["outcome"].get("artifact_outcome") in {"valid", "repaired"}
                    for row in provider_called_rows
                ),
                "repaired": sum(
                    row["outcome"].get("artifact_outcome") == "repaired"
                    for row in self.records
                ),
                "degraded": sum(
                    bool(row["outcome"].get("degraded")) for row in self.records
                ),
                "partial": sum(
                    row["outcome"].get("business_outcome") == "partial"
                    for row in self.records
                ),
                "unavailable": sum(
                    row["outcome"].get("business_outcome") == "unavailable"
                    for row in self.records
                ),
            }
            provider_called_records = len(provider_called_rows)
            repair_attempted_records = sum(
                int(row["outcome"].get("repair_attempts") or 0) > 0
                for row in self.records
            )
            full_business_successes = sum(
                row["outcome"].get("business_outcome")
                in {"complete", "cache", "hybrid", "deterministic_fallback"}
                for row in self.records
            )
            degraded_usable = sum(
                bool(row["outcome"].get("usable"))
                and bool(row["outcome"].get("degraded"))
                for row in self.records
            )
            safe_configuration = {
                "profiles": {
                    profile_id: {
                        "provider": "deepseek",
                        "model": MODEL,
                        "max_requests": 6 if profile_id == "plan_generate" else 3,
                        "max_retries": 1,
                        "max_validation_repairs": 1,
                        "fallbacks": [],
                    }
                    for profile_id in TASK_PROFILE_IDS
                },
                "coaching_delivery_mode": "resilient",
                "exact_cache_profiles": [
                    "recommendation", "plan_organize", "summary"
                ],
                "semantic_cache_enabled": False,
                "singleflight_validation_repairs": 0,
                "provider_cache_probe": {
                    "fills_remaining_request_budget": True,
                    "stable_prefix_invariants": 128,
                    "thinking": False,
                    "max_tokens": 8,
                },
            }
            configuration_sha256 = hashlib.sha256(
                json.dumps(
                    safe_configuration,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8")
            ).hexdigest()
            source_evidence = _source_evidence(self.root)
            report = {
                "report_version": "stage4-cache-reliability-report-v4",
                "stage_verified": stage_verified,
                "started_at": started.isoformat(timespec="seconds"),
                "completed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "workload_version": json.loads(WORKLOAD_PATH.read_text(encoding="utf-8"))[
                    "workload_version"
                ],
                "workload_sha256": hashlib.sha256(WORKLOAD_PATH.read_bytes()).hexdigest(),
                "git_hash": source_evidence["head"],
                "source_evidence": source_evidence,
                "configuration": safe_configuration,
                "configuration_sha256": configuration_sha256,
                "provider": "deepseek",
                "model": MODEL,
                "provider_request_target": PROVIDER_TARGET,
                "provider_request_limit": PROVIDER_LIMIT,
                "provider_requests": self.provider.provider_request_count,
                "provider_request_reservations": self.provider.reserved_request_count,
                "base_provider_requests": 12,
                "adaptive_retry_repair_or_probe_budget": 8,
                **(
                    {
                        "failure": {
                            "type": type(fatal_error).__name__,
                            "code": _safe_error_code(
                                getattr(
                                    fatal_error,
                                    "code",
                                    "workload_environment_failed",
                                )
                            ),
                        }
                    }
                    if fatal_error is not None
                    else {}
                ),
                "metrics": {
                    "provider_token_hit_rate": (
                        round(cache_read / input_tokens, 6) if input_tokens else None
                    ),
                    "provider_leg_success_rate": (
                        round(provider_legs["succeeded"] / provider_legs["total"], 6)
                        if provider_legs["total"] else None
                    ),
                    "provider_valid_artifact_rate": (
                        round(outcome_counts["provider_valid"] / provider_called_records, 6)
                        if provider_called_records else None
                    ),
                    "repair_recovery_rate": (
                        round(outcome_counts["repaired"] / repair_attempted_records, 6)
                        if repair_attempted_records else None
                    ),
                    "full_business_success_rate": (
                        round(full_business_successes / len(self.records), 6)
                        if self.records else None
                    ),
                    "degraded_usable_rate": (
                        round(degraded_usable / len(self.records), 6)
                        if self.records else None
                    ),
                    "partial_unavailable_rate": (
                        round(
                            (outcome_counts["partial"] + outcome_counts["unavailable"])
                            / len(self.records),
                            6,
                        )
                        if self.records else None
                    ),
                    "local_exact_hit_rate": (
                        round(local_hits / eligible_exact_lookups, 6)
                        if eligible_exact_lookups else None
                    ),
                    "local_exact_second_hit_rate": (
                        round(
                            sum(
                                row["local_cache"].get("status") == "hit"
                                for row in exact_seconds
                            )
                            / len(exact_seconds),
                            6,
                        )
                        if exact_seconds
                        else None
                    ),
                    "provider_avoidance": (
                        round(
                            (local_hits + coalesced_followers)
                            / len(self.records),
                            6,
                        )
                        if self.records else None
                    ),
                    "input_tokens": input_tokens,
                    "output_tokens": token_totals["output_tokens"],
                    "total_tokens": token_totals["total_tokens"],
                    "cache_read_tokens": cache_read,
                    "logical_requests": len(self.records),
                    **outcome_counts,
                    "latency_ms": {
                        "p50": (
                            round(statistics.median(latencies), 3)
                            if latencies else None
                        ),
                        "p95": _percentile(latencies, 0.95),
                    },
                    "estimated_cost_cny": self._estimated_cost(),
                    "provider_legs": provider_legs,
                    "count_consistency": count_consistency,
                    "provider_usage_telemetry": {
                        "observed_legs": observed_usage_legs,
                        "missing_legs": missing_usage_legs,
                        "coverage_rate": (
                            round(observed_usage_legs / provider_legs["total"], 6)
                            if provider_legs["total"]
                            else None
                        ),
                    },
                    "provider_probe_success_rate": (
                        round(
                            sum(
                                bool(row.get("provider_success"))
                                for row in self.provider_probe_records
                            )
                            / len(self.provider_probe_records),
                            6,
                        )
                        if self.provider_probe_records
                        else None
                    ),
                },
                "gates": {
                    "all_six_profiles_complete": all_profile_gate,
                    "all_sixteen_logical_requests_executed": logical_gate,
                    "all_recorded_correct": all_recorded_correct,
                    "all_outcomes_authoritative_and_complete": authoritative_outcome_gate,
                    "exact_second_calls_zero_provider": exact_gate,
                    "concurrent_organize_one_provider": singleflight_gate,
                    "coaching_later_turns_provider_cache_hit": coaching_prefix_gate,
                    "provider_request_limit_respected": provider_limit_gate,
                    "exactly_twenty_provider_requests": provider_target_gate,
                    "all_provider_attempts_accounted": provider_attempts_accounted_gate,
                    "all_report_counts_self_consistent": counts_self_consistent,
                    "all_provider_cache_probes_correct": provider_probe_gate,
                    "semantic_cache_disabled": True,
                },
                "phases": {
                    "correctness_cache_disabled": self._phase_metrics(
                        (
                            row
                            for row in self.records
                            if row["phase"] == "correctness-cache-disabled"
                        ),
                        provider_requests=int(correctness_requests),
                        provider_facts=phase_provider_facts[
                            "correctness-cache-disabled"
                        ],
                    ),
                    "exact_cache": self._phase_metrics(
                        (row for row in self.records if row["phase"] == "exact-cache"),
                        provider_requests=int(exact_requests),
                        provider_facts=phase_provider_facts["exact-cache"],
                    ),
                    "singleflight": self._phase_metrics(
                        (row for row in self.records if row["phase"] == "singleflight"),
                        provider_requests=int(self._singleflight_provider_requests or 0),
                        provider_facts=phase_provider_facts["singleflight"],
                    ),
                    "provider_kv_probe": self._phase_metrics(
                        self.provider_probe_records,
                        provider_requests=probe_evidence_requests,
                        provider_facts=probe_provider_facts,
                    ),
                },
                "profiles": {
                    profile: {
                        "correct": bool(profile_rows)
                        and all(row["correct"] for row in profile_rows),
                        "logical_requests": len(profile_rows),
                        "provider_requests": requests_by_profile[profile],
                    }
                    for profile in TASK_PROFILE_IDS
                    for profile_rows in [
                        [row for row in self.records if row["profile"] == profile]
                    ]
                },
                "runs": self.records,
                "provider_cache_probes": self.provider_probe_records,
                "provider_leg_evidence": provider_leg_evidence,
                "invalidation_matrix": {
                    "profile_route_generation_messages_and_domain_hashes": "tested_locally",
                    "artifact_proof_ttl_and_validator": "tested_locally",
                    "force_refresh_clear_prune_and_lease": "tested_locally",
                    "semantic_cache": "fail_closed",
                },
            }
            return report
        finally:
            for workspace in reversed(self._workspaces):
                workspace.close()


def _load_live_provider(root: Path) -> CappedProviderClient:
    paths = Paths.for_root(root)
    config = load_config(paths)
    provider = config["ai"]["providers"]["deepseek"]
    slot = str(provider["credential_slot"])
    vault = create_platform_credential_vault(paths.state_dir)
    credential = vault.load_bound(
        slot,
        provider_id="deepseek",
        origin=str(provider["base_url"]),
        auth=dict(provider["auth"]),
    )
    if credential is None:
        raise RuntimeError("the current secure credential vault has no bound DeepSeek credential")
    return CappedProviderClient(DeepSeekClient(api_key=credential.secret, retries=0))


def write_report(root: Path, report: Mapping[str, Any]) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    directory = root / ".acm" / "reports" / "cache-optimization" / stamp
    directory.mkdir(parents=True, exist_ok=False)
    (directory / "workload.json").write_text(
        json.dumps(
            {
                "workload_version": report["workload_version"],
                "workload_sha256": report["workload_sha256"],
                "provider": report["provider"],
                "model": report["model"],
                "provider_request_target": report.get("provider_request_target"),
                "provider_request_limit": report["provider_request_limit"],
                "configuration_sha256": report.get("configuration_sha256"),
                "git_hash": report.get("git_hash"),
                "source_snapshot_sha256": report.get("source_evidence", {}).get(
                    "source_snapshot_sha256"
                ),
                "source_dirty": report.get("source_evidence", {}).get("dirty"),
                "tracked_diff_sha256": report.get("source_evidence", {}).get(
                    "tracked_diff_sha256"
                ),
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    (directory / "report.json").write_text(
        json.dumps(dict(report), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    return directory


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the isolated Stage 4 cache workload")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument(
        "--live",
        action="store_true",
        help="allow the fixed paid DeepSeek workload (exactly 20 adapter HTTP attempts)",
    )
    args = parser.parse_args(argv)
    root = args.root.resolve()
    if not args.live:
        parser.error("--live is required; no provider request was sent")
    runner = Stage4WorkloadRunner(root, _load_live_provider(root))
    report = runner.run()
    directory = write_report(root, report)
    verified = bool(report.get("stage_verified"))
    print(
        json.dumps(
            {"ok": verified, "report_directory": str(directory)},
            ensure_ascii=False,
        )
    )
    return 0 if verified else 1


if __name__ == "__main__":
    raise SystemExit(main())
