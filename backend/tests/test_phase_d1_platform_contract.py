from __future__ import annotations

from pathlib import Path

import sys
import types
import pytest
import yaml

# The execution image used for source-level contract validation does not ship the optional
# asyncssh runtime dependency.  Install a minimal import stub; the PTY unit test below injects
# a fake connection and never calls asyncssh.connect().
if 'asyncssh' not in sys.modules:
    _permission_denied = type('PermissionDenied', (Exception,), {})
    sys.modules['asyncssh'] = types.SimpleNamespace(PermissionDenied=_permission_denied, connect=None)

from app.actions.registry import ActionRegistry, RegistryError
from app.collectors.asyncssh_adapter import AsyncSSHDeviceAdapter
from app.platforms.contracts import PlatformProfileStatus
from app.platforms.registry import PlatformProfileRegistry
from app.platforms.resolvers import PlatformResolverError, resolve_platform_value


ROOT = Path(__file__).resolve().parents[2]


def test_ruijie_partial_platform_contract_is_source_backed_and_blocked_for_autonomous_reproduction():
    actions = ActionRegistry(ROOT / 'profiles')
    platforms = PlatformProfileRegistry(ROOT / 'profiles')
    loaded = platforms.get('RUIJIE_VOIP_AIM_V1')
    p = loaded.definition

    assert p.status == PlatformProfileStatus.PARTIAL
    assert len(loaded.checksum) == 64
    assert p.autonomous_reproduction_actions == []
    assert not p.production_ready_for('AUTONOMOUS_REPRODUCTION')
    # Live-device validation 2026-08-13 closed all previously blocking gaps:
    # PCM active-call RX/TX sequence, debug cleanup idempotency, and FXS submode
    # prompt were all confirmed. The profile stays PARTIAL only because the real
    # adapter binding into a live reproduction session is not yet promoted.
    gap_keys = {g.key for g in p.blocking_gaps_for('AUTONOMOUS_REPRODUCTION')}
    assert gap_keys == set()

    for action_id in p.readonly_actions:
        action = actions.action(action_id)
        assert action.risk_level == 'L0'
        assert action.contract_status == 'VERIFIED'
        assert action.source_refs
        assert 'RUIJIE_VOIP_AIM_V1' in action.supported_platforms


