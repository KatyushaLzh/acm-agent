"""Deterministic Markdown knowledge-card rendering and preview support.

This module intentionally contains no model or database code.  A model may
produce a structured entry, but only the functions here are allowed to turn it
into a candidate Markdown file.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "summary-schema-v1"
MAX_MARKDOWN_BYTES = 1024 * 1024
UTF8_BOM = b"\xef\xbb\xbf"
PRESET_NAMES = ("algorithms-v1", "tricks-v1")

_TEMPLATE_DIR = Path(__file__).with_name("knowledge_templates")
_HEADING_RE = re.compile(r"^(#{1,6})[ \t]+(.+?)[ \t]*$")
_BULLET_FIELD_RE = re.compile(
    r"^[ \t]*[-*+][ \t]+(?:\*\*)?([^:\n*]+?)(?:\*\*)?[ \t]*:[ \t]*(.*)$"
)
_PLAIN_FIELD_RE = re.compile(r"^[ \t]*(?:\*\*)?([^:\n*]+?)(?:\*\*)?[ \t]*:[ \t]*(.*)$")
_FENCE_RE = re.compile(r"^[ \t]*(```|~~~)")
_HTML_RE = re.compile(
    r"<!--[\s\S]*?-->|<!DOCTYPE\b|</?(?:a|abbr|address|article|aside|audio|b|base|blockquote|body|br|button|canvas|caption|code|col|data|details|dialog|div|dl|em|embed|fieldset|figure|footer|form|h[1-6]|head|header|hr|html|i|iframe|img|input|label|li|link|main|mark|meta|nav|object|ol|option|p|picture|pre|script|section|select|small|source|span|strong|style|summary|table|tbody|td|template|textarea|tfoot|th|thead|time|title|tr|track|u|ul|video)(?:\s[^>]*)?/?>",
    re.IGNORECASE,
)
_KNOWN_FIELD_KEYS = {
    "source": "source",
    "model": "model",
    "invariant / correctness": "correctness",
    "invariant/correctness": "correctness",
    "correctness": "correctness",
    "implementation": "implementation",
    "complexity": "complexity",
    "pitfalls": "pitfalls",
    "trigger": "trigger",
    "conclusion / proof": "conclusion",
    "conclusion/proof": "conclusion",
    "结论 / 证明": "conclusion",
    "来源": "source",
    "模型": "model",
    "正确性": "correctness",
    "实现": "implementation",
    "复杂度": "complexity",
    "易错点": "pitfalls",
    "触发条件": "trigger",
}


class KnowledgeError(ValueError):
    """Base class for deterministic knowledge-file failures."""


class MarkdownPathError(KnowledgeError):
    pass


class MarkdownContentError(KnowledgeError):
    pass


class MarkdownHashMismatch(KnowledgeError):
    pass


class SchemaValidationError(KnowledgeError):
    pass


class EntryValidationError(KnowledgeError):
    pass


class DuplicateDecisionRequired(KnowledgeError):
    def __init__(self, matches: Sequence["DuplicateMatch"]):
        self.matches = tuple(matches)
        labels = ", ".join(match.title for match in self.matches)
        super().__init__(f"fuzzy duplicate requires merge/new decision: {labels}")


@dataclass(frozen=True)
class MarkdownDocument:
    path: Path
    exists: bool
    raw: bytes
    text: str
    has_bom: bool
    newline: str
    ends_with_newline: bool
    baseline_sha256: str


@dataclass(frozen=True)
class SchemaInference:
    schema: dict[str, Any]
    confidence: float
    stable: bool
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class DuplicateMatch:
    title: str
    kind: str
    score: float
    start: int
    end: int


@dataclass(frozen=True)
class DuplicateDiagnosis:
    exact: tuple[DuplicateMatch, ...]
    fuzzy: tuple[DuplicateMatch, ...]


@dataclass(frozen=True)
class ExactSourceEntry:
    title: str
    markdown: str
    start: int
    end: int


@dataclass(frozen=True)
class CandidateResult:
    candidate_bytes: bytes
    unified_diff: str
    baseline_sha256: str
    candidate_sha256: str
    rendered_entry: str
    action: str
    target_existed: bool
    duplicate_diagnosis: DuplicateDiagnosis


@dataclass(frozen=True)
class _Heading:
    level: int
    title: str
    start: int
    line_end: int
    end: int


def _load_json_resource(name: str) -> dict[str, Any]:
    try:
        payload = json.loads((_TEMPLATE_DIR / name).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SchemaValidationError(f"cannot load built-in schema {name}: {exc}") from exc
    return validate_summary_schema(payload)


def get_builtin_schema(preset: str) -> dict[str, Any]:
    """Return a validated, detached built-in schema."""

    normalized = str(preset).strip().casefold()
    if normalized not in PRESET_NAMES:
        raise SchemaValidationError(f"unknown knowledge preset: {preset}")
    return _load_json_resource(f"{normalized}.json")


def get_builtin_template(preset: str) -> str:
    normalized = str(preset).strip().casefold()
    if normalized not in PRESET_NAMES:
        raise SchemaValidationError(f"unknown knowledge preset: {preset}")
    try:
        return (_TEMPLATE_DIR / f"{normalized}.md").read_text(encoding="utf-8")
    except OSError as exc:
        raise MarkdownContentError(f"cannot load built-in template {preset}: {exc}") from exc


def list_builtin_templates() -> list[dict[str, Any]]:
    return [
        {
            "preset": name,
            "schema": get_builtin_schema(name),
            "template": get_builtin_template(name),
        }
        for name in PRESET_NAMES
    ]


def schema_sha256(schema: Mapping[str, Any]) -> str:
    normalized = validate_summary_schema(schema)
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def validate_summary_schema(schema: Mapping[str, Any]) -> dict[str, Any]:
    """Validate and normalize the non-executable ``summary-schema-v1`` format."""

    if not isinstance(schema, Mapping):
        raise SchemaValidationError("schema must be an object")
    allowed = {
        "version", "name", "category_heading_level", "entry_heading_level",
        "toc", "fields", "blank_lines_between_fields", "blank_lines_between_entries",
    }
    unknown = set(schema) - allowed
    if unknown:
        raise SchemaValidationError(f"unknown schema keys: {', '.join(sorted(unknown))}")
    if schema.get("version") != SCHEMA_VERSION:
        raise SchemaValidationError(f"schema.version must be {SCHEMA_VERSION}")
    name = _single_line(schema.get("name", "自定义总结"), "schema.name", max_length=80)
    category_level = _bounded_int(schema.get("category_heading_level"), "category_heading_level", 1, 5)
    entry_level = _bounded_int(schema.get("entry_heading_level"), "entry_heading_level", 2, 6)
    if entry_level <= category_level:
        raise SchemaValidationError("entry_heading_level must be deeper than category_heading_level")
    toc = str(schema.get("toc", "preserve")).strip().casefold()
    if toc not in {"typora", "none", "preserve"}:
        raise SchemaValidationError("toc must be typora, none, or preserve")
    between_fields = _bounded_int(
        schema.get("blank_lines_between_fields", 0), "blank_lines_between_fields", 0, 2
    )
    between_entries = _bounded_int(
        schema.get("blank_lines_between_entries", 1), "blank_lines_between_entries", 1, 3
    )
    raw_fields = schema.get("fields")
    if not isinstance(raw_fields, list) or not raw_fields:
        raise SchemaValidationError("schema.fields must be a non-empty array")
    if len(raw_fields) > 24:
        raise SchemaValidationError("schema.fields may contain at most 24 fields")
    normalized_fields: list[dict[str, Any]] = []
    keys: set[str] = set()
    labels: set[str] = set()
    for index, raw_field in enumerate(raw_fields):
        if not isinstance(raw_field, Mapping):
            raise SchemaValidationError(f"fields[{index}] must be an object")
        field_allowed = {"key", "label", "required", "layout", "instruction"}
        field_unknown = set(raw_field) - field_allowed
        if field_unknown:
            raise SchemaValidationError(
                f"unknown fields[{index}] keys: {', '.join(sorted(field_unknown))}"
            )
        key = str(raw_field.get("key", "")).strip()
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,39}", key):
            raise SchemaValidationError(f"fields[{index}].key is invalid")
        if key in keys:
            raise SchemaValidationError(f"duplicate field key: {key}")
        label = _single_line(raw_field.get("label"), f"fields[{index}].label", max_length=80)
        label_key = label.casefold()
        if label_key in labels:
            raise SchemaValidationError(f"duplicate field label: {label}")
        required = raw_field.get("required", False)
        if not isinstance(required, bool):
            raise SchemaValidationError(f"fields[{index}].required must be boolean")
        layout = str(raw_field.get("layout", "bullet")).strip().casefold()
        if layout not in {"bullet", "subheading"}:
            raise SchemaValidationError(f"fields[{index}].layout must be bullet or subheading")
        instruction = str(raw_field.get("instruction", "")).strip()
        if len(instruction) > 500 or _contains_html(instruction):
            raise SchemaValidationError(f"fields[{index}].instruction is invalid")
        keys.add(key)
        labels.add(label_key)
        normalized_fields.append(
            {
                "key": key,
                "label": label,
                "required": required,
                "layout": layout,
                "instruction": instruction,
            }
        )
    return {
        "version": SCHEMA_VERSION,
        "name": name,
        "category_heading_level": category_level,
        "entry_heading_level": entry_level,
        "toc": toc,
        "fields": normalized_fields,
        "blank_lines_between_fields": between_fields,
        "blank_lines_between_entries": between_entries,
    }


def validate_markdown_path(path: str | os.PathLike[str], *, allow_create: bool = False) -> Path:
    """Resolve a local Markdown path and reject indirection/remote path forms."""

    raw = os.fspath(path)
    if not isinstance(raw, str) or not raw.strip() or "\x00" in raw:
        raise MarkdownPathError("Markdown path is empty or contains NUL")
    raw = raw.strip()
    slash = raw.replace("/", "\\")
    if slash.startswith("\\\\") or slash.casefold().startswith(("\\\\?\\", "\\\\.\\")):
        raise MarkdownPathError("UNC and device paths are not allowed")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise MarkdownPathError("Markdown path must be absolute")
    if candidate.suffix.casefold() != ".md":
        raise MarkdownPathError("knowledge target must use the .md extension")
    absolute = Path(os.path.abspath(candidate))
    _reject_reparse_components(absolute)
    if absolute.exists():
        try:
            mode = absolute.stat().st_mode
        except OSError as exc:
            raise MarkdownPathError(f"cannot inspect Markdown path: {exc}") from exc
        if not stat.S_ISREG(mode):
            raise MarkdownPathError("knowledge target must be a regular file")
    elif not allow_create:
        raise MarkdownPathError("Markdown target does not exist")
    else:
        parent = absolute.parent
        if not parent.exists() or not parent.is_dir():
            raise MarkdownPathError("parent directory does not exist")
    return absolute


def inspect_markdown_path(
    path: str | os.PathLike[str],
    *,
    allow_create: bool = False,
    initial_text: str | None = None,
) -> MarkdownDocument:
    """Read and validate an existing target, or describe a confirmed new one."""

    target = validate_markdown_path(path, allow_create=allow_create)
    exists = target.exists()
    if exists:
        try:
            raw = target.read_bytes()
        except OSError as exc:
            raise MarkdownContentError(f"cannot read Markdown target: {exc}") from exc
    else:
        if initial_text is None:
            raise MarkdownContentError("new Markdown target requires confirmed initial_text")
        if _contains_html(initial_text):
            raise MarkdownContentError("raw HTML is not allowed in a knowledge template")
        raw = initial_text.encode("utf-8")
    if len(raw) > MAX_MARKDOWN_BYTES:
        raise MarkdownContentError("Markdown target exceeds the 1 MiB limit")
    if b"\x00" in raw:
        raise MarkdownContentError("Markdown target contains NUL")
    has_bom = raw.startswith(UTF8_BOM)
    payload = raw[len(UTF8_BOM):] if has_bom else raw
    try:
        text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MarkdownContentError("Markdown target must be UTF-8 or UTF-8 BOM") from exc
    newline = _dominant_newline(text)
    return MarkdownDocument(
        path=target,
        exists=exists,
        raw=raw,
        text=text,
        has_bom=has_bom,
        newline=newline,
        ends_with_newline=text.endswith(("\n", "\r")),
        baseline_sha256=hashlib.sha256(raw).hexdigest(),
    )


def readback_markdown(
    path: str | os.PathLike[str], *, expected_sha256: str | None = None
) -> MarkdownDocument:
    """Safely read an applied target and optionally verify its exact bytes."""

    document = inspect_markdown_path(path)
    if expected_sha256 is not None:
        expected = str(expected_sha256).strip().casefold()
        if not re.fullmatch(r"[0-9a-f]{64}", expected):
            raise MarkdownHashMismatch("expected_sha256 must be 64 lowercase/uppercase hex digits")
        if document.baseline_sha256 != expected:
            raise MarkdownHashMismatch(
                f"Markdown readback hash mismatch: expected {expected}, got {document.baseline_sha256}"
            )
    return document


def markdown_bytes_sha256(payload: bytes) -> str:
    if not isinstance(payload, bytes):
        raise MarkdownContentError("Markdown hash payload must be bytes")
    return hashlib.sha256(payload).hexdigest()


def infer_summary_schema(text_or_document: str | MarkdownDocument) -> SchemaInference:
    """Infer a conservative schema from repeated Markdown entry cards."""

    text = text_or_document.text if isinstance(text_or_document, MarkdownDocument) else str(text_or_document)
    headings = _scan_headings(text)
    useful = [heading for heading in headings if _normalize_title(heading.title) not in {"toc", "目录"}]
    warnings: list[str] = []
    if len(useful) < 2:
        warnings.append("没有足够的标题层级来稳定推断 schema")
        fallback = get_builtin_schema("algorithms-v1")
        fallback["name"] = "自动推断（低置信度）"
        fallback["toc"] = "typora" if "[TOC]" in text else "preserve"
        return SchemaInference(fallback, 0.2, False, tuple(warnings))

    pairs: dict[tuple[int, int], int] = {}
    for category in useful:
        descendants = [h for h in useful if h.start > category.start and h.start < category.end and h.level > category.level]
        if descendants:
            pair = (category.level, min(h.level for h in descendants))
            pairs[pair] = pairs.get(pair, 0) + 1
    if not pairs:
        warnings.append("无法识别专题标题与条目标题")
        fallback = get_builtin_schema("algorithms-v1")
        fallback["name"] = "自动推断（低置信度）"
        return SchemaInference(fallback, 0.25, False, tuple(warnings))
    category_level, entry_level = max(pairs.items(), key=lambda item: (item[1], -item[0][0]))[0]
    entries = [heading for heading in useful if heading.level == entry_level]
    observed: dict[str, dict[str, Any]] = {}
    entries_with_fields = 0
    field_gap_samples: list[int] = []
    for heading in entries[:40]:
        body = text[heading.line_end:heading.end]
        fields = _extract_fields(body, entry_level)
        if fields:
            entries_with_fields += 1
        previous_line: int | None = None
        for label, layout, line_index in fields:
            normalized_label = label.strip()
            key = normalized_label.casefold()
            slot = observed.setdefault(
                key, {"label": normalized_label, "count": 0, "layouts": {}, "first": len(observed)}
            )
            slot["count"] += 1
            slot["layouts"][layout] = slot["layouts"].get(layout, 0) + 1
            if previous_line is not None:
                field_gap_samples.append(max(0, line_index - previous_line - 1))
            previous_line = line_index
    if not observed:
        warnings.append("条目中没有可识别的字段标签")
        fallback = get_builtin_schema("algorithms-v1")
        fallback.update(
            {
                "name": "自动推断（低置信度）",
                "category_heading_level": category_level,
                "entry_heading_level": entry_level,
                "toc": "typora" if "[TOC]" in text else "preserve",
            }
        )
        return SchemaInference(validate_summary_schema(fallback), 0.35, False, tuple(warnings))

    denominator = max(1, entries_with_fields)
    selected = [slot for slot in observed.values() if slot["count"] / denominator >= 0.5]
    if not selected:
        selected = sorted(observed.values(), key=lambda slot: (-slot["count"], slot["first"]))[:8]
        warnings.append("字段在不同条目间不稳定，仅采用最常见字段")
    selected.sort(key=lambda slot: slot["first"])
    used_keys: set[str] = set()
    fields: list[dict[str, Any]] = []
    for slot in selected:
        key = _field_key(slot["label"], used_keys)
        used_keys.add(key)
        layout = max(slot["layouts"].items(), key=lambda item: item[1])[0]
        fields.append(
            {
                "key": key,
                "label": slot["label"],
                "required": slot["count"] / denominator >= 0.8,
                "layout": layout,
                "instruction": "",
            }
        )
    field_coverage = sum(min(1.0, slot["count"] / denominator) for slot in selected) / len(selected)
    structural_coverage = min(1.0, entries_with_fields / max(1, len(entries)))
    confidence = round(min(0.98, 0.45 + 0.3 * structural_coverage + 0.25 * field_coverage), 3)
    stable = confidence >= 0.75 and len(entries) >= 1
    if not stable:
        warnings.append("推断置信度低于 0.75，写入前应由用户确认 schema")
    gap = 1 if field_gap_samples and sum(field_gap_samples) / len(field_gap_samples) >= 0.5 else 0
    schema = validate_summary_schema(
        {
            "version": SCHEMA_VERSION,
            "name": "从现有 Markdown 自动推断",
            "category_heading_level": category_level,
            "entry_heading_level": entry_level,
            "toc": "typora" if "[TOC]" in text else "preserve",
            "fields": fields,
            "blank_lines_between_fields": gap,
            "blank_lines_between_entries": 1,
        }
    )
    return SchemaInference(schema, confidence, stable, tuple(warnings))


def validate_structured_entry(entry: Mapping[str, Any], schema: Mapping[str, Any]) -> dict[str, Any]:
    normalized_schema = validate_summary_schema(schema)
    if not isinstance(entry, Mapping):
        raise EntryValidationError("entry must be an object")
    allowed = {"topic", "title", "aliases", "confidence", "fields", "rationale"}
    unknown = set(entry) - allowed
    if unknown:
        raise EntryValidationError(f"unknown entry keys: {', '.join(sorted(unknown))}")
    topic = _entry_heading(entry.get("topic"), "entry.topic")
    title = _entry_heading(entry.get("title"), "entry.title")
    aliases_raw = entry.get("aliases", [])
    if not isinstance(aliases_raw, list) or len(aliases_raw) > 20:
        raise EntryValidationError("entry.aliases must be an array with at most 20 values")
    aliases: list[str] = []
    seen_aliases: set[str] = set()
    for index, alias_raw in enumerate(aliases_raw):
        alias = _entry_heading(alias_raw, f"entry.aliases[{index}]")
        key = _normalize_title(alias)
        if key and key != _normalize_title(title) and key not in seen_aliases:
            aliases.append(alias)
            seen_aliases.add(key)
    confidence_raw = entry.get("confidence", 1.0)
    if isinstance(confidence_raw, bool) or not isinstance(confidence_raw, (int, float)):
        raise EntryValidationError("entry.confidence must be a number")
    confidence = float(confidence_raw)
    if not 0 <= confidence <= 1:
        raise EntryValidationError("entry.confidence must be between 0 and 1")
    raw_fields = entry.get("fields")
    if not isinstance(raw_fields, Mapping):
        raise EntryValidationError("entry.fields must be an object")
    known_keys = {field["key"] for field in normalized_schema["fields"]}
    extra_keys = set(raw_fields) - known_keys
    if extra_keys:
        raise EntryValidationError(f"entry contains unknown fields: {', '.join(sorted(extra_keys))}")
    fields: dict[str, str] = {}
    for field in normalized_schema["fields"]:
        value = raw_fields.get(field["key"], "")
        if value is None:
            value = ""
        if not isinstance(value, str):
            raise EntryValidationError(f"field {field['key']} must be a string")
        value = value.strip()
        if len(value.encode("utf-8")) > 64 * 1024:
            raise EntryValidationError(f"field {field['key']} exceeds 64 KiB")
        if _contains_html(value):
            raise EntryValidationError(f"field {field['key']} contains raw HTML")
        if field["required"] and not value:
            raise EntryValidationError(f"required field is empty: {field['key']}")
        fields[field["key"]] = value
    rationale = str(entry.get("rationale", "")).strip()
    if len(rationale.encode("utf-8")) > 16 * 1024 or _contains_html(rationale):
        raise EntryValidationError("entry.rationale is invalid")
    return {
        "topic": topic,
        "title": title,
        "aliases": aliases,
        "confidence": confidence,
        "fields": fields,
        "rationale": rationale,
    }


def render_structured_entry(
    entry: Mapping[str, Any], schema: Mapping[str, Any], *, newline: str = "\n"
) -> str:
    normalized_schema = validate_summary_schema(schema)
    normalized_entry = validate_structured_entry(entry, normalized_schema)
    if newline not in {"\n", "\r\n"}:
        raise EntryValidationError("newline must be LF or CRLF")
    blocks = ["#" * normalized_schema["entry_heading_level"] + " " + normalized_entry["title"]]
    field_blocks: list[str] = []
    for field in normalized_schema["fields"]:
        value = normalized_entry["fields"].get(field["key"], "")
        if not value:
            continue
        lines = value.splitlines() or [""]
        if field["layout"] == "bullet":
            rendered = [f"- {field['label']}: {lines[0]}"]
            rendered.extend(f"  {line}" if line else "" for line in lines[1:])
        else:
            heading_level = min(6, normalized_schema["entry_heading_level"] + 1)
            rendered = ["#" * heading_level + " " + field["label"], "", *lines]
        field_blocks.append(newline.join(rendered))
    if field_blocks:
        separator = newline * (normalized_schema["blank_lines_between_fields"] + 1)
        blocks.append(separator.join(field_blocks))
    return (newline * 2).join(blocks)


def parse_rendered_entry(
    rendered_markdown: str,
    schema: Mapping[str, Any],
    *,
    topic: str,
    aliases: Sequence[str] = (),
    confidence: float = 1.0,
    rationale: str = "",
) -> dict[str, Any]:
    """Parse a user-edited single entry back into the structured protocol.

    The parser accepts only the field markers declared by the schema and one
    entry-level heading.  It is deliberately not a general Markdown parser.
    """

    normalized_schema = validate_summary_schema(schema)
    if not isinstance(rendered_markdown, str):
        raise EntryValidationError("rendered_markdown must be a string")
    if len(rendered_markdown.encode("utf-8")) > 64 * 1024:
        raise EntryValidationError("rendered entry exceeds 64 KiB")
    if "\x00" in rendered_markdown or _contains_html(rendered_markdown):
        raise EntryValidationError("rendered entry contains NUL or raw HTML")
    normalized = rendered_markdown.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    headings = _scan_headings(normalized)
    entry_level = normalized_schema["entry_heading_level"]
    top = [heading for heading in headings if heading.level <= entry_level]
    if len(top) != 1 or top[0].start != 0 or top[0].level != entry_level:
        raise EntryValidationError("rendered entry must contain exactly one entry-level heading")
    title = top[0].title
    lines = normalized.splitlines()
    marker_by_line: dict[int, tuple[dict[str, Any], str]] = {}
    seen: set[str] = set()
    declared_labels = {field["label"].casefold(): field for field in normalized_schema["fields"]}
    for index, line in enumerate(lines[1:], start=1):
        top_level_bullet = line.startswith(("-", "*", "+"))
        bullet_match = _BULLET_FIELD_RE.match(line) if top_level_bullet else None
        if bullet_match:
            declared = declared_labels.get(bullet_match.group(1).strip().casefold())
            if declared is None:
                raise EntryValidationError(
                    f"unknown rendered field label: {bullet_match.group(1).strip()}"
                )
            if declared["layout"] != "bullet":
                raise EntryValidationError(f"rendered field uses wrong layout: {declared['key']}")
        heading_match = _HEADING_RE.match(line)
        if heading_match and len(heading_match.group(1)) == min(6, entry_level + 1):
            declared = declared_labels.get(heading_match.group(2).strip().casefold())
            if declared is None:
                raise EntryValidationError(
                    f"unknown rendered field label: {heading_match.group(2).strip()}"
                )
            if declared["layout"] != "subheading":
                raise EntryValidationError(f"rendered field uses wrong layout: {declared['key']}")
        for field in normalized_schema["fields"]:
            if field["layout"] == "bullet":
                match = _BULLET_FIELD_RE.match(line) if top_level_bullet else None
                is_match = bool(match and match.group(1).strip().casefold() == field["label"].casefold())
                initial = match.group(2) if is_match and match else ""
            else:
                match = _HEADING_RE.match(line)
                wanted_level = min(6, entry_level + 1)
                is_match = bool(
                    match
                    and len(match.group(1)) == wanted_level
                    and match.group(2).strip().casefold() == field["label"].casefold()
                )
                initial = ""
            if is_match:
                if field["key"] in seen:
                    raise EntryValidationError(f"duplicate rendered field: {field['key']}")
                seen.add(field["key"])
                marker_by_line[index] = (field, initial)
                break
    ordered_markers = sorted(marker_by_line)
    expected_order = {field["key"]: index for index, field in enumerate(normalized_schema["fields"])}
    marker_order = [expected_order[marker_by_line[line][0]["key"]] for line in ordered_markers]
    if marker_order != sorted(marker_order):
        raise EntryValidationError("rendered fields do not follow schema order")
    values: dict[str, str] = {field["key"]: "" for field in normalized_schema["fields"]}
    for position, marker_line in enumerate(ordered_markers):
        field, initial = marker_by_line[marker_line]
        next_line = ordered_markers[position + 1] if position + 1 < len(ordered_markers) else len(lines)
        tail = lines[marker_line + 1:next_line]
        while tail and not tail[0].strip():
            tail.pop(0)
        while tail and not tail[-1].strip():
            tail.pop()
        if field["layout"] == "bullet":
            continuation = [line[2:] if line.startswith("  ") else line for line in tail]
            value_lines = ([initial] if initial else []) + continuation
        else:
            value_lines = tail
        values[field["key"]] = "\n".join(value_lines).strip()
    return validate_structured_entry(
        {
            "topic": topic,
            "title": title,
            "aliases": list(aliases),
            "confidence": confidence,
            "fields": values,
            "rationale": rationale,
        },
        normalized_schema,
    )


def diagnose_duplicates(
    text_or_document: str | MarkdownDocument,
    schema: Mapping[str, Any],
    entry: Mapping[str, Any],
    *,
    fuzzy_threshold: float = 0.78,
) -> DuplicateDiagnosis:
    text = text_or_document.text if isinstance(text_or_document, MarkdownDocument) else str(text_or_document)
    normalized_schema = validate_summary_schema(schema)
    normalized_entry = validate_structured_entry(entry, normalized_schema)
    entry_level = normalized_schema["entry_heading_level"]
    headings = [heading for heading in _scan_headings(text) if heading.level == entry_level]
    wanted_titles = {_normalize_title(normalized_entry["title"])}
    wanted_titles.update(_normalize_title(alias) for alias in normalized_entry["aliases"])
    wanted_source = _normalize_source(normalized_entry["fields"].get("source", ""))
    exact: list[DuplicateMatch] = []
    fuzzy: list[DuplicateMatch] = []
    for heading in headings:
        normalized_title = _normalize_title(heading.title)
        body = text[heading.line_end:heading.end]
        existing_source = _extract_source(body)
        if wanted_source and existing_source and wanted_source == existing_source:
            exact.append(DuplicateMatch(heading.title, "source", 1.0, heading.start, heading.end))
            continue
        if normalized_title in wanted_titles:
            exact.append(DuplicateMatch(heading.title, "title", 1.0, heading.start, heading.end))
            continue
        score = max(
            difflib.SequenceMatcher(None, normalized_title, wanted).ratio()
            for wanted in wanted_titles
            if wanted
        )
        if score >= fuzzy_threshold:
            fuzzy.append(DuplicateMatch(heading.title, "fuzzy", round(score, 4), heading.start, heading.end))
    exact.sort(key=lambda item: item.start)
    fuzzy.sort(key=lambda item: (-item.score, item.start))
    return DuplicateDiagnosis(tuple(exact), tuple(fuzzy))


def find_exact_source_entries(
    text_or_document: str | MarkdownDocument,
    schema: Mapping[str, Any],
    source: str,
) -> tuple[ExactSourceEntry, ...]:
    """Return complete entries whose ``Source`` value exactly matches ``source``.

    The comparison intentionally uses the same case-insensitive, whitespace and
    Markdown-link normalization as duplicate detection.  It does not use title
    or fuzzy similarity, so callers can safely decide whether an existing card
    should be supplied to a model for semantic merging.
    """

    text = text_or_document.text if isinstance(text_or_document, MarkdownDocument) else str(text_or_document)
    normalized_schema = validate_summary_schema(schema)
    wanted = _normalize_source(str(source))
    if not wanted:
        return ()
    matches: list[ExactSourceEntry] = []
    for heading in _scan_headings(text):
        if heading.level != normalized_schema["entry_heading_level"]:
            continue
        body = text[heading.line_end:heading.end]
        if _extract_source(body) != wanted:
            continue
        matches.append(
            ExactSourceEntry(
                title=heading.title,
                markdown=text[heading.start:heading.end].strip("\r\n"),
                start=heading.start,
                end=heading.end,
            )
        )
    return tuple(matches)


def build_markdown_candidate(
    document: MarkdownDocument,
    schema: Mapping[str, Any],
    entry: Mapping[str, Any],
    *,
    duplicate_action: str = "auto",
    merge_title: str | None = None,
) -> CandidateResult:
    """Create candidate bytes and diff without writing the target file.

    ``auto`` merges only an exact Source match and otherwise creates a new card.
    ``merge`` additionally allows an explicitly selected fuzzy ``merge_title``.
    ``new`` always inserts a new entry.
    """

    normalized_schema = validate_summary_schema(schema)
    normalized_entry = validate_structured_entry(entry, normalized_schema)
    rendered = render_structured_entry(normalized_entry, normalized_schema, newline=document.newline)
    return _build_candidate(
        document,
        normalized_schema,
        normalized_entry,
        rendered,
        duplicate_action=duplicate_action,
        merge_title=merge_title,
    )


def build_markdown_candidate_from_rendered(
    document: MarkdownDocument,
    schema: Mapping[str, Any],
    *,
    topic: str,
    rendered_markdown: str,
    aliases: Sequence[str] = (),
    confidence: float = 1.0,
    rationale: str = "",
    duplicate_action: str = "auto",
    merge_title: str | None = None,
) -> CandidateResult:
    """Validate a user-edited card and refresh its deterministic candidate."""

    normalized_schema = validate_summary_schema(schema)
    normalized_entry = parse_rendered_entry(
        rendered_markdown,
        normalized_schema,
        topic=topic,
        aliases=aliases,
        confidence=confidence,
        rationale=rationale,
    )
    normalized_rendered = rendered_markdown.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    normalized_rendered = normalized_rendered.replace("\n", document.newline)
    result = _build_candidate(
        document,
        normalized_schema,
        normalized_entry,
        normalized_rendered,
        duplicate_action=duplicate_action,
        merge_title=merge_title,
    )
    return replace(result, rendered_entry=rendered_markdown.strip("\r\n"))


def _build_candidate(
    document: MarkdownDocument,
    normalized_schema: Mapping[str, Any],
    normalized_entry: Mapping[str, Any],
    rendered: str,
    *,
    duplicate_action: str,
    merge_title: str | None,
) -> CandidateResult:
    action = str(duplicate_action).strip().casefold()
    if action not in {"auto", "merge", "new"}:
        raise EntryValidationError("duplicate_action must be auto, merge, or new")
    diagnosis = diagnose_duplicates(document, normalized_schema, normalized_entry)
    source_matches = [item for item in diagnosis.exact if item.kind == "source"]
    if action == "auto" and len(source_matches) > 1:
        raise EntryValidationError("multiple exact duplicate anchors require an explicit new-entry decision")
    if action == "merge" and len(diagnosis.exact) > 1:
        raise EntryValidationError("multiple exact duplicate anchors require an explicit new-entry decision")
    selected: DuplicateMatch | None = None
    if action == "auto" and source_matches:
        selected = source_matches[0]
    elif action == "merge":
        candidates = list(diagnosis.exact) or list(diagnosis.fuzzy)
        if merge_title:
            wanted = _normalize_title(merge_title)
            candidates = [item for item in candidates if _normalize_title(item.title) == wanted]
        if not candidates:
            raise EntryValidationError("selected merge target is not a diagnosed duplicate")
        selected = candidates[0]
    if selected:
        suffix = document.text[selected.end:]
        if suffix:
            gap = document.newline * (normalized_schema["blank_lines_between_entries"] + 1)
            suffix = gap + suffix.lstrip("\r\n")
        candidate_text = document.text[:selected.start] + rendered + suffix
        final_action = "merge"
    else:
        candidate_text = _insert_entry(document.text, normalized_schema, normalized_entry["topic"], rendered, document.newline)
        final_action = "new"
    candidate_text = _restore_trailing_newline(candidate_text, document.ends_with_newline, document.newline)
    payload = candidate_text.encode("utf-8")
    candidate_bytes = (UTF8_BOM if document.has_bom else b"") + payload
    if len(candidate_bytes) > MAX_MARKDOWN_BYTES:
        raise MarkdownContentError("candidate Markdown exceeds the 1 MiB limit")
    diff = unified_markdown_diff(document, candidate_bytes)
    return CandidateResult(
        candidate_bytes=candidate_bytes,
        unified_diff=diff,
        baseline_sha256=document.baseline_sha256,
        candidate_sha256=hashlib.sha256(candidate_bytes).hexdigest(),
        rendered_entry=rendered,
        action=final_action,
        target_existed=document.exists,
        duplicate_diagnosis=diagnosis,
    )


def unified_markdown_diff(document: MarkdownDocument, candidate_bytes: bytes) -> str:
    payload = candidate_bytes[len(UTF8_BOM):] if candidate_bytes.startswith(UTF8_BOM) else candidate_bytes
    try:
        candidate_text = payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MarkdownContentError("candidate is not UTF-8") from exc
    label = document.path.name
    return "".join(
        difflib.unified_diff(
            document.text.splitlines(keepends=True),
            candidate_text.splitlines(keepends=True),
            fromfile=f"a/{label}",
            tofile=f"b/{label}",
        )
    )


def _single_line(value: Any, field: str, *, max_length: int) -> str:
    if not isinstance(value, str):
        raise SchemaValidationError(f"{field} must be a string")
    result = value.strip()
    if not result or len(result) > max_length or "\n" in result or "\r" in result or _contains_html(result):
        raise SchemaValidationError(f"{field} is invalid")
    return result


def _entry_heading(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise EntryValidationError(f"{field} must be a string")
    result = value.strip()
    if (
        not result or len(result) > 160 or "\n" in result or "\r" in result
        or result.startswith("#") or _contains_html(result)
    ):
        raise EntryValidationError(f"{field} is invalid")
    return result


def _bounded_int(value: Any, field: str, lower: int, upper: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not lower <= value <= upper:
        raise SchemaValidationError(f"{field} must be an integer in [{lower}, {upper}]")
    return value


def _contains_html(value: str) -> bool:
    return bool(_HTML_RE.search(value))


def _reject_reparse_components(path: Path) -> None:
    parts = path.parts
    if not parts:
        raise MarkdownPathError("invalid Markdown path")
    current = Path(parts[0])
    for part in parts[1:]:
        current = current / part
        if not os.path.lexists(current):
            continue
        try:
            info = os.lstat(current)
        except OSError as exc:
            raise MarkdownPathError(f"cannot inspect path component: {exc}") from exc
        attributes = getattr(info, "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        if stat.S_ISLNK(info.st_mode) or attributes & reparse_flag:
            raise MarkdownPathError("symlink/reparse-point targets are not allowed")


def _dominant_newline(text: str) -> str:
    crlf = text.count("\r\n")
    lf = text.count("\n") - crlf
    return "\r\n" if crlf > lf else "\n"


def _scan_headings(text: str) -> list[_Heading]:
    raw: list[tuple[int, int, str, int]] = []
    offset = 0
    fence: str | None = None
    for line in text.splitlines(keepends=True):
        logical = line.rstrip("\r\n")
        fence_match = _FENCE_RE.match(logical)
        if fence_match:
            marker = fence_match.group(1)[0]
            fence = None if fence == marker else (marker if fence is None else fence)
            offset += len(line)
            continue
        if fence is None:
            match = _HEADING_RE.match(logical)
            if match:
                raw.append((len(match.group(1)), offset, match.group(2).strip(), offset + len(line)))
        offset += len(line)
    headings: list[_Heading] = []
    for index, (level, start, title, line_end) in enumerate(raw):
        end = len(text)
        for next_level, next_start, _, _ in raw[index + 1:]:
            if next_level <= level:
                end = next_start
                break
        headings.append(_Heading(level, title, start, line_end, end))
    return headings


def _extract_fields(body: str, entry_level: int) -> list[tuple[str, str, int]]:
    result: list[tuple[str, str, int]] = []
    fence: str | None = None
    for index, line in enumerate(body.splitlines()):
        fence_match = _FENCE_RE.match(line)
        if fence_match:
            marker = fence_match.group(1)[0]
            fence = None if fence == marker else (marker if fence is None else fence)
            continue
        if fence is not None:
            continue
        heading = _HEADING_RE.match(line)
        if heading and len(heading.group(1)) == min(6, entry_level + 1):
            result.append((heading.group(2).strip(), "subheading", index))
            continue
        match = _BULLET_FIELD_RE.match(line)
        if match:
            result.append((match.group(1).strip(), "bullet", index))
    return result


def _field_key(label: str, used: set[str]) -> str:
    known = _KNOWN_FIELD_KEYS.get(label.strip().casefold())
    if known and known not in used:
        return known
    ascii_key = re.sub(r"[^a-z0-9]+", "_", label.casefold()).strip("_")
    if not ascii_key or not ascii_key[0].isalpha():
        ascii_key = "field"
    ascii_key = ascii_key[:32]
    candidate = ascii_key
    suffix = 2
    while candidate in used:
        candidate = f"{ascii_key[:35]}_{suffix}"
        suffix += 1
    return candidate


def _normalize_title(value: str) -> str:
    value = re.sub(r"[`*_~]", "", value.casefold())
    value = re.sub(r"[\s\-—_:：/\\|（）()\[\]{}]+", " ", value)
    return value.strip()


def _normalize_source(value: str) -> str:
    value = value.strip().casefold()
    link = re.search(r"\[([^\]]*)\]\(([^)]+)\)", value)
    if link:
        label = link.group(1).strip("` <>")
        value = label if re.fullmatch(r"[a-z0-9:_-]+", label) else link.group(2)
    value = value.strip("` <>\t\r\n").rstrip("/")
    codeforces = re.search(
        r"codeforces\.com/(?:problemset/problem|contest)/(\d+)(?:/problem)?/([a-z0-9]+)",
        value,
    )
    if codeforces:
        return f"cf{codeforces.group(1)}{codeforces.group(2)}"
    luogu = re.search(r"luogu\.com\.cn/problem/([a-z0-9_-]+)", value)
    if luogu:
        return luogu.group(1)
    return value


def _extract_source(body: str) -> str:
    for line in body.splitlines():
        match = _BULLET_FIELD_RE.match(line) or _PLAIN_FIELD_RE.match(line)
        if match and match.group(1).strip().casefold() in {"source", "来源"}:
            return _normalize_source(match.group(2))
    return ""


def _insert_entry(text: str, schema: Mapping[str, Any], topic: str, rendered: str, newline: str) -> str:
    category_level = schema["category_heading_level"]
    headings = _scan_headings(text)
    topic_key = _normalize_title(topic)
    category = next(
        (
            heading for heading in headings
            if heading.level == category_level and _normalize_title(heading.title) == topic_key
        ),
        None,
    )
    gap = newline * (schema["blank_lines_between_entries"] + 1)
    if category:
        position = category.end
        before = text[:position].rstrip("\r\n")
        after = text[position:].lstrip("\r\n")
        candidate = before + gap + rendered
        if after:
            candidate += gap + after
        return candidate
    category_heading = "#" * category_level + " " + topic
    before = text.rstrip("\r\n")
    prefix = gap if before else ""
    return before + prefix + category_heading + newline * 2 + rendered


def _restore_trailing_newline(text: str, wanted: bool, newline: str) -> str:
    stripped = text.rstrip("\r\n")
    return stripped + (newline if wanted else "")


__all__ = [
    "CandidateResult",
    "DuplicateDecisionRequired",
    "DuplicateDiagnosis",
    "DuplicateMatch",
    "ExactSourceEntry",
    "EntryValidationError",
    "KnowledgeError",
    "MAX_MARKDOWN_BYTES",
    "MarkdownContentError",
    "MarkdownDocument",
    "MarkdownHashMismatch",
    "MarkdownPathError",
    "PRESET_NAMES",
    "SCHEMA_VERSION",
    "SchemaInference",
    "SchemaValidationError",
    "build_markdown_candidate",
    "build_markdown_candidate_from_rendered",
    "diagnose_duplicates",
    "find_exact_source_entries",
    "get_builtin_schema",
    "get_builtin_template",
    "infer_summary_schema",
    "inspect_markdown_path",
    "list_builtin_templates",
    "markdown_bytes_sha256",
    "parse_rendered_entry",
    "readback_markdown",
    "render_structured_entry",
    "schema_sha256",
    "unified_markdown_diff",
    "validate_markdown_path",
    "validate_structured_entry",
    "validate_summary_schema",
]
