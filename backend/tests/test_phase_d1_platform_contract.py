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
    gap_keys = {g.key for g in p.blocking_gaps_for('AUTONOMOUS_REPRODUCTION')}
    assert {
        'VOICE_VLAN_PARSER',
        'VOICE_GATEWAY_RESOLVER',
        'VOICE_INTERFACE_VERIFICATION',
        'PCM_CLEANUP_COMMANDS',
        'DEBUG_CLEANUP_COMMANDS',
        'REALTIME_HOOK_EVENT_SOURCE',
    } <= gap_keys

    for action_id in p.readonly_actions:
        action = actions.action(action_id)
        assert action.risk_level == 'L0'
        assert action.contract_status == 'VERIFIED'
        assert action.source_refs
        assert 'RUIJIE_VOIP_AIM_V1' in action.supported_platforms


def test_known_pcm_start_syntax_is_documented_but_not_activatable():
    p = PlatformProfileRegistry(ROOT / 'profiles').get('RUIJIE_VOIP_AIM_V1').definition
    templates = {x.template_id: x for x in p.known_diagnostic_templates}
    assert templates['PCM_RX_ON_KNOWN_SYNTAX'].command_template == 'voip dsp diag set {voice_gateway_ip} 40000 1 pcm_rx on'
    assert templates['PCM_TX_ON_KNOWN_SYNTAX'].command_template == 'voip dsp diag set {voice_gateway_ip} 50000 1 pcm_tx on'
    assert all(x.status == 'DOCUMENTED_ONLY' for x in [templates['PCM_RX_ON_KNOWN_SYNTAX'], templates['PCM_TX_ON_KNOWN_SYNTAX']])
    assert 'START_PCM_RX' not in p.autonomous_reproduction_actions
    assert 'START_PCM_TX' not in p.autonomous_reproduction_actions


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
