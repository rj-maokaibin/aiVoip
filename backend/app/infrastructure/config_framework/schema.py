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


def parse_config_result(stdout: str, *, allow_data_only: bool = False) -> ConfigResult:
    """Parse unified-framework output without weakening mutation acknowledgement.

    Current VOIP `dev_config get` may return a data-only JSON object such as
    ``{"data": [...]}``.  That shape is accepted only when the caller explicitly
    opts into read-mode via ``allow_data_only``.  Mutation responses must still
    carry the framework's authoritative ``rcode``/``rmsg`` acknowledgement.
    """

    payload: Any = None
    last_error: Exception | None = None
    for candidate in _json_candidates(stdout):
        try:
            decoded = json.loads(candidate)
        except (TypeError, ValueError) as exc:
            last_error = exc
            continue
        if not isinstance(decoded, dict):
            continue
        if "rcode" in decoded and "rmsg" in decoded:
            payload = decoded
            break
        if allow_data_only and "data" in decoded:
            payload = decoded
            break

    if not isinstance(payload, dict):
        raise ConfigFrameworkParseError("CONFIG_FRAMEWORK_RESPONSE_INVALID") from last_error

    if "rcode" in payload and "rmsg" in payload:
        return ConfigResult(
            rcode=str(payload["rcode"]),
            rmsg=str(payload["rmsg"]),
            data=payload.get("data"),
            raw=payload,
        )

    if allow_data_only and "data" in payload:
        return ConfigResult(
            rcode="00000000",
            rmsg="success",
            data=payload.get("data"),
            raw=payload,
        )

    raise ConfigFrameworkParseError("CONFIG_FRAMEWORK_RESPONSE_INVALID") from last_error
