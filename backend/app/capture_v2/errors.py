from __future__ import annotations

from typing import Any


class CaptureV2Error(RuntimeError):
    def __init__(self, code: str, *, details: dict[str, Any] | None = None):
        self.code = code
        self.details = details or {}
        super().__init__(code)


def ensure(condition: bool, code: str, **details: Any) -> None:
    if not condition:
        raise CaptureV2Error(code, details=details)
