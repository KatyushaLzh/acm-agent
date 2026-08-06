"""Deterministic problem-context parsing and safe AI source replacement helpers.

This module intentionally has no network or database dependencies.  Callers fetch
public problem payloads themselves, then pass the response through the narrow
parsers below.  In particular, only statement fields are accepted; editorial and
solution-shaped fields are never traversed or returned.
"""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Mapping, Sequence


CONTEXT_MAX_BYTES = 128 * 1024
SOURCE_MAX_BYTES = 256 * 1024
CONTEXT_TTL_DAYS = 30


class AIContextError(RuntimeError):
    """Base error for deterministic context and patch helpers."""


class ContextParseError(AIContextError):
    """A remote payload did not contain a recognizable public statement."""


class ContextValidationError(AIContextError):
    """Manually supplied problem context is unsafe or too large."""


class SourceValidationError(AIContextError):
    """A source path or full source replacement violates workspace policy."""


class PatchConflictError(AIContextError):
    """The source changed since the patch preview was generated."""


@dataclass(frozen=True, slots=True)
class PatchApplyResult:
    source_path: Path
    backup_path: Path
    original_sha256: str
    applied_sha256: str
    diff: str


@dataclass(frozen=True, slots=True)
class PatchRevertResult:
    source_path: Path
    backup_path: Path
    replaced_sha256: str
    restored_sha256: str


def _collapse_inline(value: str) -> str:
    return re.sub(r"[ \t\f\v]+", " ", value).strip()


