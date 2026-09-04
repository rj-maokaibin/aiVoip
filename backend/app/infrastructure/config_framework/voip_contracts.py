from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from app.infrastructure.config_framework.schema import ConfigFrameworkParseError, ConfigResult


@dataclass(frozen=True)
class VoipUserInfoContract:
    rows: tuple[Mapping[str, Any], ...]


@dataclass(frozen=True)
class VoipRegStateContract:
    rows: tuple[Mapping[str, Any], ...]


def _rows(result: ConfigResult, *, module: str) -> tuple[Mapping[str, Any], ...]:
    if not result.success:
        raise ConfigFrameworkParseError(f"{module}:UNSUCCESSFUL_RESULT")
    data = result.data
    if isinstance(data, Mapping):
        # Some framework modules wrap rows under data/list/table. Keep this
        # parser structural and do not invent field-level product semantics.
        for key in ("data", "list", "table"):
            candidate = data.get(key)
            if isinstance(candidate, list):
                data = candidate
                break
    if not isinstance(data, list) or not all(isinstance(row, Mapping) for row in data):
        raise ConfigFrameworkParseError(f"{module}:DATA_ROWS_INVALID")
    return tuple(data)


def parse_voip_user_info(result: ConfigResult) -> VoipUserInfoContract:
    return VoipUserInfoContract(rows=_rows(result, module="voipUserInfo"))


def parse_voip_reg_state(result: ConfigResult) -> VoipRegStateContract:
    return VoipRegStateContract(rows=_rows(result, module="voipRegState"))
