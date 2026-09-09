from __future__ import annotations

import asyncio
import socket
import time
from datetime import datetime, timezone

from app.automation.adapters.pbx.esl import FreeSwitchEventSocketClient
from app.automation.adapters.pbx.identity import PbxExtensionIdentityError, normalize_sip_user_wire_identity
from app.automation.adapters.pbx.profile import FusionPbxLabProfile
from app.automation.adapters.pbx.registration import FusionPbxRegistrationProbe
from app.automation.gates.golden_web_config import SipRegistrationEvidence


class FreeSwitchRegistrationObserverError(RuntimeError):
    pass


class FreeSwitchRegistrationObserver:
    """ESL-first registration observer with read-only fs_cli reconciliation."""

    def __init__(
        self,
        profile: FusionPbxLabProfile,
        *,
        fallback: FusionPbxRegistrationProbe | None = None,
        client: FreeSwitchEventSocketClient | None = None,
    ) -> None:
        self.profile = profile
        self.fallback = fallback or FusionPbxRegistrationProbe(
            fs_cli_bin=profile.fs_cli_bin,
            poll_interval_seconds=1.0,
            command_timeout_seconds=5.0,
        )
        self.client = client or FreeSwitchEventSocketClient(profile, timeout_seconds=1.0)
        self._armed = False

    async def arm(self) -> None:
        if self._armed:
            return
        await asyncio.to_thread(self.client.connect)
        await asyncio.to_thread(self.client.subscribe)
        self._armed = True

    async def close(self) -> None:
        await asyncio.to_thread(self.client.close)
        self._armed = False

    async def wait_registered(
        self,
        *,
        number: str,
        timeout_seconds: float,
        require_esl_event: bool = False,
    ) -> SipRegistrationEvidence:
        try:
            target = normalize_sip_user_wire_identity(number)
        except PbxExtensionIdentityError as exc:
            raise FreeSwitchRegistrationObserverError("PBX_REGISTRATION_IDENTITY_INVALID") from exc
        timeout = float(timeout_seconds)
        if timeout <= 0 or timeout > 60.0:
            raise FreeSwitchRegistrationObserverError("PBX_REGISTRATION_TIMEOUT_INVALID")
        if not self._armed:
            await self.arm()

        deadline = time.monotonic() + timeout
        event_observed = False
        fallback_observed = False
        fallback_refs: tuple[str, ...] = ()
        fallback_details: dict = {}
        last_reconcile = 0.0
        event_stream_error: str | None = None

        while time.monotonic() < deadline:
            try:
                event = await asyncio.to_thread(self.client.read_event)
            except (TimeoutError, socket.timeout):
                event = None
            except OSError as exc:
                event = None
                event_stream_error = type(exc).__name__
            if event is not None and event.kind == "REGISTERED" and event.identity == target:
                event_observed = True
                details = {
                    "provider": "freeswitch_esl",
                    "event_observed": True,
                    "fallback_observed": fallback_observed,
                    "event_kind": event.kind,
                    "event_subclass": event.subclass,
                    "event_stream_error": event_stream_error,
                    "mutation": False,
                    "secret_values_emitted": False,
                }
                refs = (f"freeswitch-esl://registration/{target}",) + fallback_refs
                return SipRegistrationEvidence(
                    registered=True,
                    number=target,
                    evidence_refs=refs,
                    source_timestamp=datetime.now(timezone.utc),
                    details=details,
                )

            now = time.monotonic()
            if now - last_reconcile >= 1.0:
                fallback_evidence = await asyncio.to_thread(
                    self.fallback.observe_registered_once, number=target
                )
                fallback_observed = bool(fallback_evidence.registered)
                fallback_refs = tuple(fallback_evidence.evidence_refs)
                fallback_details = dict(fallback_evidence.details or {})
                last_reconcile = now
                if fallback_observed and not require_esl_event:
                    return SipRegistrationEvidence(
                        registered=True,
                        number=target,
                        evidence_refs=fallback_refs,
                        source_timestamp=fallback_evidence.source_timestamp,
                        details={
                            "provider": "freeswitch_esl_with_fs_cli_reconcile",
                            "event_observed": event_observed,
                            "fallback_observed": True,
                            "fallback": fallback_details,
                            "event_stream_error": event_stream_error,
                            "mutation": False,
                            "secret_values_emitted": False,
                        },
                    )

        return SipRegistrationEvidence(
            registered=False,
            number=target,
            evidence_refs=fallback_refs,
            source_timestamp=datetime.now(timezone.utc),
            details={
                "provider": "freeswitch_esl_with_fs_cli_reconcile",
                "event_observed": event_observed,
                "fallback_observed": fallback_observed,
                "fallback": fallback_details,
                "event_stream_error": event_stream_error,
                "require_esl_event": require_esl_event,
                "mutation": False,
                "secret_values_emitted": False,
            },
        )
