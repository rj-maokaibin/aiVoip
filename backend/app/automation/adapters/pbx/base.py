from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Mapping, Protocol


@dataclass(frozen=True)
class SipRegistrationEvidence:
    registered: bool
    number: str
    evidence_refs: tuple[str, ...] = ()
    source_timestamp: datetime | None = None
    details: Mapping[str, Any] | None = None


class SipRegistrationProbe(Protocol):
    async def wait_registered(self, *, number: str, timeout_seconds: float) -> SipRegistrationEvidence: ...


@dataclass(frozen=True)
class TemporaryExtensionSpec:
    extension_uuid: str
    extension: str
    password: str = field(repr=False, compare=False)


class TemporaryExtensionProvider(Protocol):
    def create(self, spec: TemporaryExtensionSpec): ...
    def delete(self, spec: TemporaryExtensionSpec) -> Mapping[str, Any]: ...
    def verify_absent(self, spec: TemporaryExtensionSpec) -> tuple[bool, Mapping[str, Any]]: ...
