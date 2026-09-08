from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select

from app.automation.adapters.pbx.fusionpbx_config import FusionPbxConfigProvider, FusionPbxExtensionView
from app.automation.adapters.pbx.fusionpbx_mutation import FusionPbxMutationProvider
from app.automation.adapters.pbx.resource_authority import (
    PbxExtensionLeaseManager, PbxLeaseToken, PbxResourceAuthorityError, PbxResourceState,
)
from app.automation.adapters.pbx.runtime_read import FreeSwitchRuntimeReadProbe
from app.automation.pbx_models import PbxExtensionResource, PbxMutationSnapshot, PbxNode


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PbxResourceManagerError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class PbxProvisionResult:
    status: str
    extension: str
    ownership_marker: str | None
    runtime_visible: bool
    observed_after_unknown: bool = False

    def safe_dict(self) -> dict:
        return {
            "status": self.status,
            "extension": self.extension,
            "ownership_marker": self.ownership_marker,
            "runtime_visible": self.runtime_visible,
            "observed_after_unknown": self.observed_after_unknown,
            "secret_values_emitted": False,
        }


class PbxResourceManager:
    """Transactional PBX extension lifecycle over lease-fenced providers."""

    def __init__(
        self,
        session_factory,
        *,
        authority: PbxExtensionLeaseManager,
        config: FusionPbxConfigProvider,
        mutation: FusionPbxMutationProvider,
        runtime: FreeSwitchRuntimeReadProbe,
    ) -> None:
        self.session_factory = session_factory
        self.authority = authority
        self.config = config
        self.mutation = mutation
        self.runtime = runtime

    @staticmethod
    def ownership_marker(token: PbxLeaseToken) -> str:
        return f"AIVOIP_AUTOMATION:{token.run_id}:{token.lease_epoch}"

    def _node_domain(self, token: PbxLeaseToken) -> tuple[str, str]:
        with self.session_factory() as session:
            node = session.get(PbxNode, token.pbx_node_id)
            if node is None:
                raise PbxResourceManagerError("PBX_NODE_NOT_FOUND")
            metadata = node.provider_metadata_json or {}
            domains = metadata.get("domains") or {}
            domain_name = str(domains.get(token.domain_id) or "")
            if not domain_name:
                for domain in self.config.discover_domains():
                    if domain.domain_id == token.domain_id:
                        domain_name = domain.domain_name
                        break
            if not domain_name:
                raise PbxResourceManagerError("PBX_DOMAIN_NOT_FOUND")
            return node.id, domain_name

    def _exact_view(self, token: PbxLeaseToken) -> FusionPbxExtensionView | None:
        for view in self.config.get_extension(token.extension):
            if view.domain_id == token.domain_id and view.extension == token.extension:
                return view
        return None

    def _set_resource(self, token: PbxLeaseToken, *, state: str, **changes) -> None:
        with self.session_factory() as session:
            row = session.execute(
                select(PbxExtensionResource).where(PbxExtensionResource.id == token.resource_id).with_for_update()
            ).scalar_one_or_none()
            if row is None or not self.authority.validate(token):
                raise PbxResourceManagerError("PBX_LEASE_FENCED")
            row.state = state
            for key, value in changes.items():
                setattr(row, key, value)
            session.commit()

    def _snapshot_absence(self, token: PbxLeaseToken) -> None:
        with self.session_factory() as session:
            existing = session.execute(
                select(PbxMutationSnapshot).where(
                    PbxMutationSnapshot.lease_id == token.lease_id,
                    PbxMutationSnapshot.mutation_kind == "CREATE_EXTENSION",
                )
            ).scalar_one_or_none()
            if existing is None:
                session.add(PbxMutationSnapshot(
                    resource_id=token.resource_id,
                    lease_id=token.lease_id,
                    run_id=token.run_id,
                    mutation_kind="CREATE_EXTENSION",
                    snapshot_json={"extension": token.extension, "exists": False},
                ))
                session.commit()

    def provision_extension(
        self,
        token: PbxLeaseToken,
        *,
        password: str,
        template_identity: str = "7102",
    ) -> PbxProvisionResult:
        if not self.authority.validate(token):
            raise PbxResourceManagerError("PBX_LEASE_FENCED")
        _, domain_name = self._node_domain(token)
        marker = self.ownership_marker(token)
        before = self._exact_view(token)
        if before is not None:
            if before.description == marker:
                visible = self.runtime.user_visible(token.extension, domain_name=domain_name)
                self._set_resource(
                    token, state=PbxResourceState.READY.value, created_by_automation=True,
                    deletable=True, ownership_marker=marker, dirty_reason=None,
                )
                return PbxProvisionResult("CONFIRMED_BY_OBSERVATION", token.extension, marker, visible, True)
            raise PbxResourceManagerError("PBX_EXTENSION_ALREADY_EXISTS")

        self._snapshot_absence(token)
        self._set_resource(token, state=PbxResourceState.PROVISIONING.value)
        result = self.mutation.create_extension(
            token, domain_name=domain_name, password=password, template_identity=template_identity,
        )
        after = self._exact_view(token)
        observed_after_unknown = result.status == "UNKNOWN"
        if after is None or after.description != marker:
            self._set_resource(
                token, state=PbxResourceState.DIRTY.value,
                dirty_reason=("PBX_MUTATION_UNKNOWN" if observed_after_unknown else "PBX_READBACK_MISMATCH"),
            )
            raise PbxResourceManagerError(
                "PBX_MUTATION_UNKNOWN" if observed_after_unknown else "PBX_READBACK_MISMATCH"
            )
        visible = self.runtime.user_visible(token.extension, domain_name=domain_name)
        if not visible:
            self._set_resource(
                token, state=PbxResourceState.DIRTY.value,
                created_by_automation=True, deletable=True, ownership_marker=marker,
                dirty_reason="PBX_RUNTIME_NOT_APPLIED",
            )
            raise PbxResourceManagerError("PBX_RUNTIME_NOT_APPLIED")
        self._set_resource(
            token, state=PbxResourceState.READY.value,
            created_by_automation=True, deletable=True, ownership_marker=marker, dirty_reason=None,
        )
        status = "CONFIRMED_BY_OBSERVATION" if observed_after_unknown else "CONFIRMED"
        return PbxProvisionResult(status, token.extension, marker, True, observed_after_unknown)

    def deprovision_extension(self, token: PbxLeaseToken) -> PbxProvisionResult:
        if not self.authority.validate(token):
            raise PbxResourceManagerError("PBX_LEASE_FENCED")
        _, domain_name = self._node_domain(token)
        marker = self.ownership_marker(token)
        self._set_resource(token, state=PbxResourceState.CLEANUP.value)
        current = self._exact_view(token)
        if current is None:
            self._mark_free(token)
            return PbxProvisionResult("CONFIRMED_BY_OBSERVATION", token.extension, marker, False, True)
        if current.description != marker:
            self._set_resource(
                token, state=PbxResourceState.DIRTY.value,
                dirty_reason="PBX_RESOURCE_OWNERSHIP_UNPROVEN",
            )
            raise PbxResourceManagerError("PBX_RESOURCE_OWNERSHIP_UNPROVEN")
        result = self.mutation.delete_extension(token, domain_name=domain_name)
        after = self._exact_view(token)
        if after is not None:
            self._set_resource(
                token, state=PbxResourceState.DIRTY.value,
                dirty_reason=("PBX_MUTATION_UNKNOWN" if result.status == "UNKNOWN" else "PBX_CLEANUP_FAILED"),
            )
            raise PbxResourceManagerError(
                "PBX_MUTATION_UNKNOWN" if result.status == "UNKNOWN" else "PBX_CLEANUP_FAILED"
            )
        if self.runtime.user_visible(token.extension, domain_name=domain_name):
            self._set_resource(
                token, state=PbxResourceState.DIRTY.value, dirty_reason="PBX_RUNTIME_NOT_APPLIED"
            )
            raise PbxResourceManagerError("PBX_RUNTIME_NOT_APPLIED")
        self._mark_free(token)
        status = "CONFIRMED_BY_OBSERVATION" if result.status == "UNKNOWN" else "CONFIRMED"
        return PbxProvisionResult(status, token.extension, marker, False, result.status == "UNKNOWN")

    def _mark_free(self, token: PbxLeaseToken) -> None:
        self._set_resource(
            token,
            state=PbxResourceState.FREE.value,
            created_by_automation=False,
            deletable=False,
            ownership_marker=None,
            dirty_reason=None,
            last_cleanup_at=utcnow(),
        )

    def release_extension(self, token: PbxLeaseToken) -> None:
        self.authority.release(token)