def _normalize_blocks(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    lines = [_collapse_inline(line) for line in value.split("\n")]
    result: list[str] = []
    blank = True
    for line in lines:
        if line:
            result.append(line)
            blank = False
        elif not blank:
            result.append("")
            blank = True
    while result and not result[-1]:
        result.pop()
    return "\n".join(result)


class _CodeforcesStatementParser(HTMLParser):
    """Capture only the first ``.problem-statement`` subtree."""

    _BLOCK_TAGS = {
        "address", "article", "blockquote", "div", "dl", "dt", "dd",
        "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2",
        "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "ol", "p",
        "pre", "section", "table", "tbody", "td", "tfoot", "th", "thead",
        "tr", "ul",
    }
    _SKIP_TAGS = {"script", "style", "noscript", "svg"}
    _VOID_TAGS = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.target_depth = 0
        self.skip_depth = 0
        self.found = False
        self.parts: list[str] = []
        self._section_title_depths: list[int] = []

    @staticmethod
    def _classes(attrs: list[tuple[str, str | None]]) -> set[str]:
        value = dict(attrs).get("class") or ""
        return {item.casefold() for item in value.split()}

    def _newline(self, count: int = 1) -> None:
        if not self.parts:
            return
        self.parts.append("\n" * count)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        lowered = tag.casefold()
        classes = self._classes(attrs)
        if not self.target_depth:
            if not self.found and "problem-statement" in classes:
                self.found = True
                self.target_depth = 1
            return

        is_void = lowered in self._VOID_TAGS
        if not is_void:
            self.target_depth += 1
        if self.skip_depth:
            if not is_void:
                self.skip_depth += 1
            return
        if lowered in self._SKIP_TAGS:
            self.skip_depth = 1
            return
        if "section-title" in classes:
            self._newline(2)
            self.parts.append("## ")
            self._section_title_depths.append(self.target_depth)
        elif lowered == "li":
            self._newline()
            self.parts.append("- ")
        elif lowered == "br":
            self._newline()
        elif lowered in self._BLOCK_TAGS:
            self._newline(2 if lowered in {"div", "p", "pre", "table"} else 1)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.target_depth and tag.casefold() in {"br", "hr"}:
            self._newline()

    def handle_endtag(self, tag: str) -> None:
        if not self.target_depth:
            return
        if self.skip_depth:
            self.skip_depth -= 1
        else:
            lowered = tag.casefold()
            if lowered in self._BLOCK_TAGS:
                self._newline(2 if lowered in {"div", "p", "pre", "table"} else 1)
            if self._section_title_depths and self._section_title_depths[-1] == self.target_depth:
                self._section_title_depths.pop()
                self._newline(2)
        self.target_depth -= 1

    def handle_data(self, data: str) -> None:
        if self.target_depth and not self.skip_depth:
            self.parts.append(data)


def extract_codeforces_statement(payload: str | bytes) -> str:
    """Extract readable text from the first Codeforces statement element."""
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8-sig", errors="strict")
    if not isinstance(payload, str):
        raise ContextParseError("Codeforces statement payload must be HTML text")
    parser = _CodeforcesStatementParser()
    try:
        parser.feed(payload)
        parser.close()
    except (ValueError, UnicodeError) as exc:
        raise ContextParseError(f"invalid Codeforces statement HTML: {exc}") from exc
    statement = _normalize_blocks(unescape("".join(parser.parts)))
    if not parser.found or not statement:
        raise ContextParseError("Codeforces page has no non-empty .problem-statement")
    return statement


_LUOGU_SECTION_FIELDS: tuple[tuple[tuple[str, ...], str], ...] = (
    (("background",), "题目背景"),
    (("description",), "题目描述"),
    (("inputFormat", "formatI", "input_format"), "输入格式"),
    (("outputFormat", "formatO", "output_format"), "输出格式"),
    (("hint", "note"), "说明/提示"),
)
_LUOGU_CONTENT_KEYS = {key for keys, _ in _LUOGU_SECTION_FIELDS for key in keys} | {
    "samples", "sample", "title", "name",
}
_BANNED_PAYLOAD_KEYS = {
    "editorial", "editorials", "solution", "solutions", "answer", "answers",
    "tutorial", "tutorials", "explanation", "analysis", "题解", "答案",
}
_BANNED_PAYLOAD_KEYS_FOLDED = {item.casefold() for item in _BANNED_PAYLOAD_KEYS}


def _is_banned_payload_key(key: Any) -> bool:
    folded = str(key).casefold().replace("_", "").replace("-", "")
    return any(token in folded for token in _BANNED_PAYLOAD_KEYS_FOLDED)


def _first_text(candidate: Mapping[str, Any], keys: Sequence[str]) -> str | None:
    for key in keys:
        value = candidate.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _is_statement_candidate(candidate: Mapping[str, Any]) -> bool:
    return any(_first_text(candidate, keys) is not None for keys, _ in _LUOGU_SECTION_FIELDS)


def _decode_luogu_payload(payload: Any) -> Any:
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8-sig", errors="strict")
    if isinstance(payload, str):
        stripped = payload.lstrip()
        if stripped.startswith(("{", "[")):
            try:
                return json.loads(payload)
            except json.JSONDecodeError as exc:
                raise ContextParseError(f"invalid Luogu JSON payload: {exc}") from exc
        # Reuse the existing strict public-page parser without broadening it.
        try:
            from .platforms import parse_lentille_context

            return parse_lentille_context(payload)
        except Exception as exc:
            raise ContextParseError(f"invalid Luogu public problem page: {exc}") from exc
    return payload


def _statement_candidates(node: Any, *, banned: bool = False) -> list[Mapping[str, Any]]:
    result: list[Mapping[str, Any]] = []
    if isinstance(node, Mapping):
        if banned:
            return result
        keys = {str(key) for key in node}
        if keys & _LUOGU_CONTENT_KEYS and _is_statement_candidate(node):
            result.append(node)
        nested_content = node.get("content")
        if isinstance(nested_content, Mapping) and _is_statement_candidate(nested_content):
            # Luogu normally keeps samples beside ``content``, not inside it.
            merged = dict(nested_content)
            for sibling in ("samples", "sample", "title", "name"):
                if sibling in node:
                    merged[sibling] = node[sibling]
            result.append(merged)
        for key, value in node.items():
            result.extend(
                _statement_candidates(
                    value,
                    banned=_is_banned_payload_key(key),
                )
            )
    elif isinstance(node, Sequence) and not isinstance(node, (str, bytes, bytearray)):
        for value in node:
            result.extend(_statement_candidates(value, banned=banned))
    return result


def _sample_blocks(samples: Any) -> list[str]:
    if not isinstance(samples, Sequence) or isinstance(samples, (str, bytes, bytearray)):
        return []
    result: list[str] = []
    for index, item in enumerate(samples, 1):
        sample_input: Any = None
        sample_output: Any = None
        if isinstance(item, Mapping):
            sample_input = item.get("input") or item.get("in")
            sample_output = item.get("output") or item.get("out")
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes, bytearray)):
            if len(item) >= 2:
                sample_input, sample_output = item[0], item[1]
        if not isinstance(sample_input, str) or not isinstance(sample_output, str):
            continue
        result.extend(
            [
                f"### 样例输入 {index}",
                "```text",
                sample_input.strip("\r\n"),
                "```",
                f"### 样例输出 {index}",
                "```text",
                sample_output.strip("\r\n"),
                "```",
            ]
        )
    return result


