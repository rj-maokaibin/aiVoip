from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

import yaml

TBD_CURRENT_PRODUCT = "TBD_CURRENT_PRODUCT"
_ALLOWED_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"}
_ALLOWED_RPC_STYLES = {"single", "cmd_array"}


class WebApiProfileError(ValueError):
    pass


@dataclass(frozen=True)
class WebRpcItemProfile:
    method: str
    module: str

    def __post_init__(self) -> None:
        if not self.method or not self.module:
            raise WebApiProfileError("WEB_RPC_ITEM_REQUIRED_FIELDS_MISSING")
        if self.method == TBD_CURRENT_PRODUCT or self.module == TBD_CURRENT_PRODUCT:
            return
        if "." not in self.method:
            raise WebApiProfileError(f"WEB_RPC_ITEM_METHOD_INVALID:{self.method}")

    @property
    def source_bound(self) -> bool:
        return self.method != TBD_CURRENT_PRODUCT and self.module != TBD_CURRENT_PRODUCT


@dataclass(frozen=True)
class WebOperationProfile:
    semantic_action: str
    endpoint: str
    method: str
    rpc_method: str | None = None
    mutation: bool = False
    readback_operation: str | None = None
    rpc_style: str = "single"
    rpc_items: tuple[WebRpcItemProfile, ...] = ()
    writable_modules: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        parsed = urlsplit(self.endpoint)
        if parsed.scheme or parsed.netloc or not self.endpoint.startswith("/"):
            raise WebApiProfileError("WEB_PROFILE_ENDPOINT_MUST_BE_RELATIVE")
        method = self.method.upper()
        if method not in _ALLOWED_METHODS:
            raise WebApiProfileError(f"WEB_PROFILE_METHOD_INVALID:{self.method}")
        object.__setattr__(self, "method", method)
        if self.rpc_style not in _ALLOWED_RPC_STYLES:
            raise WebApiProfileError(f"WEB_RPC_STYLE_INVALID:{self.rpc_style}")
        if self.rpc_style == "cmd_array":
            if self.rpc_method not in {"cmdArr", TBD_CURRENT_PRODUCT}:
                raise WebApiProfileError("WEB_CMD_ARRAY_RPC_METHOD_MUST_BE_CMDARR")
            if not self.rpc_items:
                raise WebApiProfileError("WEB_CMD_ARRAY_ITEMS_REQUIRED")
            modules = tuple(item.module for item in self.rpc_items)
            if len(modules) != len(set(modules)):
                raise WebApiProfileError("WEB_CMD_ARRAY_DUPLICATE_MODULE")
            unknown_writes = set(self.writable_modules) - set(modules)
            if unknown_writes:
                raise WebApiProfileError(
                    f"WEB_WRITABLE_MODULE_NOT_IN_RPC_ITEMS:{','.join(sorted(unknown_writes))}"
                )
            if self.mutation and set(self.writable_modules) != set(modules):
                raise WebApiProfileError("WEB_MUTATION_WRITABLE_SCOPE_MUST_MATCH_RPC_ITEMS")
            if not self.mutation and self.writable_modules:
                raise WebApiProfileError("WEB_READ_OPERATION_CANNOT_DECLARE_WRITABLE_MODULES")
        elif self.rpc_items or self.writable_modules:
            raise WebApiProfileError("WEB_SINGLE_RPC_CANNOT_DECLARE_CMD_ARRAY_FIELDS")

    @property
    def source_bound(self) -> bool:
        if self.rpc_method == TBD_CURRENT_PRODUCT:
            return False
        return all(item.source_bound for item in self.rpc_items)


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
        allowed_op = {
            "endpoint", "method", "rpc_method", "mutation", "readback_operation",
            "rpc_style", "rpc_items", "side_effects",
        }
        for semantic_action, spec in operation_raw.items():
            if not isinstance(spec, Mapping):
                raise WebApiProfileError(f"WEB_OPERATION_INVALID:{semantic_action}")
            op_extra = set(spec) - allowed_op
            if op_extra:
                raise WebApiProfileError(
                    f"WEB_OPERATION_UNKNOWN_FIELDS:{semantic_action}:{','.join(sorted(op_extra))}"
                )
            raw_items = spec.get("rpc_items", [])
            if not isinstance(raw_items, list):
                raise WebApiProfileError(f"WEB_RPC_ITEMS_INVALID:{semantic_action}")
            rpc_items: list[WebRpcItemProfile] = []
            for index, item in enumerate(raw_items):
                if not isinstance(item, Mapping) or set(item) != {"method", "module"}:
                    raise WebApiProfileError(f"WEB_RPC_ITEM_INVALID:{semantic_action}:{index}")
                rpc_items.append(WebRpcItemProfile(method=str(item["method"]), module=str(item["module"])))

            side_effects = spec.get("side_effects", {})
            if side_effects is None:
                side_effects = {}
            if not isinstance(side_effects, Mapping) or set(side_effects) - {"writable_modules"}:
                raise WebApiProfileError(f"WEB_SIDE_EFFECTS_INVALID:{semantic_action}")
            writable = side_effects.get("writable_modules", [])
            if not isinstance(writable, list) or not all(isinstance(item, str) and item for item in writable):
                raise WebApiProfileError(f"WEB_WRITABLE_MODULES_INVALID:{semantic_action}")

            operations[str(semantic_action)] = WebOperationProfile(
                semantic_action=str(semantic_action),
                endpoint=str(spec.get("endpoint") or ""),
                method=str(spec.get("method") or ""),
                rpc_method=(str(spec["rpc_method"]) if spec.get("rpc_method") is not None else None),
                mutation=bool(spec.get("mutation", False)),
                readback_operation=(
                    str(spec["readback_operation"])
                    if spec.get("readback_operation") is not None else None
                ),
                rpc_style=str(spec.get("rpc_style") or "single"),
                rpc_items=tuple(rpc_items),
                writable_modules=tuple(writable),
            )
        return cls(profile_id=profile_id, auth_provider=auth_provider, operations=operations)

    @classmethod
    def load_yaml(cls, path: str | Path) -> "WebApiProfile":
        with Path(path).open("r", encoding="utf-8") as handle:
            raw = yaml.safe_load(handle)
        if not isinstance(raw, Mapping):
            raise WebApiProfileError("WEB_PROFILE_ROOT_INVALID")
        return cls.from_mapping(raw)
