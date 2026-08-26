"""Generate a prompt-free model usage and estimated-cost baseline report."""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import platform
import sqlite3
import subprocess
from typing import Any, Iterable, Mapping

from . import __version__
from .ai_telemetry import PRICE_CATALOG_PATH, estimate_cost, load_price_catalog
from .storage_schema import SCHEMA_VERSION
from .usage import normalize_usage


WORKLOAD_PATH = Path(__file__).with_name("model-provider-workload.v1.json")


def _json_object(value: Any) -> dict[str, Any]:
    try:
        parsed = json.loads(str(value or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return dict(parsed) if isinstance(parsed, Mapping) else {}


def _profile(kind: str, request_summary: Mapping[str, Any]) -> str:
    if kind == "plan_import":
        mode = str(request_summary.get("mode") or "unknown")
        return f"plan_{mode}"
    if kind == "markdown_summary":
        return "summary"
    return str(kind)


def _duration_ms(created_at: Any, completed_at: Any) -> int | None:
    try:
        start = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        end = datetime.fromisoformat(str(completed_at).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return max(0, round((end - start).total_seconds() * 1000))


def _git_summary(root: Path) -> dict[str, Any]:
    try:
        revision = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True,
        ).stdout.strip()
        dirty = bool(subprocess.run(
            ["git", "status", "--porcelain"], cwd=root, text=True,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, check=True,
        ).stdout.strip())
    except (OSError, subprocess.CalledProcessError):
        return {"revision": None, "dirty": None}
    return {"revision": revision, "dirty": dirty}


def read_runs(database: Path) -> tuple[int, list[dict[str, Any]]]:
    database_uri = f"{database.resolve().as_uri()}?mode=ro"
    connection = sqlite3.connect(database_uri, uri=True)
    connection.row_factory = sqlite3.Row
    try:
        schema_version = int(connection.execute("PRAGMA user_version").fetchone()[0])
        columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(ai_runs)")}
        optional = [name for name in ("telemetry_json", "estimated_cost_json") if name in columns]
        selected = [
            "id", "kind", "model", "request_summary_json", "status", "finish_reason",
            "usage_json", "error_json", "created_at", "completed_at", *optional,
        ]
        rows = [dict(row) for row in connection.execute(
            f"SELECT {','.join(selected)} FROM ai_runs ORDER BY created_at,id"
        )]
    finally:
        connection.close()
    return schema_version, rows


def build_report(
    rows: Iterable[Mapping[str, Any]],
    *,
    workload: Mapping[str, Any],
    price_catalog: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    safe_runs: list[dict[str, Any]] = []
    required_profiles = [str(item["id"]) for item in workload.get("profiles", [])]
    observed_profiles: set[str] = set()
    totals: dict[str, int | float] = {
        "runs": 0, "complete_runs": 0, "failed_runs": 0, "known_usage_runs": 0,
        "unknown_usage_runs": 0, "total_tokens_known": 0, "failure_tokens_known": 0,
        "cache_read_tokens_known": 0, "estimated_cost_known": 0.0,
    }
    failed_usage_unknown = 0
    provider_requests_known = 0
    provider_request_runs_known = 0
    protocol_repairs_known = 0
    protocol_repair_runs_known = 0

    for raw in rows:
        request = _json_object(raw.get("request_summary_json"))
        usage = normalize_usage(_json_object(raw.get("usage_json")))
        error = _json_object(raw.get("error_json"))
        if not usage and isinstance(error.get("usage"), Mapping):
            usage = normalize_usage(error["usage"])
        telemetry = _json_object(raw.get("telemetry_json"))
        profile_id = _profile(str(raw.get("kind") or ""), request)
        observed_profiles.add(profile_id)
        status = str(raw.get("status") or "unknown")
        total_tokens = usage.get("total_tokens")
        total_known = isinstance(total_tokens, (int, float)) and not isinstance(total_tokens, bool)
        estimate = estimate_cost(
            model=str(raw.get("model") or ""), usage=usage,
            created_at=str(raw.get("created_at") or "") or None, catalog=price_catalog,
        )
        provider_requests = telemetry.get("provider_requests", usage.get("provider_requests"))
        protocol_repairs = telemetry.get("protocol_repairs", usage.get("protocol_repairs"))
        if isinstance(provider_requests, int) and not isinstance(provider_requests, bool):
            provider_requests_known += provider_requests
            provider_request_runs_known += 1
        if isinstance(protocol_repairs, int) and not isinstance(protocol_repairs, bool):
            protocol_repairs_known += protocol_repairs
            protocol_repair_runs_known += 1
        totals["runs"] += 1
        if status == "complete":
            totals["complete_runs"] += 1
        if status in {"failed", "interrupted"}:
            totals["failed_runs"] += 1
        if total_known:
            totals["known_usage_runs"] += 1
            totals["total_tokens_known"] += total_tokens
            if status in {"failed", "interrupted"}:
                totals["failure_tokens_known"] += total_tokens
        else:
            totals["unknown_usage_runs"] += 1
            if status in {"failed", "interrupted"}:
                failed_usage_unknown += 1
        cache_read = usage.get("cache_read_tokens")
        if isinstance(cache_read, (int, float)) and not isinstance(cache_read, bool):
            totals["cache_read_tokens_known"] += cache_read
        if estimate.get("status") == "known":
            totals["estimated_cost_known"] += float(estimate["amount"])
        safe_runs.append({
            "run_id": str(raw.get("id") or ""),
            "kind": str(raw.get("kind") or ""),
            "profile": profile_id,
            "model": str(raw.get("model") or ""),
            "status": status,
            "finish_reason": raw.get("finish_reason"),
            "usage": usage,
            "telemetry": telemetry,
            "duration_ms": _duration_ms(raw.get("created_at"), raw.get("completed_at")),
            "estimated_cost": estimate,
            "error_code": error.get("code"),
        })

    run_count = int(totals["runs"])
    complete = int(totals["complete_runs"])
    totals["estimated_cost_known"] = round(float(totals["estimated_cost_known"]), 12)
    summary = {
        "workload_version": workload.get("workload_version"),
        "coverage": {
            "required_profiles": required_profiles,
            "observed_profiles": sorted(observed_profiles),
            "missing_profiles": sorted(set(required_profiles) - observed_profiles),
            "complete": set(required_profiles).issubset(observed_profiles),
        },
        "totals": totals,
        "provider_requests": {
            "known_total": provider_requests_known,
            "known_runs": provider_request_runs_known,
            "unknown_runs": run_count - provider_request_runs_known,
        },
        "protocol_repairs": {
            "known_total": protocol_repairs_known,
            "known_runs": protocol_repair_runs_known,
            "unknown_runs": run_count - protocol_repair_runs_known,
        },
        "failed_usage_unknown_runs": failed_usage_unknown,
        "provider_success_rate": None if not run_count else round(complete / run_count, 6),
        "workflow_final_success_rate": None if not run_count else round(complete / run_count, 6),
        "correctness_gate_rate": None if not run_count else round(complete / run_count, 6),
        "rate_definition": "complete ai_run / all ai_runs; fallback outcomes are not inferred",
    }
    return safe_runs, summary


def generate_baseline(root: Path, database: Path, output: Path) -> dict[str, Any]:
    workload = json.loads(WORKLOAD_PATH.read_text(encoding="utf-8"))
    price_catalog = load_price_catalog()
    schema_version, rows = read_runs(database)
    safe_runs, summary = build_report(rows, workload=workload, price_catalog=price_catalog)
    output.mkdir(parents=True, exist_ok=True)
    workload_hash = hashlib.sha256(WORKLOAD_PATH.read_bytes()).hexdigest()
    manifest = {
        "baseline_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "acm_agent_version": __version__,
        "workload_version": workload.get("workload_version"),
        "workload_sha256": workload_hash,
        "price_catalog_version": price_catalog.get("catalog_version"),
        "price_catalog_source": price_catalog.get("source"),
        "database_schema_observed": schema_version,
        "database_schema_supported": SCHEMA_VERSION,
        "python": platform.python_version(),
        "platform": platform.system(),
        "git": _git_summary(root),
        "data_source": "sqlite_ai_runs_read_only",
        "contains_sensitive_prompts": False,
        "run_count": len(safe_runs),
    }
    (output / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    (output / "runs.jsonl").write_text("".join(json.dumps(item, ensure_ascii=False) + "\n" for item in safe_runs), encoding="utf-8")
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"manifest": manifest, "summary": summary, "output": str(output)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--database", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    root = args.root.resolve()
    database = (args.database or root / ".acm" / "state.db").resolve()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output = (args.output or root / ".acm" / "reports" / "ai-cost-baselines" / f"{stamp}-v1").resolve()
    result = generate_baseline(root, database, output)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = ["WORKLOAD_PATH", "build_report", "generate_baseline", "read_runs"]
