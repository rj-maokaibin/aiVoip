from __future__ import annotations

from enum import Enum
from typing import Protocol, TypeVar


AuthorityTokenT = TypeVar("AuthorityTokenT")


class AuthorityMode(str, Enum):
    MUTATING = "mutating"


class DeviceAuthority(Protocol[AuthorityTokenT]):
    """Single authority abstraction for state-changing DUT operations."""

    def acquire(
        self,
        *,
        device_id: str,
        run_id: str,
        owner_worker_id: str,
        mode: AuthorityMode = AuthorityMode.MUTATING,
    ) -> AuthorityTokenT: ...

    def renew(self, token: AuthorityTokenT) -> AuthorityTokenT: ...

    def validate(self, token: AuthorityTokenT) -> AuthorityTokenT: ...

    def release(self, token: AuthorityTokenT) -> None: ...
