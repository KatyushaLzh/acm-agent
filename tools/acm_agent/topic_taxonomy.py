"""Versioned deterministic mapping from platform tags to training topics."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Iterable

from .tag_policy import normalize_tags, split_meta_tags, tag_key


_PATH = Path(__file__).with_name("topic_taxonomy.v1.json")
_DATA = json.loads(_PATH.read_text(encoding="utf-8"))
TAXONOMY_VERSION = str(_DATA["version"])
TOPIC_LABELS = {str(row["key"]): str(row["label"]) for row in _DATA["topics"]}
_EXACT = {
    tag_key(alias): str(row["key"])
    for row in _DATA["topics"]
    for alias in row.get("aliases", [])
}
_CONTAINS = tuple(
    (tag_key(fragment), str(row["key"]))
    for row in _DATA["topics"]
    for fragment in row.get("contains", [])
    if tag_key(fragment)
)
_IGNORED = {tag_key(value) for value in _DATA.get("ignored", [])}


@dataclass(frozen=True)
class TagClassification:
    topics: tuple[str, ...]
    unclassified: tuple[str, ...]


def classify_tags(tags: Iterable[str]) -> TagClassification:
    """Classify subject tags without asking a model or inventing missing tags."""

    subject, _ignored_meta = split_meta_tags(normalize_tags(list(tags)))
    topics: set[str] = set()
    unclassified: list[str] = []
    for tag in subject:
        key = tag_key(tag)
        if key in _IGNORED:
            continue
        topic = _EXACT.get(key)
        if topic is None:
            topic = next((value for fragment, value in _CONTAINS if fragment in key), None)
        if topic is None:
            unclassified.append(tag)
        else:
            topics.add(topic)
    return TagClassification(tuple(sorted(topics)), tuple(sorted(unclassified, key=tag_key)))


def topic_label(topic_key: str) -> str:
    return TOPIC_LABELS.get(str(topic_key), str(topic_key))


__all__ = [
    "TAXONOMY_VERSION",
    "TOPIC_LABELS",
    "TagClassification",
    "classify_tags",
    "topic_label",
]
