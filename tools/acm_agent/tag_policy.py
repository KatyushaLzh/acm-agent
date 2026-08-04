"""Deterministic normalization and effective-tag policy.

Platform tags remain factual source data.  Training consumers use the subject
tags returned here after metadata filtering and explicit per-problem overrides.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Sequence


_YEAR_RE = re.compile(r"^(?:18|19|20)\d{2}$")
_REGION_RE = re.compile(r"(?:省选|省份|各省|地区选拔)", re.I)
_EVENT_RE = re.compile(r"(?:集训队|冬令营|夏令营|联赛|赛事来源)", re.I)
_COMPILER_RE = re.compile(r"(?:^|\b)(?:需?要?\s*)?o2(?:优化)?(?:\b|$)", re.I)
_REGION_EXACT = {
    "北京", "天津", "上海", "重庆", "河北", "山西", "辽宁", "吉林", "黑龙江",
    "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南",
    "广东", "海南", "四川", "贵州", "云南", "陕西", "甘肃", "青海", "台湾",
    "内蒙古", "广西", "西藏", "宁夏", "新疆", "香港", "澳门",
}
_EVENT_EXACT = {"noi", "ioi", "usaco", "icpc", "acm", "wc", "ctsc", "apio"}


def normalize_tag(value: Any) -> str:
    """Collapse whitespace without changing the user's display spelling."""

    return " ".join(str(value).split()) if isinstance(value, str) else ""


def tag_key(value: Any) -> str:
    """Return the stable comparison key used in SQLite override rows."""

    return normalize_tag(value).casefold()


def normalize_tags(values: Any) -> list[str]:
    """Normalize and case-insensitively deduplicate a tag sequence."""

    if not isinstance(values, (list, tuple, set)):
        return []
    result: list[str] = []
    seen: set[str] = set()
    for raw in values:
        tag = normalize_tag(raw)
        key = tag.casefold()
        if tag and key not in seen:
            seen.add(key)
            result.append(tag)
    return result


def meta_tag_reason(value: Any) -> str | None:
    """Classify deterministic non-subject metadata, or return ``None``."""

    tag = normalize_tag(value)
    folded = tag.casefold()
    if not tag:
        return None
    if _YEAR_RE.fullmatch(tag):
        return "year"
    if tag in _REGION_EXACT or _REGION_RE.search(tag):
        return "region"
    if folded in _EVENT_EXACT or _EVENT_RE.search(tag):
        return "event_source"
    if _COMPILER_RE.search(tag):
        return "compiler_option"
    return None


def split_meta_tags(values: Any) -> tuple[list[str], list[dict[str, str]]]:
    """Return subject tags and ignored metadata with machine-readable reasons."""

    subject: list[str] = []
    ignored: list[dict[str, str]] = []
    for tag in normalize_tags(values):
        reason = meta_tag_reason(tag)
        if reason:
            ignored.append({"tag": tag, "reason": reason})
        else:
            subject.append(tag)
    return subject, ignored


def effective_tags(
    base_tags: Any,
    overrides: Sequence[Mapping[str, Any]] | Iterable[Mapping[str, Any]] = (),
) -> list[str]:
    """Apply metadata filtering, explicit suppressions, then explicit additions.

    Addition rows intentionally run last, allowing a deliberate human addition
    to override the default metadata policy.
    """

    subject, _ignored = split_meta_tags(base_tags)
    rows = list(overrides)

    def field(row: Mapping[str, Any], name: str, default: Any = None) -> Any:
        try:
            return row[name]
        except (KeyError, IndexError, TypeError):
            getter = getattr(row, "get", None)
            return getter(name, default) if getter else default

    suppressed = {
        str(field(row, "tag_key") or tag_key(field(row, "tag")))
        for row in rows
        if str(field(row, "action") or "").lower() == "suppress"
    }
    result = [tag for tag in subject if tag_key(tag) not in suppressed]
    seen = {tag_key(tag) for tag in result}
    for row in rows:
        if str(field(row, "action") or "").lower() != "add":
            continue
        tag = normalize_tag(field(row, "tag"))
        key = str(field(row, "tag_key") or tag_key(tag))
        if tag and key not in seen:
            seen.add(key)
            result.append(tag)
    return result


def tag_diff(before: Any, after: Any) -> tuple[list[str], list[str]]:
    """Return display tags added to and removed from ``before``."""

    old = normalize_tags(before)
    new = normalize_tags(after)
    old_keys = {tag_key(tag) for tag in old}
    new_keys = {tag_key(tag) for tag in new}
    added = [tag for tag in new if tag_key(tag) not in old_keys]
    removed = [tag for tag in old if tag_key(tag) not in new_keys]
    return added, removed


__all__ = [
    "effective_tags",
    "meta_tag_reason",
    "normalize_tag",
    "normalize_tags",
    "split_meta_tags",
    "tag_diff",
    "tag_key",
]
