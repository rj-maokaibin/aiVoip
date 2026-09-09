from __future__ import annotations

import io
import json
from types import SimpleNamespace

import pytest

from app.automation.adapters.pbx.esl import (
    FreeSwitchEventSocketClient,
    FreeSwitchEslError,
    load_event_socket_password,
    map_runtime_event,
)
from app.automation.adapters.pbx.profile import FusionPbxLabProfile


def _profile(config_path: str = "/tmp/event_socket.conf.xml") -> FusionPbxLabProfile:
    return FusionPbxLabProfile.from_dict({
        "schema_version": "pbx-lab-profile-v1",
        "node_key": "test-pbx",
        "host": "127.0.0.1",
        "fusionpbx_root": "/tmp/fusionpbx",
        "internal_port": 5060,
        "event_socket": {"host": "127.0.0.1", "port": 8021, "config": config_path},
        "extension_pool": {"start": 7900, "end": 7999},
        "protected_extensions": ["7102"],
        "source_fence": {"version": "test-v1", "files": {"x": "0" * 64}},
    })


def _frame(headers: dict[str, str], body: str = "") -> bytes:
    local = dict(headers)
    if body:
        local["Content-Length"] = str(len(body.encode("utf-8")))
    head = "".join(f"{k}: {v}\n" for k, v in local.items()) + "\n"
    return head.encode("utf-8") + body.encode("utf-8")


class FakeSocket:
    def __init__(self, incoming: bytes):
        self.incoming = io.BytesIO(incoming)
        self.sent = bytearray()
        self.closed = False

    def settimeout(self, _timeout):
        pass

    def makefile(self, _mode):
        return self.incoming

    def sendall(self, data: bytes):
        self.sent.extend(data)

    def close(self):
        self.closed = True


def test_password_loader_reads_runtime_config_without_persisting_secret(tmp_path) -> None:
    secret = "runtime-only-secret"
    path = tmp_path / "event_socket.conf.xml"
    path.write_text(
        f'<configuration><settings><param name="password" value="{secret}"/></settings></configuration>',
        encoding="utf-8",
    )
    assert load_event_socket_password(_profile(str(path))) == secret


def test_runtime_event_mapper_redacts_unrelated_sensitive_headers() -> None:
    secret = "must-not-leak"
    event = map_runtime_event({
        "Event-Name": "CUSTOM",
        "Event-Subclass": "sofia::register",
        "sip-auth-username": "7900",
        "realm": "pbx.test",
        "password": secret,
        "variable_sip_auth_password": secret,
    })
    assert event is not None
    assert event.kind == "REGISTERED"
    assert event.identity == "7900"
    assert event.domain == "pbx.test"
    safe = event.safe_dict()
    assert secret not in repr(safe)
    assert safe["secret_values_emitted"] is False


def test_event_socket_client_auth_api_subscribe_and_event(monkeypatch) -> None:
    payload = json.dumps({
        "Event-Name": "DTMF",
        "Unique-ID": "uuid-1",
        "DTMF-Digit": "5",
        "variable_sip_auth_password": "hidden",
    })
    incoming = b"".join([
        _frame({"Content-Type": "auth/request"}),
        _frame({"Content-Type": "command/reply", "Reply-Text": "+OK accepted"}),
        _frame({"Content-Type": "api/response"}, "FreeSWITCH Version test\n"),
        _frame({"Content-Type": "command/reply", "Reply-Text": "+OK event listener enabled json"}),
        _frame({"Content-Type": "text/event-json"}, payload),
    ])
    fake = FakeSocket(incoming)
    monkeypatch.setattr("socket.create_connection", lambda *_a, **_k: fake)
    client = FreeSwitchEventSocketClient(
        _profile(), password_loader=lambda _profile: "runtime-secret"
    )
    health = client.safe_health()
    assert health["connected"] is True
    assert health["subscribed"] is True
    assert "runtime-secret" not in repr(health)
    event = client.read_event()
    assert event is not None
    assert event.kind == "DTMF"
    assert event.uuid == "uuid-1"
    assert event.dtmf_digit == "5"
    assert "hidden" not in repr(event.safe_dict())


def test_custom_probe_event_is_strict_and_safe(monkeypatch) -> None:
    incoming = b"".join([
        _frame({"Content-Type": "auth/request"}),
        _frame({"Content-Type": "command/reply", "Reply-Text": "+OK accepted"}),
        _frame({"Content-Type": "command/reply", "Reply-Text": "+OK"}),
    ])
    fake = FakeSocket(incoming)
    monkeypatch.setattr("socket.create_connection", lambda *_a, **_k: fake)
    client = FreeSwitchEventSocketClient(
        _profile(), password_loader=lambda _profile: "runtime-secret"
    )
    client.connect()
    client.send_custom_probe_event("probe-123")
    sent = fake.sent.decode("utf-8")
    assert "Event-Subclass: aivoip::probe" in sent
    assert "Probe-ID: probe-123" in sent
    with pytest.raises(FreeSwitchEslError, match="FREESWITCH_ESL_PROBE_ID_INVALID"):
        client.send_custom_probe_event("bad probe\n")


def test_probe_event_maps_without_raw_headers() -> None:
    event = map_runtime_event({
        "Event-Name": "CUSTOM",
        "Event-Subclass": "aivoip::probe",
        "Probe-ID": "probe-xyz",
        "password": "must-not-leak",
    })
    assert event is not None
    assert event.kind == "PROBE"
    assert event.probe_id == "probe-xyz"
    assert "must-not-leak" not in repr(event.safe_dict())
