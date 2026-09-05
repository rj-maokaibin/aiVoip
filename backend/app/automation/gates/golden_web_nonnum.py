from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Mapping

from app.automation.actions.dispatcher import ActionDispatcher, ActionEvidence, ActionHandlerResult
from app.automation.adapters.entries.web import WebEntryAdapter
from app.automation.adapters.pbx.base import (
    PbxContractError,
    PbxIdentityRequest,
    PbxProvisioner,
    PbxProvisioningReceipt,
    validate_pbx_identity_request,
)
from app.automation.assertions.engine import AssertionEngine
from app.automation.assertions.resolver import EvidenceEnvelope
from app.automation.cleanup import CleanupStepSpec, PersistedCleanupCoordinator, SqlAlchemyCleanupStepStore
from app.automation.event_wait import InMemoryEventBus
from app.automation.gates.golden_web_config import (
    WEB_CONFIG_ACTION,
    WEB_CONFIG_ROUTE,
    WEB_READ_ACTION,
    SipRegistrationProbe,
    observed_account,
    snapshot_writable_bundle,
)
from app.automation.gates.golden_web_nonnum_contract import (
    GOLDEN_WEB_NONNUM_CASE_ID,
    build_nonnum_probe,
)
from app.automation.orchestrator import (
    AutomationOrchestrator,
    AutomationRunContext,
    PrecheckResult,
    RuntimeBlocked,
    RuntimeHooks,
)
from app.automation.persistence import SqlAlchemyRuntimeRecorder
from app.automation.product_contracts.extension_identifier import ExtensionIdentifierContract
from app.automation.registry import TestDefinition
from app.capture_v2.db_models import CaptureLease
from app.capture_v2.enums import CaptureLeaseState
from app.infrastructure.action_route import ActionEntry, RunIntent
from app.infrastructure.device_authority.capture_lease_adapter import CaptureLeaseCompatibilityAdapter


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class GoldenWebNonnumGate:
    """PR-E nonnumeric WEB Golden with provider-neutral PBX lifecycle.

    WEB remains the only DUT mutation entry. PBX lifecycle is injected through the
    PbxProvisioner protocol. DeviceAuthority is released only after both DUT and
    temporary PBX state have been reverse-verified.
    """

    def __init__(
        self,
        *,
        definition: TestDefinition,
        run_id: str,
        device_id: str,
        worker_id: str,
        target_number: str,
        capability_present: bool,
        extension_contract: ExtensionIdentifierContract,
        pbx_request: PbxIdentityRequest,
        pbx: PbxProvisioner,
        web: WebEntryAdapter,
        registration_probe: SipRegistrationProbe,
        authority: CaptureLeaseCompatibilityAdapter,
        session_factory,
        registration_timeout_seconds: float = 60.0,
    ) -> None:
        if definition.case.case_id != GOLDEN_WEB_NONNUM_CASE_ID:
            raise ValueError("WEB_NONNUM_GOLDEN_CASE_ID_MISMATCH")
        if definition.case.entry is not ActionEntry.WEB:
            raise ValueError("WEB_NONNUM_GOLDEN_ENTRY_MUST_BE_WEB")

        self.definition = definition
        self.case = replace(definition.case, parameters=dict(definition.case.parameters))
        self.run_id = run_id
        self.device_id = device_id
        self.worker_id = worker_id
        self.target_number = str(target_number)
        self.capability_present = bool(capability_present)
        self.extension_contract = extension_contract
        self.pbx_request = pbx_request
        self.pbx = pbx
        self.web = web
        self.registration_probe = registration_probe
        self.authority = authority
        self.session_factory = session_factory
        self.registration_timeout_seconds = float(registration_timeout_seconds)
        self.runtime: dict[str, Any] = {
            "token": None,
            "snapshot": None,
            "probe": None,
            "pbx_receipt": None,
        }

    async def _precheck(self, context: AutomationRunContext) -> PrecheckResult:
        if context.case.case_id != GOLDEN_WEB_NONNUM_CASE_ID:
            return PrecheckResult(False, "WEB_NONNUM_GOLDEN_CASE_ID_MISMATCH")
        if not context.case.executable:
            return PrecheckResult(
                False,
                f"WEB_NONNUM_CONTRACT_NOT_EXECUTABLE:{context.case.contract_status.value}",
            )
        if not self.capability_present:
            return PrecheckResult(False, "NONNUM_EXTENSION_CAPABILITY_REQUIRED")
        target = self.target_number
        if not target or (target.isascii() and target.isdigit()):
            return PrecheckResult(False, "WEB_NONNUM_TARGET_REQUIRED")
        validation = self.extension_contract.validate(target)
        if not validation.accepted:
            return PrecheckResult(False, f"WEB_NONNUM_TARGET_INVALID:{validation.reason}")
        if self.pbx_request.extension != target:
            return PrecheckResult(False, "PBX_EXTENSION_TARGET_MISMATCH")
        try:
            validate_pbx_identity_request(self.extension_contract, self.pbx_request)
        except PbxContractError as exc:
            return PrecheckResult(False, str(exc))
        if self.registration_timeout_seconds <= 0 or self.registration_timeout_seconds > 60.0:
            return PrecheckResult(False, "WEB_REGISTRATION_TIMEOUT_INVALID")
        return PrecheckResult(True)

    async def _reserve(self, context: AutomationRunContext):
        token = self.authority.acquire(
            device_id=self.device_id,
            run_id=context.run_id,
            owner_worker_id=self.worker_id,
        )
        self.runtime["token"] = token
        return token

    async def _snapshot(self, context: AutomationRunContext) -> None:
        read = await self.web.execute(WEB_READ_ACTION, {}, context)
        snapshot = snapshot_writable_bundle(read)
        probe = build_nonnum_probe(
            snapshot,
            self.target_number,
            contract=self.extension_contract,
            capability_present=self.capability_present,
        )
        self.runtime.update(snapshot=snapshot, probe=probe)
        self.case.parameters["target_number"] = self.target_number

    async def _provision(self, context: AutomationRunContext) -> None:
        receipt = await self.pbx.provision_identity(self.pbx_request, run_id=context.run_id)
        self.runtime["pbx_receipt"] = receipt
        verification = await self.pbx.verify_identity(receipt)
        context.evidence.put(
            "pbx",
            EvidenceEnvelope(
                data={
                    "provisioned": verification.matched,
                    "provider": receipt.provider,
                    "resource_id": receipt.resource_id,
                    "kind": receipt.kind.value,
                    "extension": receipt.extension,
                    "auth_id": receipt.auth_id,
                    "details": dict(verification.details),
                },
                evidence_refs=tuple(receipt.evidence_refs) + tuple(verification.evidence_refs),
                source_timestamp=(verification.source_timestamp or receipt.source_timestamp or utcnow()),
            ),
        )
        if not verification.matched:
            raise RuntimeBlocked("PBX_IDENTITY_PROVISION_VERIFY_FAILED")

    async def _configure(self, context, _args) -> ActionHandlerResult:
        probe = self.runtime.get("probe")
        if not isinstance(probe, Mapping):
            raise RuntimeError("WEB_NONNUM_GOLDEN_PROBE_NOT_PREPARED")

        mutation = await self.web.configure_voip_bundle(probe, context)
        evidence: list[ActionEvidence] = []
        if mutation.unknown_result:
            if isinstance(mutation.readback, Mapping):
                evidence.append(
                    ActionEvidence(
                        source="entry",
                        data={"unknown_readback": mutation.readback},
                        evidence_refs=("web-nonnum-golden://unknown-readback",),
                        source_timestamp=utcnow(),
                    )
                )
            return ActionHandlerResult(
                success=False,
                output={"accepted": False, "error": mutation.error},
                evidence=tuple(evidence),
                unknown_result=True,
            )

        readback = await self.web.execute(WEB_READ_ACTION, {}, context)
        account = observed_account(readback)
        evidence.append(
            ActionEvidence(
                source="entry",
                data={
                    **account,
                    "mutation_accepted": mutation.accepted,
                    "readback_accepted": readback.accepted,
                },
                evidence_refs=("web-nonnum-golden://config-readback",),
                source_timestamp=utcnow(),
            )
        )

        registration = await self.registration_probe.wait_registered(
            number=self.target_number,
            timeout_seconds=self.registration_timeout_seconds,
        )
        evidence.append(
            ActionEvidence(
                source="sip",
                data={
                    "registered": registration.registered,
                    "number": registration.number,
                    "details": dict(registration.details or {}),
                },
                evidence_refs=registration.evidence_refs,
                source_timestamp=registration.source_timestamp or utcnow(),
            )
        )
        return ActionHandlerResult(
            success=bool(mutation.accepted and readback.accepted and registration.registered),
            output={
                "mutation_accepted": mutation.accepted,
                "readback_accepted": readback.accepted,
                "registration_observed": registration.registered,
            },
            evidence=tuple(evidence),
        )

    async def _restore_action(self) -> dict[str, Any]:
        snapshot = self.runtime.get("snapshot")
        token = self.runtime.get("token")
        if not isinstance(snapshot, Mapping):
            return {"restore_required": False, "snapshot_not_captured": True}
        if token is None:
            raise RuntimeError("WEB_NONNUM_RESTORE_AUTHORITY_MISSING")

        current = await self.web.execute(WEB_READ_ACTION, {})
        try:
            current_bundle = snapshot_writable_bundle(current)
        except RuntimeBlocked:
            current_bundle = None
        if current_bundle == snapshot:
            return {"restore_required": False, "already_restored": True}

        restored = await self.web.configure_voip_bundle(snapshot)
        if restored.unknown_result:
            raise RuntimeError("WEB_NONNUM_RESTORE_RESULT_UNKNOWN")
        if not restored.accepted:
            raise RuntimeError(f"WEB_NONNUM_RESTORE_REJECTED:{restored.error}")
        return {"restore_required": True, "web_restore_accepted": True}

    async def _restore_verify(self):
        snapshot = self.runtime.get("snapshot")
        if not isinstance(snapshot, Mapping):
            return True, {"mutation_not_started": True}
        current = await self.web.execute(WEB_READ_ACTION, {})
        try:
            actual = snapshot_writable_bundle(current)
        except RuntimeBlocked as exc:
            return False, {"reason": str(exc)}
        matched = actual == snapshot
        return matched, {"web_reverse_verify": matched}

    async def _delete_pbx_action(self) -> dict[str, Any]:
        receipt = self.runtime.get("pbx_receipt")
        if not isinstance(receipt, PbxProvisioningReceipt):
            return {"pbx_identity_created": False}
        deleted = await self.pbx.delete_identity(receipt, run_id=self.run_id)
        if not deleted.matched:
            raise RuntimeError("PBX_IDENTITY_DELETE_REJECTED")
        return {
            "pbx_identity_created": True,
            "provider": receipt.provider,
            "resource_id": receipt.resource_id,
        }

    async def _delete_pbx_verify(self):
        receipt = self.runtime.get("pbx_receipt")
        if not isinstance(receipt, PbxProvisioningReceipt):
            return True, {"pbx_identity_created": False}
        verification = await self.pbx.verify_deleted(receipt)
        return verification.matched, {
            "pbx_identity_deleted": verification.matched,
            "provider": receipt.provider,
            "resource_id": receipt.resource_id,
            "details": dict(verification.details),
        }

    async def _release_action(self) -> dict[str, Any]:
        token = self.runtime.get("token")
        if token is None:
            return {"authority_acquired": False}
        self.authority.release(token)
        return {"authority_acquired": True, "lease_epoch": token.lease_epoch}

    async def _release_verify(self):
        token = self.runtime.get("token")
        if token is None:
            return True, {"authority_acquired": False}
        with self.session_factory() as db:
            row = db.get(CaptureLease, token.device_id)
            released = bool(
                row is not None
                and row.state == CaptureLeaseState.RELEASED.value
                and int(row.lease_epoch) == token.lease_epoch
            )
        return released, {"lease_released": released, "lease_epoch": token.lease_epoch}

    async def run(self):
        dispatcher = ActionDispatcher()
        dispatcher.register(
            action_id=WEB_CONFIG_ACTION,
            route=WEB_CONFIG_ROUTE,
            handler=self._configure,
            mutates=True,
        )
        cleanup = PersistedCleanupCoordinator(
            store=SqlAlchemyCleanupStepStore(self.session_factory),
            steps=(
                CleanupStepSpec("restore_web_voip_bundle", self._restore_action, self._restore_verify),
                CleanupStepSpec("delete_pbx_identity", self._delete_pbx_action, self._delete_pbx_verify),
                CleanupStepSpec(
                    "release_device_authority",
                    self._release_action,
                    self._release_verify,
                    release_authority=True,
                ),
            ),
        )
        runtime = AutomationOrchestrator(
            dispatcher=dispatcher,
            events=InMemoryEventBus(),
            assertions=AssertionEngine(),
            cleanup=cleanup,
            hooks=RuntimeHooks(
                precheck=self._precheck,
                reserve=self._reserve,
                snapshot=self._snapshot,
                provision=self._provision,
            ),
            recorder=SqlAlchemyRuntimeRecorder(self.session_factory),
        )
        return await runtime.run(
            self.case,
            run_id=self.run_id,
            worker_id=self.worker_id,
            intent=RunIntent.VERIFY,
        )
