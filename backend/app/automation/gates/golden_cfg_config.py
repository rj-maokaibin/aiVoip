from __future__ import annotations

import copy
from dataclasses import replace
from datetime import datetime, timezone
from typing import Any, Mapping

from app.automation.actions.dispatcher import ActionDispatcher, ActionEvidence, ActionHandlerResult
from app.automation.assertions.engine import AssertionEngine
from app.automation.assertions.resolver import EvidenceEnvelope
from app.automation.cleanup import CleanupStepSpec, PersistedCleanupCoordinator, SqlAlchemyCleanupStepStore
from app.automation.event_wait import InMemoryEventBus
from app.automation.orchestrator import AutomationOrchestrator, AutomationRunContext, PrecheckResult, RuntimeBlocked, RuntimeHooks
from app.automation.persistence import SqlAlchemyRuntimeRecorder
from app.automation.registry import TestDefinition
from app.capture_v2.db_models import CaptureLease
from app.capture_v2.enums import CaptureLeaseState
from app.infrastructure.action_route import ActionBackend, ActionEntry, ActionPurpose, ActionRoute, ActionTransport, RunIntent
from app.infrastructure.config_framework.executor import ConfigFrameworkExecutor
from app.infrastructure.config_framework.schema import ConfigResult, mask_secrets
from app.infrastructure.device_authority.capture_lease_adapter import CaptureLeaseCompatibilityAdapter
from app.infrastructure.mutation.contract import MutationStatus