_FENCED_SAMPLE_RE = re.compile(
    r"(?ms)^#{2,4}\s*(?:样例|sample)\s*(?:输入|input)\s*(\d*)\s*$"
    r"\s*```(?:text)?\s*\n(.*?)\n```\s*"
    r"^#{2,4}\s*(?:样例|sample)\s*(?:输出|output)\s*(\d*)\s*$"
    r"\s*```(?:text)?\s*\n(.*?)\n```\s*",
    re.IGNORECASE,
)


def extract_statement_samples(statement: str) -> list[dict[str, str]]:
    """Extract paired fenced samples already admitted into statement text.

    The public parsers deliberately flatten remote payloads into a narrow
    Markdown statement.  This second deterministic pass recovers only explicit
    input/output fenced pairs; it never guesses from prose or example code.
    """

    if not isinstance(statement, str) or not statement.strip():
        return []
    samples: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for ordinal, match in enumerate(_FENCED_SAMPLE_RE.finditer(statement), 1):
        left_index, sample_input, right_index, sample_output = match.groups()
        if left_index and right_index and left_index != right_index:
            continue
        pair = (
            sample_input.replace("\r\n", "\n").replace("\r", "\n").strip("\n"),
            sample_output.replace("\r\n", "\n").replace("\r", "\n").strip("\n"),
        )
        if pair in seen:
            continue
        seen.add(pair)
        samples.append(
            {
                "name": f"sample{left_index or right_index or ordinal}",
                "input": pair[0] + "\n",
                "output": pair[1] + "\n",
            }
        )
    return samples


def extract_luogu_statement(payload: Any) -> str:
    """Extract only whitelisted statement fields from a Luogu public payload."""
    decoded = _decode_luogu_payload(payload)
    candidates = _statement_candidates(decoded)
    if not candidates:
        raise ContextParseError("Luogu payload has no recognizable statement fields")

    def score(candidate: Mapping[str, Any]) -> int:
        return sum(
            len(value.encode("utf-8"))
            for keys, _ in _LUOGU_SECTION_FIELDS
            if (value := _first_text(candidate, keys)) is not None
        )

    content = dict(max(candidates, key=score))
    # Translated statements can be more complete than the root locale while
    # Luogu keeps official samples only on the parent problem object.  Preserve
    # the highest-scoring prose, then attach samples from another whitelisted
    # statement candidate instead of silently dropping them.
    if not (content.get("samples") or content.get("sample")):
        sample_source = next(
            (
                candidate
                for candidate in candidates
                if candidate.get("samples") or candidate.get("sample")
            ),
            None,
        )
        if sample_source is not None:
            for key in ("samples", "sample"):
                if sample_source.get(key):
                    content[key] = sample_source[key]
                    break
    blocks: list[str] = []
    for keys, heading in _LUOGU_SECTION_FIELDS:
        value = _first_text(content, keys)
        if value is not None:
            blocks.extend([f"## {heading}", value.strip()])
        if keys[0] == "outputFormat":
            blocks.extend(_sample_blocks(content.get("samples") or content.get("sample")))
    statement = "\n\n".join(block for block in blocks if block != "")
    if not statement:
        raise ContextParseError("Luogu statement fields are empty")
    return statement


def parse_problem_statement(platform: str, payload: Any) -> str:
    """Dispatch a fetched public payload through the platform's narrow parser."""
    normalized = str(platform).strip().casefold()
    if normalized in {"codeforces", "cf"}:
        return extract_codeforces_statement(payload)
    if normalized in {"luogu", "lg"}:
        return extract_luogu_statement(payload)
    raise ContextParseError(f"unsupported problem-context platform: {platform}")


def content_sha256(content: str | bytes) -> str:
    data = content.encode("utf-8") if isinstance(content, str) else bytes(content)
    return hashlib.sha256(data).hexdigest()


def validate_manual_context(content: str, *, max_bytes: int = CONTEXT_MAX_BYTES) -> str:
    if not isinstance(content, str):
        raise ContextValidationError("manual problem context must be text")
    if "\x00" in content:
        raise ContextValidationError("manual problem context must not contain NUL")
    if not content.strip():
        raise ContextValidationError("manual problem context must not be empty")
    if len(content.encode("utf-8")) > max_bytes:
        raise ContextValidationError(f"manual problem context exceeds {max_bytes} bytes")
    return content


