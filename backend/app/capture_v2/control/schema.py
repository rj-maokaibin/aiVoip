from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

SCHEMA_VERSION = "capture-v2-remote-action-v1"
STATUS_SCHEMA_VERSION = "capture-v2-remote-status-v1"
_ACTION_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ControlActionType(StrEnum):
    SOFTWARE_REGRESSION = "SOFTWARE_REGRESSION"
    DEPLOYMENT_PREFLIGHT = "DEPLOYMENT_PREFLIGHT"
    ACTIVATION_REHEARSAL = "ACTIVATION_REHEARSAL"
    GATE_LEASE_RACE = "GATE_LEASE_RACE"
    GATE_LEASE_FENCING = "GATE_LEASE_FENCING"
    GATE_OWNERSHIP = "GATE_OWNERSHIP"
    GATE_OWNERSHIP_ADOPT = "GATE_OWNERSHIP_ADOPT"
    GATE_SEGMENT = "GATE_SEGMENT"
    GATE_READINESS_FXS = "GATE_READINESS_FXS"
    GATE_COLLECT = "GATE_COLLECT"
    GATE_EVALUATE = "GATE_EVALUATE"
    GOLDEN_ARCHIVE_RECOVER = "GOLDEN_ARCHIVE_RECOVER"
    FAULT_WORKER_SIGNAL = "FAULT_WORKER_SIGNAL"
    FAULT_QUARANTINE_COPY = "FAULT_QUARANTINE_COPY"
    FAULT_RESTORE_COPY = "FAULT_RESTORE_COPY"
    HUMAN_STEP = "HUMAN_STEP"


class ControlState(StrEnum):
    PENDING = "PENDING"
    CLAIMED = "CLAIMED"
    RUNNING = "RUNNING"
    WAITING_HUMAN = "WAITING_HUMAN"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    INCONCLUSIVE = "INCONCLUSIVE"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True)
class SafetySpec:
    require_clean_git: bool = True
    require_v1_authority: bool = True
    require_v2_disabled: bool = True
    allow_worker_kill: bool = False
    allow_server_quarantine: bool = False
    expected_head: str | None = None

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> "SafetySpec":
        raw = raw or {}
        unknown = set(raw) - {
            "require_clean_git", "require_v1_authority", "require_v2_disabled",
            "allow_worker_kill", "allow_server_quarantine", "expected_head",
        }
        if unknown:
            raise ValueError(f"UNKNOWN_SAFETY_FIELDS:{','.join(sorted(unknown))}")
        return cls(
            require_clean_git=bool(raw.get("require_clean_git", True)),
            require_v1_authority=bool(raw.get("require_v1_authority", True)),
            require_v2_disabled=bool(raw.get("require_v2_disabled", True)),
            allow_worker_kill=bool(raw.get("allow_worker_kill", False)),
            allow_server_quarantine=bool(raw.get("allow_server_quarantine", False)),
            expected_head=(str(raw["expected_head"]).strip() if raw.get("expected_head") else None),
        )


@dataclass(frozen=True)
class RemoteAction:
    action_id: str
    sequence: int
    created_at: datetime
    expires_at: datetime
    action_type: ControlActionType
    parameters: dict[str, Any] = field(default_factory=dict)
    safety: SafetySpec = field(default_factory=SafetySpec)
    requested_by: str = "remote-controller"
    note: str = ""
    schema_version: str = SCHEMA_VERSION

    @staticmethod
    def _parse_ts(value: Any, field_name: str) -> datetime:
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field_name}_REQUIRED")
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field_name}_INVALID") from exc
        if dt.tzinfo is None:
            raise ValueError(f"{field_name}_MUST_BE_TZ_AWARE")
        return dt.astimezone(timezone.utc)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RemoteAction":
        if not isinstance(raw, dict):
            raise ValueError("ACTION_MUST_BE_OBJECT")
        allowed = {
            "schema_version", "action_id", "sequence", "created_at", "expires_at",
            "action_type", "parameters", "safety", "requested_by", "note",
        }
        unknown = set(raw) - allowed
        if unknown:
            raise ValueError(f"UNKNOWN_ACTION_FIELDS:{','.join(sorted(unknown))}")
        if raw.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("ACTION_SCHEMA_UNSUPPORTED")
        action_id = str(raw.get("action_id") or "")
        if not _ACTION_ID_RE.match(action_id):
            raise ValueError("ACTION_ID_INVALID")
        try:
            sequence = int(raw.get("sequence"))
        except (TypeError, ValueError) as exc:
            raise ValueError("SEQUENCE_INVALID") from exc
        if sequence < 1:
            raise ValueError("SEQUENCE_INVALID")
        try:
            action_type = ControlActionType(str(raw.get("action_type")))
        except ValueError as exc:
            raise ValueError("ACTION_TYPE_UNSUPPORTED") from exc
        params = raw.get("parameters") or {}
        if not isinstance(params, dict):
            raise ValueError("PARAMETERS_MUST_BE_OBJECT")
        return cls(
            action_id=action_id,
            sequence=sequence,
            created_at=cls._parse_ts(raw.get("created_at"), "CREATED_AT"),
            expires_at=cls._parse_ts(raw.get("expires_at"), "EXPIRES_AT"),
            action_type=action_type,
            parameters=dict(params),
            safety=SafetySpec.from_dict(raw.get("safety")),
            requested_by=str(raw.get("requested_by") or "remote-controller"),
            note=str(raw.get("note") or ""),
        )

    @classmethod
    def load(cls, path: Path) -> "RemoteAction":
        return cls.from_dict(json.loads(path.read_text(encoding="utf-8")))

    def canonical_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "action_id": self.action_id,
            "sequence": self.sequence,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "action_type": self.action_type.value,
            "parameters": self.parameters,
            "safety": {
                "require_clean_git": self.safety.require_clean_git,
                "require_v1_authority": self.safety.require_v1_authority,
                "require_v2_disabled": self.safety.require_v2_disabled,
                "allow_worker_kill": self.safety.allow_worker_kill,
                "allow_server_quarantine": self.safety.allow_server_quarantine,
                "expected_head": self.safety.expected_head,
            },
            "requested_by": self.requested_by,
            "note": self.note,
        }

    def digest(self) -> str:
        raw = json.dumps(self.canonical_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def expired(self, now: datetime | None = None) -> bool:
        now = now or datetime.now(timezone.utc)
        return now.astimezone(timezone.utc) >= self.expires_at


@dataclass
class ControlStatus:
    action_id: str
    sequence: int
    action_type: str
    state: ControlState
    runner_id: str
    action_sha256: str
    updated_at: str
    started_at: str | None = None
    finished_at: str | None = None
    return_code: int | None = None
    verdict: str | None = None
    result_path: str | None = None
    stdout_sha256: str | None = None
    stderr_sha256: str | None = None
    error: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)
    schema_version: str = STATUS_SCHEMA_VERSION

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "action_id": self.action_id,
            "sequence": self.sequence,
            "action_type": self.action_type,
            "state": self.state.value,
            "runner_id": self.runner_id,
            "action_sha256": self.action_sha256,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "return_code": self.return_code,
            "verdict": self.verdict,
            "result_path": self.result_path,
            "stdout_sha256": self.stdout_sha256,
            "stderr_sha256": self.stderr_sha256,
            "error": self.error,
            "detail": self.detail,
        }
