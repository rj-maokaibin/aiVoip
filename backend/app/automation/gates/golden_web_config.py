from __future__ import annotations

import copy
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol

from app.automation.actions.dispatcher import ActionDispatcher, ActionEvidence, ActionHandlerResult
from app.automation.adapters.entries.web import EntryResult, WebEntryAdapter
from app.automation.assertions.engine import AssertionEngine
from app.automation.cleanup import CleanupStepSpec, PersistedCleanupCoordinator, SqlAlchemyCleanupStepStore
from app.automation.event_wait import InMemoryEventBus
from app.automation.orchestrator import AutomationOrchestrator, AutomationRunContext, PrecheckResult, RuntimeBlocked, RuntimeHooks
from app.automation.persistence import SqlAlchemyRuntimeRecorder
from app.automation.registry import TestDefinition
from app.capture_v2.db_models import CaptureLease
from app.capture_v2.enums import CaptureLeaseState
from app.infrastructure.action_route import ActionBackend, ActionEntry, ActionPurpose, ActionRoute, ActionTransport, RunIntent
from app.infrastructure.config_framework.executor import ConfigFrameworkExecutor
from app.infrastructure.device_authority.capture_lease_adapter import CaptureLeaseCompatibilityAdapter
from app.infrastructure.device_authority.keepalive import AuthorityKeepalive

