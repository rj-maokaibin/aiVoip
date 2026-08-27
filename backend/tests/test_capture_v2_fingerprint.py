from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.capture_v2.bridge import CaptureV2ABBridge
from app.capture_v2.errors import CaptureV2Error
from app.capture_v2.profiles.fingerprint import DeviceFingerprint, DeviceFingerprintResolver
from app.capture_v2.profiles.resolver import EffectiveProfileResolver
from app.capture_v2.voice_context import VoiceContextV2

PROFILE_ROOT = Path(__file__).resolve().parents[2] / "profiles"

# Real payloads captured from Production M7 C06 DUT G1U060H000384 (APF3260-M, mt7981).
REAL_COMPATIBLE = "mediatek,mt7981-spim-snand-rfb"
REAL_MODEL = "MediaTek MT7981 RFB"
REAL_RELEASE = (
    "DISTRIB_ID='Ruijie'\n"
    "DISTRIB_RELEASE='2.0'\n"
    "DISTRIB_REVISION='3.0(1)B11P420'\n"
    "DISTRIB_TARGET='mediatek/apf3260'\n"
    "DISTRIB_ARCH='aarch64_cortex-a53'\n"
    "DISTRIB_DESCRIPTION='Ruijie 2.0 ReyeeOS 2.420.0.2017;AP_3.0(1)B11P420,Release(13201712)'\n"
)


class _FakeReader:
    def __init__(self, commands: dict[str, str]):
        self._commands = commands

    async def run(self, command: str, *, timeout=None):
        if command not in self._commands:
            raise AssertionError(f"unexpected command: {command}")
        return self._commands[command]


def _real_fingerprint() -> DeviceFingerprint:
    return DeviceFingerprint(
        platform_id="mt7981",
        models=("apf3260",),
        vendor="ruijie",
        soc="mt7981",
        raw={
            "compatible": REAL_COMPATIBLE,
            "device_tree_model": REAL_MODEL,
            "openwrt_release": REAL_RELEASE,
        },
    )


def test_fingerprint_parses_real_mt7981_dut_payloads():
    reader = _FakeReader(
        {
            "cat /proc/device-tree/compatible": REAL_COMPATIBLE + "\n",
            "cat /proc/device-tree/model": REAL_MODEL + "\n",
            "cat /etc/openwrt_release": REAL_RELEASE,
        }
    )
    fp = asyncio.run(DeviceFingerprintResolver(reader).resolve())
    assert fp.platform_id == "mt7981"
    assert fp.soc == "mt7981"
    assert "apf3260" in fp.models
    assert fp.vendor == "ruijie"
    tokens = fp.tokens()
    assert "mt7981" in tokens
    assert "apf3260" in tokens


def test_fingerprint_probe_error_is_best_effort():
    class _BrokenReader:
        async def run(self, command: str, *, timeout=None):
            raise OSError("no route to host")

    fp = asyncio.run(DeviceFingerprintResolver(_BrokenReader()).resolve())
    assert fp.platform_id is None
    assert fp.models == ()
    assert fp.tokens() == set()


def test_resolver_still_fails_closed_without_fingerprint_when_device_empty():
    device = SimpleNamespace(platform_id=None, device_info=None)
    with pytest.raises(CaptureV2Error) as exc:
        EffectiveProfileResolver(PROFILE_ROOT).resolve(
            device=device, requested_profile_id="voip-standard"
        )
    assert exc.value.code == "PLATFORM_PROFILE_NOT_FOUND"


def test_resolver_matches_mt7981_with_fingerprint_tokens_even_when_device_empty():
    # Reproduces the production blocker: CaseDevice has platform_id=None and
    # device_info=None, so only the real-DUT fingerprint tokens can match mt7981.
    device = SimpleNamespace(platform_id=None, device_info=None)
    resolved = EffectiveProfileResolver(PROFILE_ROOT).resolve(
        device=device,
        requested_profile_id="voip-standard",
        extra_tokens=_real_fingerprint().tokens(),
    )
    assert resolved.platform_profile_id == "mt7981"
    assert resolved.capture_profile_id == "voip-standard"


def test_resolver_merges_fingerprint_with_existing_device_info():
    device = SimpleNamespace(platform_id=None, device_info={"model": "APF3260-M"})
    resolved = EffectiveProfileResolver(PROFILE_ROOT).resolve(
        device=device,
        requested_profile_id="voip-standard",
        extra_tokens=_real_fingerprint().tokens(),
    )
    assert resolved.platform_profile_id == "mt7981"


def test_bridge_fresh_resolve_enriches_profile_from_fingerprint(monkeypatch):
    device = SimpleNamespace(id="D1", platform_id=None, device_info=None)
    seen = {}

    bridge = CaptureV2ABBridge(
        session_factory=lambda: None,
        adapter=object(),
        profile_root=PROFILE_ROOT,
        requested_profile_id="voip-standard",
    )
    monkeypatch.setattr(bridge, "_existing", lambda reproduction_session_id: None)
    monkeypatch.setattr(bridge, "_ensure_capture_session", lambda **kwargs: "CS1")
    monkeypatch.setattr(bridge, "_persist_fingerprint", lambda device, fp: None)

    class FakeFingerprintResolver:
        def __init__(self, reader):
            pass

        async def resolve(self):
            return _real_fingerprint()

    monkeypatch.setattr("app.capture_v2.bridge.DeviceFingerprintResolver", FakeFingerprintResolver)

    class FakeSupervisor:
        async def establish_ownership(self, **kwargs):
            seen["ownership"] = kwargs
            return SimpleNamespace(lease=SimpleNamespace(lease_epoch=9))

    monkeypatch.setattr(
        "app.capture_v2.bridge.build_capture_v2_ab",
        lambda *, adapter, effective_profile: FakeSupervisor(),
    )

    class FakeVoiceResolver:
        def __init__(self, reader):
            pass

        async def resolve(self):
            return VoiceContextV2(
                gateway_ip="192.168.3.200", voice_vlan_id="400", interface="br-lan_400"
            )

    monkeypatch.setattr("app.capture_v2.bridge.VoiceContextResolverV2", FakeVoiceResolver)

    result = asyncio.run(
        bridge.establish(reproduction_session_id="R1", device=device, worker_id="W2")
    )
    assert result.capture_session_id == "CS1"
    assert result.effective_profile.platform_profile_id == "mt7981"
    assert seen["ownership"]["voice_interface"] == "br-lan_400"
