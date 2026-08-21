from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class GateVerdict(StrEnum):
    PASS = "PASS"
    FAIL = "FAIL"
    INCONCLUSIVE = "INCONCLUSIVE"
    DEFERRED_REAL_GATE = "DEFERRED_REAL_GATE"


@dataclass(frozen=True)
class GateCheck:
    name: str
    passed: bool | None
    expected: Any = None
    observed: Any = None
    details: dict[str, Any] = field(default_factory=dict)

    @property
    def verdict(self) -> GateVerdict:
        if self.passed is True:
            return GateVerdict.PASS
        if self.passed is False:
            return GateVerdict.FAIL
        return GateVerdict.INCONCLUSIVE


@dataclass(frozen=True)
class GateCaseResult:
    gate_id: str
    verdict: GateVerdict
    checks: tuple[GateCheck, ...]
    summary: str
    evidence_bundle: str | None = None
    facts: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=utcnow_iso)

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["verdict"] = self.verdict.value
        payload["checks"] = [
            {**asdict(check), "verdict": check.verdict.value} for check in self.checks
        ]
        return payload


@dataclass(frozen=True)
class GateDeviceSpec:
    device_id: str
    model: str
    host: str
    port: int = 22
    username: str = "admin"
    platform_id: str | None = None

    def as_profile_device(self):
        from types import SimpleNamespace

        info = {"model": self.model}
        if self.platform_id:
            info["platform"] = self.platform_id
        return SimpleNamespace(
            id=self.device_id,
            platform_id=self.platform_id,
            device_info=info,
        )


@dataclass(frozen=True)
class GateRunPaths:
    root: Path
    case_dir: Path
    db_dir: Path
    dut_dir: Path
    server_dir: Path
    logs_dir: Path

    @classmethod
    def create(cls, root: Path, gate_id: str, device_id: str) -> "GateRunPaths":
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        safe_gate = gate_id.replace("/", "-")
        safe_device = device_id.replace("/", "-")
        case_dir = Path(root) / f"{stamp}_{safe_gate}_{safe_device}"
        paths = cls(
            root=Path(root),
            case_dir=case_dir,
            db_dir=case_dir / "db",
            dut_dir=case_dir / "dut",
            server_dir=case_dir / "server",
            logs_dir=case_dir / "logs",
        )
        for path in (paths.case_dir, paths.db_dir, paths.dut_dir, paths.server_dir, paths.logs_dir):
            path.mkdir(parents=True, exist_ok=True)
        return paths
