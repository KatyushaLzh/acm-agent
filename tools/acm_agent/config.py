from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .ai_policy import DEFAULT_MODEL, DEFAULT_REASONING_EFFORT


CONFIG_VERSION = 8


@dataclass(frozen=True)
class Paths:
    root: Path
    state_dir: Path
    config: Path
    database: Path
    cache: Path
    build: Path
    cases: Path
    reports: Path
    failures: Path
    plan: Path
    plan_readme: Path

    @classmethod
    def for_root(cls, root: Path) -> "Paths":
        root = root.resolve()
        state = root / ".acm"
        return cls(
            root=root,
            state_dir=state,
            config=state / "config.json",
            database=state / "state.db",
            cache=state / "cache",
            build=state / "build",
            cases=state / "cases",
            reports=state / "reports",
            failures=state / "failures",
            plan=root / "training" / "data-structures-30d" / "plan.json",
            plan_readme=root / "training" / "data-structures-30d" / "README.md",
        )

    def ensure(self) -> None:
        for path in (
            self.state_dir,
            self.cache,
            self.build,
            self.cases,
            self.reports,
            self.failures,
        ):
            path.mkdir(parents=True, exist_ok=True)


DEFAULT_CONFIG: dict[str, Any] = {
    "version": CONFIG_VERSION,
    "accounts": {
        "codeforces": {"handle": ""},
        "luogu": {"uid": ""},
    },
    "recommendation": {
        "mode": "plan_first",
        "count": 3,
        "target_cf_rating": None,
        "platform_ratio": {"codeforces": 0.6, "luogu": 0.4},
    },
    "sync": {
        "status_ttl_hours": 6,
        "catalog_ttl_hours": 24,
        "timeout_seconds": 20,
    },
    "ai": {
        "recommendation_model": DEFAULT_MODEL,
        "coaching_model": DEFAULT_MODEL,
        "summary_model": DEFAULT_MODEL,
        "recommendation_thinking": False,
        "coaching_thinking": True,
        "summary_thinking": True,
        "reasoning_effort": DEFAULT_REASONING_EFFORT,
        "summary_reasoning_effort": DEFAULT_REASONING_EFFORT,
    },
}


def _is_sensitive_ai_key(key: Any) -> bool:
    normalized = "".join(
        character for character in str(key).casefold() if character.isalnum()
    )
    return normalized in {"apikey", "authorization", "accesstoken", "token", "secret"} or any(
        marker in normalized for marker in ("apikey", "authorization", "token", "secret")
    )


def _merge_defaults(defaults: Any, current: Any) -> Any:
    """Return ``current`` overlaid on defaults without dropping unknown keys."""
    if not isinstance(defaults, dict) or not isinstance(current, dict):
        return current
    merged = {key: json.loads(json.dumps(value)) for key, value in defaults.items()}
    for key, value in current.items():
        if key in defaults:
            merged[key] = _merge_defaults(defaults[key], value)
        else:
            merged[key] = value
    return merged


def _upgrade_config(data: dict[str, Any]) -> tuple[dict[str, Any], bool]:
    version = data.get("version")
    if version not in (1, 2, 3, 4, 5, 6, 7, CONFIG_VERSION):
        raise ValueError(f"Unsupported config version: {version!r}")
    upgraded = _merge_defaults(DEFAULT_CONFIG, data)
    ai = upgraded.get("ai")
    if isinstance(ai, dict):
        source_ai = data.get("ai") if isinstance(data.get("ai"), dict) else {}
        if version in (1, 2):
            # Phase-three summaries are coaching-like calls.  Existing users
            # therefore keep their explicit coaching model/thinking choices
            # instead of silently reverting to the new-install defaults.
            if "summary_model" not in source_ai:
                ai["summary_model"] = ai.get(
                    "coaching_model", DEFAULT_CONFIG["ai"]["summary_model"]
                )
            if "summary_thinking" not in source_ai:
                ai["summary_thinking"] = ai.get(
                    "coaching_thinking", DEFAULT_CONFIG["ai"]["summary_thinking"]
                )
            if "summary_reasoning_effort" not in source_ai:
                ai["summary_reasoning_effort"] = ai.get(
                    "reasoning_effort",
                    DEFAULT_CONFIG["ai"]["summary_reasoning_effort"],
                )
        for retired_key in (
            "validation_model",
            "validation_thinking",
            "validation_reasoning_effort",
            "stress_prepare_timeout_seconds",
            "stress_generation_mode",
        ):
            ai.pop(retired_key, None)
        for key in list(ai):
            if _is_sensitive_ai_key(key):
                del ai[key]
    upgraded["version"] = CONFIG_VERSION
    return upgraded, upgraded != data


def load_config(paths: Paths, *, required: bool = True) -> dict[str, Any]:
    if not paths.config.exists():
        if required:
            raise FileNotFoundError(
                f"Configuration not found: {paths.config}. Run '.\\acm.ps1 init' first."
            )
        return json.loads(json.dumps(DEFAULT_CONFIG))
    data = json.loads(paths.config.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("Configuration root must be a JSON object")
    config, changed = _upgrade_config(data)
    if changed:
        save_config(paths, config)
    return config


def save_config(paths: Paths, config: dict[str, Any]) -> None:
    paths.ensure()
    payload = json.dumps(config, ensure_ascii=False, indent=2) + "\n"
    fd, temporary = tempfile.mkstemp(
        prefix="config-", suffix=".json", dir=paths.state_dir
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, paths.config)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
