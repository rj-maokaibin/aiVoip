from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Mapping

from app.infrastructure.mutation.contract import MutationStatus


_SECRET_KEYS = {
    "authorization",
    "cookie",
    "csrf",
    "passwd",
    "password",
    "password_ref",
    "secret",
    "sid",
    "token",
}
_MASK = "***"


class ConfigFrameworkError(RuntimeError):
    pass


class ConfigFrameworkParseError(ConfigFrameworkError):
    pass


class ConfigFrameworkDomainError(ConfigFrameworkError):
    def __init__(self, result: "ConfigResult"):
        super().__init__(f"CONFIG_FRAMEWORK_ERROR:{result.rcode}:{result.rmsg}")
        self.result = result


@dataclass(frozen=True)
class ConfigResult:
    rcode: str
    rmsg: str
    data: Any | None
    raw: Mapping[str, Any]

    @property
    def success(self) -> bool:
        return self.rcode == "00000000" and self.rmsg == "success"


@dataclass(frozen=True)
class ConfigMutationResult:
    status: MutationStatus
    response: ConfigResult | None = None
    readback: ConfigResult | None = None
    observed_after_unknown: bool = False

    @property
    def success(self) -> bool:
        if self.status is not MutationStatus.APPLIED:
            return False
        result = self.response or self.readback
        return bool(result and result.success)


def mask_secrets(value: Any) -> Any:
    """Recursively preserve structure while removing secret values."""

    if isinstance(value, Mapping):
        masked: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            masked[key_text] = _MASK if key_text.lower() in _SECRET_KEYS else mask_secrets(item)
        return masked
    if isinstance(value, list):
        return [mask_secrets(item) for item in value]
    if isinstance(value, tuple):
        return tuple(mask_secrets(item) for item in value)
    return value


def _json_candidates(stdout: str) -> list[str]:
    stripped = stdout.strip()
    if not stripped:
        return []
    candidates = [stripped]
    lines = [line.strip() for line in stripped.splitlines() if line.strip()]
    candidates.extend(reversed(lines))
    return candidates


def parse_config_result(stdout: str) -> ConfigResult:
    payload: Any = None
    last_error: Exception | None = None
    for candidate in _json_candidates(stdout):
        try:
            payload = json.loads(candidate)
            if isinstance(payload, dict) and "rcode" in payload and "rmsg" in payload:
                break
        except (TypeError, ValueError) as exc:
            last_error = exc
            continue
    if not isinstance(payload, dict) or "rcode" not in payload or "rmsg" not in payload:
        raise ConfigFrameworkParseError("CONFIG_FRAMEWORK_RESPONSE_INVALID") from last_error
    return ConfigResult(
        rcode=str(payload["rcode"]),
        rmsg=str(payload["rmsg"]),
        data=payload.get("data"),
        raw=payload,
    )
