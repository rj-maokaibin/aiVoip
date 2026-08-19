from __future__ import annotations

from app.core.config import settings
from app.diagnosis.gateway import compact_context, redact_gateway_value


def test_ai2_gateway_context_drops_device_credentials_and_unapproved_metadata(monkeypatch):
    monkeypatch.setattr(settings, "reasoning_gateway_include_device_identifiers", True)
    snapshot = {
        "case": {"summary": "设备 192.168.1.10 出现周期性电流音", "status": "ANALYZING"},
        "devices": [{
            "id": "device-secret",
            "ip": "192.168.1.10",
            "ssh_port": 22,
            "sn": "SN123456",
            "platform_id": "openwrt",
            "username": "root",
            "password": "super-secret-password",
            "device_info": {
                "password": "device-info-password",
                "token": "device-info-token",
                "private_key": "private-key-material",
                "software_version": "1.0.0",
            },
        }],
        "evidences": [{
            "id": "evidence-1",
            "type": "PACKET_CAPTURE",
            "source": "DUT",
            "filename": "capture.pcap",
            "sha256": "a" * 64,
            "metadata": {
                "capture_point": "voice_vlan",
                "packet_count": 100,
                "password": "metadata-password",
                "token": "metadata-token",
                "object_key": "private/path/to/object",
            },
        }],
        "analyzers": {
            "packet": {
                "run_id": "run-1",
                "status": "SUCCESS",
                "version": "1",
                "summary": {
                    "password": "summary-password",
                    "peer": "10.0.0.2",
                },
                "result": {
                    "packet": {
                        "summary": {"authorization": "Bearer secret", "peer": "10.0.0.3"},
                        "anomalies": [{"detail": "password=embedded-secret from 10.0.0.4"}],
                        "calls": [{"call_id": "c1", "caller": "13800138000", "callee": "10086", "state": "ACTIVE"}],
                    }
                },
            }
        },
        "similar_cases": [],
        "knowledge": [],
    }

    context = compact_context(snapshot)
    rendered = str(context)

    assert "super-secret-password" not in rendered
    assert "device-info-password" not in rendered
    assert "device-info-token" not in rendered
    assert "private-key-material" not in rendered
    assert "metadata-password" not in rendered
    assert "metadata-token" not in rendered
    assert "private/path/to/object" not in rendered
    assert "summary-password" not in rendered
    assert "Bearer secret" not in rendered
    assert "embedded-secret" not in rendered
    assert "192.168.1.10" not in rendered
    assert "10.0.0.2" not in rendered
    assert "10.0.0.3" not in rendered
    assert "10.0.0.4" not in rendered
    assert "13800138000" not in rendered
    assert "10086" not in rendered
    assert "[REDACTED_IP]" in rendered
    assert "[REDACTED_SECRET_FIELD]" in rendered or "[REDACTED_SECRET_LINE]" in rendered
    assert context["evidences"][0]["metadata"] == {"capture_point": "voice_vlan", "packet_count": 100}
    assert "device_info" not in context["devices"][0]
    assert "username" not in context["devices"][0]
    assert "password" not in context["devices"][0]


def test_ai2_gateway_baseline_recursive_redaction_removes_nested_secrets():
    baseline = {
        "known": ["peer=192.168.10.1", "password=do-not-send"],
        "decision": {
            "token": "do-not-send-token",
            "nested": {"authorization": "Bearer do-not-send", "phone": "13800138000"},
        },
    }
    redacted = redact_gateway_value(baseline)
    rendered = str(redacted)
    assert "do-not-send" not in rendered
    assert "192.168.10.1" not in rendered
    assert "13800138000" not in rendered
    assert "[REDACTED_IP]" in rendered
    assert "[REDACTED_SECRET_FIELD]" in rendered or "[REDACTED_SECRET_LINE]" in rendered
