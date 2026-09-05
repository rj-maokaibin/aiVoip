from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

import yaml

TBD_CURRENT_PRODUCT = "TBD_CURRENT_PRODUCT"
_ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}


class WebApiProfileError(ValueError):
    pass


@dataclass(frozen=True)
class WebOperationProfile:
    semantic_action: str
    endpoint: str
    method: str
    rpc_method: str | None = None
    mutation: bool = False
    readback_operation: str | None = None

    def __post_init__(self) -> None:
        parsed = urlsplit(self.endpoint)
        if parsed.scheme or parsed.netloc or not self.endpoint.startswith("/"):
            raise WebApiProfileError("WEB_PROFILE_ENDPOINT_MUST_BE_RELATIVE")
        method = self.method.upper()
        if method not in _ALLOWED_METHODS:
            raise WebApiProfileError(f"WEB_PROFILE_METHOD_INVALID:{self.method}")
        object.__setattr__(self, "method", method)

    @property
    def source_bound(self) -> bool:
        return self.rpc_method != TBD_CURRENT_PRODUCT


@dataclass(frozen=True)
class WebApiProfile:
    profile_id: str
    auth_provider: str
    operations: Mapping[str, WebOperationProfile]

    def operation(self, semantic_action: str) -> WebOperationProfile:
        try:
            return self.operations[semantic_action]
        except KeyError as exc:
            raise WebApiProfileError(f"WEB_OPERATION_NOT_FOUND:{semantic_action}") from exc

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> "WebApiProfile":
        extra = set(raw) - {"id", "auth_provider", "operations"}
        if extra:
            raise WebApiProfileError(f"WEB_PROFILE_UNKNOWN_FIELDS:{','.join(sorted(extra))}")
        profile_id = str(raw.get("id") or "").strip()
        auth_provider = str(raw.get("auth_provider") or "").strip()
        operation_raw = raw.get("operations")
        if not profile_id or not auth_provider or not isinstance(operation_raw, Mapping):
            raise WebApiProfileError("WEB_PROFILE_REQUIRED_FIELDS_MISSING")

        operations: dict[str, WebOperationProfile] = {}
        allowed_op = {"endpoint", "method", "rpc_method", "mutation", "readback_operation"}
        for semantic_action, spec in operation_raw.items():
            if not isinstance(spec, Mapping):
                raise WebApiProfileError(f"WEB_OPERATION_INVALID:{semantic_action}")
            op_extra = set(spec) - allowed_op
            if op_extra:
                raise WebApiProfileError(f"WEB_OPERATION_UNKNOWN_FIELDS:{semantic_action}:{','.join(sorted(op_extra))}")
            operations[str(semantic_action)] = WebOperationProfile(
                semantic_action=str(semantic_action),
                endpoint=str(spec.get("endpoint") or ""),
                method=str(spec.get("method") or ""),
                rpc_method=(str(spec["rpc_method"]) if spec.get("rpc_method") is not None else None),
                mutation=bool(spec.get("mutation", False)),
                readback_operation=(str(spec["readback_operation"]) if spec.get("readback_operation") is not None else None),
            )
        return cls(profile_id=profile_id, auth_provider=auth_provider, operations=operations)

    @classmethod
    def load_yaml(cls, path: str | Path) -> "WebApiProfile":
        with Path(path).open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
        if not isinstance(raw, Mapping):
            raise WebApiProfileError("WEB_PROFILE_ROOT_INVALID")
        return cls.from_mapping(raw)
