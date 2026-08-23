from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from app.capture_v2.errors import CaptureV2Error
from app.capture_v2.profiles.resolver import EffectiveProfileResolver
from app.capture_v2.profiles.schema import CaptureProfile, LeaseConfig


PROFILE_ROOT = Path(__file__).resolve().parents[2] / "profiles"


def test_standard_profile_resolves_mt7621_and_freezes_five_second_segments():
    device = SimpleNamespace(platform_id=None, device_info={"model": "APF1250"})
    resolved = EffectiveProfileResolver(PROFILE_ROOT).resolve(
        device=device, requested_profile_id="voip-standard"
    )
    assert resolved.platform_profile_id == "mt7621"
    assert resolved.resolved["capture"]["mode"] == "FULL_VOICE"
    assert resolved.resolved["capture"]["snaplen"] == 0
    assert resolved.resolved["capture"]["segment_seconds"] == 5
    assert resolved.resolved["transfer"]["parallelism"] == 1
    assert resolved.resolved["spool"]["pressure_policy"] == "FAIL_CLOSED_NO_EVICT_UNACKED"
    assert resolved.resolved["fxs"]["hook_glitch_max_ms"] == 100
    assert resolved.resolved["readiness"]["sip_expectation_timeout_seconds"] == 3
    assert resolved.resolved["coverage"]["pre_trigger_seconds"] == 10
    assert resolved.resolved["quality"]["policy_version"] == "capture-quality-v2.1"
    assert len(resolved.checksum_sha256) == 64


def test_standard_profile_resolves_mt7981_by_model():
    device = SimpleNamespace(platform_id=None, device_info={"product_model": "APF3260-M"})
    resolved = EffectiveProfileResolver(PROFILE_ROOT).resolve(
        device=device, requested_profile_id="voip-standard"
    )
    assert resolved.platform_profile_id == "mt7981"


def test_unknown_platform_fails_closed():
    device = SimpleNamespace(platform_id=None, device_info={"model": "UNKNOWN"})
    with pytest.raises(CaptureV2Error) as exc:
        EffectiveProfileResolver(PROFILE_ROOT).resolve(device=device, requested_profile_id="voip-standard")
    assert exc.value.code == "PLATFORM_PROFILE_NOT_FOUND"


def test_profile_schema_rejects_non_five_second_segment():
    with pytest.raises(ValidationError):
        CaptureProfile.model_validate(
            {
                "schema_version": 2,
                "profile_id": "bad",
                "profile_version": "1",
                "capture": {"mode": "FULL_VOICE", "snaplen": 0, "segment_seconds": 10},
            }
        )


def test_lease_ttl_must_have_two_renew_intervals_margin():
    with pytest.raises(ValidationError):
        LeaseConfig(ttl_seconds=20, renew_interval_seconds=10)


def test_resolver_ignores_unrelated_legacy_platform_profiles(tmp_path):
    import shutil

    capture_dir = tmp_path / "capture" / "v2.1"
    platform_dir = tmp_path / "platforms"
    capture_dir.mkdir(parents=True)
    platform_dir.mkdir(parents=True)

    shutil.copy(PROFILE_ROOT / "capture" / "v2.1" / "standard.yaml", capture_dir / "standard.yaml")
    shutil.copy(PROFILE_ROOT / "platforms" / "capture_v2_mt7621.yaml", platform_dir / "capture_v2_mt7621.yaml")
    (platform_dir / "generic_openwrt.yaml").write_text(
        "platform_id: generic-openwrt\ncommands:\n  ip_addr: ip addr\n", encoding="utf-8"
    )

    device = SimpleNamespace(platform_id=None, device_info={"model": "APF1250"})
    resolved = EffectiveProfileResolver(tmp_path).resolve(
        device=device, requested_profile_id="voip-standard"
    )
    assert resolved.platform_profile_id == "mt7621"


def test_profile_schema_rejects_non_full_voice_mode_and_nonzero_snaplen():
    for capture in (
        {"mode": "FILTERED", "snaplen": 0, "segment_seconds": 5},
        {"mode": "FULL_VOICE", "snaplen": 256, "segment_seconds": 5},
    ):
        with pytest.raises(ValidationError):
            CaptureProfile.model_validate(
                {
                    "schema_version": 2,
                    "profile_id": "bad",
                    "profile_version": "1",
                    "capture": capture,
                }
            )


def test_effective_profile_is_immutable_and_deterministic():
    device = SimpleNamespace(platform_id=None, device_info={"model": "APF1250"})
    resolver = EffectiveProfileResolver(PROFILE_ROOT)
    a = resolver.resolve(device=device, requested_profile_id="voip-standard")
    b = resolver.resolve(device=device, requested_profile_id="voip-standard")
    assert a.checksum_sha256 == b.checksum_sha256
    with pytest.raises(ValidationError):
        a.capture_profile_id = "mutated"
