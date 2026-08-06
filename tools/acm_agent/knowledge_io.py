"""Guarded atomic writes for Markdown summary proposals.

The database owns proposal lifecycle state; this module only performs the
filesystem half of that saga.  Every operation is guarded by the exact target
path and content hashes supplied by a reviewed proposal.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import stat
import tempfile
import threading
from typing import Any

from .knowledge import (
    KnowledgeError,
    MAX_MARKDOWN_BYTES,
    UTF8_BOM,
    inspect_markdown_path,
    validate_markdown_path,
)


class MarkdownWriteConflict(RuntimeError):
    """The target no longer matches the proposal baseline."""


@dataclass(frozen=True)
class MarkdownApplyResult:
    path: Path
    backup_path: Path | None
    applied_sha256: str


@dataclass(frozen=True)
class MarkdownRevertResult:
    path: Path
    restored_sha256: str | None
    removed_new_file: bool


_LOCKS_GUARD = threading.Lock()
_TARGET_LOCKS: dict[str, threading.RLock] = {}


def _target_lock(path: Path) -> threading.RLock:
    key = os.path.normcase(str(path))
    with _LOCKS_GUARD:
        return _TARGET_LOCKS.setdefault(key, threading.RLock())


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _validate_candidate(payload: bytes, expected_sha256: str) -> None:
    if len(payload) > MAX_MARKDOWN_BYTES:
        raise ValueError("Markdown candidate exceeds the 1 MiB limit")
    if b"\x00" in payload:
        raise ValueError("Markdown candidate contains NUL")
    body = payload[len(UTF8_BOM) :] if payload.startswith(UTF8_BOM) else payload
    try:
        body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValueError("Markdown candidate must be UTF-8 or UTF-8 BOM") from exc
    if _sha256(payload) != str(expected_sha256):
        raise MarkdownWriteConflict("candidate hash does not match the reviewed proposal")


def _atomic_write(path: Path, payload: bytes, *, mode: int | None = None) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.acm-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
        try:
            directory_fd = os.open(path.parent, os.O_RDONLY)
        except (AttributeError, OSError):
            directory_fd = None
        if directory_fd is not None:
            try:
                os.fsync(directory_fd)
            except OSError:
                pass
            finally:
                os.close(directory_fd)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def apply_markdown_candidate(
    path: str | Path,
    *,
    target_existed: bool,
    baseline_sha256: str | None,
    candidate_bytes: bytes,
    candidate_sha256: str,
    backup_root: str | Path,
    proposal_id: str,
) -> MarkdownApplyResult:
    try:
        target = validate_markdown_path(path, allow_create=not target_existed)
    except KnowledgeError as exc:
        raise MarkdownWriteConflict(str(exc)) from exc
    payload = bytes(candidate_bytes)
    _validate_candidate(payload, candidate_sha256)
    with _target_lock(target):
        current: bytes | None
        mode: int | None = None
        if target_existed:
            try:
                document = inspect_markdown_path(target)
            except KnowledgeError as exc:
                raise MarkdownWriteConflict(str(exc)) from exc
            current = document.raw
            if document.baseline_sha256 != str(baseline_sha256 or ""):
                raise MarkdownWriteConflict("Markdown target changed after preview")
            mode = stat.S_IMODE(target.stat().st_mode)
        else:
            if target.exists() or os.path.lexists(target):
                raise MarkdownWriteConflict("Markdown target appeared after preview")
            current = None

        backup_dir = Path(backup_root) / str(proposal_id)
        backup_dir.mkdir(parents=True, exist_ok=True)
        backup_path: Path | None = None
        if current is not None:
            backup_path = backup_dir / "original.md"
            _atomic_write(backup_path, current)
        metadata = {
            "version": 1,
            "target_existed": bool(target_existed),
            "baseline_sha256": baseline_sha256,
            "candidate_sha256": candidate_sha256,
        }
        _atomic_write(
            backup_dir / "metadata.json",
            (json.dumps(metadata, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )
        _atomic_write(target, payload, mode=mode)
        if _sha256(target.read_bytes()) != candidate_sha256:
            raise OSError("Markdown write verification failed")
        return MarkdownApplyResult(target, backup_path, candidate_sha256)


def compensate_markdown_apply(
    path: str | Path,
    *,
    target_existed: bool,
    applied_sha256: str,
    backup_path: str | Path | None,
) -> None:
    try:
        target = validate_markdown_path(path, allow_create=True)
    except KnowledgeError as exc:
        raise MarkdownWriteConflict(str(exc)) from exc
    with _target_lock(target):
        if not target.exists() or _sha256(target.read_bytes()) != str(applied_sha256):
            raise MarkdownWriteConflict("cannot compensate because Markdown target changed")
        if target_existed:
            if backup_path is None:
                raise MarkdownWriteConflict("baseline backup is missing")
            backup = Path(backup_path)
            baseline = backup.read_bytes()
            _atomic_write(target, baseline, mode=stat.S_IMODE(target.stat().st_mode))
        else:
            target.unlink()


def revert_markdown_candidate(
    path: str | Path,
    *,
    target_existed: bool,
    applied_sha256: str,
    baseline_sha256: str | None,
    backup_path: str | Path | None,
) -> MarkdownRevertResult:
    try:
        target = validate_markdown_path(path)
    except KnowledgeError as exc:
        raise MarkdownWriteConflict(str(exc)) from exc
    with _target_lock(target):
        current = target.read_bytes()
        if _sha256(current) != str(applied_sha256):
            raise MarkdownWriteConflict("Markdown target changed after AI summary apply")
        if not target_existed:
            target.unlink()
            return MarkdownRevertResult(target, None, True)
        if backup_path is None:
            raise MarkdownWriteConflict("Markdown baseline backup is missing")
        backup = Path(backup_path)
        if not backup.is_file():
            raise MarkdownWriteConflict("Markdown baseline backup is missing")
        baseline = backup.read_bytes()
        if _sha256(baseline) != str(baseline_sha256 or ""):
            raise MarkdownWriteConflict("Markdown baseline backup hash is invalid")
        _atomic_write(target, baseline, mode=stat.S_IMODE(target.stat().st_mode))
        restored = _sha256(target.read_bytes())
        if restored != baseline_sha256:
            raise OSError("Markdown revert verification failed")
        return MarkdownRevertResult(target, restored, False)


def current_markdown_sha256(path: str | Path) -> str | None:
    try:
        target = validate_markdown_path(path, allow_create=True)
    except KnowledgeError as exc:
        raise MarkdownWriteConflict(str(exc)) from exc
    if not target.exists():
        return None
    return _sha256(target.read_bytes())


__all__ = [
    "MarkdownApplyResult",
    "MarkdownRevertResult",
    "MarkdownWriteConflict",
    "apply_markdown_candidate",
    "compensate_markdown_apply",
    "current_markdown_sha256",
    "revert_markdown_candidate",
]
