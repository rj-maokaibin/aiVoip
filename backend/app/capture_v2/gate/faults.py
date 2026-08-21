from __future__ import annotations

import json
import os
import shutil
import signal
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.capture_v2.errors import CaptureV2Error


@dataclass
class GateFaultPlan:
    """Process-local deterministic failpoints consumed only by Gate tooling."""

    sftp_fail_before_get_count: int = 0
    sftp_fail_after_get_count: int = 0
    persist_fail_count: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path | None) -> "GateFaultPlan":
        if path is None:
            return cls()
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(
            sftp_fail_before_get_count=max(0, int(raw.get("sftp_fail_before_get_count", 0))),
            sftp_fail_after_get_count=max(0, int(raw.get("sftp_fail_after_get_count", 0))),
            persist_fail_count=max(0, int(raw.get("persist_fail_count", 0))),
            metadata=dict(raw.get("metadata") or {}),
        )


class FaultInjectingAdapter:
    """Adapter proxy for deterministic SFTP Gate failures.

    Shell/CLI behavior is passed through unchanged. The proxy exists only when a
    Gate command explicitly loads a fault plan; production factories never use it.
    """

    def __init__(self, adapter, plan: GateFaultPlan):
        self._adapter = adapter
        self.plan = plan

    def __getattr__(self, name: str):
        return getattr(self._adapter, name)

    async def sftp_get(self, remote_path: str, local_path: str, timeout: float | None = None):
        if self.plan.sftp_fail_before_get_count > 0:
            self.plan.sftp_fail_before_get_count -= 1
            raise CaptureV2Error("GATE_INJECTED_SFTP_FAILURE", details={"phase": "BEFORE_GET"})
        result = await self._adapter.sftp_get(remote_path, local_path, timeout=timeout)
        if self.plan.sftp_fail_after_get_count > 0:
            self.plan.sftp_fail_after_get_count -= 1
            raise CaptureV2Error("GATE_INJECTED_SFTP_FAILURE", details={"phase": "AFTER_GET"})
        return result

    async def scp_get(self, remote_path: str, local_path: str, timeout: float | None = None):
        # Same deterministic failpoints for SCP transport (R3-02 interrupt under
        # --transport scp): fail before the exact GET, or fail after a successful
        # GET to simulate a lost transfer response.
        if self.plan.sftp_fail_before_get_count > 0:
            self.plan.sftp_fail_before_get_count -= 1
            raise CaptureV2Error("GATE_INJECTED_SCP_FAILURE", details={"phase": "BEFORE_GET"})
        result = await self._adapter.scp_get(remote_path, local_path, timeout=timeout)
        if self.plan.sftp_fail_after_get_count > 0:
            self.plan.sftp_fail_after_get_count -= 1
            raise CaptureV2Error("GATE_INJECTED_SCP_FAILURE", details={"phase": "AFTER_GET"})
        return result


class FaultInjectingStore:
    def __init__(self, store, plan: GateFaultPlan):
        self._store = store
        self.plan = plan

    def __getattr__(self, name: str):
        return getattr(self._store, name)

    def persist(self, **kwargs):
        if self.plan.persist_fail_count > 0:
            self.plan.persist_fail_count -= 1
            raise CaptureV2Error("GATE_INJECTED_SERVER_STORE_FAILURE")
        return self._store.persist(**kwargs)


class GateFaultInjector:
    """Explicit reversible fault operations for real Gate execution."""

    def __init__(self, *, store_root: Path | None = None, quarantine_root: Path | None = None):
        self.store_root = Path(store_root).resolve() if store_root else None
        self.quarantine_root = Path(quarantine_root or "/tmp/capture-v2-gate-quarantine").resolve()
        self.quarantine_root.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def signal_worker(pid: int, action: str) -> None:
        pid = int(pid)
        if pid <= 1:
            raise CaptureV2Error("GATE_FAULT_PID_REFUSED", details={"pid": pid})
        mapping = {"kill": signal.SIGKILL, "term": signal.SIGTERM, "pause": signal.SIGSTOP, "resume": signal.SIGCONT}
        if action not in mapping:
            raise CaptureV2Error("GATE_FAULT_ACTION_INVALID", details={"action": action})
        try:
            os.kill(pid, mapping[action])
        except ProcessLookupError as exc:
            raise CaptureV2Error("GATE_FAULT_PROCESS_NOT_FOUND", details={"pid": pid}) from exc

    def _assert_store_path(self, path: Path) -> Path:
        resolved = Path(path).resolve()
        if self.store_root is None:
            raise CaptureV2Error("GATE_STORE_ROOT_REQUIRED")
        try:
            resolved.relative_to(self.store_root)
        except ValueError as exc:
            raise CaptureV2Error(
                "GATE_FAULT_PATH_OUTSIDE_STORE_ROOT",
                details={"path": str(resolved), "store_root": str(self.store_root)},
            ) from exc
        return resolved

    def quarantine_server_copy(self, path: Path) -> dict[str, str]:
        src = self._assert_store_path(path)
        if not src.is_file():
            raise CaptureV2Error("GATE_FAULT_SOURCE_NOT_FOUND", details={"path": str(src)})
        token = uuid4().hex
        dst = self.quarantine_root / f"{token}__{src.name}"
        shutil.move(str(src), str(dst))
        record = {"token": token, "original": str(src), "quarantine": str(dst)}
        (self.quarantine_root / f"{token}.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        return record

    def restore_quarantined(self, token: str) -> dict[str, str]:
        meta = self.quarantine_root / f"{token}.json"
        if not meta.is_file():
            raise CaptureV2Error("GATE_FAULT_TOKEN_NOT_FOUND", details={"token": token})
        record = json.loads(meta.read_text(encoding="utf-8"))
        src = Path(record["quarantine"])
        dst = self._assert_store_path(Path(record["original"]))
        if dst.exists():
            raise CaptureV2Error("GATE_FAULT_RESTORE_TARGET_EXISTS", details={"path": str(dst)})
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(src), str(dst))
        meta.unlink(missing_ok=True)
        return record
