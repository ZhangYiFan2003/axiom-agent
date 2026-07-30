from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, Protocol

from axiom.memory.models import MemoryScopeType
from axiom.types import Message

FactAction = Literal["upsert", "retract"]

FACT_CATEGORIES = {
    "fact",
    "preference",
    "constraint",
    "project_decision",
    "environment",
    "identity-neutral_profile",
    "workflow",
}

SENSITIVE_CATEGORY_HINTS = {
    "health",
    "religion",
    "politics",
    "political",
    "sexual",
    "race",
    "ethnicity",
    "finance",
    "financial",
    "location",
}

SECRET_VALUE_PATTERN = re.compile(
    r"(authorization|bearer\s+[a-z0-9._-]+|sk-[a-z0-9]+|api[_-]?key|"
    r"\.env|private\s+key|session[_-]?token|cookie)",
    re.IGNORECASE,
)
WINDOWS_PATH_PATTERN = re.compile(r"\b[A-Za-z]:\\[^\s]+")
BASE64_BLOB_PATTERN = re.compile(r"\b[A-Za-z0-9+/]{80,}={0,2}\b")

_STRUCTURED_REMEMBER = re.compile(
    r"^\s*remember\s+"
    r"(?P<scope>thread|project|user)\s+"
    r"(?P<category>[a-z_][a-z0-9_-]*):(?P<key>[a-z_][a-z0-9_-]*)\s*=\s*"
    r"(?P<value>.+?)\s*$",
    re.IGNORECASE,
)
_STRUCTURED_FORGET = re.compile(
    r"^\s*forget\s+"
    r"(?P<scope>thread|project|user)\s+"
    r"(?P<category>[a-z_][a-z0-9_-]*):(?P<key>[a-z_][a-z0-9_-]*)\s*$",
    re.IGNORECASE,
)


@dataclass(slots=True)
class FactCandidate:
    key: str
    value: str
    category: str
    scope_type: MemoryScopeType
    confidence: float
    explicit: bool
    source_event_start_id: int
    source_event_end_id: int
    evidence: str | None = None
    action: FactAction = "upsert"


@dataclass(slots=True)
class FactExtractionRunResult:
    processed_event_end_id: int | None = None
    candidate_count: int = 0
    accepted_count: int = 0
    rejected_count: int = 0
    merged_or_saved_count: int = 0
    retracted_count: int = 0
    error: str | None = None


class FactExtractor(Protocol):
    async def extract(
        self,
        messages: Sequence[Message],
        *,
        summary: str | None = None,
    ) -> Sequence[FactCandidate]: ...


class DeterministicFactExtractor:
    version = "deterministic-fact-extractor-v1"

    async def extract(
        self,
        messages: Sequence[Message],
        *,
        summary: str | None = None,
    ) -> Sequence[FactCandidate]:
        candidates: list[FactCandidate] = []
        for message in messages:
            if message.role != "user" or not isinstance(message.content, str):
                continue
            event_id = _event_id(message)
            if event_id is None:
                continue
            parsed = _parse_message(message.content, event_id)
            candidates.extend(parsed)
        return sorted(
            candidates,
            key=lambda item: (
                item.source_event_start_id,
                item.scope_type.value,
                item.category,
                item.key,
                item.value,
            ),
        )


def normalize_fact_key(category: str, key: str) -> str:
    category = _normalize_slug(category)
    key = _normalize_slug(key)
    if ":" in key:
        return key
    return f"{category}:{key}"


def validate_fact_candidate(candidate: FactCandidate) -> FactCandidate | None:
    category = _normalize_slug(candidate.category)
    key = normalize_fact_key(category, candidate.key)
    value = " ".join(candidate.value.strip().split())
    if not value and candidate.action == "upsert":
        return None
    if category not in FACT_CATEGORIES:
        return None
    if _looks_sensitive(category, key, value):
        return None
    confidence = min(max(float(candidate.confidence), 0.0), 1.0)
    if not candidate.explicit:
        return None
    if candidate.scope_type == MemoryScopeType.USER:
        if category != "preference" or confidence < 0.9:
            return None
    elif confidence < 0.9:
        return None
    return FactCandidate(
        key=key,
        value=value[:500],
        category=category,
        scope_type=candidate.scope_type,
        confidence=confidence,
        explicit=True,
        source_event_start_id=candidate.source_event_start_id,
        source_event_end_id=candidate.source_event_end_id,
        evidence=(candidate.evidence or "")[:500] or None,
        action=candidate.action,
    )


def _parse_message(text: str, event_id: int) -> list[FactCandidate]:
    candidates: list[FactCandidate] = []
    for line in text.splitlines():
        remember = _STRUCTURED_REMEMBER.match(line)
        if remember:
            candidates.append(
                FactCandidate(
                    key=remember.group("key"),
                    value=remember.group("value"),
                    category=remember.group("category"),
                    scope_type=MemoryScopeType(remember.group("scope").casefold()),
                    confidence=0.95,
                    explicit=True,
                    source_event_start_id=event_id,
                    source_event_end_id=event_id,
                    evidence=line.strip(),
                )
            )
            continue
        forget = _STRUCTURED_FORGET.match(line)
        if forget:
            category = forget.group("category")
            key = normalize_fact_key(category, forget.group("key"))
            candidates.append(
                FactCandidate(
                    key=key,
                    value="",
                    category=category,
                    scope_type=MemoryScopeType(forget.group("scope").casefold()),
                    confidence=1.0,
                    explicit=True,
                    source_event_start_id=event_id,
                    source_event_end_id=event_id,
                    evidence=line.strip(),
                    action="retract",
                )
            )
            continue
        natural = _parse_natural_preference(line, event_id)
        if natural is not None:
            candidates.append(natural)
    return candidates


def _parse_natural_preference(line: str, event_id: int) -> FactCandidate | None:
    lowered = " ".join(line.strip().casefold().split())
    if lowered in {
        "i prefer responses in chinese",
        "please answer me in chinese",
        "i prefer chinese explanations",
    }:
        return FactCandidate(
            key="preference:response_language",
            value="Chinese",
            category="preference",
            scope_type=MemoryScopeType.USER,
            confidence=0.95,
            explicit=True,
            source_event_start_id=event_id,
            source_event_end_id=event_id,
            evidence=line.strip(),
        )
    if lowered in {
        "for this thread, only modify tests",
        "this thread should only modify tests",
    }:
        return FactCandidate(
            key="constraint:modify_scope",
            value="tests only",
            category="constraint",
            scope_type=MemoryScopeType.THREAD,
            confidence=0.95,
            explicit=True,
            source_event_start_id=event_id,
            source_event_end_id=event_id,
            evidence=line.strip(),
        )
    return None


def _event_id(message: Message) -> int | None:
    if not message.name:
        return None
    prefix, _, value = message.name.partition(":")
    if prefix != "event":
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _normalize_slug(value: str) -> str:
    value = value.strip().casefold().replace("-", "_")
    value = re.sub(r"\s+", "_", value)
    return re.sub(r"[^a-z0-9_:_]", "", value)


def _looks_sensitive(category: str, key: str, value: str) -> bool:
    searchable = f"{category} {key} {value}".casefold()
    if any(hint in searchable for hint in SENSITIVE_CATEGORY_HINTS):
        return True
    if SECRET_VALUE_PATTERN.search(searchable):
        return True
    if WINDOWS_PATH_PATTERN.search(value):
        return True
    return bool(BASE64_BLOB_PATTERN.search(value))
