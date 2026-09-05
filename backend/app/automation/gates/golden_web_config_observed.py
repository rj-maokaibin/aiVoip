from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from app.automation.actions.dispatcher import ActionEvidence, ActionHandlerResult
from app.automation.adapters.entries.web import EntryResult
from app.automation.gates.golden_web_config import (
    WEB_READ_ACTION,
    GoldenWebConfigGate,
    observed_account,
)
from app.automation.orchestrator import RuntimeBlocked


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


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

    No retry is performed here. If the transport result is UNKNOWN, only the
    automatic read-only observation can resolve it. A proven target readback is
    sufficient to continue to SIP registration; any other observation remains
    INCONCLUSIVE and cleanup runs from the original five-module snapshot.
    """

    async def _finish_from_account(
        self,
        *,
        account: Mapping[str, Any],
        evidence: list[ActionEvidence],
        mutation_accepted: bool,
        mutation_result_unknown: bool,
    ) -> ActionHandlerResult:
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
            success=bool(registration.registered),
            output={
                "mutation_accepted": bool(mutation_accepted),
                "mutation_result_unknown": bool(mutation_result_unknown),
                "mutation_effect_observed": True,
                "readback_accepted": True,
                "registration_observed": registration.registered,
            },
            evidence=tuple(evidence),
        )

    async def _configure(self, context, _args) -> ActionHandlerResult:
        probe = self.runtime.get("probe")
        if not isinstance(probe, Mapping):
            raise RuntimeError("WEB_GOLDEN_PROBE_NOT_PREPARED")

        self._validate_mutation_authority()
        mutation = await self.web.configure_voip_bundle(probe, context)
        evidence: list[ActionEvidence] = []

        if mutation.unknown_result:
            account = observed_unknown_target(
                mutation.readback,
                target_number=self.target_number,
            )
            if account is None:
                if isinstance(mutation.readback, Mapping):
                    evidence.append(
                        ActionEvidence(
                            source="entry",
                            data={
                                "mutation_result_unknown": True,
                                "mutation_effect_observed": False,
                            },
                            evidence_refs=("web-golden://unknown-readback",),
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