def _as_utc(value: datetime | str) -> datetime:
    if isinstance(value, str):
        candidate = value.strip()
        if candidate.endswith("Z"):
            candidate = candidate[:-1] + "+00:00"
        value = datetime.fromisoformat(candidate)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def is_context_fresh(
    fetched_at: datetime | str | None,
    *,
    now: datetime | None = None,
    ttl_days: int = CONTEXT_TTL_DAYS,
) -> bool:
    if fetched_at is None or ttl_days < 0:
        return False
    try:
        fetched = _as_utc(fetched_at)
        current = _as_utc(now or datetime.now(timezone.utc))
    except (TypeError, ValueError):
        return False
    age = current - fetched
    return timedelta(0) <= age < timedelta(days=ttl_days)


def validate_managed_cpp(root: str | Path, source_path: str | Path) -> Path:
    """Resolve and validate exactly ``ROOT/YYYY/M/D/*.cpp`` (no traversal)."""
    root_path = Path(root).resolve()
    candidate = Path(source_path)
    if not candidate.is_absolute():
        candidate = root_path / candidate
    resolved = candidate.resolve(strict=False)
    try:
        relative = resolved.relative_to(root_path)
    except ValueError as exc:
        raise SourceValidationError("source path escapes the ACM workspace") from exc
    if len(relative.parts) != 4 or resolved.suffix.casefold() != ".cpp":
        raise SourceValidationError("source must match YYYY/M/D/*.cpp under the ACM workspace")
    year_raw, month_raw, day_raw, filename = relative.parts
    if not (year_raw.isdigit() and month_raw.isdigit() and day_raw.isdigit() and filename):
        raise SourceValidationError("source must match YYYY/M/D/*.cpp under the ACM workspace")
    if len(year_raw) != 4:
        raise SourceValidationError("managed source year must use four digits")
    try:
        date(int(year_raw), int(month_raw), int(day_raw))
    except ValueError as exc:
        raise SourceValidationError("managed source date is invalid") from exc
    return resolved


def validate_cpp_source(source: str | bytes, *, max_bytes: int = SOURCE_MAX_BYTES) -> str:
    if isinstance(source, bytes):
        try:
            source = source.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SourceValidationError("C++ source must be valid UTF-8") from exc
    if not isinstance(source, str):
        raise SourceValidationError("C++ source must be text")
    if "\x00" in source:
        raise SourceValidationError("C++ source must not contain NUL")
    if not source.strip():
        raise SourceValidationError("C++ source must not be empty")
    if len(source.encode("utf-8")) > max_bytes:
        raise SourceValidationError(f"C++ source exceeds {max_bytes} bytes")
    return source


def validate_model_replacement(source: str, *, max_bytes: int = SOURCE_MAX_BYTES) -> str:
    source = validate_cpp_source(source, max_bytes=max_bytes)
    if re.search(r"(^|\n)\s*```", source):
        raise SourceValidationError("model replacement must be plain source without Markdown fences")
    return source


def _cpp_comment_bodies(source: str) -> list[str]:
    """Extract C++ comments while ignoring comment markers in strings/chars."""
    comments: list[str] = []
    index = 0
    length = len(source)
    while index < length:
        char = source[index]
        if char in {'"', "'"}:
            quote = char
            index += 1
            while index < length:
                if source[index] == "\\":
                    index += 2
                    continue
                if source[index] == quote:
                    index += 1
                    break
                index += 1
            continue
        if source.startswith("//", index):
            end = source.find("\n", index + 2)
            if end < 0:
                end = length
            comments.append(source[index + 2 : end])
            index = end
            continue
        if source.startswith("/*", index):
            end = source.find("*/", index + 2)
            if end < 0:
                comments.append(source[index + 2 :])
                break
            comments.append(source[index + 2 : end])
            index = end + 2
            continue
        index += 1
    return comments


def validate_patch_explanatory_comments(original: str, replacement: str) -> str:
    """Require at least one meaningful new source comment in an AI replacement."""
    original = validate_cpp_source(original)
    replacement = validate_model_replacement(replacement)

    def normalized_comments(source: str) -> set[str]:
        return {
            re.sub(r"\s+", " ", body).strip()
            for body in _cpp_comment_bodies(source)
            if len(re.sub(r"\s+", "", body)) >= 4
        }

    before = normalized_comments(original)
    added = normalized_comments(replacement) - before
    if not added:
        raise SourceValidationError(
            "replacement_code 必须在修改处加入新注释，说明原代码错误和修复原因"
        )
    return replacement


def unified_source_diff(original: str, replacement: str, *, path: str = "solution.cpp") -> str:
    original = validate_cpp_source(original)
    replacement = validate_model_replacement(replacement)
    return "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            replacement.splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
    )


def _atomic_write(path: Path, data: bytes, *, mode: int | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if mode is not None:
            os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def _safe_backup_id(value: str | None, original_hash: str) -> str:
    if value is None:
        return original_hash[:16]
    value = str(value).strip()
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}", value):
        raise SourceValidationError("backup_id contains unsafe characters")
    return value


