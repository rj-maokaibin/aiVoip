from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from typing import Any, Mapping

import httpx

from app.automation.actions.dispatcher import ActionEvidence, ActionHandlerResult
from app.automation.adapters.entries.web import EntryResult
from app.automation.adapters.web_auth.legacy_luci import LegacyLuciAuthError
from app.automation.gates.golden_web_config import (
    WEB_READ_ACTION,
    WEB_WRITABLE_MODULES,
    GoldenWebConfigGate,
    config_payload_from_web_module,
    observed_account,
    snapshot_writable_bundle,
    build_cleanup_restore_bundle,
    cleanup_user_restored,
)
from app.automation.orchestrator import RuntimeBlocked
from app.infrastructure.config_framework.executor import ConfigFrameworkExecutor
from app.infrastructure.transport.http import HttpEvidence

_UNKNOWN_TARGET_OBSERVE_BACKOFF_SECONDS = (2.0, 5.0)
_UNKNOWN_TARGET_OBSERVE_ATTEMPT_TIMEOUT_SECONDS = 20.0
_UNKNOWN_TARGET_OBSERVE_RETRYABLE = (
    LegacyLuciAuthError,
    httpx.TransportError,
    asyncio.TimeoutError,
    TimeoutError,
)

_CLEANUP_WEB_READ_BACKOFF_SECONDS = (0.0, 1.0, 2.0, 4.0)
_CLEANUP_WEB_READ_ATTEMPT_TIMEOUT_SECONDS = 20.0


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _http_evidence_summary(evidence: HttpEvidence) -> dict[str, Any]:
    """Return only sanitized transport completion metadata for persisted evidence."""

    response = evidence.response if isinstance(evidence.response, Mapping) else None
    status_code = response.get("status_code") if response is not None else None
    return {
        "request_id": evidence.request_id,
        "attempt": evidence.attempt,
        "method": evidence.method,
        "path": evidence.path,
        "elapsed_ms": round(float(evidence.elapsed_ms), 3),
        "status_code": status_code,
        "error": evidence.error,
    }


def observed_unknown_target(
    readback: Any,
    *,
    target_number: str,
) -> dict[str, Any] | None:
    """Resolve an UNKNOWN WEB mutation only from its read-only observation.

    The transport layer never retries mutations. When a mutation result is
    UNKNOWN it performs the profile-bound readback first. This helper accepts
    that already-sanitized readback and proves only the target identity fields.
    It never guesses success and it never persists a raw password-bearing
    runtime response.
    """

    if not isinstance(readback, Mapping):
        return None
    try:
        account = observed_account(EntryResult(accepted=True, output=readback))
    except RuntimeBlocked:
        return None
    target = str(target_number)
    if account.get("number") != target or account.get("disName") != target:
        return None
    return account


