from types import SimpleNamespace

import pytest

from app.capture_v2 import production_readiness
from app.capture_v2.enums import ReadinessStatus
from app.contracts.enums import CaptureChannel, ChannelHealth
from app.reproduction.barriers import ArmReadinessBarrier


def _activity_gated_profile():
    return SimpleNamespace(
        arm_barrier=SimpleNamespace(
            readiness_mode="ACTIVITY_GATED",
            min_pcap_packets=0,
            require_advancing=False,
        )
    )


def test_arm_barrier_accepts_v2_stage1_path_ready_without_idle_pcap_header():
    observed = {
        "PCAP": {
            "status": ChannelHealth.HEALTHY.value,
            "packet_count": 0,
            "advancing": True,
            "enabled": True,
            "pcap_header_valid": False,
            "capture_path_ready": True,
        }
    }
    assert ArmReadinessBarrier._channel_ready(
        CaptureChannel.PCAP, observed, _activity_gated_profile()
    ) == (True, None)


def test_arm_barrier_does_not_treat_missing_header_as_ready_without_stage1_proof():
    observed = {
        "PCAP": {
            "status": ChannelHealth.HEALTHY.value,
            "packet_count": 0,
            "advancing": True,
            "enabled": True,
            "pcap_header_valid": False,
            "capture_path_ready": False,
        }
    }
    assert ArmReadinessBarrier._channel_ready(
        CaptureChannel.PCAP, observed, _activity_gated_profile()
    ) == (False, "PCAP_HEADER_INVALID")


@pytest.mark.asyncio
async def test_production_stage1_uses_real_path_controls_and_persists_ready(monkeypatch, tmp_path):
    producer = SimpleNamespace(pid=123, process_starttime=456)

    class EffectiveProfile:
        resolved = {
            "platform_resource": {"spool_max_unacked_bytes": 1024 * 1024},
            "spool": {"max_oldest_unacked_seconds": 60},
        }

        def model_dump(self):
            return {"resolved": self.resolved}

    class Lease:
        def validate(self, token):
            assert token.lease_epoch == 7

    class Producer:
        async def inspect_owned(self):
            return [producer]

    class Reader:
        async def run(self, command):
            assert "/tmp/aivoip_capture/epochs/EPOCH-1/active" in command
            return "1\n"

    class Downloader:
        async def get(self, *, remote_path, local_path, timeout=None):
            assert remote_path == "/etc/openwrt_release"
            local_path.write_bytes(b"DISTRIB_ID=OpenWrt\n")

    class Pressure:
        def evaluate(self, **kwargs):
            assert kwargs["capture_session_id"] == "CAP-1"
            return SimpleNamespace(
                state="NORMAL",
                unacked_bytes=0,
                oldest_unacked_seconds=0.0,
                reasons=(),
            )

    captured = {}

    class FakeDSession:
        def __init__(self, **kwargs):
            captured["capture_session_id"] = kwargs["capture_session_id"]

        def evaluate_stage1(self, checks):
            captured["checks"] = checks.as_dict()
            return SimpleNamespace(status=ReadinessStatus.READY, reasons=())

    monkeypatch.setattr(production_readiness, "CaptureV2DSession", FakeDSession)

    store = SimpleNamespace(root=tmp_path / "objects")
    session = SimpleNamespace(
        bootstrap=SimpleNamespace(
            capture_session_id="CAP-1",
            ownership=SimpleNamespace(
                producer=producer,
                capture_epoch_token="EPOCH-1",
            ),
            voice_context=SimpleNamespace(
                gateway_ip="192.168.0.253",
                voice_vlan_id="400",
                interface="br-lan_400",
            ),
            effective_profile=EffectiveProfile(),
        ),
        token=SimpleNamespace(lease_epoch=7),
        control_authority="ACTIVE",
        components={
            "lease": Lease(),
            "producer": Producer(),
            "reader": Reader(),
            "store": store,
            "downloader": Downloader(),
            "pressure": Pressure(),
        },
    )
    arm_snapshot = {
        "DEBUG": {
            "status": "HEALTHY",
            "enabled": True,
            "heartbeat": True,
        },
        "PCM_RX": {"configured": True, "enabled": True},
        "PCM_TX": {"configured": True, "enabled": True},
    }

    result = await production_readiness.evaluate_production_stage1(
        c_session=session,
        arm_snapshot=arm_snapshot,
        session_factory=object(),
    )

    assert result["ready"] is True
    assert result["active_dir_exists"] is True
    assert result["producer_count"] == 1
    assert result["exact_producer_count"] == 1
    assert result["transfer_probe"]["ok"] is True
    assert result["store_probe"]["ok"] is True
    assert captured["capture_session_id"] == "CAP-1"
    assert all(captured["checks"].values())
