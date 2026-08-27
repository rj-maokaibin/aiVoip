from __future__ import annotations

import asyncio
from uuid import uuid4
from typing import TYPE_CHECKING, Any
from datetime import datetime, timezone

from app.capture_v2.errors import CaptureV2Error

if TYPE_CHECKING:
    from app.capture_v2.lease.manager import LeaseToken
else:
    LeaseToken = Any
from app.capture_v2.transport.readonly import ReadOnlyDeviceTransport
from app.capture_v2.transport.shell_scripts import (
    clear_stale_fence_script,
    fenced_script,
    publish_fence_script,
    release_fence_script,
)


class FencedDeviceMutator:
    """All DUT mutations run once, under op.lock and lease_epoch fencing.

    No blind SSH retry is allowed. If transport outcome is unknown, callers must
    inspect actual DUT state before deciding whether to issue a new operation.
    """

    def __init__(self, adapter, reader: ReadOnlyDeviceTransport):
        self.adapter = adapter
        self.reader = reader
        # One authority worker may have several async tasks, but DUT mutations must
        # still be serialized locally. DB lease fencing protects between workers;
        # this lock protects concurrent operations inside the winning worker.
        self._mutation_lock = asyncio.Lock()

    async def _run_once(self, command: str, *, operation_id: str) -> str:
        async with self._mutation_lock:
            try:
                result = await self.adapter.execute_shell(command, retries=0)
            except Exception as exc:
                raise CaptureV2Error(
                    "MUTATION_RESULT_UNKNOWN", details={"operation_id": operation_id, "error": type(exc).__name__}
                ) from exc
        status = int(result.exit_status or 0)
        if status == 73:
            raise CaptureV2Error("LEASE_FENCED", details={"operation_id": operation_id})
        if status == 75:
            raise CaptureV2Error("OPERATION_LOCK_BUSY", details={"operation_id": operation_id})
        if status == 74:
            raise CaptureV2Error("PRODUCER_IDENTITY_MISMATCH", details={"operation_id": operation_id})
        if status != 0:
            raise CaptureV2Error(
                "DEVICE_MUTATION_FAILED",
                details={
                    "operation_id": operation_id,
                    "exit_status": status,
                    "stdout": result.stdout,
                    "stderr": result.stderr,
                },
            )
        return result.stdout or ""

    @staticmethod
    def _ensure_token_live(token: LeaseToken) -> None:
        expires = token.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) >= expires:
            raise CaptureV2Error(
                "LEASE_EXPIRED_LOCAL",
                details={"lease_epoch": token.lease_epoch, "expires_at": expires.isoformat()},
            )

    async def _read_control_state(self) -> dict[str, str]:
        """Best-effort read of the DUT-side capture fence metadata."""
        try:
            return {
                "lease_epoch": (
                    await self.reader.read_text(
                        "/tmp/aivoip_capture/control/lease_epoch", missing_ok=True
                    )
                )
                or "",
                "session_id": (
                    await self.reader.read_text(
                        "/tmp/aivoip_capture/control/session_id", missing_ok=True
                    )
                )
                or "",
                "owner_worker": (
                    await self.reader.read_text(
                        "/tmp/aivoip_capture/control/owner_worker", missing_ok=True
                    )
                )
                or "",
            }
        except Exception:
            return {}

    async def _clear_stale_fence(self, *, operation_id: str) -> None:
        script = clear_stale_fence_script(operation_id=operation_id)
        await self._run_once(script, operation_id=operation_id)

    async def _heal_stale_fence(self, token: LeaseToken) -> None:
        """Take over a DUT fence left by a dead prior session.

        A crash/abort without cleanup leaves the DUT control files behind; a fresh
        reproduction on the same DUT would otherwise be permanently LEASE_FENCED.
        Only clear the stale control when no live capture producer exists; a live
        foreign capture keeps the strict fence (LEASE_FENCED) intact.
        """
        control = await self._read_control_state()
        foreign = (
            control.get("session_id") not in ("", token.capture_session_id)
            or control.get("owner_worker") not in ("", token.owner_worker_id)
        )
        if not foreign:
            return
        try:
            from app.capture_v2.recovery.scanner import RecoveryScanner

            inventory = await RecoveryScanner(self.reader).scan()
        except Exception:
            return  # best-effort: fall back to the strict fence on scan failure
        if inventory.v2_producers or inventory.legacy_producers:
            return  # a live capture owns the DUT -> strict fence preserved
        await self._clear_stale_fence(operation_id=str(uuid4()))

    async def publish_fence(self, token: LeaseToken, *, boot_id: str, operation_id: str | None = None) -> None:
        # Never allow a delayed worker to publish an already-expired authority term.
        # The DUT script independently enforces monotonic epoch publication as the
        # second fence, so a stale worker cannot roll N+1 back to N.
        self._ensure_token_live(token)
        operation_id = operation_id or str(uuid4())
        # Self-heal stale fence state from a dead prior session before publishing
        # our own, otherwise the DUT refuses the new authority with LEASE_FENCED.
        await self._heal_stale_fence(token)
        script = publish_fence_script(
            lease_epoch=token.lease_epoch,
            session_id=token.capture_session_id,
            owner_worker=token.owner_worker_id,
            boot_id=boot_id,
            operation_id=operation_id,
        )
        try:
            await self._run_once(script, operation_id=operation_id)
        except CaptureV2Error as exc:
            if exc.code != "MUTATION_RESULT_UNKNOWN":
                raise
            # Observe-before-retry: a lost response is success if the fence is visible.
            epoch = await self.reader.read_text("/tmp/aivoip_capture/control/lease_epoch", missing_ok=True)
            session_id = await self.reader.read_text("/tmp/aivoip_capture/control/session_id", missing_ok=True)
            owner = await self.reader.read_text("/tmp/aivoip_capture/control/owner_worker", missing_ok=True)
            if epoch == str(token.lease_epoch) and session_id == token.capture_session_id and owner == token.owner_worker_id:
                return
            raise

    async def release_fence(self, token: LeaseToken, *, operation_id: str | None = None) -> None:
        """Fenced removal of the DUT capture fence after a session finalizes.

        Leaves the DUT pristine so the next reproduction can publish a fresh
        epoch without a stale-owner LEASE_FENCED.  Only the current lease
        authority (matching DUT lease_epoch) may clear it.
        """
        self._ensure_token_live(token)
        operation_id = operation_id or str(uuid4())
        script = release_fence_script(lease_epoch=token.lease_epoch, operation_id=operation_id)
        await self._run_once(script, operation_id=operation_id)

    async def execute_fenced(
        self,
        token: LeaseToken,
        *,
        body: str,
        operation_id: str | None = None,
    ) -> str:
        self._ensure_token_live(token)
        operation_id = operation_id or str(uuid4())
        return await self._run_once(
            fenced_script(lease_epoch=token.lease_epoch, operation_id=operation_id, body=body),
            operation_id=operation_id,
        )
