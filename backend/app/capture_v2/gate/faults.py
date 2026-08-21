from __future__ import annotations

import json
import os
import shutil
import signal
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from app.capture_v2.errors import CaptureV2Error


@dataclass
class GateFaultPlan:
    """Process-local deterministic failpoints consumed only by Gate tooling.

    The legacy counters remain the activation/count mechanism so existing Gate
    composition stays backward compatible. ``metadata.mode`` selects newer
    phase-accurate real-gate failures without changing production factories.
    """

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

    @property
    def mode(self) -> str:
        return str(self.metadata.get("mode") or "").strip().upper()


class FaultInjectingAdapter:
    """Adapter proxy for deterministic Gate transport/mutation failures.

    Production factories never use this proxy. New mutation modes deliberately
    reuse ``sftp_fail_before_get_count`` as a one-shot activation counter so the
    existing GateRunner wrapping condition remains backward compatible.
    """

    def __init__(self, adapter, plan: GateFaultPlan):
        self._adapter = adapter
        self.plan = plan

    def __getattr__(self, name: str):
        return getattr(self._adapter, name)

    def _download_faults_enabled(self) -> bool:
        return self.plan.mode not in {"ACK_RESPONSE_LOST_ONCE", "REMOTE_DELETE_FAIL_ONCE"}

    async def sftp_get(self, remote_path: str, local_path: str, timeout: float | None = None):
        if self._download_faults_enabled() and self.plan.sftp_fail_before_get_count > 0:
            self.plan.sftp_fail_before_get_count -= 1
            raise CaptureV2Error("GATE_INJECTED_SFTP_FAILURE", details={"phase": "BEFORE_GET"})
        result = await self._adapter.sftp_get(remote_path, local_path, timeout=timeout)
        if self._download_faults_enabled() and self.plan.sftp_fail_after_get_count > 0:
            self.plan.sftp_fail_after_get_count -= 1
            raise CaptureV2Error("GATE_INJECTED_SFTP_FAILURE", details={"phase": "AFTER_GET"})
        return result

    async def scp_get(self, remote_path: str, local_path: str, timeout: float | None = None):
        if self._download_faults_enabled() and self.plan.sftp_fail_before_get_count > 0:
            self.plan.sftp_fail_before_get_count -= 1
            raise CaptureV2Error("GATE_INJECTED_SCP_FAILURE", details={"phase": "BEFORE_GET"})
        result = await self._adapter.scp_get(remote_path, local_path, timeout=timeout)
        if self._download_faults_enabled() and self.plan.sftp_fail_after_get_count > 0:
            self.plan.sftp_fail_after_get_count -= 1
            raise CaptureV2Error("GATE_INJECTED_SCP_FAILURE", details={"phase": "AFTER_GET"})
        return result

    async def execute_shell(self, command: str, *args, **kwargs):
        """Inject only on fenced immutable-segment DELETE commands.

        ACK_RESPONSE_LOST_ONCE executes the real DELETE first and then drops the
        response, modelling an unknown mutation result. REMOTE_DELETE_FAIL_ONCE
        returns a deterministic non-zero command result before executing DELETE.
        """
        is_segment_delete = "rm -f --" in command and "seg_" in command and ".pcap" in command
        if is_segment_delete and self.plan.sftp_fail_before_get_count > 0:
            if self.plan.mode == "REMOTE_DELETE_FAIL_ONCE":
                self.plan.sftp_fail_before_get_count -= 1
                return SimpleNamespace(
                    exit_status=82,
                    stdout="",
                    stderr="GATE_INJECTED_REMOTE_DELETE_FAILURE",
                )
            if self.plan.mode == "ACK_RESPONSE_LOST_ONCE":
                result = await self._adapter.execute_shell(command, *args, **kwargs)
                self.plan.sftp_fail_before_get_count -= 1
                raise RuntimeError("GATE_INJECTED_ACK_RESPONSE_LOST")
        return await self._adapter.execute_shell(command, *args, **kwargs)


class FaultInjectingStore:
    def __init__(self, store, plan: GateFaultPlan):
        self._store = store
        self.plan = plan
        self.quarantine_root = Path("/tmp/capture-v2-gate-quarantine")
        self.quarantine_root.mkdir(parents=True, exist_ok=True)

    def __getattr__(self, name: str):
        return getattr(self._store, name)

    def persist(self, **kwargs):
        mode = self.plan.mode
        if mode not in {"AFTER_DURABLE_BEFORE_DB", "PERSISTED_BEFORE_ACK", "SERVER_COPY_LOSS_BEFORE_DELETE"}:
            if self.plan.persist_fail_count > 0:
                self.plan.persist_fail_count -= 1
                raise CaptureV2Error("GATE_INJECTED_SERVER_STORE_FAILURE")
            return self._store.persist(**kwargs)

        persisted = self._store.persist(**kwargs)
        if mode == "AFTER_DURABLE_BEFORE_DB" and self.plan.persist_fail_count > 0:
            self.plan.persist_fail_count -= 1
            raise CaptureV2Error(
                "GATE_INJECTED_AFTER_DURABLE_BEFORE_DB",
                details={"storage_key": kwargs.get("storage_key")},
            )
        # PERSISTED_BEFORE_ACK consumes its counter from the Pump phase hook.
        # SERVER_COPY_LOSS_BEFORE_DELETE consumes its counter in verify().
        return persisted

    def gate_after_persisted_before_ack(self, segment_id: str) -> None:
        if self.plan.mode == "PERSISTED_BEFORE_ACK" and self.plan.persist_fail_count > 0:
            self.plan.persist_fail_count -= 1
            raise CaptureV2Error(
                "GATE_INJECTED_PERSISTED_BEFORE_ACK",
                details={"segment_id": segment_id},
            )

    def verify(self, *, storage_key: str, size: int, sha256: str) -> bool:
        if self.plan.mode == "SERVER_COPY_LOSS_BEFORE_DELETE" and self.plan.persist_fail_count > 0:
            root = getattr(self._store, "root", None)
            if root is None:
                raise CaptureV2Error("GATE_SERVER_COPY_QUARANTINE_UNSUPPORTED")
            root_path = Path(root).resolve()
            target = (root_path / storage_key).resolve()
            try:
                target.relative_to(root_path)
            except ValueError as exc:
                raise CaptureV2Error("GATE_FAULT_PATH_OUTSIDE_STORE_ROOT", details={"path": str(target)}) from exc
            if target.is_file():
                token = uuid4().hex
                dst = self.quarantine_root / f"{token}__{target.name}"
                shutil.move(str(target), str(dst))
                (self.quarantine_root / f"{token}.json").write_text(
                    json.dumps({
                        "token": token,
                        "original": str(target),
                        "quarantine": str(dst),
                        "storage_key": storage_key,
                    }, indent=2) + "\n",
                    encoding="utf-8",
                )
                self.plan.persist_fail_count -= 1
                return False
        return self._store.verify(storage_key=storage_key, size=size, sha256=sha256)


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
