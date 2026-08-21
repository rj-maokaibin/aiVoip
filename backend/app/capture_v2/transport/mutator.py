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
from app.capture_v2.transport.shell_scripts import fenced_script, publish_fence_script


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

    async def publish_fence(self, token: LeaseToken, *, boot_id: str, operation_id: str | None = None) -> None:
        # Never allow a delayed worker to publish an already-expired authority term.
        # The DUT script independently enforces monotonic epoch publication as the
        # second fence, so a stale worker cannot roll N+1 back to N.
        self._ensure_token_live(token)
        operation_id = operation_id or str(uuid4())
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
