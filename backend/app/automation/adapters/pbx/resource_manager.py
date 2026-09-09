from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select

from app.automation.adapters.pbx.fusionpbx_config import FusionPbxConfigProvider, FusionPbxExtensionView
from app.automation.adapters.pbx.fusionpbx_mutation import FusionPbxMutationProvider
from app.automation.adapters.pbx.identity import PbxExtensionIdentityError, normalize_dial_alias
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
    dial_alias: str | None = None

    def safe_dict(self) -> dict:
        return {
            "status": self.status,
            "extension": self.extension,
            "ownership_marker": self.ownership_marker,
            "runtime_visible": self.runtime_visible,
            "observed_after_unknown": self.observed_after_unknown,
            "dial_alias": self.dial_alias,
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

    def _snapshot_absence(self, token: PbxLeaseToken, *, dial_alias: str | None = None) -> None:
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
                    snapshot_json={"extension": token.extension, "dial_alias": dial_alias, "exists": False},
                ))
                session.commit()

    def provision_extension(
        self,
        token: PbxLeaseToken,
        *,
        password: str,
        template_identity: str = "7102",
        dial_alias: str | None = None,
    ) -> PbxProvisionResult:
        if not self.authority.validate(token):
            raise PbxResourceManagerError("PBX_LEASE_FENCED")
        _, domain_name = self._node_domain(token)
        marker = self.ownership_marker(token)
        before = self._exact_view(token)
        if dial_alias is not None:
            try:
                dial_alias = normalize_dial_alias(dial_alias)
            except PbxExtensionIdentityError as exc:
                raise PbxResourceManagerError(str(exc)) from exc
            if dial_alias == token.extension:
                raise PbxResourceManagerError("PBX_DIAL_ALIAS_EQUALS_IDENTITY")
            conflicts = self.config.get_extension(dial_alias)
            foreign_conflicts = [view for view in conflicts if view.extension != token.extension]
            if foreign_conflicts or (conflicts and before is None):
                raise PbxResourceManagerError("PBX_DIAL_ALIAS_CONFLICT")
        if before is not None:
            if before.description == marker:
                if before.number_alias != dial_alias:
                    raise PbxResourceManagerError("PBX_DIAL_ALIAS_READBACK_MISMATCH")
                visible = self.runtime.user_visible(token.extension, domain_name=domain_name)
                self._set_resource(
                    token, state=PbxResourceState.READY.value, created_by_automation=True,
                    deletable=True, ownership_marker=marker, dial_alias=dial_alias, dirty_reason=None,
                    provider_metadata_json={"user_context": before.user_context, "extension_uuid": before.extension_uuid, "dial_alias": dial_alias},
                )
                return PbxProvisionResult("CONFIRMED_BY_OBSERVATION", token.extension, marker, visible, True, dial_alias)
            raise PbxResourceManagerError("PBX_EXTENSION_ALREADY_EXISTS")

        self._snapshot_absence(token, dial_alias=dial_alias)
        self._set_resource(token, state=PbxResourceState.PROVISIONING.value)
        result = self.mutation.create_extension(
            token, domain_name=domain_name, password=password, template_identity=template_identity,
            dial_alias=dial_alias,
        )
        after = self._exact_view(token)
        observed_after_unknown = result.status == "UNKNOWN"
        if after is None or after.description != marker or after.number_alias != dial_alias:
            self._set_resource(
                token, state=PbxResourceState.DIRTY.value,
                dirty_reason=("PBX_MUTATION_UNKNOWN" if observed_after_unknown else "PBX_READBACK_MISMATCH"),
            )
            raise PbxResourceManagerError(
                "PBX_MUTATION_UNKNOWN" if observed_after_unknown else "PBX_READBACK_MISMATCH"
            )
        visible = self.runtime.user_visible(token.extension, domain_name=domain_name)
        alias_visible = bool(dial_alias and self.runtime.user_visible(dial_alias, domain_name=domain_name))
        if not visible or (dial_alias is not None and not alias_visible):
            self._set_resource(
                token, state=PbxResourceState.DIRTY.value,
                created_by_automation=True, deletable=True, ownership_marker=marker, dial_alias=dial_alias,
                provider_metadata_json={"user_context": after.user_context, "extension_uuid": after.extension_uuid, "dial_alias": dial_alias},
                dirty_reason="PBX_RUNTIME_NOT_APPLIED",
            )
            raise PbxResourceManagerError("PBX_RUNTIME_NOT_APPLIED")
        self._set_resource(
            token, state=PbxResourceState.READY.value,
            created_by_automation=True, deletable=True, ownership_marker=marker, dial_alias=dial_alias, dirty_reason=None,
            provider_metadata_json={"user_context": after.user_context, "extension_uuid": after.extension_uuid, "dial_alias": dial_alias},
        )
        status = "CONFIRMED_BY_OBSERVATION" if observed_after_unknown else "CONFIRMED"
        return PbxProvisionResult(status, token.extension, marker, True, observed_after_unknown, dial_alias)

    def deprovision_extension(self, token: PbxLeaseToken) -> PbxProvisionResult:
        if not self.authority.validate(token):
            raise PbxResourceManagerError("PBX_LEASE_FENCED")
        _, domain_name = self._node_domain(token)
        with self.session_factory() as session:
            owned = session.get(PbxExtensionResource, token.resource_id)
            marker = str((owned.ownership_marker if owned else None) or self.ownership_marker(token))
            dial_alias = str((owned.dial_alias if owned else None) or "") or None
        if not marker.startswith("AIVOIP_AUTOMATION:"):
            raise PbxResourceManagerError("PBX_RESOURCE_OWNERSHIP_UNPROVEN")
        self._set_resource(token, state=PbxResourceState.CLEANUP.value)
        current = self._exact_view(token)
        if current is None:
            runtime_visible = self.runtime.user_visible(token.extension, domain_name=domain_name)
            alias_visible = bool(dial_alias and self.runtime.user_visible(dial_alias, domain_name=domain_name))
            runtime_visible = runtime_visible or alias_visible
            if runtime_visible:
                with self.session_factory() as session:
                    row = session.get(PbxExtensionResource, token.resource_id)
                    metadata = (row.provider_metadata_json or {}) if row is not None else {}
                user_context = str(metadata.get("user_context") or "")
                if not user_context:
                    user_context = self.runtime.user_context(token.extension, domain_name=domain_name) or ""
                if not user_context:
                    self._set_resource(
                        token, state=PbxResourceState.DIRTY.value,
                        dirty_reason="PBX_RUNTIME_RECONCILE_CONTEXT_MISSING",
                    )
                    raise PbxResourceManagerError("PBX_RUNTIME_RECONCILE_CONTEXT_MISSING")
                self.mutation.reconcile_absent_extension_cache(
                    token,
                    domain_name=domain_name,
                    user_context=user_context,
                    dial_alias=dial_alias,
                    ownership_marker=marker,
                )
                runtime_visible = self.runtime.user_visible(token.extension, domain_name=domain_name)
                alias_visible = bool(dial_alias and self.runtime.user_visible(dial_alias, domain_name=domain_name))
                runtime_visible = runtime_visible or alias_visible
                if runtime_visible:
                    self._set_resource(
                        token, state=PbxResourceState.DIRTY.value, dirty_reason="PBX_RUNTIME_NOT_APPLIED"
                    )
                    raise PbxResourceManagerError("PBX_RUNTIME_NOT_APPLIED")
            self._mark_free(token)
            return PbxProvisionResult("CONFIRMED_BY_OBSERVATION", token.extension, marker, False, True, dial_alias)
        if current.description != marker:
            self._set_resource(
                token, state=PbxResourceState.DIRTY.value,
                dirty_reason="PBX_RESOURCE_OWNERSHIP_UNPROVEN",
            )
            raise PbxResourceManagerError("PBX_RESOURCE_OWNERSHIP_UNPROVEN")
        result = self.mutation.delete_extension(
            token, domain_name=domain_name, ownership_marker=marker
        )
        after = self._exact_view(token)
        if after is not None:
            self._set_resource(
                token, state=PbxResourceState.DIRTY.value,
                dirty_reason=("PBX_MUTATION_UNKNOWN" if result.status == "UNKNOWN" else "PBX_CLEANUP_FAILED"),
            )
            raise PbxResourceManagerError(
                "PBX_MUTATION_UNKNOWN" if result.status == "UNKNOWN" else "PBX_CLEANUP_FAILED"
            )
        runtime_visible = self.runtime.user_visible(token.extension, domain_name=domain_name)
        alias_visible = bool(dial_alias and self.runtime.user_visible(dial_alias, domain_name=domain_name))
        runtime_visible = runtime_visible or alias_visible
        if runtime_visible:
            user_context = current.user_context or self.runtime.user_context(
                token.extension, domain_name=domain_name
            ) or ""
            if not user_context:
                self._set_resource(
                    token,
                    state=PbxResourceState.DIRTY.value,
                    dirty_reason="PBX_RUNTIME_RECONCILE_CONTEXT_MISSING",
                )
                raise PbxResourceManagerError("PBX_RUNTIME_RECONCILE_CONTEXT_MISSING")
            self.mutation.reconcile_absent_extension_cache(
                token,
                domain_name=domain_name,
                user_context=user_context,
                dial_alias=dial_alias,
                ownership_marker=marker,
            )
            runtime_visible = self.runtime.user_visible(token.extension, domain_name=domain_name)
            alias_visible = bool(dial_alias and self.runtime.user_visible(dial_alias, domain_name=domain_name))
            runtime_visible = runtime_visible or alias_visible
            if runtime_visible:
                self._set_resource(
                    token, state=PbxResourceState.DIRTY.value, dirty_reason="PBX_RUNTIME_NOT_APPLIED"
                )
                raise PbxResourceManagerError("PBX_RUNTIME_NOT_APPLIED")
        self._mark_free(token)
        status = "CONFIRMED_BY_OBSERVATION" if result.status == "UNKNOWN" else "CONFIRMED"
        return PbxProvisionResult(status, token.extension, marker, False, result.status == "UNKNOWN", dial_alias)

    def _mark_free(self, token: PbxLeaseToken) -> None:
        self._set_resource(
            token,
            state=PbxResourceState.FREE.value,
            created_by_automation=False,
            deletable=False,
            ownership_marker=None,
            dial_alias=None,
            dirty_reason=None,
            last_cleanup_at=utcnow(),
        )

    def release_extension(self, token: PbxLeaseToken) -> None:
        self.authority.release(token)