def test_confirmed_reversible_syntax_is_not_activatable_without_retry_safe_cleanup():
    p = PlatformProfileRegistry(ROOT / 'profiles').get('RUIJIE_VOIP_AIM_V1').definition
    templates = {x.template_id: x for x in p.known_diagnostic_templates}
    assert templates['PCM_RX_ON_KNOWN_SYNTAX'].command_template == 'voip dsp diag set {voice_gateway_ip} 40000 1 pcm_rx on'
    assert templates['PCM_TX_ON_KNOWN_SYNTAX'].command_template == 'voip dsp diag set {voice_gateway_ip} 50000 1 pcm_tx on'
    assert templates['PCM_RX_ON_KNOWN_SYNTAX'].status == 'CONFIRMED_REVERSIBLE'
    assert templates['PCM_TX_ON_KNOWN_SYNTAX'].status == 'CONFIRMED_REVERSIBLE'
    assert templates['DEBUG_SYSTEM_ON_KNOWN_SYNTAX'].command_template == 'debug sys debug'
    assert templates['DEBUG_EVENT_ON_KNOWN_SYNTAX'].command_template == 'de p on'
    assert templates['CM_DEBUG_ON_KNOWN_SYNTAX'].command_template == 'de cm de'
    assert templates['SYSTEM_EVENT_DEBUG_ON_KNOWN_SYNTAX'].command_template == 'de sys de'
    assert templates['SIP_PACKET_LOG_ON_KNOWN_SYNTAX'].command_template == 'voip sip log-pkt on'
    assert templates['PCM_RX_ON_KNOWN_SYNTAX'].cleanup_command_template == 'voip dsp diag set {voice_gateway_ip} 40000 1 pcm_rx off'
    assert templates['PCM_RX_ON_KNOWN_SYNTAX'].cleanup_status == 'CONFIRMED_NON_IDEMPOTENT'
    assert templates['PCM_RX_ON_KNOWN_SYNTAX'].cleanup_idempotent is False
    assert templates['PCM_RX_ON_KNOWN_SYNTAX'].cleanup_retry_strategy == 'VERIFY_QUIET_THEN_EXECUTE_ONCE'
    assert 'UDP 40000' in templates['PCM_RX_ON_KNOWN_SYNTAX'].cleanup_guard
    assert templates['PCM_TX_ON_KNOWN_SYNTAX'].cleanup_command_template == 'voip dsp diag set {voice_gateway_ip} 50000 1 pcm_tx off'
    assert templates['PCM_TX_ON_KNOWN_SYNTAX'].cleanup_status == 'CONFIRMED_NON_IDEMPOTENT'
    assert templates['PCM_TX_ON_KNOWN_SYNTAX'].cleanup_idempotent is False
    assert templates['PCM_TX_ON_KNOWN_SYNTAX'].cleanup_retry_strategy == 'VERIFY_QUIET_THEN_EXECUTE_ONCE'
    assert 'UDP 50000' in templates['PCM_TX_ON_KNOWN_SYNTAX'].cleanup_guard
    assert templates['HOOK_DEBUG_ON_KNOWN_SYNTAX'].cleanup_command_template == 'debug p off'
    assert templates['HOOK_DEBUG_ON_KNOWN_SYNTAX'].cleanup_idempotent is True
    assert templates['DEBUG_EVENT_ON_KNOWN_SYNTAX'].cleanup_command_template == 'de p off'
    # Live-device validation 2026-08-13: two consecutive `de p off` both harmless.
    assert templates['DEBUG_EVENT_ON_KNOWN_SYNTAX'].cleanup_status == 'CONFIRMED_IDEMPOTENT'
    assert templates['DEBUG_EVENT_ON_KNOWN_SYNTAX'].cleanup_idempotent is True
    assert templates['SIP_PACKET_LOG_ON_KNOWN_SYNTAX'].cleanup_command_template == 'voip sip log-pkt off'
    # Live-device validation 2026-08-13: two consecutive calls both returned set OK.
    assert templates['SIP_PACKET_LOG_ON_KNOWN_SYNTAX'].cleanup_status == 'CONFIRMED_IDEMPOTENT'
    assert templates['SIP_PACKET_LOG_ON_KNOWN_SYNTAX'].cleanup_idempotent is True
    assert templates['DEBUG_SYSTEM_ON_KNOWN_SYNTAX'].status == 'CONFIRMED_NO_DEDICATED_CLEANUP'
    assert templates['SIP_DEBUG_ON_KNOWN_SYNTAX'].status == 'CONFIRMED_NO_DEDICATED_CLEANUP'
    # FXS submode prompt contract confirmed on the live device.
    assert templates['FXS_SUBMODE_SNAPSHOT'].command_template == 'voip fxs 1'
    assert templates['FXS_SUBMODE_SNAPSHOT'].status == 'CONFIRMED_SUBMODE_PROMPT'
    assert templates['FXS_SUBMODE_SNAPSHOT'].submode_prompt == 'AIM(fxs/1)> '
    assert templates['FXS_SUBMODE_SNAPSHOT'].snapshot_command == 'show information'
    assert 'Hook State' in templates['FXS_SUBMODE_SNAPSHOT'].snapshot_fields
    assert 'START_PCM_RX' not in p.autonomous_reproduction_actions
    assert 'START_PCM_TX' not in p.autonomous_reproduction_actions


def test_dev_config_runtime_context_resolvers():
    gateway = resolve_platform_value('DEV_CONFIG_VOIP_SERVICE_GATEWAY_V1', '''{
      "data": [{"hdl": 0, "svrName": "192.168.3.200", "svrPort": 5060}],
      "version": "1.0.0"
    }''')
    vlan_id = resolve_platform_value('DEV_CONFIG_VOICE_VLAN_ID_V1', '''{
      "enable": "1", "vlanid": 400, "version": "1.0.0"
    }''')
    assert gateway == '192.168.3.200'
    assert vlan_id == '400'


def test_dynamic_voice_interface_resolver_requires_vlan_specific_ready_link():
    output = '''
22: br-lan_400: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 qdisc noqueue
23: br-lan_500: <BROADCAST,MULTICAST,UP> mtu 1500 qdisc noqueue
'''
    assert resolve_platform_value(
        'IP_LINK_VOICE_INTERFACE_V1', output, voice_vlan_id='400'
    ) == 'br-lan_400'
    with pytest.raises(PlatformResolverError, match='INTERFACE_NOT_READY'):
        resolve_platform_value('IP_LINK_VOICE_INTERFACE_V1', output, voice_vlan_id='500')


