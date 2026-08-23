import asyncio
from pathlib import Path
from types import SimpleNamespace

from app.capture_v2.bridge import CaptureV2ABBridge
from app.capture_v2.profiles.resolver import EffectiveProfileResolver
from app.capture_v2.voice_context import VoiceContextV2


PROFILE_ROOT = Path(__file__).resolve().parents[2] / "profiles"


def test_bridge_replays_persisted_effective_profile_on_takeover(monkeypatch):
    device = SimpleNamespace(id="D1", platform_id=None, device_info={"model": "APF1250"})
    persisted = EffectiveProfileResolver(PROFILE_ROOT).resolve(
        device=device, requested_profile_id="voip-standard"
    )
    existing = SimpleNamespace(id="CS1", effective_profile=persisted.model_dump(mode="json"))
    seen = {}

    bridge = CaptureV2ABBridge(
        session_factory=lambda: None,
        adapter=object(),
        profile_root=PROFILE_ROOT,
        requested_profile_id="SHOULD_NOT_BE_RE_RESOLVED",
    )
    monkeypatch.setattr(bridge, "_existing", lambda reproduction_session_id: existing)
    monkeypatch.setattr(bridge, "_ensure_capture_session", lambda **kwargs: "CS1")

    class FakeSupervisor:
        async def establish_ownership(self, **kwargs):
            seen["ownership_kwargs"] = kwargs
            return SimpleNamespace(lease=SimpleNamespace(lease_epoch=9))

    def fake_build(*, adapter, effective_profile):
        seen["effective"] = effective_profile
        return FakeSupervisor()

    class FakeVoiceResolver:
        def __init__(self, reader): pass
        async def resolve(self):
            return VoiceContextV2(
                gateway_ip="192.168.3.200", voice_vlan_id="400", interface="br-lan_400"
            )

    monkeypatch.setattr("app.capture_v2.bridge.build_capture_v2_ab", fake_build)
    monkeypatch.setattr("app.capture_v2.bridge.VoiceContextResolverV2", FakeVoiceResolver)

    result = asyncio.run(bridge.establish(
        reproduction_session_id="R1", device=device, worker_id="W2"
    ))
    assert result.capture_session_id == "CS1"
    assert seen["effective"].checksum_sha256 == persisted.checksum_sha256
    assert seen["ownership_kwargs"] == {
        "capture_session_id": "CS1",
        "device_id": "D1",
        "worker_id": "W2",
        "voice_interface": "br-lan_400",
    }
