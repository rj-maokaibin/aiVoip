from __future__ import annotations

from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.automation.adapters.pbx.call_session import PbxCallSessionManager, PbxCallSessionError
from app.automation.adapters.pbx.esl import FreeSwitchRuntimeEvent
from app.automation.adapters.pbx.profile import FusionPbxLabProfile
from app.automation.adapters.pbx.resource_authority import (
    PbxExtensionLeaseManager, PbxResourceState, PbxResourceType,
)
from app.automation.adapters.pbx.runtime_control import (
    FreeSwitchRuntimeProvider, FreeSwitchRuntimeControlError,
)
from app.automation.pbx_models import PbxExtensionResource, PbxNode
from app.db.base import Base


def _session_factory():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:", connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)

def _profile() -> FusionPbxLabProfile:
    return FusionPbxLabProfile.from_dict({
        "schema_version": "pbx-lab-profile-v1",
        "node_key": "test-pbx",
        "host": "127.0.0.1",
        "fusionpbx_root": "/tmp/fusionpbx",
        "internal_port": 5060,
        "event_socket": {
            "host": "127.0.0.1", "port": 8021,
            "config": "/tmp/event_socket.conf.xml",
        },
        "extension_pool": {"start": 7900, "end": 7999},
        "protected_extensions": ["7102"],
        "source_fence": {"version": "test-v1", "files": {"x": "0" * 64}},
    })


def _leased_endpoint():
    Session = _session_factory()
    with Session() as session:
        node = PbxNode(
            node_key="test-pbx", provider="fusionpbx", runtime_provider="freeswitch",
            host="127.0.0.1", fusionpbx_root="/tmp/fusionpbx",
            internal_profile="internal", internal_port=5060, status="PBX_READY",
            source_fence_version="test-v1", provider_metadata_json={"domains": {"d1": "pbx.test"}},
        )
        session.add(node); session.flush()
        resource = PbxExtensionResource(
            pbx_node_id=node.id, domain_id="d1", extension="7900.a",
            resource_type=PbxResourceType.TEMPORARY_AUTOMATION.value,
            state=PbxResourceState.FREE.value,
        )
        session.add(resource); session.commit(); node_id = node.id
    authority = PbxExtensionLeaseManager(Session, ttl_seconds=60)
    token = authority.acquire_extension(
        pbx_node_id=node_id, extension="7900.a", run_id="run-call", owner_worker_id="worker-1"
    )
    return Session, authority, token

def test_call_session_tracks_esl_state_rtp_dtmf_and_hangup() -> None:
    Session, authority, token = _leased_endpoint()
    manager = PbxCallSessionManager(Session, authority=authority)
    view = manager.create(run_id="run-call", callee=token)
    assert view.state == "CREATED"
    call_uuid = "11111111-2222-3333-4444-555555555555"
    view = manager.bind_uuid(view.id, call_uuid)
    assert view.state == "ORIGINATING"
    for kind, expected in [("RINGING", "RINGING"), ("ANSWERED", "ANSWERED")]:
        view = manager.apply_event(
            view.id, FreeSwitchRuntimeEvent(kind=kind, raw_event_name=kind, uuid=call_uuid)
        )
        assert view.state == expected
    view = manager.record_rtp(view.id, {"rtp_audio_in_packet_count": 8, "secret_values_emitted": False})
    assert view.state == "MEDIA"
    view = manager.apply_event(
        view.id,
        FreeSwitchRuntimeEvent(kind="DTMF", raw_event_name="DTMF", uuid=call_uuid, dtmf_digit="5"),
    )
    assert view.state == "MEDIA"
    view = manager.apply_event(
        view.id,
        FreeSwitchRuntimeEvent(
            kind="HANGUP", raw_event_name="CHANNEL_HANGUP_COMPLETE",
            uuid=call_uuid, hangup_cause="NORMAL_CLEARING",
        ),
    )
    assert view.state == "COMPLETED"
    assert view.hangup_cause == "NORMAL_CLEARING"
    assert manager.active_for_resource(token.resource_id) == ()