class ObservedGoldenWebConfigGate(GoldenWebConfigGate):
    """PR-D Golden with observe-before-retry semantics for mutation UNKNOWN.

    No mutation retry is performed here. If the transport result is UNKNOWN,
    only bounded read-only observations can resolve it. A proven target readback
    is sufficient to continue to SIP registration; any other observation remains
    INCONCLUSIVE and cleanup restores only the mutated ``voipUserInfo`` module
    from the original five-module snapshot; reverse verification still checks
    the complete five-module snapshot for unintended drift.
    """

    async def _observe_unknown_target(
        self,
        context,
        initial_readback: Any,
    ) -> dict[str, Any] | None:
        account = observed_unknown_target(
            initial_readback,
            target_number=self.target_number,
        )
        if account is not None:
            return account

        # The adapter already owns the first bounded fresh-session observation.
        # Keep only a very small outer read-only grace window for a DUT that is
        # still committing after that observation. Each call has its own hard
        # wall-clock budget so normal read/auth retry policies cannot multiply
        # into several minutes. Mutation is never reissued here.
        for delay in _UNKNOWN_TARGET_OBSERVE_BACKOFF_SECONDS:
            await asyncio.sleep(delay)
            try:
                readback = await asyncio.wait_for(
                    self.web.execute(WEB_READ_ACTION, {}, context),
                    timeout=_UNKNOWN_TARGET_OBSERVE_ATTEMPT_TIMEOUT_SECONDS,
                )
            except _UNKNOWN_TARGET_OBSERVE_RETRYABLE:
                continue
            if not readback.accepted:
                continue
            account = observed_unknown_target(
                readback.output,
                target_number=self.target_number,
            )
            if account is not None:
                return account
        return None

    async def _observe_unknown_target_via_config(self) -> dict[str, Any] | None:
        """Prove an UNKNOWN WEB Save through the existing SSH read-only config path.

        WEB remains the only mutation entry.  This path performs only
        ``ConfigFrameworkExecutor.get`` and accepts the target only when the
        returned ``voipUserInfo`` exactly matches the profile-bound probe.
        """

        probe = self.runtime.get("probe")
        if not isinstance(probe, Mapping):
            return None
        probe_user_info = probe.get("voipUserInfo")
        if probe_user_info is None:
            return None
        try:
            expected = config_payload_from_web_module(probe_user_info)
            current = await self.config.get("voipUserInfo")
        except Exception:
            return None
        if not ConfigFrameworkExecutor.payload_matches_readback(expected, current):
            return None
        rows = expected.get("data")
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], Mapping):
            return None
        row = rows[0]
        target = str(self.target_number)
        if str(row.get("number")) != target or str(row.get("disName")) != target:
            return None
        return {
            "number": row.get("number"),
            "disName": row.get("disName"),
            "authId": row.get("authId"),
        }

    async def _cleanup_read(self, context=None) -> EntryResult:
        """Bound cleanup observation time and recover only local WEB session state."""

        invalidate = getattr(self.web.session_manager, "invalidate", None)
        last_error: Exception | None = None
        for attempt, delay in enumerate(_CLEANUP_WEB_READ_BACKOFF_SECONDS):
            if delay:
                await asyncio.sleep(delay)
            # The test mutation has already proven this session can Save and read
            # back the DUT. Reuse it for the first cleanup observation instead of
            # forcing a fresh login while the five-module apply may still be
            # settling. Only recover local auth/session state after a failed first
            # attempt; retries remain read-only.
            if attempt > 0 and callable(invalidate):
                invalidate()
            try:
                result = await asyncio.wait_for(
                    self.web.execute(WEB_READ_ACTION, {}, context),
                    timeout=_CLEANUP_WEB_READ_ATTEMPT_TIMEOUT_SECONDS,
                )
            except _UNKNOWN_TARGET_OBSERVE_RETRYABLE as exc:
                last_error = exc
                continue
            if result.accepted:
                return result
        raise RuntimeError("WEB_GOLDEN_CLEANUP_READ_UNAVAILABLE") from last_error

    async def _restore_action(self) -> dict[str, Any]:
        snapshot = self.runtime.get("snapshot")
        if not isinstance(snapshot, Mapping):
            return {"restore_required": False, "snapshot_not_captured": True}
        current = await self._cleanup_read()
        current_bundle = snapshot_writable_bundle(current)
        if cleanup_user_restored(current_bundle, snapshot):
            return {
                "restore_required": False,
                "bundle_already_restored": True,
                "restore_modules": list(WEB_WRITABLE_MODULES),
            }

        restore_bundle = build_cleanup_restore_bundle(current_bundle, snapshot)
        self._validate_mutation_authority()
        restored = await self.web.configure_voip_bundle(restore_bundle)
        if restored.unknown_result:
            # Never retry an UNKNOWN cleanup mutation. Observe only.
            observed = await self._cleanup_read()
            try:
                actual = snapshot_writable_bundle(observed)
                effect_observed = cleanup_user_restored(actual, snapshot)
            except RuntimeBlocked:
                effect_observed = False
            if effect_observed:
                return {
                    "restore_required": True,
                    "restore_modules": list(WEB_WRITABLE_MODULES),
                    "web_restore_result_unknown": True,
                    "web_restore_effect_observed": True,
                }
            raise RuntimeError("WEB_GOLDEN_RESTORE_RESULT_UNKNOWN")
        if not restored.accepted:
            raise RuntimeError(f"WEB_GOLDEN_RESTORE_REJECTED:{restored.error}")
        return {
            "restore_required": True,
            "restore_modules": list(WEB_WRITABLE_MODULES),
            "web_restore_accepted": True,
        }

    async def _restore_verify(self):
        snapshot = self.runtime.get("snapshot")
        if not isinstance(snapshot, Mapping):
            return True, {"mutation_not_started": True}
        current = await self._cleanup_read()
        try:
            actual = snapshot_writable_bundle(current)
            restored = cleanup_user_restored(actual, snapshot)
        except RuntimeBlocked as exc:
            return False, {"reason": str(exc)}
        return restored, {
            "web_reverse_verify": restored,
            "writable_modules": list(self.runtime.get("snapshot", {}).keys()),
            "restored_identity_fields": ["number", "disName"],
            "preserved_user_module_verified": True,
        }

    async def _finish_from_account(
        self,
        *,
        account: Mapping[str, Any],
        evidence: list[ActionEvidence],
        mutation_accepted: bool,
        mutation_result_unknown: bool,
    ) -> ActionHandlerResult:
        registration_identity = self._registration_identity()
        registration = await self.registration_probe.wait_registered(
            number=registration_identity,
            timeout_seconds=self.registration_timeout_seconds,
        )
        evidence.append(
            ActionEvidence(
                source="sip",
                data={
                    "registered": registration.registered,
                    "number": registration.number,
                    "registration_identity": registration.number,
                    "configured_number": self.target_number,
                    "details": dict(registration.details or {}),
                },
                evidence_refs=registration.evidence_refs,
                source_timestamp=registration.source_timestamp or utcnow(),
            )
        )
        return ActionHandlerResult(
            success=bool(registration.registered),
            output={
                "mutation_accepted": bool(mutation_accepted),
                "mutation_result_unknown": bool(mutation_result_unknown),
                "mutation_effect_observed": True,
                "readback_accepted": True,
                "registration_observed": registration.registered,
                "configured_number": self.target_number,
                "registration_identity": registration_identity,
            },
            evidence=tuple(evidence),
        )

    async def _configure(self, context, _args) -> ActionHandlerResult:
        probe = self.runtime.get("probe")
        if not isinstance(probe, Mapping):
            raise RuntimeError("WEB_GOLDEN_PROBE_NOT_PREPARED")

        if probe.get("voipUserInfo") is None:
            raise RuntimeError("WEB_GOLDEN_VOIP_USER_PROBE_MISSING")

        self._validate_mutation_authority()
        mutation = await self.web.configure_voip_bundle(probe, context)
        evidence: list[ActionEvidence] = []

        if mutation.unknown_result:
            transport_evidence = [_http_evidence_summary(item) for item in mutation.evidence]
            # Keep only the already-sanitized completion metadata in process-local
            # runtime so the live runner can persist an actionable UNKNOWN reason.
            # Request/response payloads, headers, cookies and credentials are never
            # copied into this diagnostic channel.
            self.runtime["sanitized_mutation_transport_evidence"] = list(transport_evidence)
            self.runtime["unknown_initial_readback_available"] = isinstance(
                mutation.readback, Mapping
            )
            self.runtime["sanitized_unknown_observation_diagnostics"] = [
                {
                    "attempt": item.get("attempt"),
                    "phase": item.get("phase"),
                    "elapsed_ms": item.get("elapsed_ms"),
                    "status_code": item.get("status_code"),
                    "accepted": item.get("accepted"),
                    "error": item.get("error"),
                    "detail": item.get("detail"),
                }
                for item in mutation.observation_diagnostics
                if isinstance(item, Mapping)
            ]
            account = await self._observe_unknown_target(context, mutation.readback)
            observed_via = "web"
            if account is None:
                account = await self._observe_unknown_target_via_config()
                if account is not None:
                    observed_via = "ssh_config_read_only"
            if account is None:
                evidence.append(
                    ActionEvidence(
                        source="entry",
                        data={
                            "mutation_result_unknown": True,
                            "mutation_effect_observed": False,
                            "transport_evidence": transport_evidence,
                            "initial_readback_available": isinstance(mutation.readback, Mapping),
                        },
                        evidence_refs=("web-golden://unknown-transport",),
                        source_timestamp=utcnow(),
                    )
                )
                return ActionHandlerResult(
                    success=False,
                    output={
                        "accepted": False,
                        "error": mutation.error,
                        "observe_before_retry": True,
                        "retry_executed": False,
                        "transport_evidence": transport_evidence,
                        "initial_readback_available": isinstance(mutation.readback, Mapping),
                    },
                    evidence=tuple(evidence),
                    unknown_result=True,
                )

            evidence.append(
                ActionEvidence(
                    source="entry",
                    data={
                        **dict(account),
                        "mutation_accepted": False,
                        "mutation_result_unknown": True,
                        "mutation_effect_observed": True,
                        "readback_accepted": True,
                        "observation_transport": observed_via,
                        "transport_evidence": transport_evidence,
                    },
                    evidence_refs=("web-golden://unknown-target-observed",),
                    source_timestamp=utcnow(),
                )
            )
            return await self._finish_from_account(
                account=account,
                evidence=evidence,
                mutation_accepted=False,
                mutation_result_unknown=True,
            )

        readback = await self.web.execute(WEB_READ_ACTION, {}, context)
        account = observed_account(readback)
        evidence.append(
            ActionEvidence(
                source="entry",
                data={
                    **account,
                    "mutation_accepted": mutation.accepted,
                    "mutation_result_unknown": False,
                    "mutation_effect_observed": True,
                    "readback_accepted": readback.accepted,
                },
                evidence_refs=("web-golden://config-readback",),
                source_timestamp=utcnow(),
            )
        )
        if not mutation.accepted or not readback.accepted:
            return ActionHandlerResult(
                success=False,
                output={
                    "mutation_accepted": mutation.accepted,
                    "mutation_result_unknown": False,
                    "mutation_effect_observed": True,
                    "readback_accepted": readback.accepted,
                    "registration_observed": False,
                },
                evidence=tuple(evidence),
            )

        return await self._finish_from_account(
            account=account,
            evidence=evidence,
            mutation_accepted=True,
            mutation_result_unknown=False,
        )
