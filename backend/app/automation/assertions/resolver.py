from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import re
from typing import Any, Mapping


class _Missing:
    def __repr__(self) -> str:
        return "<MISSING>"


MISSING = _Missing()
_PART = re.compile(r"^(?P<name>[^[]+)?(?P<indexes>(?:\[[0-9]+\])*)$")
_INDEX = re.compile(r"\[([0-9]+)\]")


@dataclass(frozen=True)
class EvidenceEnvelope:
    data: Any
    evidence_refs: tuple[str, ...] = ()
    source_timestamp: datetime | None = None
    route: dict[str, Any] | None = None


@dataclass
class NormalizedEvidenceStore:
    _sources: dict[str, EvidenceEnvelope] = field(default_factory=dict)

    def put(self, source: str, envelope: EvidenceEnvelope) -> None:
        self._sources[source] = envelope

    def has(self, source: str) -> bool:
        return source in self._sources

    def get(self, source: str) -> EvidenceEnvelope | None:
        return self._sources.get(source)

    def as_dict(self) -> dict[str, EvidenceEnvelope]:
        return dict(self._sources)


def resolve_path(data: Any, path: str) -> Any:
    if path == "":
        return data
    current = data
    for raw_part in path.split("."):
        match = _PART.fullmatch(raw_part)
        if not match:
            return MISSING
        name = match.group("name")
        if name:
            if isinstance(current, Mapping) and name in current:
                current = current[name]
            else:
                return MISSING
        for raw_index in _INDEX.findall(match.group("indexes") or ""):
            index = int(raw_index)
            if isinstance(current, (list, tuple)) and 0 <= index < len(current):
                current = current[index]
            else:
                return MISSING
    return current
