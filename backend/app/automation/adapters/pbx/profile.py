from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class FusionPbxProfileError(ValueError):
    pass


@dataclass(frozen=True)
class FusionPbxLabProfile:
    schema_version: str
    node_key: str
    host: str
    fusionpbx_root: str
    php_bin: str
    fs_cli_bin: str
    internal_profile: str
    internal_port: int
    event_socket_host: str
    event_socket_port: int
    event_socket_config: str
    pool_start: int
    pool_end: int
    protected_extensions: tuple[str, ...]
    source_fence_version: str
    source_hashes: dict[str, str]

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FusionPbxLabProfile":
        pool = data.get("extension_pool") or {}
        event_socket = data.get("event_socket") or {}
        fence = data.get("source_fence") or {}
        value = cls(
            schema_version=str(data.get("schema_version") or ""),
            node_key=str(data.get("node_key") or ""),
            host=str(data.get("host") or ""),
            fusionpbx_root=str(data.get("fusionpbx_root") or ""),
            php_bin=str(data.get("php_bin") or "/usr/bin/php"),
            fs_cli_bin=str(data.get("fs_cli_bin") or "/usr/bin/fs_cli"),
            internal_profile=str(data.get("internal_profile") or "internal"),
            internal_port=int(data.get("internal_port") or 0),
            event_socket_host=str(event_socket.get("host") or "127.0.0.1"),
            event_socket_port=int(event_socket.get("port") or 8021),
            event_socket_config=str(event_socket.get("config") or "/etc/freeswitch/autoload_configs/event_socket.conf.xml"),
            pool_start=int(pool.get("start") or 0),
            pool_end=int(pool.get("end") or 0),
            protected_extensions=tuple(str(v) for v in data.get("protected_extensions") or ()),
            source_fence_version=str(fence.get("version") or ""),
            source_hashes={str(k): str(v) for k, v in (fence.get("files") or {}).items()},
        )
        value.validate()
        return value

    def validate(self) -> None:
        if self.schema_version != "pbx-lab-profile-v1":
            raise FusionPbxProfileError("PBX_PROFILE_SCHEMA_UNSUPPORTED")
        if not self.node_key or not self.host or not self.fusionpbx_root.startswith("/"):
            raise FusionPbxProfileError("PBX_PROFILE_IDENTITY_INVALID")
        if not (1 <= self.internal_port <= 65535):
            raise FusionPbxProfileError("PBX_PROFILE_PORT_INVALID")
        if not self.event_socket_host or not (1 <= self.event_socket_port <= 65535):
            raise FusionPbxProfileError("PBX_PROFILE_EVENT_SOCKET_INVALID")
        if not self.event_socket_config.startswith("/"):
            raise FusionPbxProfileError("PBX_PROFILE_EVENT_SOCKET_CONFIG_INVALID")
        if self.pool_start < 1 or self.pool_end < self.pool_start:
            raise FusionPbxProfileError("PBX_PROFILE_POOL_INVALID")
        if not self.source_fence_version or not self.source_hashes:
            raise FusionPbxProfileError("PBX_PROFILE_SOURCE_FENCE_REQUIRED")
        for rel, digest in self.source_hashes.items():
            if rel.startswith("/") or ".." in Path(rel).parts or len(digest) != 64:
                raise FusionPbxProfileError("PBX_PROFILE_SOURCE_HASH_INVALID")


def load_fusionpbx_profile(path: str | Path) -> FusionPbxLabProfile:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise FusionPbxProfileError("PBX_PROFILE_OBJECT_REQUIRED")
    return FusionPbxLabProfile.from_dict(data)