GOLDEN_CFG_CONFIG_CASE_ID = "Golden-CFG-CONFIG-001"
G0_MODULE = "voipUserInfo"
G0_ACTION = "voip.account.configure"
G0_ROUTE = ActionRoute(
    entry=ActionEntry.NONE,
    transport=ActionTransport.SSH,
    backend=ActionBackend.CONFIG_FRAMEWORK,
    purpose=ActionPurpose.TEST,
    target=G0_MODULE,
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def extract_set_payload(result: ConfigResult) -> dict[str, Any]:
    data = result.raw.get("data") if isinstance(result.raw, Mapping) else None
    if data is None:
        data = result.data
    if not isinstance(data, list) or not data or not isinstance(data[0], Mapping):
        raise RuntimeBlocked("G0_EXISTING_VOIP_ACCOUNT_REQUIRED")
    return {"data": copy.deepcopy(data)}


def build_display_name_probe(snapshot: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    payload = copy.deepcopy(dict(snapshot))
    rows = payload.get("data")
    if not isinstance(rows, list) or not rows or not isinstance(rows[0], dict):
        raise RuntimeBlocked("G0_EXISTING_VOIP_ACCOUNT_REQUIRED")
    current = str(rows[0].get("disName") or "")
    marker = "G0" if current != "G0" else "G1"
    rows[0]["disName"] = marker
    return payload, marker


def safe_readback(result: ConfigResult) -> dict[str, Any]:
    masked = mask_secrets(result.raw)
    return {
        "rcode": result.rcode,
        "rmsg": result.rmsg,
        "data": masked.get("data") if isinstance(masked, Mapping) else None,
    }


class GoldenCfgConfigGate:
    """G0 backend gate executed by the deterministic Automation Test Runtime."""

    def __init__(
        self,
        *,
        definition: TestDefinition,
        run_id: str,
        device_id: str,
        worker_id: str,
        config: ConfigFrameworkExecutor,
        authority: CaptureLeaseCompatibilityAdapter,
        session_factory,
        command_timeout: float = 20.0,
    ) -> None:
        if definition.case.case_id != GOLDEN_CFG_CONFIG_CASE_ID:
            raise ValueError("G0_CASE_ID_MISMATCH")
        if definition.case.entry is not ActionEntry.NONE:
            raise ValueError("G0_ENTRY_MUST_BE_NONE")
        self.definition = definition
        self.case = replace(definition.case, parameters=dict(definition.case.parameters))
        self.run_id = run_id
        self.device_id = device_id
        self.worker_id = worker_id
        self.config = config
        self.authority = authority
        self.session_factory = session_factory
        self.command_timeout = command_timeout
        self.runtime: dict[str, Any] = {"token": None, "snapshot": None, "probe": None, "marker": None}

    async def _precheck(self, context: AutomationRunContext) -> PrecheckResult:
        return PrecheckResult(context.case.case_id == GOLDEN_CFG_CONFIG_CASE_ID, "G0_CASE_ID_MISMATCH")

    async def _reserve(self, context: AutomationRunContext):
        token = self.authority.acquire(
            device_id=self.device_id,
            run_id=context.run_id,
            owner_worker_id=self.worker_id,
        )
        self.runtime["token"] = token
        return token

    async def _snapshot(self, context: AutomationRunContext) -> None:
        result = await self.config.get(G0_MODULE, timeout=self.command_timeout)
        if not result.success:
            raise RuntimeBlocked(f"G0_SNAPSHOT_FAILED:{result.rcode}")
        snapshot = extract_set_payload(result)
        probe, marker = build_display_name_probe(snapshot)
        self.runtime.update(snapshot=snapshot, probe=probe, marker=marker)
        self.case.parameters["probe_disname"] = marker
        context.evidence.put(
            "system",
            EvidenceEnvelope(
                data={"snapshot_ready": True, "module": G0_MODULE},
                evidence_refs=("g0://snapshot/voipUserInfo",),
                source_timestamp=utcnow(),
                route={
                    "entry": "none",
                    "transport": "ssh",
                    "backend": "config_framework",
                    "purpose": "observation",
                    "target": G0_MODULE,
                },
            ),
        )

    async def _configure(self, context, _args) -> ActionHandlerResult:
        probe = self.runtime.get("probe")
        if not isinstance(probe, Mapping):
            raise RuntimeError("G0_PROBE_NOT_PREPARED")
        mutation = await self.config.set(
            G0_MODULE,
            probe,
            authority_token=context.authority_token,
            timeout=self.command_timeout,
        )
        readback = await self.config.get(G0_MODULE, timeout=self.command_timeout)
        return ActionHandlerResult(
            success=bool(mutation.success and readback.success),
            output={
                "module": G0_MODULE,
                "mutation_status": mutation.status.value,
                "readback_success": readback.success,
                "observed_after_unknown": mutation.observed_after_unknown,
            },
            evidence=(ActionEvidence(
                source="config_framework",
                data=safe_readback(readback),
                evidence_refs=("g0://effective-readback/voipUserInfo",),
                source_timestamp=utcnow(),
            ),),
            unknown_result=mutation.status is MutationStatus.UNKNOWN,
        )

    async def _restore_action(self) -> dict[str, Any]:
        snapshot = self.runtime.get("snapshot")
        token = self.runtime.get("token")
        if not isinstance(snapshot, Mapping):
            return {"restore_required": False, "reason": "snapshot_not_captured"}
        if token is None:
            raise RuntimeError("G0_RESTORE_AUTHORITY_MISSING")
        current = await self.config.get(G0_MODULE, timeout=self.command_timeout)
        if ConfigFrameworkExecutor.payload_matches_readback(snapshot, current):
            return {"restore_required": False, "already_restored": True}
        restored = await self.config.set(
            G0_MODULE,
            snapshot,
            authority_token=token,
            timeout=self.command_timeout,
        )
        return {
            "restore_required": True,
            "restore_status": restored.status.value,
            "observed_after_unknown": restored.observed_after_unknown,
        }

    async def _restore_verify(self):
        snapshot = self.runtime.get("snapshot")
        if not isinstance(snapshot, Mapping):
            return True, {"mutation_not_started": True}
        current = await self.config.get(G0_MODULE, timeout=self.command_timeout)
        return ConfigFrameworkExecutor.payload_matches_readback(snapshot, current), {
            "restore_readback_success": current.success,
            "module": G0_MODULE,
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
            action_id=G0_ACTION,
            route=G0_ROUTE,
            handler=self._configure,
            mutates=True,
        )
        cleanup = PersistedCleanupCoordinator(
            store=SqlAlchemyCleanupStepStore(self.session_factory),
            steps=(
                CleanupStepSpec("restore_voip_user_info", self._restore_action, self._restore_verify),
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
            hooks=RuntimeHooks(precheck=self._precheck, reserve=self._reserve, snapshot=self._snapshot),
            recorder=SqlAlchemyRuntimeRecorder(self.session_factory),
        )
        return await runtime.run(
            self.case,
            run_id=self.run_id,
            worker_id=self.worker_id,
            intent=RunIntent.VERIFY,
        )