def test_aim_fxs_event_resolver_extracts_timestamp_line_hook_and_dtmf():
    output = '''
2026-08-03 17:03:25.539000 [0] D:: [D]OFFHOOK
2026-08-03 17:03:26.735000 [0] D:: [D]DTMF<8>
2026-08-03 17:03:43.635000 [0] D:: [D]ONHOOK
'''
    assert resolve_platform_value('AIM_FXS_EVENT_V1', output) == [
        {'timestamp': '2026-08-03 17:03:25.539000', 'line': 0, 'event': 'OFFHOOK', 'digit': None},
        {'timestamp': '2026-08-03 17:03:26.735000', 'line': 0, 'event': 'DTMF', 'digit': '8'},
        {'timestamp': '2026-08-03 17:03:43.635000', 'line': 0, 'event': 'ONHOOK', 'digit': None},
    ]


@pytest.mark.parametrize('payload', [
    '{"data": []}',
    '{"data": [{"svrName": "192.168.3.200"}, {"svrName": "192.168.3.201"}]}',
    '{"data": [{"svrName": "not-an-ip"}]}',
])
def test_gateway_resolver_rejects_ambiguous_or_invalid_values(payload: str):
    with pytest.raises(PlatformResolverError):
        resolve_platform_value('DEV_CONFIG_VOIP_SERVICE_GATEWAY_V1', payload)


@pytest.mark.parametrize('payload', [
    '{"enable": "0", "vlanid": 400}',
    '{"enable": "1", "vlanid": 0}',
    '{"enable": "1", "vlanid": 4095}',
    '{"enable": "1", "vlanid": "bad"}',
])
def test_vlan_resolver_rejects_disabled_or_invalid_values(payload: str):
    with pytest.raises(PlatformResolverError):
        resolve_platform_value('DEV_CONFIG_VOICE_VLAN_ID_V1', payload)


def test_action_registry_rejects_duplicate_action_ids(tmp_path: Path):
    (tmp_path / 'actions').mkdir()
    (tmp_path / 'collect').mkdir()
    doc = {'actions': [{'id': 'A', 'risk_level': 'L0', 'executor': 'shell', 'command': 'true', 'evidence_type': 'X'}]}
    (tmp_path / 'actions' / 'a.yaml').write_text(yaml.safe_dump(doc), encoding='utf-8')
    (tmp_path / 'actions' / 'b.yaml').write_text(yaml.safe_dump(doc), encoding='utf-8')
    with pytest.raises(RegistryError, match='DUPLICATE_ACTION:A'):
        ActionRegistry(tmp_path)


class _FakeStdout:
    def __init__(self, initial: str):
        self.chunks = [initial]

    async def read(self, _n: int):
        if self.chunks:
            return self.chunks.pop(0)
        return ''


class _FakeStdin:
    def __init__(self, stdout: _FakeStdout):
        self.stdout = stdout
        self.writes: list[str] = []

    def write(self, value: str):
        self.writes.append(value)
        cmd = value.strip()
        if cmd and cmd != 'exit':
            self.stdout.chunks.append(f'{cmd}\r\nRESULT:{cmd}\r\nAIM>')

    def write_eof(self):
        pass


class _FakeProcess:
    def __init__(self):
        self.stdout = _FakeStdout('AIM banner\r\nAIM>')
        self.stdin = _FakeStdin(self.stdout)
        self.closed = False

    async def wait_closed(self):
        self.closed = True

    def close(self):
        self.closed = True


class _FakeConn:
    def __init__(self):
        self.process_count = 0
        self.process: _FakeProcess | None = None

    async def create_process(self, executable: str, term_type: str):
        assert executable == 'aim'
        assert term_type == 'xterm'
        self.process_count += 1
        self.process = _FakeProcess()
        return self.process

    def close(self):
        pass

    async def wait_closed(self):
        pass


@pytest.mark.asyncio
async def test_aim_pty_is_persistent_across_root_commands():
    adapter = AsyncSSHDeviceAdapter(ip='192.0.2.1', port=22, username='admin', password='secret')
    conn = _FakeConn()
    adapter.conn = conn

    first = await adapter.execute_cli('sys show bind-if')
    second = await adapter.execute_cli('voip sip regc show running RC1')

    assert conn.process_count == 1
    assert 'RESULT:sys show bind-if' in first.stdout
    assert 'RESULT:voip sip regc show running RC1' in second.stdout
    await adapter.disconnect()
