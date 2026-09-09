from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from sqlalchemy import select

from app.automation.pbx_models import PbxExtensionResource, PbxResourceLease
from app.automation.adapters.pbx.identity import (
    PbxExtensionIdentityError, normalize_dial_alias, normalize_testlab_provider_mutation_identity,
)
from app.core.ids import new_id


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PbxResourceType(str, Enum):
    STATIC_BASELINE = "STATIC_BASELINE"
    TEMPORARY_AUTOMATION = "TEMPORARY_AUTOMATION"


class PbxResourceState(str, Enum):
    FREE = "FREE"
    RESERVED = "RESERVED"
    PROVISIONING = "PROVISIONING"
    READY = "READY"
    IN_USE = "IN_USE"
    CLEANUP = "CLEANUP"
    DIRTY = "DIRTY"
    RECOVERING = "RECOVERING"


class PbxLeaseState(str, Enum):
    ACTIVE = "ACTIVE"
    RELEASED = "RELEASED"
    EXPIRED = "EXPIRED"


class PbxResourceAuthorityError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class PbxLeaseToken:
    lease_id: str
    resource_id: str
    pbx_node_id: str
    domain_id: str
    extension: str
    run_id: str
    owner_worker_id: str
    lease_epoch: int
    expires_at: datetime


class PbxExtensionLeaseManager:
    """Persisted extension authority with epoch fencing.

    The database row is the authority source. PBX mutation must validate this
    token immediately before the provider call; a stale epoch is fail-closed.
    """

    def __init__(self, session_factory, *, ttl_seconds: float = 60.0) -> None:
        if ttl_seconds <= 0:
            raise ValueError("PBX_LEASE_TTL_INVALID")
        self.session_factory = session_factory
        self.ttl_seconds = float(ttl_seconds)

    def _token(self, row: PbxExtensionResource, lease: PbxResourceLease) -> PbxLeaseToken:
        return PbxLeaseToken(
            lease_id=lease.id,
            resource_id=row.id,
            pbx_node_id=row.pbx_node_id,
            domain_id=row.domain_id,
            extension=row.extension,
            run_id=lease.run_id,
            owner_worker_id=lease.owner_worker_id,
            lease_epoch=int(lease.lease_epoch),
            expires_at=lease.expires_at,
        )

    def acquire_extension(
        self,
        *,
        pbx_node_id: str,
        extension: str,
        run_id: str,
        owner_worker_id: str,
    ) -> PbxLeaseToken:
        try:
            extension = normalize_testlab_provider_mutation_identity(extension)
        except PbxExtensionIdentityError as exc:
            raise PbxResourceAuthorityError("PBX_PROVIDER_IDENTITY_INVALID") from exc
        now = utcnow()
        with self.session_factory() as session:
            row = session.execute(
                select(PbxExtensionResource)
                .where(
                    PbxExtensionResource.pbx_node_id == pbx_node_id,
                    PbxExtensionResource.extension == str(extension),
                )
                .with_for_update()
            ).scalar_one_or_none()
            if row is None:
                raise PbxResourceAuthorityError("PBX_EXTENSION_NOT_FOUND")
            if row.resource_type != PbxResourceType.TEMPORARY_AUTOMATION.value:
                raise PbxResourceAuthorityError("PBX_RESOURCE_PROTECTED")
            if row.state in {PbxResourceState.DIRTY.value, PbxResourceState.RECOVERING.value}:
                raise PbxResourceAuthorityError("PBX_RESOURCE_DIRTY")

            if row.lease_id:
                lease = session.get(PbxResourceLease, row.lease_id)
                if lease and lease.state == PbxLeaseState.ACTIVE.value:
                    expires = lease.expires_at
                    if expires.tzinfo is None:
                        expires = expires.replace(tzinfo=timezone.utc)
                    if expires > now:
                        if lease.run_id == run_id and lease.owner_worker_id == owner_worker_id:
                            return self._token(row, lease)
                        raise PbxResourceAuthorityError("PBX_RESOURCE_BUSY")
                    lease.state = PbxLeaseState.EXPIRED.value
                    lease.released_at = now

            if row.state not in {PbxResourceState.FREE.value, PbxResourceState.RESERVED.value}:
                raise PbxResourceAuthorityError("PBX_RESOURCE_BUSY")

            lease_id = new_id()
            epoch = int(row.lease_epoch or 0) + 1
            expires_at = now + timedelta(seconds=self.ttl_seconds)
            lease = PbxResourceLease(
                id=lease_id,
                pbx_node_id=row.pbx_node_id,
                resource_id=row.id,
                run_id=run_id,
                owner_worker_id=owner_worker_id,
                lease_epoch=epoch,
                state=PbxLeaseState.ACTIVE.value,
                acquired_at=now,
                expires_at=expires_at,
            )
            session.add(lease)
            row.lease_id = lease_id
            row.lease_epoch = epoch
            row.owner_run_id = run_id
            row.state = PbxResourceState.RESERVED.value
            session.commit()
            return self._token(row, lease)

    def acquire_endpoint(
        self,
        *,
        pbx_node_id: str,
        extension: str,
        dial_alias: str | None,
        run_id: str,
        owner_worker_id: str,
    ) -> PbxLeaseToken:
        try:
            extension = normalize_testlab_provider_mutation_identity(extension)
            alias = normalize_dial_alias(dial_alias) if dial_alias is not None else None
        except PbxExtensionIdentityError as exc:
            raise PbxResourceAuthorityError("PBX_ENDPOINT_IDENTITY_INVALID") from exc
        if alias is None:
            return self.acquire_extension(
                pbx_node_id=pbx_node_id, extension=extension, run_id=run_id,
                owner_worker_id=owner_worker_id,
            )
        if alias == extension:
            raise PbxResourceAuthorityError("PBX_DIAL_ALIAS_EQUALS_IDENTITY")

        now = utcnow()
        with self.session_factory() as session:
            rows = session.execute(
                select(PbxExtensionResource)
                .where(
                    PbxExtensionResource.pbx_node_id == pbx_node_id,
                    PbxExtensionResource.extension.in_([extension, alias]),
                )
                .order_by(PbxExtensionResource.extension.asc())
                .with_for_update()
            ).scalars().all()
            by_extension = {row.extension: row for row in rows}
            primary = by_extension.get(extension)
            alias_row = by_extension.get(alias)
            if primary is None or alias_row is None:
                raise PbxResourceAuthorityError("PBX_ENDPOINT_RESOURCE_NOT_FOUND")
            if primary.domain_id != alias_row.domain_id:
                raise PbxResourceAuthorityError("PBX_ENDPOINT_DOMAIN_MISMATCH")

            active: dict[str, PbxResourceLease] = {}
            for row in (primary, alias_row):
                if row.resource_type != PbxResourceType.TEMPORARY_AUTOMATION.value:
                    raise PbxResourceAuthorityError("PBX_RESOURCE_PROTECTED")
                if row.state in {PbxResourceState.DIRTY.value, PbxResourceState.RECOVERING.value}:
                    raise PbxResourceAuthorityError("PBX_RESOURCE_DIRTY")
                if row.lease_id:
                    lease = session.get(PbxResourceLease, row.lease_id)
                    if lease and lease.state == PbxLeaseState.ACTIVE.value:
                        expires = lease.expires_at
                        if expires.tzinfo is None:
                            expires = expires.replace(tzinfo=timezone.utc)
                        if expires > now:
                            if lease.run_id != run_id or lease.owner_worker_id != owner_worker_id:
                                raise PbxResourceAuthorityError("PBX_RESOURCE_BUSY")
                            active[row.extension] = lease
                        else:
                            lease.state = PbxLeaseState.EXPIRED.value
                            lease.released_at = now
                if row.state not in {PbxResourceState.FREE.value, PbxResourceState.RESERVED.value}:
                    raise PbxResourceAuthorityError("PBX_RESOURCE_BUSY")

            if active:
                if set(active) != {extension, alias}:
                    raise PbxResourceAuthorityError("PBX_ENDPOINT_PARTIAL_LEASE")
                alias_lease = active[alias]
                if not (
                    primary.dial_alias == alias
                    and primary.dial_alias_resource_id == alias_row.id
                    and primary.dial_alias_lease_id == alias_lease.id
                    and int(primary.dial_alias_lease_epoch or -1) == int(alias_lease.lease_epoch)
                ):
                    raise PbxResourceAuthorityError("PBX_ENDPOINT_BINDING_MISMATCH")
                return self._token(primary, active[extension])

            expires_at = now + timedelta(seconds=self.ttl_seconds)
            leases: dict[str, PbxResourceLease] = {}
            for row in (primary, alias_row):
                epoch = int(row.lease_epoch or 0) + 1
                lease = PbxResourceLease(
                    id=new_id(), pbx_node_id=row.pbx_node_id, resource_id=row.id,
                    run_id=run_id, owner_worker_id=owner_worker_id, lease_epoch=epoch,
                    state=PbxLeaseState.ACTIVE.value, acquired_at=now, expires_at=expires_at,
                )
                session.add(lease)
                row.lease_id = lease.id
                row.lease_epoch = epoch
                row.owner_run_id = run_id
                row.state = PbxResourceState.RESERVED.value
                leases[row.extension] = lease

            alias_lease = leases[alias]
            primary.dial_alias = alias
            primary.dial_alias_resource_id = alias_row.id
            primary.dial_alias_lease_id = alias_lease.id
            primary.dial_alias_lease_epoch = int(alias_lease.lease_epoch)
            session.commit()
            return self._token(primary, leases[extension])

    def acquire_recovery(
        self,
        *,
        pbx_node_id: str,
        extension: str,
        run_id: str,
        owner_worker_id: str,
    ) -> PbxLeaseToken:
        now = utcnow()
        with self.session_factory() as session:
            row = session.execute(
                select(PbxExtensionResource).where(
                    PbxExtensionResource.pbx_node_id == pbx_node_id,
                    PbxExtensionResource.extension == str(extension),
                ).with_for_update()
            ).scalar_one_or_none()
            if row is None:
                raise PbxResourceAuthorityError("PBX_EXTENSION_NOT_FOUND")
            if row.resource_type != PbxResourceType.TEMPORARY_AUTOMATION.value:
                raise PbxResourceAuthorityError("PBX_RESOURCE_PROTECTED")
            if row.state not in {PbxResourceState.DIRTY.value, PbxResourceState.RECOVERING.value}:
                raise PbxResourceAuthorityError("PBX_RESOURCE_NOT_RECOVERABLE")
            if not row.created_by_automation or not str(row.ownership_marker or "").startswith("AIVOIP_AUTOMATION:"):
                raise PbxResourceAuthorityError("PBX_RESOURCE_OWNERSHIP_UNPROVEN")
            if row.lease_id:
                prior = session.get(PbxResourceLease, row.lease_id)
                if prior and prior.state == PbxLeaseState.ACTIVE.value:
                    expires = prior.expires_at
                    if expires.tzinfo is None:
                        expires = expires.replace(tzinfo=timezone.utc)
                    if expires > now:
                        if prior.run_id == run_id and prior.owner_worker_id == owner_worker_id:
                            return self._token(row, prior)
                        raise PbxResourceAuthorityError("PBX_RESOURCE_BUSY")
                    prior.state = PbxLeaseState.EXPIRED.value
                    prior.released_at = now
            epoch = int(row.lease_epoch or 0) + 1
            lease = PbxResourceLease(
                id=new_id(), pbx_node_id=row.pbx_node_id, resource_id=row.id,
                run_id=run_id, owner_worker_id=owner_worker_id, lease_epoch=epoch,
                state=PbxLeaseState.ACTIVE.value, acquired_at=now,
                expires_at=now + timedelta(seconds=self.ttl_seconds),
            )
            session.add(lease)
            row.lease_id = lease.id
            row.lease_epoch = epoch
            row.owner_run_id = run_id
            row.state = PbxResourceState.RECOVERING.value
            session.commit()
            return self._token(row, lease)

    def resume_active_lease(
        self,
        *,
        pbx_node_id: str,
        extension: str,
        run_id: str,
        owner_worker_id: str,
    ) -> PbxLeaseToken:
        now = utcnow()
        with self.session_factory() as session:
            row = session.execute(
                select(PbxExtensionResource).where(
                    PbxExtensionResource.pbx_node_id == pbx_node_id,
                    PbxExtensionResource.extension == str(extension),
                )
            ).scalar_one_or_none()
            if row is None or not row.lease_id:
                raise PbxResourceAuthorityError("PBX_ACTIVE_LEASE_NOT_FOUND")
            lease = session.get(PbxResourceLease, row.lease_id)
            if lease is None or lease.state != PbxLeaseState.ACTIVE.value:
                raise PbxResourceAuthorityError("PBX_ACTIVE_LEASE_NOT_FOUND")
            expires = lease.expires_at
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            if expires <= now:
                raise PbxResourceAuthorityError("PBX_LEASE_EXPIRED")
            if lease.run_id != run_id or lease.owner_worker_id != owner_worker_id:
                raise PbxResourceAuthorityError("PBX_LEASE_FENCED")
            if row.owner_run_id != run_id or int(row.lease_epoch) != int(lease.lease_epoch):
                raise PbxResourceAuthorityError("PBX_LEASE_FENCED")
            return self._token(row, lease)

    def acquire_first_available(
        self, *, pbx_node_id: str, run_id: str, owner_worker_id: str
    ) -> PbxLeaseToken:
        with self.session_factory() as session:
            candidates = session.execute(
                select(PbxExtensionResource.extension)
                .where(
                    PbxExtensionResource.pbx_node_id == pbx_node_id,
                    PbxExtensionResource.resource_type == PbxResourceType.TEMPORARY_AUTOMATION.value,
                    PbxExtensionResource.state == PbxResourceState.FREE.value,
                )
                .order_by(PbxExtensionResource.extension.asc())
            ).scalars().all()
        for extension in candidates:
            try:
                return self.acquire_extension(
                    pbx_node_id=pbx_node_id,
                    extension=extension,
                    run_id=run_id,
                    owner_worker_id=owner_worker_id,
                )
            except PbxResourceAuthorityError as exc:
                if exc.code != "PBX_RESOURCE_BUSY":
                    raise
        raise PbxResourceAuthorityError("PBX_RESOURCE_POOL_EXHAUSTED")

    def _paired_alias_matches(self, session, row: PbxExtensionResource, token: PbxLeaseToken, now: datetime) -> bool:
        if not row.dial_alias:
            return True
        if not row.dial_alias_resource_id or not row.dial_alias_lease_id or row.dial_alias_lease_epoch is None:
            return False
        alias_row = session.get(PbxExtensionResource, row.dial_alias_resource_id)
        alias_lease = session.get(PbxResourceLease, row.dial_alias_lease_id)
        if alias_row is None or alias_lease is None:
            return False
        expires = alias_lease.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
        return bool(
            alias_row.pbx_node_id == row.pbx_node_id
            and alias_row.domain_id == row.domain_id
            and alias_row.extension == row.dial_alias
            and alias_row.lease_id == row.dial_alias_lease_id
            and int(alias_row.lease_epoch) == int(row.dial_alias_lease_epoch)
            and alias_row.owner_run_id == token.run_id
            and alias_lease.state == PbxLeaseState.ACTIVE.value
            and alias_lease.run_id == token.run_id
            and alias_lease.owner_worker_id == token.owner_worker_id
            and int(alias_lease.lease_epoch) == int(row.dial_alias_lease_epoch)
            and expires > now
        )

    def validate(self, token: PbxLeaseToken) -> bool:
        now = utcnow()
        with self.session_factory() as session:
            row = session.get(PbxExtensionResource, token.resource_id)
            lease = session.get(PbxResourceLease, token.lease_id)
            if row is None or lease is None:
                return False
            expires = lease.expires_at
            if expires.tzinfo is None:
                expires = expires.replace(tzinfo=timezone.utc)
            primary_ok = bool(
                row.lease_id == token.lease_id
                and int(row.lease_epoch) == token.lease_epoch
                and row.owner_run_id == token.run_id
                and lease.state == PbxLeaseState.ACTIVE.value
                and lease.run_id == token.run_id
                and lease.owner_worker_id == token.owner_worker_id
                and int(lease.lease_epoch) == token.lease_epoch
                and expires > now
            )
            return primary_ok and self._paired_alias_matches(session, row, token, now)

    def validate_dial_alias_binding(self, token: PbxLeaseToken, dial_alias: str) -> bool:
        if not self.validate(token):
            return False
        with self.session_factory() as session:
            row = session.get(PbxExtensionResource, token.resource_id)
            return bool(row is not None and row.dial_alias == str(dial_alias))

    def renew(self, token: PbxLeaseToken) -> PbxLeaseToken:
        now = utcnow()
        with self.session_factory() as session:
            row = session.execute(
                select(PbxExtensionResource)
                .where(PbxExtensionResource.id == token.resource_id)
                .with_for_update()
            ).scalar_one_or_none()
            lease = session.get(PbxResourceLease, token.lease_id)
            if row is None or lease is None or not self._matches(row, lease, token):
                raise PbxResourceAuthorityError("PBX_LEASE_FENCED")
            if not self._paired_alias_matches(session, row, token, now):
                raise PbxResourceAuthorityError("PBX_DIAL_ALIAS_LEASE_FENCED")
            expires_at = now + timedelta(seconds=self.ttl_seconds)
            lease.renewed_at = now
            lease.expires_at = expires_at
            if row.dial_alias_lease_id:
                alias_lease = session.get(PbxResourceLease, row.dial_alias_lease_id)
                if alias_lease is None:
                    raise PbxResourceAuthorityError("PBX_DIAL_ALIAS_LEASE_FENCED")
                alias_lease.renewed_at = now
                alias_lease.expires_at = expires_at
            session.commit()
            return self._token(row, lease)

    def _set_dial_alias_state(self, token: PbxLeaseToken, state: str) -> None:
        now = utcnow()
        with self.session_factory() as session:
            row = session.execute(
                select(PbxExtensionResource)
                .where(PbxExtensionResource.id == token.resource_id)
                .with_for_update()
            ).scalar_one_or_none()
            lease = session.get(PbxResourceLease, token.lease_id)
            if row is None or lease is None or not self._matches(row, lease, token):
                raise PbxResourceAuthorityError("PBX_LEASE_FENCED")
            if not row.dial_alias:
                return
            if not self._paired_alias_matches(session, row, token, now):
                raise PbxResourceAuthorityError("PBX_DIAL_ALIAS_LEASE_FENCED")
            alias_row = session.execute(
                select(PbxExtensionResource)
                .where(PbxExtensionResource.id == row.dial_alias_resource_id)
                .with_for_update()
            ).scalar_one_or_none()
            if alias_row is None:
                raise PbxResourceAuthorityError("PBX_DIAL_ALIAS_RESOURCE_NOT_FOUND")
            alias_row.state = state
            if state == PbxResourceState.FREE.value:
                alias_row.last_cleanup_at = now
                alias_row.dirty_reason = None
            session.commit()

    def mark_dial_alias_in_use(self, token: PbxLeaseToken) -> None:
        self._set_dial_alias_state(token, PbxResourceState.IN_USE.value)

    def mark_dial_alias_free(self, token: PbxLeaseToken) -> None:
        self._set_dial_alias_state(token, PbxResourceState.FREE.value)

    @staticmethod
    def _matches(row: PbxExtensionResource, lease: PbxResourceLease, token: PbxLeaseToken) -> bool:
        return bool(
            row.lease_id == token.lease_id
            and int(row.lease_epoch) == token.lease_epoch
            and row.owner_run_id == token.run_id
            and lease.state == PbxLeaseState.ACTIVE.value
            and lease.run_id == token.run_id
            and lease.owner_worker_id == token.owner_worker_id
            and int(lease.lease_epoch) == token.lease_epoch
        )

    def release(self, token: PbxLeaseToken) -> None:
        now = utcnow()
        with self.session_factory() as session:
            row = session.execute(
                select(PbxExtensionResource)
                .where(PbxExtensionResource.id == token.resource_id)
                .with_for_update()
            ).scalar_one_or_none()
            lease = session.get(PbxResourceLease, token.lease_id)
            if row is None or lease is None or not self._matches(row, lease, token):
                raise PbxResourceAuthorityError("PBX_LEASE_FENCED")
            if row.state != PbxResourceState.FREE.value:
                raise PbxResourceAuthorityError("PBX_RESOURCE_CLEANUP_REQUIRED")
            if not self._paired_alias_matches(session, row, token, now):
                raise PbxResourceAuthorityError("PBX_DIAL_ALIAS_LEASE_FENCED")

            if row.dial_alias:
                alias_row = session.execute(
                    select(PbxExtensionResource)
                    .where(PbxExtensionResource.id == row.dial_alias_resource_id)
                    .with_for_update()
                ).scalar_one_or_none()
                alias_lease = session.get(PbxResourceLease, row.dial_alias_lease_id)
                if alias_row is None or alias_lease is None:
                    raise PbxResourceAuthorityError("PBX_DIAL_ALIAS_LEASE_FENCED")
                if alias_row.state != PbxResourceState.FREE.value:
                    raise PbxResourceAuthorityError("PBX_DIAL_ALIAS_CLEANUP_REQUIRED")
                alias_lease.state = PbxLeaseState.RELEASED.value
                alias_lease.released_at = now
                alias_row.lease_id = None
                alias_row.owner_run_id = None

            lease.state = PbxLeaseState.RELEASED.value
            lease.released_at = now
            row.lease_id = None
            row.owner_run_id = None
            row.dial_alias = None
            row.dial_alias_resource_id = None
            row.dial_alias_lease_id = None
            row.dial_alias_lease_epoch = None
            session.commit()
