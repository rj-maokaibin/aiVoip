from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from app.collectors.device_adapter import CommandResult
from app.reproduction.real_platform import RealReproductionPlatform


@dataclass
class FakeAdapter:
    """In-memory AsyncSSHDeviceAdapter stand-in recording every command."""

    shell_responses: dict = field(default_factory=dict)
    cli_responses: dict = field(default_factory=dict)
    shell_calls: list = field(default_factory=list)
    cli_calls: list = field(default_factory=list)
    connected: bool = False

    async def connect(self):
        self.connected = True

    async def disconnect(self):
        self.connected = False

    async def execute_shell(self, command: str, timeout: float | None = None) -> CommandResult:
        self.shell_calls.append(command)
        for prefix, out in self.shell_responses.items():
            if command.startswith(prefix):
                return CommandResult(stdout=out)
        return CommandResult(stdout='')

    async def execute_cli(self, command: str, timeout: float | None = None) -> CommandResult:
        self.cli_calls.append(command)
        for prefix, out in self.cli_responses.items():
            if command.startswith(prefix):
                return CommandResult(stdout=out)
        return CommandResult(stdout='AIM>')

    def _teardown(self):
        # Stop the background bridge loop thread.
        if hasattr(self, '_platform'):
            pass


_DEV_CONFIG_SVC = (
    '{"code":0,"data":[{"svrName":"192.168.3.200","svrPort":5060}]}'
)
_DEV_CONFIG_VLAN = (
    '{"enable":1,"vlanid":400}'
)
_IP_LINK = (
    '1: lo: <LOOPBACK,UP,LOWER_UP> mtu 65536 ...\n'
    '2: eth0: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 ...\n'
    '24: br-lan_400: <BROADCAST,MULTICAST,UP,LOWER_UP> mtu 1500 ...\n'
)


@pytest.fixture
def fake():
    adapter = FakeAdapter(
        shell_responses={
            'dev_config get -m voipServInfo': _DEV_CONFIG_SVC,
            'dev_config get -m voice_vlan': _DEV_CONFIG_VLAN,
            'ip -o link show': _IP_LINK,
            'timeout -t 3 tcpdump -ni br-lan_400 -c 1': (
                'tcpdump: listening on br-lan_400, link-type EN10MB (Ethernet), capture size 262144 bytes\n'
                '0 packets captured\n'
            ),
            'timeout -t 5 tcpdump -ni br-lan_400 -c 1': (
                'tcpdump: listening on br-lan_400, link-type EN10MB (Ethernet), capture size 262144 bytes\n'
                '0 packets captured\n'
            ),
            'rm -f /tmp/aiVoip_pretrigger': (
                'aGVsbG8gd29ybGQ='  # base64("hello world")
            ),
        },
        cli_responses={
            'voip dsp diag set 192.168.3.200 40000 1 pcm_rx on': 'AIM>',
            'voip dsp diag set 192.168.3.200 50000 1 pcm_tx on': 'AIM>',
        },
    )
    yield adapter


class _Device:
    def __init__(self):
        self.device_info = {}


def test_platform_identity():
    assert RealReproductionPlatform.platform_id == 'ruijie-voip-aim-real'
    assert RealReproductionPlatform.version == '0.6.0'


def test_resolve_voice_context_parses_real_output(fake):
    p = RealReproductionPlatform(adapter=fake)
    ctx = p.resolve_voice_context(_Device())
    assert ctx.voice_vlan_id == '400'
    assert ctx.voice_interface == 'br-lan_400'
    assert ctx.voice_gateway_ip == '192.168.3.200'
    assert ctx.interface_up is True
    assert ctx.resolver_id == 'REAL_VOICE_CONTEXT_V1'
    # The three context commands must have been issued over shell.
    assert any(c.startswith('dev_config get -m voipServInfo') for c in fake.shell_calls)
    assert any(c.startswith('dev_config get -m voice_vlan') for c in fake.shell_calls)
    assert any(c.startswith('ip -o link show') for c in fake.shell_calls)


def test_arm_runs_real_commands_and_returns_snapshot(fake):
    p = RealReproductionPlatform(adapter=fake)
    snap = p.arm(session_id='s1', device=_Device(), actions=['START_PCM_RX', 'START_PCM_TX', 'ENABLE_BASIC_VOIP_DEBUG', 'START_VOICE_PCAP'])
    # PCM ON commands were sent over the AIM PTY.
    assert any('pcm_rx on' in c for c in fake.cli_calls)
    assert any('pcm_tx on' in c for c in fake.cli_calls)
    # Full debug enable sequence was sent.
    assert any('voip sip log-pkt on' in c for c in fake.cli_calls)
    assert any('debug p on' in c for c in fake.cli_calls)
    # PCAP readiness validated by a listening probe.
    assert snap['PCM_RX']['status'] == 'HEALTHY'
    assert snap['PCM_RX']['enabled'] is True
    assert snap['PCAP']['pcap_header_valid'] is True
    assert snap['PCAP']['status'] == 'HEALTHY'
    assert snap['DEBUG']['enabled'] is True
    assert snap['LOG']['status'] == 'HEALTHY'


def test_arm_pcap_failed_when_not_listening(fake):
    fake.shell_responses['timeout -t 3 tcpdump -ni br-lan_400 -c 1'] = 'tcpdump: no suitable device found'
    p = RealReproductionPlatform(adapter=fake)
    snap = p.arm(session_id='s1', device=_Device(), actions=['START_VOICE_PCAP'])
    assert snap['PCAP']['status'] == 'FAILED'
    assert snap['PCAP']['pcap_header_valid'] is False
    assert snap['PCAP']['advancing'] is False


def test_cleanup_quiet_channel_skips_off(fake):
    # All probes report 0 packets -> guard treats channels as quiet -> no OFF command.
    p = RealReproductionPlatform(adapter=fake)
    snap = p.cleanup(session_id='s1', device=_Device(), actions=['STOP_PCM_RX', 'STOP_PCM_TX'])
    assert snap['PCM_RX']['status'] == 'STOPPED'
    assert snap['PCM_RX']['quiet_verified'] is True
    assert snap['PCM_RX']['off_executed'] is False
    assert snap['PCM_TX']['status'] == 'STOPPED'
    # No pcm_* off command was issued.
    assert not any(' off' in c for c in fake.cli_calls)


def test_cleanup_debug_off_sequence(fake):
    p = RealReproductionPlatform(adapter=fake)
    snap = p.cleanup(session_id='s1', device=_Device(), actions=['DISABLE_BASIC_VOIP_DEBUG', 'DISABLE_DSP_DEBUG'])
    assert snap['DEBUG']['status'] == 'STOPPED'
    assert snap['DEBUG']['off_verified'] is True
    assert any('voip sip log-pkt off' in c for c in fake.cli_calls)


def test_build_pretrigger_capture_returns_pcap_bytes(fake):
    p = RealReproductionPlatform(adapter=fake)
    ctx = p.resolve_voice_context(_Device())
    cap = p.build_pretrigger_capture(context=ctx, start_ms=1000, end_ms=31000)
    assert cap.pcap == b'hello world'


def test_live_probe_and_call_capture_are_empty_passthrough(fake):
    p = RealReproductionPlatform(adapter=fake)
    ctx = p.resolve_voice_context(_Device())
    assert p.build_live_probe(context=ctx, start_ms=100, call_id='c1').pcap == b''
    assert p.build_call_capture(context=ctx, start_ms=100, end_ms=500, call_id='c1', profile_id='P', signal=None).pcap == b''
