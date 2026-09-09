from __future__ import annotations

import asyncio
import copy
from dataclasses import replace
from typing import Any, Mapping

from app.automation.actions.dispatcher import ActionEvidence, ActionHandlerResult
from app.automation.adapters.entries.web import EntryResult
from app.automation.adapters.pbx.identity import (
    PbxExtensionIdentityError,
    normalize_automation_identity,
)
from app.automation.gates.golden_web_config import (
    GOLDEN_WEB_CONFIG_CASE_ID,
    WEB_READ_ACTION,
    WEB_WRITABLE_MODULES,
    _account_rows,
    config_payload_from_web_module,
    observed_account,
    snapshot_writable_bundle,
    utcnow,
)
from app.automation.gates.golden_web_config_observed import ObservedGoldenWebConfigGate
from app.automation.orchestrator import PrecheckResult, RuntimeBlocked
from app.automation.registry import TestDefinition

GOLDEN_PBX_REGISTER_CASE_ID = "Golden-PBX-REGISTER-002"
_MANAGED_FIELDS = ("number", "disName", "authId", "passwd")


def build_managed_identity_probe(
    snapshot: Mapping[str, Any], *, identity: str, password: str
) -> dict[str, Any]:
    target = normalize_automation_identity(identity)
    if not password:
        raise RuntimeBlocked("PBX_REGISTER_PASSWORD_REQUIRED")
    probe = copy.deepcopy(dict(snapshot))
    missing = [module for module in WEB_WRITABLE_MODULES if module not in probe]
    if missing:
        raise RuntimeBlocked(f"WEB_WRITABLE_SNAPSHOT_INCOMPLETE:{','.join(missing)}")
    row = _account_rows(probe["voipUserInfo"])[0]
    row["number"] = target
    row["disName"] = target
    row["authId"] = target
    row["passwd"] = password
    return {module: probe[module] for module in WEB_WRITABLE_MODULES}


def managed_identity_matches(
    bundle: Mapping[str, Any], *, identity: str, password: str
) -> bool:
    if "voipUserInfo" not in bundle:
        return False
    row = _account_rows(bundle["voipUserInfo"])[0]
    return all(
        str(row.get(field) or "") == expected
        for field, expected in (
            ("number", identity), ("disName", identity),
            ("authId", identity), ("passwd", password),
        )
    )


def build_managed_restore_bundle(
    current_bundle: Mapping[str, Any], snapshot: Mapping[str, Any]
) -> dict[str, Any]:
    restore = copy.deepcopy(dict(current_bundle))
    missing = [module for module in WEB_WRITABLE_MODULES if module not in restore]
    if missing:
        raise RuntimeBlocked(f"WEB_WRITABLE_CURRENT_INCOMPLETE:{','.join(missing)}")
    current_row = _account_rows(restore["voipUserInfo"])[0]
    snapshot_row = _account_rows(snapshot["voipUserInfo"])[0]
    for field in _MANAGED_FIELDS:
        current_row[field] = snapshot_row.get(field)
    return {module: restore[module] for module in WEB_WRITABLE_MODULES}


def managed_fields_restored(
    current_bundle: Mapping[str, Any], snapshot: Mapping[str, Any]
) -> bool:
    if "voipUserInfo" not in current_bundle or "voipUserInfo" not in snapshot:
        return False
    current = _account_rows(current_bundle["voipUserInfo"])[0]
    original = _account_rows(snapshot["voipUserInfo"])[0]
    return all(current.get(field) == original.get(field) for field in _MANAGED_FIELDS)


def _safe_account_if_target(
    output: Mapping[str, Any] | None, *, identity: str, password: str
) -> dict[str, Any] | None:
    if not isinstance(output, Mapping):
        return None
    modules = output.get("modules")
    if not isinstance(modules, Mapping) or "voipUserInfo" not in modules:
        return None
    if not managed_identity_matches(modules, identity=identity, password=password):
        return None
    row = _account_rows(modules["voipUserInfo"])[0]
    return {"number": row.get("number"), "disName": row.get("disName"), "authId": row.get("authId")}


