from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any


CONFIG_VERSION = 2


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
        "recommendation_model": "deepseek-v4-flash",
        "coaching_model": "deepseek-v4-flash",
        "recommendation_thinking": False,
        "coaching_thinking": True,
        "reasoning_effort": "high",
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
    if version not in (1, CONFIG_VERSION):
        raise ValueError(f"Unsupported config version: {version!r}")
    upgraded = _merge_defaults(DEFAULT_CONFIG, data)
    ai = upgraded.get("ai")
    if isinstance(ai, dict):
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
