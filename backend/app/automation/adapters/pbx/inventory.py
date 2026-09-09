from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from app.automation.adapters.pbx.fusionpbx_config import FusionPbxConfigProvider
from app.automation.adapters.pbx.identity import PbxExtensionIdentityError, normalize_testlab_provider_mutation_identity
from app.automation.adapters.pbx.profile import FusionPbxLabProfile
from app.automation.adapters.pbx.resource_authority import PbxResourceState, PbxResourceType
from app.automation.adapters.pbx.source_fence import FusionPbxSourceFence
from app.automation.pbx_models import PbxExtensionResource, PbxNode


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PbxInventoryService:
    """Synchronize safe inventory facts into the aiVoip control database.

    Existing unmanaged PBX identities are protected as STATIC_BASELINE. An absent
    number in the configured automation pool may be represented as FREE, but it is
    not deletable until a later provision transaction writes an ownership marker.
    """

    def __init__(
        self,
        session_factory,
        profile: FusionPbxLabProfile,
        *,
        config_provider: FusionPbxConfigProvider | None = None,
        source_fence: FusionPbxSourceFence | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.profile = profile
        self.config = config_provider or FusionPbxConfigProvider(profile)
        self.source_fence = source_fence or FusionPbxSourceFence(profile)

    def ensure_automation_identity(
        self,
        *,
        pbx_node_id: str,
        domain_id: str,
        extension: str,
    ) -> PbxExtensionResource:
        try:
            identity = normalize_testlab_provider_mutation_identity(extension)
        except PbxExtensionIdentityError as exc:
            raise ValueError("PBX_PROVIDER_IDENTITY_INVALID") from exc
        if identity in self.profile.protected_extensions:
            raise ValueError("PBX_RESOURCE_PROTECTED")

        matches = [
            view for view in self.config.get_extension(identity)
            if view.domain_id == domain_id and (view.extension == identity or view.number_alias == identity)
        ]
        with self.session_factory() as session:
            row = session.execute(
                select(PbxExtensionResource).where(
                    PbxExtensionResource.pbx_node_id == pbx_node_id,
                    PbxExtensionResource.domain_id == domain_id,
                    PbxExtensionResource.extension == identity,
                )
            ).scalar_one_or_none()
            if matches:
                if row is None:
                    row = PbxExtensionResource(
                        pbx_node_id=pbx_node_id, domain_id=domain_id, extension=identity,
                        resource_type=PbxResourceType.STATIC_BASELINE.value,
                        state=PbxResourceState.READY.value, deletable=False,
                        created_by_automation=False, last_health_at=utcnow(),
                    )
                    session.add(row)
                    session.commit()
                raise ValueError("PBX_AUTOMATION_IDENTITY_ALREADY_EXISTS")
            if row is None:
                row = PbxExtensionResource(
                    pbx_node_id=pbx_node_id, domain_id=domain_id, extension=identity,
                    resource_type=PbxResourceType.TEMPORARY_AUTOMATION.value,
                    state=PbxResourceState.FREE.value, lease_epoch=0,
                    created_by_automation=False, deletable=False, last_health_at=utcnow(),
                )
                session.add(row)
                session.commit()
                session.refresh(row)
                session.expunge(row)
                return row
            if row.resource_type != PbxResourceType.TEMPORARY_AUTOMATION.value:
                raise ValueError("PBX_RESOURCE_PROTECTED")
            if row.created_by_automation or row.lease_id is not None or row.state != PbxResourceState.FREE.value:
                raise ValueError("PBX_RESOURCE_BUSY")
            row.last_health_at = utcnow()
            session.commit()
            session.refresh(row)
            session.expunge(row)
            return row

    def sync(self) -> dict:
        fence = self.source_fence.verify()
        domains = self.config.discover_domains()
        identities = self.config.list_identities()
        now = utcnow()
        with self.session_factory() as session:
            node = session.execute(
                select(PbxNode).where(PbxNode.node_key == self.profile.node_key)
            ).scalar_one_or_none()
            if node is None:
                node = PbxNode(
                    node_key=self.profile.node_key,
                    provider="fusionpbx",
                    runtime_provider="freeswitch",
                    host=self.profile.host,
                    fusionpbx_root=self.profile.fusionpbx_root,
                    internal_profile=self.profile.internal_profile,
                    internal_port=self.profile.internal_port,
                    status="PBX_READY",
                    source_fence_version=fence.version,
                    last_health_at=now,
                )
                session.add(node)
                session.flush()
            else:
                node.status = "PBX_READY"
                node.source_fence_version = fence.version
                node.last_health_at = now

            protected = set(self.profile.protected_extensions)
            for domain in domains:
                existing = identities.get(domain.domain_id, set())
                numbers = set(protected) | {
                    str(v) for v in range(self.profile.pool_start, self.profile.pool_end + 1)
                }
                for number in sorted(numbers):
                    row = session.execute(
                        select(PbxExtensionResource).where(
                            PbxExtensionResource.pbx_node_id == node.id,
                            PbxExtensionResource.domain_id == domain.domain_id,
                            PbxExtensionResource.extension == number,
                        )
                    ).scalar_one_or_none()
                    present = number in existing
                    if row is None:
                        row = PbxExtensionResource(
                            pbx_node_id=node.id,
                            domain_id=domain.domain_id,
                            extension=number,
                            resource_type=(
                                PbxResourceType.STATIC_BASELINE.value
                                if number in protected or present
                                else PbxResourceType.TEMPORARY_AUTOMATION.value
                            ),
                            state=(
                                PbxResourceState.READY.value if present else PbxResourceState.FREE.value
                            ),
                            lease_epoch=0,
                            created_by_automation=False,
                            deletable=False,
                            last_health_at=now,
                        )
                        session.add(row)
                    else:
                        row.last_health_at = now
                        if number in protected:
                            row.resource_type = PbxResourceType.STATIC_BASELINE.value
                            row.deletable = False
                        elif present and not row.created_by_automation:
                            row.resource_type = PbxResourceType.STATIC_BASELINE.value
                            row.state = PbxResourceState.READY.value
                            row.deletable = False
                        elif not present and not row.created_by_automation and row.lease_id is None:
                            row.resource_type = PbxResourceType.TEMPORARY_AUTOMATION.value
                            row.state = PbxResourceState.FREE.value
                            row.deletable = False
            session.commit()
            return {
                "node_id": node.id,
                "domain_count": len(domains),
                "pool_start": self.profile.pool_start,
                "pool_end": self.profile.pool_end,
                "mutation_executed": False,
                "secret_values_emitted": False,
            }