def expected_patch_backup_path(
    root: str | Path,
    source_path: str | Path,
    *,
    backup_id: str | None,
    original_sha256: str,
) -> Path:
    root_path = Path(root).resolve()
    source = validate_managed_cpp(root_path, source_path)
    token = _safe_backup_id(backup_id, original_sha256)
    return (
        root_path
        / ".acm"
        / "ai-backups"
        / f"{source.stem}.{token}.{original_sha256[:12]}.cpp.bak"
    )


def apply_source_patch(
    root: str | Path,
    source_path: str | Path,
    replacement: str,
    *,
    expected_sha256: str,
    backup_id: str | None = None,
) -> PatchApplyResult:
    """Atomically replace a managed source after a baseline-hash check."""
    source = validate_managed_cpp(root, source_path)
    if not source.is_file():
        raise SourceValidationError("managed source does not exist")
    original_bytes = source.read_bytes()
    original = validate_cpp_source(original_bytes)
    actual_hash = content_sha256(original_bytes)
    if actual_hash != str(expected_sha256).casefold():
        raise PatchConflictError("source changed after patch preview")
    replacement = validate_model_replacement(replacement)
    replacement_bytes = replacement.encode("utf-8")
    applied_hash = content_sha256(replacement_bytes)
    relative = source.relative_to(Path(root).resolve()).as_posix()
    diff = unified_source_diff(original, replacement, path=relative)

    backup = expected_patch_backup_path(
        root, source, backup_id=backup_id, original_sha256=actual_hash
    )
    if backup.exists():
        if backup.read_bytes() != original_bytes:
            raise SourceValidationError("backup name already exists with different content")
    else:
        _atomic_write(backup, original_bytes)
    mode = source.stat().st_mode
    _atomic_write(source, replacement_bytes, mode=mode)
    return PatchApplyResult(source, backup, actual_hash, applied_hash, diff)


def _validate_backup_path(root: Path, backup_path: str | Path) -> Path:
    backup_root = (root / ".acm" / "ai-backups").resolve()
    candidate = Path(backup_path)
    if not candidate.is_absolute():
        candidate = root / candidate
    resolved = candidate.resolve(strict=False)
    try:
        resolved.relative_to(backup_root)
    except ValueError as exc:
        raise SourceValidationError("backup path escapes .acm/ai-backups") from exc
    if not resolved.is_file():
        raise SourceValidationError("patch backup does not exist")
    return resolved


def revert_source_patch(
    root: str | Path,
    source_path: str | Path,
    backup_path: str | Path,
    *,
    expected_applied_sha256: str,
    expected_baseline_sha256: str,
) -> PatchRevertResult:
    """Restore a backup only while the source still equals the AI-applied version."""
    root_path = Path(root).resolve()
    source = validate_managed_cpp(root_path, source_path)
    if not source.is_file():
        raise SourceValidationError("managed source does not exist")
    current_bytes = source.read_bytes()
    validate_cpp_source(current_bytes)
    current_hash = content_sha256(current_bytes)
    if current_hash != str(expected_applied_sha256).casefold():
        raise PatchConflictError("source changed after AI patch application")
    backup = _validate_backup_path(root_path, backup_path)
    restored_bytes = backup.read_bytes()
    validate_cpp_source(restored_bytes)
    restored_hash = content_sha256(restored_bytes)
    if restored_hash != str(expected_baseline_sha256).casefold():
        raise PatchConflictError("patch backup no longer matches the preview baseline")
    mode = source.stat().st_mode
    _atomic_write(source, restored_bytes, mode=mode)
    return PatchRevertResult(source, backup, current_hash, restored_hash)


__all__ = [
    "AIContextError",
    "CONTEXT_MAX_BYTES",
    "CONTEXT_TTL_DAYS",
    "ContextParseError",
    "ContextValidationError",
    "PatchApplyResult",
    "PatchConflictError",
    "PatchRevertResult",
    "SOURCE_MAX_BYTES",
    "SourceValidationError",
    "apply_source_patch",
    "content_sha256",
    "extract_codeforces_statement",
    "extract_luogu_statement",
    "expected_patch_backup_path",
    "is_context_fresh",
    "parse_problem_statement",
    "revert_source_patch",
    "unified_source_diff",
    "validate_cpp_source",
    "validate_managed_cpp",
    "validate_manual_context",
    "validate_model_replacement",
    "validate_patch_explanatory_comments",
]
