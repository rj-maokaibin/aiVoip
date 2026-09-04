from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class RunIntent(str, Enum):
    DIAGNOSE = "diagnose"
    VERIFY = "verify"
    REPRODUCE = "reproduce"


class ActionEntry(str, Enum):
    WEB = "web"
    MACC = "macc"
    APP = "app"
    NONE = "none"


class ActionTransport(str, Enum):
    HTTP_API = "http_api"
    MQTT = "mqtt"
    SSH = "ssh"


class ActionBackend(str, Enum):
    CONFIG_FRAMEWORK = "config_framework"
    NATIVE_LINUX = "native_linux"
    REMOTE_SERVICE = "remote_service"


class ActionPurpose(str, Enum):
    SETUP = "setup"
    TEST = "test"
    OBSERVATION = "observation"
    EVIDENCE = "evidence"
    CLEANUP = "cleanup"


@dataclass(frozen=True)
class ActionRoute:
    """Auditable four-dimensional route plus optional target.

    SSH is deliberately a transport, never a product entry.  MACC/APP/MQTT are
    represented as frozen V1 contracts only; their real implementations are P2.
    """

    entry: ActionEntry
    transport: ActionTransport
    backend: ActionBackend
    purpose: ActionPurpose
    target: str | None = None

    def as_dict(self) -> dict[str, str | None]:
        return {
            "entry": self.entry.value,
            "transport": self.transport.value,
            "backend": self.backend.value,
            "purpose": self.purpose.value,
            "target": self.target,
        }