GOLDEN_WEB_CONFIG_CASE_ID = "Golden-WEB-CONFIG-001"
WEB_CONFIG_ACTION = "voip.account.configure"
WEB_READ_ACTION = "voip.account.read"
WEB_WRITABLE_MODULES = (
    "voice_vlan",
    "voipServInfo",
    "voipUserInfo",
    "voipFxsTbl",
    "voipAdvanced",
)
WEB_CONFIG_ROUTE = ActionRoute(
    entry=ActionEntry.WEB,
    transport=ActionTransport.HTTP_API,
    backend=ActionBackend.CONFIG_FRAMEWORK,
    purpose=ActionPurpose.TEST,
    target="web_voip_writable_bundle",
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _is_ascii_digits(value: str) -> bool:
    return bool(value) and value.isascii() and value.isdigit()


@dataclass(frozen=True)
class SipRegistrationEvidence:
    registered: bool
    number: str
    evidence_refs: tuple[str, ...] = ()
    source_timestamp: datetime | None = None
    details: Mapping[str, Any] | None = None


class SipRegistrationProbe(Protocol):
    async def wait_registered(self, *, number: str, timeout_seconds: float) -> SipRegistrationEvidence: ...


def _entry_modules(result: EntryResult, *, runtime: bool = False) -> Mapping[str, Any]:
    if not result.accepted:
        raise RuntimeBlocked(f"WEB_READ_REJECTED:{result.error or result.status_code}")
    output = result.runtime_output if runtime and isinstance(result.runtime_output, Mapping) else result.output
    if not isinstance(output, Mapping):
        raise RuntimeBlocked("WEB_READ_OUTPUT_MISSING")
    modules = output.get("modules")
    if not isinstance(modules, Mapping):
        raise RuntimeBlocked("WEB_READ_MODULE_MAP_MISSING")
    return modules


def snapshot_writable_bundle(result: EntryResult) -> dict[str, Any]:
    # Restore snapshots must use the process-private raw WEB response. Public
    # output/evidence remains redacted, so passwd/auth secrets are never
    # persisted while reverse restore still uses the exact original values.
    modules = _entry_modules(result, runtime=True)
    missing = [module for module in WEB_WRITABLE_MODULES if module not in modules]
    if missing:
        raise RuntimeBlocked(f"WEB_WRITABLE_SNAPSHOT_INCOMPLETE:{','.join(missing)}")
    return {module: copy.deepcopy(modules[module]) for module in WEB_WRITABLE_MODULES}


def _account_rows(value: Any) -> list[dict[str, Any]]:
    current = value
    for _ in range(3):
        if isinstance(current, list):
            if current and isinstance(current[0], dict):
                return current
            break
        if isinstance(current, Mapping) and "data" in current:
            current = current["data"]
            continue
        break
    raise RuntimeBlocked("WEB_VOIP_USER_ACCOUNT_REQUIRED")


def build_numeric_probe(snapshot: Mapping[str, Any], target_number: str) -> dict[str, Any]:
    target = str(target_number).strip()
    if not _is_ascii_digits(target):
        raise RuntimeBlocked("WEB_NUMERIC_TARGET_REQUIRED")
    probe = copy.deepcopy(dict(snapshot))
    if tuple(probe) != WEB_WRITABLE_MODULES:
        missing = [module for module in WEB_WRITABLE_MODULES if module not in probe]
        if missing:
            raise RuntimeBlocked(f"WEB_WRITABLE_SNAPSHOT_INCOMPLETE:{','.join(missing)}")
    rows = _account_rows(probe["voipUserInfo"])
    rows[0]["number"] = target
    rows[0]["disName"] = target
    return probe


def observed_account(result: EntryResult) -> dict[str, Any]:
    modules = _entry_modules(result)
    if "voipUserInfo" not in modules:
        raise RuntimeBlocked("WEB_VOIP_USER_READBACK_MISSING")
    row = _account_rows(modules["voipUserInfo"])[0]
    return {
        "number": row.get("number"),
        "disName": row.get("disName"),
        "authId": row.get("authId"),
    }


def config_payload_from_web_module(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping) and isinstance(value.get("data"), list):
        return {"data": copy.deepcopy(value["data"])}
    if isinstance(value, list):
        return {"data": copy.deepcopy(value)}
    raise RuntimeBlocked("WEB_VOIP_USER_SNAPSHOT_SHAPE_UNSUPPORTED")


class GoldenWebConfigGate:
    """PR-D numeric WEB Golden; WEB owns mutation, SSH is cleanup cross-check only."""

    def __init__(
        self,
        *,
        definition: TestDefinition,
        run_id: str,
        device_id: str,
        worker_id: str,
        target_number: str,
        web: WebEntryAdapter,
        config: ConfigFrameworkExecutor,
        registration_probe: SipRegistrationProbe,
        authority: CaptureLeaseCompatibilityAdapter,
        session_factory,
        registration_timeout_seconds: float = 60.0,
        authority_keepalive_interval: float = 30.0,
    ) -> None:
        if definition.case.case_id != GOLDEN_WEB_CONFIG_CASE_ID:
            raise ValueError("WEB_GOLDEN_CASE_ID_MISMATCH")
        if definition.case.entry is not ActionEntry.WEB:
            raise ValueError("WEB_GOLDEN_ENTRY_MUST_BE_WEB")
        self.definition = definition
        self.case = replace(definition.case, parameters=dict(definition.case.parameters))
        self.run_id = run_id
        self.device_id = device_id
        self.worker_id = worker_id
        self.target_number = str(target_number)
        self.web = web
        self.config = config
        self.registration_probe = registration_probe
        self.authority = authority
        self.session_factory = session_factory
        self.registration_timeout_seconds = float(registration_timeout_seconds)
        self.keepalive = AuthorityKeepalive(
            authority,
            interval_seconds=authority_keepalive_interval,
        )
        self.runtime: dict[str, Any] = {"token": None, "snapshot": None, "probe": None}

    async def _precheck(self, context: AutomationRunContext) -> PrecheckResult:
        if context.case.case_id != GOLDEN_WEB_CONFIG_CASE_ID:
            return PrecheckResult(False, "WEB_GOLDEN_CASE_ID_MISMATCH")
        if not _is_ascii_digits(self.target_number):
            return PrecheckResult(False, "WEB_NUMERIC_TARGET_REQUIRED")
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
        self.keepalive.start(token)
        return token

    def _current_token(self):
        token = self.runtime.get("token")
        if token is None:
            raise RuntimeError("WEB_GOLDEN_AUTHORITY_MISSING")
        if self.keepalive.error is not None:
            self.keepalive.raise_if_failed()
        try:
            token = self.keepalive.token
        except RuntimeError:
            pass
        self.runtime["token"] = token
        return token

    def _validate_mutation_authority(self):
        token = self._current_token()
        self.authority.validate(token)
        return token

    async def _snapshot(self, context: AutomationRunContext) -> None:
        read = await self.web.execute(WEB_READ_ACTION, {}, context)
        snapshot = snapshot_writable_bundle(read)
        probe = build_numeric_probe(snapshot, self.target_number)
        self.runtime.update(snapshot=snapshot, probe=probe)
        self.case.parameters["target_number"] = self.target_number

    async def _configure(self, context, _args) -> ActionHandlerResult:
        probe = self.runtime.get("probe")
        if not isinstance(probe, Mapping):
            raise RuntimeError("WEB_GOLDEN_PROBE_NOT_PREPARED")
        self._validate_mutation_authority()
        mutation = await self.web.configure_voip_bundle(probe, context)
        evidence: list[ActionEvidence] = []
        if mutation.unknown_result:
            if isinstance(mutation.readback, Mapping):
                evidence.append(ActionEvidence(
                    source="entry",
                    data={"unknown_readback": mutation.readback},
                    evidence_refs=("web-golden://unknown-readback",),
                    source_timestamp=utcnow(),
                ))
            return ActionHandlerResult(
                success=False,
                output={"accepted": False, "error": mutation.error},
                evidence=tuple(evidence),
                unknown_result=True,
            )

        readback = await self.web.execute(WEB_READ_ACTION, {}, context)
        account = observed_account(readback)
        evidence.append(ActionEvidence(
            source="entry",
            data={
                **account,
                "mutation_accepted": mutation.accepted,
                "readback_accepted": readback.accepted,
                "writable_modules": list(WEB_WRITABLE_MODULES),
            },
            evidence_refs=("web-golden://config-readback",),
            source_timestamp=utcnow(),
        ))

        registration = await self.registration_probe.wait_registered(
            number=self.target_number,
            timeout_seconds=self.registration_timeout_seconds,
        )
        evidence.append(ActionEvidence(
            source="sip",
            data={
                "registered": registration.registered,
                "number": registration.number,
                "details": dict(registration.details or {}),
            },
            evidence_refs=registration.evidence_refs,
            source_timestamp=registration.source_timestamp or utcnow(),
        ))
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
        if not isinstance(snapshot, Mapping):
            return {"restore_required": False, "snapshot_not_captured": True}
        current = await self.web.execute(WEB_READ_ACTION, {})
        try:
            current_bundle = snapshot_writable_bundle(current)
        except RuntimeBlocked:
            current_bundle = None
        if current_bundle == snapshot:
            return {"restore_required": False, "already_restored": True}
        self._validate_mutation_authority()
        restored = await self.web.configure_voip_bundle(snapshot)
        if restored.unknown_result:
            raise RuntimeError("WEB_GOLDEN_RESTORE_RESULT_UNKNOWN")
        if not restored.accepted:
            raise RuntimeError(f"WEB_GOLDEN_RESTORE_REJECTED:{restored.error}")
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
        return actual == snapshot, {
            "web_reverse_verify": actual == snapshot,
            "writable_modules": list(WEB_WRITABLE_MODULES),
        }

    async def _crosscheck_action(self) -> dict[str, Any]:
        return {"transport": "ssh", "mutation": False, "module": "voipUserInfo"}

    async def _crosscheck_verify(self):
        snapshot = self.runtime.get("snapshot")
        if not isinstance(snapshot, Mapping):
            return True, {"snapshot_not_captured": True}
        expected = config_payload_from_web_module(snapshot["voipUserInfo"])
        current = await self.config.get("voipUserInfo")
        matched = ConfigFrameworkExecutor.payload_matches_readback(expected, current)
        return matched, {
            "ssh_config_crosscheck": matched,
            "module": "voipUserInfo",
            "mutation": False,
        }

    async def _release_action(self) -> dict[str, Any]:
        token = self.runtime.get("token")
        if token is None:
            return {"authority_acquired": False}
        await self.keepalive.stop()
        token = self._current_token()
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
                CleanupStepSpec("ssh_config_readback_crosscheck", self._crosscheck_action, self._crosscheck_verify),
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
        try:
            return await runtime.run(
                self.case,
                run_id=self.run_id,
                worker_id=self.worker_id,
                intent=RunIntent.VERIFY,
            )
        finally:
            try:
                await self.keepalive.stop()
            except RuntimeError:
                # A renewal failure is already fenced and must not trigger reacquire.
                pass
