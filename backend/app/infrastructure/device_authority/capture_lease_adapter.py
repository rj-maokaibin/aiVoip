from __future__ import annotations

from app.capture_v2.lease.manager import CaptureLeaseManager, LeaseToken
from app.infrastructure.device_authority.base import AuthorityMode


class CaptureLeaseCompatibilityAdapter:
    """Expose CaptureLeaseManager as the shared DeviceAuthority.

    This adapter intentionally preserves LeaseToken and lease_epoch exactly.  It
    creates no independent test lease, fencing token, table, or ownership state.
    """

    def __init__(self, manager: CaptureLeaseManager):
        self._manager = manager

    @property
    def manager(self) -> CaptureLeaseManager:
        return self._manager

    def acquire(
        self,
        *,
        device_id: str,
        run_id: str,
        owner_worker_id: str,
        mode: AuthorityMode = AuthorityMode.MUTATING,
    ) -> LeaseToken:
        if mode is not AuthorityMode.MUTATING:
            raise ValueError("AUTHORITY_MODE_UNSUPPORTED")
        return self._manager.acquire(
            device_id=device_id,
            capture_session_id=run_id,
            owner_worker_id=owner_worker_id,
        )

    def renew(self, token: LeaseToken) -> LeaseToken:
        return self._manager.renew(token)

    def validate(self, token: LeaseToken) -> LeaseToken:
        return self._manager.validate(token)

    def release(self, token: LeaseToken) -> None:
        self._manager.release(token)
