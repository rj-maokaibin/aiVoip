from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.capture_v2.db_models import CaptureSession
from app.capture_v2.factory import build_capture_v2_ab
from app.capture_v2.profiles.fingerprint import DeviceFingerprint, DeviceFingerprintResolver
from app.capture_v2.profiles.resolver import EffectiveProfileResolver
from app.capture_v2.profiles.schema import EffectiveCaptureProfile
from app.capture_v2.repository.core import CaptureSessionRepository
from app.capture_v2.supervisor import OwnershipReady
from app.capture_v2.transport.readonly import ReadOnlyDeviceTransport
from app.capture_v2.voice_context import VoiceContextResolverV2, VoiceContextV2


@dataclass(frozen=True)
class ABOwnershipBootstrapResult:
    capture_session_id: str
    effective_profile: EffectiveCaptureProfile
    voice_context: VoiceContextV2
    ownership: OwnershipReady


class CaptureV2ABBridge:
    """Safe A/B bootstrap for ownership gates and later composition integration.

    The bridge deliberately owns *only* Foundation + Ownership.  It does not
    advertise CAPTURE_PATH_READY and does not start the V1 reproduction watcher.
    A caller must keep the adapter connection on the same asyncio loop for the
    duration of this method.  Phase C/D will add lease renewal, durable segment
    transfer and readiness before this becomes the production live path.
    """

    def __init__(
        self,
        *,
        session_factory: Callable,
        adapter: Any,
        profile_root: Path,
        requested_profile_id: str,
    ):
        self.session_factory = session_factory
        self.adapter = adapter
        self.profile_root = Path(profile_root)
        self.requested_profile_id = requested_profile_id

    def _existing(self, reproduction_session_id: str) -> CaptureSession | None:
        with self.session_factory() as db:
            return db.scalar(
                select(CaptureSession).where(
                    CaptureSession.reproduction_session_id == reproduction_session_id
                )
            )

    def _ensure_capture_session(
        self,
        *,
        reproduction_session_id: str,
        device: Any,
        effective_profile: EffectiveCaptureProfile,
        supervisor,
    ) -> str:
        existing = self._existing(reproduction_session_id)
        if existing is not None:
            return existing.id
        try:
            return supervisor.create_session(
                reproduction_session_id=reproduction_session_id,
                device_id=device.id,
                effective_profile=effective_profile,
            )
        except IntegrityError:
            # Unique reproduction_session_id makes concurrent bootstrap idempotent.
            existing = self._existing(reproduction_session_id)
            if existing is None:
                raise
            return existing.id

    async def establish(
        self,
        *,
        reproduction_session_id: str,
        device: Any,
        worker_id: str,
    ) -> ABOwnershipBootstrapResult:
        existing = self._existing(reproduction_session_id)
        if existing is not None:
            # Effective Profile is immutable once the CaptureSession exists.  A
            # restart/takeover must replay the persisted snapshot, never silently
            # re-resolve today's YAML into yesterday's session.
            effective = EffectiveCaptureProfile.model_validate(existing.effective_profile)
            fingerprint: DeviceFingerprint | None = None
        else:
            # Case creation does not fingerprint the DUT, so DB-derived platform
            # tokens may be empty.  Probe the real DUT (read-only) and enrich the
            # resolution tokens so platform profiles match by SoC/model even for
            # an unpopulated CaseDevice row (e.g. APF3260-M -> mt7981).
            reader = ReadOnlyDeviceTransport(self.adapter)
            fingerprint = None
            try:
                fingerprint = await DeviceFingerprintResolver(reader).resolve()
            except Exception:
                fingerprint = None
            extra_tokens = fingerprint.tokens() if fingerprint is not None else None
            effective = EffectiveProfileResolver(self.profile_root).resolve(
                device=device,
                requested_profile_id=self.requested_profile_id,
                extra_tokens=extra_tokens,
            )
            if fingerprint is not None:
                self._persist_fingerprint(device, fingerprint)

        supervisor = build_capture_v2_ab(adapter=self.adapter, effective_profile=effective)
        capture_session_id = self._ensure_capture_session(
            reproduction_session_id=reproduction_session_id,
            device=device,
            effective_profile=effective,
            supervisor=supervisor,
        )
        reader = ReadOnlyDeviceTransport(self.adapter)
        voice = await VoiceContextResolverV2(reader).resolve()
        ownership = await supervisor.establish_ownership(
            capture_session_id=capture_session_id,
            device_id=device.id,
            worker_id=worker_id,
            voice_interface=voice.interface,
        )
        return ABOwnershipBootstrapResult(
            capture_session_id=capture_session_id,
            effective_profile=effective,
            voice_context=voice,
            ownership=ownership,
        )

    def _persist_fingerprint(self, device: Any, fingerprint: DeviceFingerprint) -> None:
        """Best-effort, auditable persistence of the discovered fingerprint.

        Never fails the establish flow; a stale DB row or a non-CaseDevice test
        stub simply skips the update.
        """
        device_id = getattr(device, "id", None)
        if not device_id:
            return
        info: dict[str, Any] = {
            "platform_id": fingerprint.platform_id,
            "models": list(fingerprint.models),
            "vendor": fingerprint.vendor,
            "soc": fingerprint.soc,
            "fingerprint_source": "dut-read-only-probe",
            "fingerprint_raw": fingerprint.raw,
        }
        if fingerprint.models:
            info["model"] = fingerprint.models[0]
        try:
            with self.session_factory() as db:
                from app.db.models import CaseDevice

                row = db.get(CaseDevice, device_id)
                if row is None:
                    return
                changed = False
                if fingerprint.platform_id and row.platform_id != fingerprint.platform_id:
                    row.platform_id = fingerprint.platform_id
                    changed = True
                existing_info = dict(row.device_info or {})
                merged = {**existing_info, **info}
                if merged != existing_info:
                    row.device_info = merged
                    changed = True
                if changed:
                    db.commit()
        except Exception:
            pass
