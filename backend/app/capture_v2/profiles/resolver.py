from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from app.capture_v2.errors import CaptureV2Error
from app.capture_v2.profiles.schema import CaptureProfile, EffectiveCaptureProfile, PlatformProfile
from app.capture_v2.profiles.validator import validate_invariants


def _load_yaml(path: Path) -> dict[str, Any]:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise CaptureV2Error("PROFILE_FILE_NOT_FOUND", details={"path": str(path)}) from exc
    except Exception as exc:
        raise CaptureV2Error("PROFILE_FILE_INVALID", details={"path": str(path)}) from exc
    if not isinstance(raw, dict):
        raise CaptureV2Error("PROFILE_FILE_INVALID", details={"path": str(path)})
    return raw


def _canonical_checksum(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class EffectiveProfileResolver:
    def __init__(self, profile_root: Path):
        self.profile_root = Path(profile_root)

    def _capture_profile(self, profile_id: str) -> CaptureProfile:
        directory = self.profile_root / "capture" / "v2.1"
        for path in sorted(directory.glob("*.yaml")):
            raw = _load_yaml(path)
            if str(raw.get("profile_id", "")) != profile_id:
                continue
            try:
                return CaptureProfile.model_validate(raw)
            except ValidationError as exc:
                raise CaptureV2Error("CAPTURE_PROFILE_SCHEMA_INVALID", details={"errors": exc.errors()}) from exc
        raise CaptureV2Error("CAPTURE_PROFILE_NOT_FOUND", details={"profile_id": profile_id})

    def _platform_profiles(self) -> list[PlatformProfile]:
        directory = self.profile_root / "platforms"
        result: list[PlatformProfile] = []
        for path in sorted(directory.glob("capture_v2_*.yaml")):
            try:
                result.append(PlatformProfile.model_validate(_load_yaml(path)))
            except ValidationError as exc:
                raise CaptureV2Error(
                    "PLATFORM_PROFILE_SCHEMA_INVALID",
                    details={"path": str(path), "errors": exc.errors()},
                ) from exc
        return result

    @staticmethod
    def _device_tokens(device: Any) -> set[str]:
        tokens: set[str] = set()
        platform_id = getattr(device, "platform_id", None)
        if platform_id:
            tokens.add(str(platform_id).strip().lower())
        info = getattr(device, "device_info", None) or {}
        if isinstance(info, dict):
            for key in ("cpu", "soc", "platform", "model", "product", "product_model", "device_model"):
                value = info.get(key)
                if value:
                    tokens.add(str(value).strip().lower())
        return tokens

    def _platform_profile(self, device: Any) -> PlatformProfile:
        tokens = self._device_tokens(device)
        profiles = self._platform_profiles()
        for profile in profiles:
            if profile.platform_id.lower() in tokens:
                return profile
            models = {m.strip().lower() for m in profile.models}
            if tokens & models:
                return profile
        for profile in profiles:
            if any(profile.platform_id.lower() in token for token in tokens):
                return profile
        raise CaptureV2Error("PLATFORM_PROFILE_NOT_FOUND", details={"device_tokens": sorted(tokens)})

    def resolve(self, *, device: Any, requested_profile_id: str) -> EffectiveCaptureProfile:
        capture = self._capture_profile(requested_profile_id)
        platform = self._platform_profile(device)
        validate_invariants(capture, platform)
        resolved = {
            "schema_version": 2,
            "capture": capture.capture.model_dump(mode="json"),
            "transfer": capture.transfer.model_dump(mode="json"),
            "channels": capture.channels.model_dump(mode="json"),
            "lease": capture.lease.model_dump(mode="json"),
            "spool": capture.spool.model_dump(mode="json"),
            "fxs": capture.fxs.model_dump(mode="json"),
            "readiness": capture.readiness.model_dump(mode="json"),
            "coverage": capture.coverage.model_dump(mode="json"),
            "quality": capture.quality.model_dump(mode="json"),
            "platform_resource": platform.resource.model_dump(mode="json"),
        }
        return EffectiveCaptureProfile(
            capture_profile_id=capture.profile_id,
            capture_profile_version=capture.profile_version,
            platform_profile_id=platform.platform_id,
            platform_profile_version=platform.profile_version,
            resolved=resolved,
            checksum_sha256=_canonical_checksum(resolved),
        )
