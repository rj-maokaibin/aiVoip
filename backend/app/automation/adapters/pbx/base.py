from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Mapping, Protocol, runtime_checkable

from app.automation.product_contracts.extension_identifier import ExtensionIdentifierContract


class PbxContractError(ValueError):
    pass


class PbxResourceKind(str, Enum):
    EXTENSION = "extension"
    ALIAS = "alias"


@dataclass(frozen=True)
class PbxIdentityRequest:
    """Provider-neutral PBX resource request; secrets are references, never plaintext."""

    kind: PbxResourceKind
    extension: str
    auth_id: str
    display_name: str
    credential_ref: str | None = None
    alias_target: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind is PbxResourceKind.EXTENSION:
            if not self.credential_ref:
                raise PbxContractError("PBX_EXTENSION_CREDENTIAL_REF_REQUIRED")
            if self.alias_target is not None:
                raise PbxContractError("PBX_EXTENSION_ALIAS_TARGET_FORBIDDEN")
        elif self.kind is PbxResourceKind.ALIAS:
            if not self.alias_target:
                raise PbxContractError("PBX_ALIAS_TARGET_REQUIRED")
            if self.credential_ref is not None:
                raise PbxContractError("PBX_ALIAS_CREDENTIAL_REF_FORBIDDEN")


@dataclass(frozen=True)
class PbxProvisioningReceipt:
    provider: str
    resource_id: str
    kind: PbxResourceKind
    extension: str
    auth_id: str
    evidence_refs: tuple[str, ...] = ()
    source_timestamp: datetime | None = None

    def __post_init__(self) -> None:
        if not self.provider or not self.resource_id:
            raise PbxContractError("PBX_PROVISIONING_RECEIPT_IDENTITY_REQUIRED")


@dataclass(frozen=True)
class PbxVerification:
    matched: bool
    details: Mapping[str, Any] = field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()
    source_timestamp: datetime | None = None


@runtime_checkable
class PbxProvisioner(Protocol):
    """Real provider binding is injected later; Automation Core never embeds PBX CLI/API details."""

    async def provision_identity(
        self,
        request: PbxIdentityRequest,
        *,
        run_id: str,
    ) -> PbxProvisioningReceipt: ...

    async def verify_identity(
        self,
        receipt: PbxProvisioningReceipt,
    ) -> PbxVerification: ...

    async def delete_identity(
        self,
        receipt: PbxProvisioningReceipt,
        *,
        run_id: str,
    ) -> PbxVerification: ...

    async def verify_deleted(
        self,
        receipt: PbxProvisioningReceipt,
    ) -> PbxVerification: ...


def validate_pbx_identity_request(
    contract: ExtensionIdentifierContract,
    request: PbxIdentityRequest,
) -> None:
    extension = contract.validate(request.extension)
    if not extension.accepted:
        raise PbxContractError(f"PBX_EXTENSION_INVALID:{extension.reason}")

    if contract.auth_id_same_rule:
        auth_id = contract.validate(request.auth_id)
        if not auth_id.accepted:
            raise PbxContractError(f"PBX_AUTH_ID_INVALID:{auth_id.reason}")

    if contract.disname_same_rule:
        display = contract.validate(request.display_name)
        if not display.accepted:
            raise PbxContractError(f"PBX_DISPLAY_NAME_INVALID:{display.reason}")

    if contract.number_auth_id_must_equal and request.extension != request.auth_id:
        raise PbxContractError("PBX_EXTENSION_AUTH_ID_MUST_EQUAL")