class PbxRegistrationGoldenGate(ObservedGoldenWebConfigGate):
    """Dynamic PBX identity registration Golden without changing numeric Golden."""

    def __init__(self, *, definition: TestDefinition, target_password: str, **kwargs) -> None:
        if definition.case.case_id != GOLDEN_PBX_REGISTER_CASE_ID:
            raise ValueError("PBX_REGISTER_CASE_ID_MISMATCH")
        target_identity = str(kwargs.pop("target_number"))
        try:
            normalize_automation_identity(target_identity)
        except PbxExtensionIdentityError as exc:
            raise ValueError("PBX_REGISTER_IDENTITY_INVALID") from exc
        if not target_password:
            raise ValueError("PBX_REGISTER_PASSWORD_REQUIRED")

        bootstrap = TestDefinition(
            case=replace(definition.case, case_id=GOLDEN_WEB_CONFIG_CASE_ID),
            checksum=definition.checksum,
            source_path=definition.source_path,
        )
        super().__init__(
            definition=bootstrap,
            target_number=target_identity,
            **kwargs,
        )
        self.definition = definition
        self.case = replace(definition.case, parameters=dict(definition.case.parameters))
        self.target_password = target_password
        self.runtime["baseline_registration_identity"] = None

    async def _precheck(self, context) -> PrecheckResult:
        if context.case.case_id != GOLDEN_PBX_REGISTER_CASE_ID:
            return PrecheckResult(False, "PBX_REGISTER_CASE_ID_MISMATCH")
        try:
            normalize_automation_identity(self.target_number)
        except PbxExtensionIdentityError:
            return PrecheckResult(False, "PBX_REGISTER_IDENTITY_INVALID")
        if self.registration_timeout_seconds <= 0 or self.registration_timeout_seconds > 60.0:
            return PrecheckResult(False, "PBX_REGISTRATION_TIMEOUT_INVALID")
        return PrecheckResult(True)

    async def _snapshot(self, context) -> None:
        read = await self.web.execute(WEB_READ_ACTION, {}, context)
        snapshot = snapshot_writable_bundle(read)
        original = _account_rows(snapshot["voipUserInfo"])[0]
        baseline_identity = str(original.get("authId") or "").strip()
        if not baseline_identity:
            raise RuntimeBlocked("PBX_BASELINE_REGISTRATION_IDENTITY_REQUIRED")
        probe = build_managed_identity_probe(
            snapshot, identity=self.target_number, password=self.target_password
        )
        self.runtime.update(
            snapshot=snapshot,
            probe=probe,
            registration_identity=self.target_number,
            baseline_registration_identity=baseline_identity,
        )
        self.case.parameters["target_identity"] = self.target_number
        arm = getattr(self.registration_probe, "arm", None)
        if callable(arm):
            await arm()

    def _registration_identity(self) -> str:
        return self.target_number

    def _baseline_registration_identity(self) -> str:
        value = self.runtime.get("baseline_registration_identity")
        if not isinstance(value, str) or not value.strip():
            raise RuntimeError("PBX_BASELINE_REGISTRATION_IDENTITY_MISSING")
        return value.strip()

    async def _wait_target_registration(self):
        waiter = self.registration_probe.wait_registered
        try:
            return await waiter(
                number=self.target_number,
                timeout_seconds=self.registration_timeout_seconds,
                require_esl_event=True,
            )
        except TypeError:
            return await waiter(
                number=self.target_number,
                timeout_seconds=self.registration_timeout_seconds,
            )

    async def _observe_unknown_target(self, context, initial_readback):
        account = _safe_account_if_target(
            initial_readback, identity=self.target_number, password=self.target_password
        )
        if account is not None:
            return account
        for delay in (0.5, 1.0, 2.0):
            await asyncio.sleep(delay)
            try:
                readback: EntryResult = await asyncio.wait_for(
                    self.web.execute(WEB_READ_ACTION, {}, context), timeout=8.0
                )
            except Exception:
                continue
            if not readback.accepted:
                continue
            try:
                bundle = snapshot_writable_bundle(readback)
            except RuntimeBlocked:
                continue
            if managed_identity_matches(
                bundle, identity=self.target_number, password=self.target_password
            ):
                return observed_account(readback)
        return None

    async def _observe_unknown_target_via_config(self):
        probe = self.runtime.get("probe")
        if not isinstance(probe, Mapping):
            return None
        expected = config_payload_from_web_module(probe["voipUserInfo"])
        try:
            current = await self.config.get("voipUserInfo")
        except Exception:
            return None
        from app.infrastructure.config_framework.executor import ConfigFrameworkExecutor
        if not ConfigFrameworkExecutor.payload_matches_readback(expected, current):
            return None
        row = _account_rows(probe["voipUserInfo"])[0]
        return {"number": row.get("number"), "disName": row.get("disName"), "authId": row.get("authId")}

    async def _finish_from_account(
        self, *, account, evidence, mutation_accepted: bool, mutation_result_unknown: bool
    ) -> ActionHandlerResult:
        registration = await self._wait_target_registration()
        details = dict(registration.details or {})
        evidence.append(ActionEvidence(
            source="sip",
            data={
                "registered": registration.registered,
                "number": registration.number,
                "registration_identity": registration.number,
                "configured_number": self.target_number,
                "event_observed": bool(details.get("event_observed")),
                "details": details,
            },
            evidence_refs=registration.evidence_refs,
            source_timestamp=registration.source_timestamp or utcnow(),
        ))
        success = bool(registration.registered and details.get("event_observed"))
        return ActionHandlerResult(
            success=success,
            output={
                "mutation_accepted": bool(mutation_accepted),
                "mutation_result_unknown": bool(mutation_result_unknown),
                "mutation_effect_observed": True,
                "readback_accepted": True,
                "registration_observed": registration.registered,
                "registration_event_observed": bool(details.get("event_observed")),
                "configured_number": self.target_number,
                "registration_identity": self.target_number,
            },
            evidence=tuple(evidence),
        )

    async def _restore_action(self) -> dict[str, Any]:
        snapshot = self.runtime.get("snapshot")
        if not isinstance(snapshot, Mapping):
            return {"restore_required": False, "snapshot_not_captured": True}
        current = await self._cleanup_read()
        current_bundle = snapshot_writable_bundle(current)
        if managed_fields_restored(current_bundle, snapshot):
            return {"restore_required": False, "bundle_already_restored": True}
        restore_bundle = build_managed_restore_bundle(current_bundle, snapshot)
        self._validate_mutation_authority()
        restored = await self.web.configure_voip_bundle(restore_bundle)
        if restored.unknown_result:
            observed = await self._cleanup_read()
            actual = snapshot_writable_bundle(observed)
            if managed_fields_restored(actual, snapshot):
                return {
                    "restore_required": True,
                    "web_restore_result_unknown": True,
                    "web_restore_effect_observed": True,
                }
            raise RuntimeError("PBX_REGISTER_RESTORE_RESULT_UNKNOWN")
        if not restored.accepted:
            raise RuntimeError(f"PBX_REGISTER_RESTORE_REJECTED:{restored.error}")
        return {"restore_required": True, "web_restore_accepted": True}

    async def _restore_verify(self):
        snapshot = self.runtime.get("snapshot")
        if not isinstance(snapshot, Mapping):
            return True, {"mutation_not_started": True}
        current = await self._cleanup_read()
        try:
            actual = snapshot_writable_bundle(current)
            restored = managed_fields_restored(actual, snapshot)
        except RuntimeBlocked as exc:
            return False, {"reason": str(exc)}
        return restored, {
            "web_reverse_verify": restored,
            "restored_identity_fields": list(_MANAGED_FIELDS),
            "secret_values_emitted": False,
        }

    async def _registration_restore_action(self) -> dict[str, Any]:
        return {
            "mutation": False,
            "registration_identity": self._baseline_registration_identity(),
        }

    async def _registration_restore_verify(self):
        identity = self._baseline_registration_identity()
        registration = await self.registration_probe.wait_registered(
            number=identity,
            timeout_seconds=self.registration_timeout_seconds,
        )
        return bool(registration.registered), {
            "registration_restored": bool(registration.registered),
            "registration_identity": identity,
            "provider": (registration.details or {}).get("provider"),
        }

    async def run(self):
        try:
            return await super().run()
        finally:
            close = getattr(self.registration_probe, "close", None)
            if callable(close):
                await close()