def test_call_session_fails_closed_on_stale_lease_and_uuid_mismatch() -> None:
    Session, authority, token = _leased_endpoint()
    manager = PbxCallSessionManager(Session, authority=authority)
    view = manager.create(run_id="run-call", callee=token)
    manager.bind_uuid(view.id, "11111111-2222-3333-4444-555555555555")
    with pytest.raises(PbxCallSessionError, match="PBX_CALL_EVENT_UUID_MISMATCH"):
        manager.apply_event(
            view.id,
            FreeSwitchRuntimeEvent(
                kind="ANSWERED", raw_event_name="CHANNEL_ANSWER",
                uuid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee",
            ),
        )
    authority.validate = lambda _token: False
    with pytest.raises(PbxCallSessionError, match="PBX_CALL_LEASE_FENCED"):
        manager.create(run_id="run-call-2", callee=token)


class FakeEslClient:
    def __init__(self):
        self.commands = []
    def connect(self):
        pass
    def close(self):
        pass
    def bgapi(self, command: str):
        self.commands.append(("bgapi", command))
        return "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    def api(self, command: str):
        self.commands.append(("api", command))
        if command.startswith("uuid_getvar"):
            if command.endswith("rtp_audio_in_packet_count"):
                return "10\n"
            if command.endswith("rtp_audio_out_packet_count"):
                return "12\n"
            return "2048\n"
        return "+OK\n"


def test_runtime_provider_uses_bounded_commands_only() -> None:
    client = FakeEslClient()
    runtime_authority = SimpleNamespace(
        validate=lambda _token: True,
        validate_dial_alias_binding=lambda _token, alias: alias == "7900",
    )
    token = SimpleNamespace(resource_id="r1")
    provider = FreeSwitchRuntimeProvider(_profile(), authority=runtime_authority, client=client)
    result = provider.originate_alias(token, dial_alias="7900", domain_name="pbx.test", tone_ms=1000)
    assert result.dial_alias == "7900"
    assert client.commands[0][0] == "bgapi"
    assert "user/7900@pbx.test" in client.commands[0][1]
    provider.inject_received_dtmf(token, result.call_uuid, "5#")
    stats = provider.rtp_stats(token, result.call_uuid)
    assert stats["rtp_audio_in_packet_count"] == 10
    provider.hangup(token, result.call_uuid)
    with pytest.raises(FreeSwitchRuntimeControlError, match="PBX_CALL_DIAL_ALIAS_INVALID"):
        provider.originate_alias(token, dial_alias="7900.a", domain_name="pbx.test")

def test_runtime_provider_refuses_all_mutation_when_lease_is_stale() -> None:
    client = FakeEslClient()
    authority = SimpleNamespace(
        validate=lambda _token: False,
        validate_dial_alias_binding=lambda _token, _alias: False,
    )
    token = SimpleNamespace(resource_id="r1")
    provider = FreeSwitchRuntimeProvider(_profile(), authority=authority, client=client)
    with pytest.raises(FreeSwitchRuntimeControlError, match="PBX_CALL_LEASE_FENCED"):
        provider.originate_alias(token, dial_alias="7900", domain_name="pbx.test")
    with pytest.raises(FreeSwitchRuntimeControlError, match="PBX_CALL_LEASE_FENCED"):
        provider.inject_received_dtmf(token, "11111111-2222-3333-4444-555555555555", "5")
    with pytest.raises(FreeSwitchRuntimeControlError, match="PBX_CALL_LEASE_FENCED"):
        provider.hangup(token, "11111111-2222-3333-4444-555555555555")
    assert client.commands == []


def test_call_timeout_is_terminal_and_not_active_for_cleanup() -> None:
    Session, authority, token = _leased_endpoint()
    manager = PbxCallSessionManager(Session, authority=authority)
    call = manager.create(run_id="run-call", callee=token)
    call = manager.fail(call.id, state="TIMEOUT", cause="INJECTED_CALL_TIMEOUT")
    assert call.state == "TIMEOUT"
    assert call.hangup_cause == "INJECTED_CALL_TIMEOUT"
    assert manager.active_for_resource(token.resource_id) == ()
