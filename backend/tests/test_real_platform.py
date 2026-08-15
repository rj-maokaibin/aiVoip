from __future__ import annotations

import time
from dataclasses import dataclass, field

import pytest

from app.collectors.device_adapter import CommandResult
from app.reproduction.real_platform import RealReproductionPlatform

# A minimal classic pcap (24-byte global header only, 0 packets) as bytes.
_DUMMY_PCAP = bytes.fromhex(
    'd4c3b2a1020004000000000000000000ffff000001000000'
)


@dataclass
class FakeAdapter:
    """In-memory AsyncSSHDeviceAdapter stand-in recording every command."""

    shell_responses: dict = field(default_factory=dict)
    cli_responses: dict = field(default_factory=dict)
    shell_calls: list = field(default_factory=list)
    cli_calls: list = field(default_factory=list)
    connected: bool = False
    aim_chunks: list = field(default_factory=list)

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

    async def read_aim_chunk(self, timeout: float = 1.0) -> str:
        if self.aim_chunks:
            return self.aim_chunks.pop(0)
        return ''

    async def write_aim(self, command: str) -> None:
        self.cli_calls.append(command)

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
            'rm -f /tmp/aiVoip_live_': __import__('base64').b64encode(_DUMMY_PCAP).decode(),
            'rm -f /tmp/aiVoip_call_': __import__('base64').b64encode(_DUMMY_PCAP).decode(),
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
    reverse = snap['reverse_validation']
    assert reverse['PCM_RX']['status'] == 'STOPPED'
    assert reverse['PCM_RX']['quiet_verified'] is True
    assert reverse['PCM_RX']['off_executed'] is False
    assert reverse['PCM_TX']['status'] == 'STOPPED'
    # No pcm_* off command was issued.
    assert not any(' off' in c for c in fake.cli_calls)


def test_cleanup_debug_off_sequence(fake):
    p = RealReproductionPlatform(adapter=fake)
    snap = p.cleanup(session_id='s1', device=_Device(), actions=['DISABLE_BASIC_VOIP_DEBUG', 'DISABLE_DSP_DEBUG'])
    reverse = snap['reverse_validation']
    assert reverse['DEBUG']['status'] == 'STOPPED'
    assert reverse['DEBUG']['off_verified'] is True
    assert any('voip sip log-pkt off' in c for c in fake.cli_calls)


def test_build_pretrigger_capture_returns_pcap_bytes(fake):
    p = RealReproductionPlatform(adapter=fake)
    ctx = p.resolve_voice_context(_Device())
    cap = p.build_pretrigger_capture(context=ctx, start_ms=1000, end_ms=31000)
    assert cap.pcap == b'hello world'


def test_live_probe_and_call_capture_run_tcpdump_and_return_pcap(fake):
    p = RealReproductionPlatform(adapter=fake)
    ctx = p.resolve_voice_context(_Device())
    # build_live_probe captures the PCM mirror ports for a 2s window.
    live = p.build_live_probe(context=ctx, start_ms=100, call_id='c1')
    assert live.pcap == _DUMMY_PCAP
    assert any('aiVoip_live_c1_' in c for c in fake.shell_calls)
    assert any('udp port 40000 or udp port 50000' in c for c in fake.shell_calls)
    assert any('rm -f /tmp/aiVoip_live_c1_' in c for c in fake.shell_calls)
    # build_call_capture captures the post-call tail window (capped at 8s).
    call = p.build_call_capture(context=ctx, start_ms=100, end_ms=500, call_id='c1', profile_id='P', signal=None)
    assert call.pcap == _DUMMY_PCAP
    assert any('aiVoip_call_c1.pcap' in c for c in fake.shell_calls)


def test_live_probe_accumulates_segments_merged_by_call_capture(fake):
    # Repeated in-call probes append short tcpdump windows; end_call's
    # build_call_capture merges them (stripping duplicate global headers) so
    # CALL_QUICK sees media spanning the whole conversation.
    seg1 = _DUMMY_PCAP + bytes.fromhex(
        '000000010000000100000001000000010000000000000001'  # 1 packet record
    )
    seg2 = _DUMMY_PCAP + bytes.fromhex(
        '000000020000000200000002000000020000000000000002'  # 1 packet record
    )
    fake.shell_responses['rm -f /tmp/aiVoip_live_'] = '____'  # default empty
    p = RealReproductionPlatform(adapter=fake)
    ctx = p.resolve_voice_context(_Device())

    # Two different probe results (each a full pcap with its own global header).
    def make_response(media):
        fake.shell_responses['rm -f /tmp/aiVoip_live_'] = __import__('base64').b64encode(media).decode()
        return media

    make_response(seg1)
    p.build_live_probe(context=ctx, start_ms=100, call_id='m1')
    make_response(seg2)
    p.build_live_probe(context=ctx, start_ms=4000, call_id='m1')

    call = p.build_call_capture(context=ctx, start_ms=100, end_ms=6000, call_id='m1', profile_id='P', signal=None)
    # Merged: first global header + both packet records (one header stripped).
    assert call.pcap[:24] == _DUMMY_PCAP
    assert call.pcap[24:48] == seg1[24:]
    assert call.pcap[48:] == seg2[24:]
    # No post-call tcpdump was issued for this call (cached merge path).
    assert not any('aiVoip_call_m1.pcap' in c for c in fake.shell_calls)


def test_call_capture_prefers_cached_real_media_from_live_probe(fake):
    # A real in-call pcap (>24 bytes: header + at least one frame) captured at
    # bind_call must be reused by end_call's build_call_capture, so CALL_QUICK
    # analyzes actual media instead of the empty post-hangup window.
    real_media = _DUMMY_PCAP + bytes.fromhex(
        '000000010000000100000001000000010000000000000001'  # 1 packet record
    )
    fake.shell_responses['rm -f /tmp/aiVoip_live_'] = __import__('base64').b64encode(real_media).decode()
    p = RealReproductionPlatform(adapter=fake)
    ctx = p.resolve_voice_context(_Device())
    p.build_live_probe(context=ctx, start_ms=100, call_id='c9')
    # end_call: must return the cached real media, NOT run a fresh tail capture.
    call = p.build_call_capture(context=ctx, start_ms=100, end_ms=500, call_id='c9', profile_id='P', signal=None)
    assert call.pcap == real_media
    # No post-call tcpdump was issued for this call (cached path).
    assert not any('aiVoip_call_c9.pcap' in c for c in fake.shell_calls)


def test_spawn_live_probe_inflight_segment_not_dropped(fake):
    # A1: an async probe that is still capturing when end_call arrives must be
    # waited on and its segment merged, NOT silently dropped from the tail.
    media = _DUMMY_PCAP + bytes.fromhex(
        '000000010000000100000001000000010000000000000001'  # 1 packet record
    )
    fake.shell_responses['rm -f /tmp/aiVoip_live_'] = __import__('base64').b64encode(media).decode()
    p = RealReproductionPlatform(adapter=fake)
    ctx = p.resolve_voice_context(_Device())
    # Spawn (async) and immediately end the call without waiting on the future.
    p.spawn_live_probe(context=ctx, start_ms=100, call_id='c10')
    call = p.build_call_capture(context=ctx, start_ms=100, end_ms=6000, call_id='c10', profile_id='P', signal=None)
    # The in-flight probe's segment was collected (not dropped) and no fresh
    # post-call tail capture was issued.
    assert call.pcap == media
    assert not any('aiVoip_call_c10.pcap' in c for c in fake.shell_calls)


# -- FXS event streaming (bridge-loop reader -> queue -> sync poll) -------------------

_FXS_OFFHOOK = '2026-08-14 12:36:01.988000 [0] D:: [D]OFFHOOK\n'
_FXS_DTMF = '2026-08-14 12:36:02.628000 [0] D:: [D]DTMF<1>\n'
_FXS_ONHOOK = '2026-08-14 12:36:04.688000 [0] D:: [D]ONHOOK\n'


def _collect_until(monitor, pred, timeout: float = 5.0) -> list:
    """Poll repeatedly, accumulating events, until ``pred(accumulated)`` is true."""
    collected = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        events = monitor.poll()
        if events:
            collected.extend(events)
        if pred(collected):
            return collected
        time.sleep(0.05)
    return collected


def test_fxs_monitor_streams_events_through_bridge_reader(fake):
    fake.aim_chunks = [_FXS_OFFHOOK, _FXS_DTMF, _FXS_ONHOOK]
    p = RealReproductionPlatform(adapter=fake)
    try:
        monitor = p.start_fxs_monitor()
        events = _collect_until(
            monitor,
            lambda evs: any(e.event == 'OFFHOOK' for e in evs)
            and any(e.event == 'DTMF' for e in evs)
            and any(e.event == 'ONHOOK' for e in evs),
        )
        assert any(e.event == 'OFFHOOK' for e in events)
        assert any(e.event == 'DTMF' and e.digit == '1' for e in events)
        assert any(e.event == 'ONHOOK' for e in events)
    finally:
        p.stop_fxs_monitor()
        p.disconnect()


def test_fxs_monitor_start_does_not_rewrite_debug(fake):
    p = RealReproductionPlatform(adapter=fake)
    try:
        monitor = p.start_fxs_monitor()
        # enable_debug=False: the arm phase already enabled FULL_DEBUG_ENABLE, so
        # starting the monitor must NOT push debug commands onto the AIM PTY.
        assert not any('debug' in c or 'de ' in c for c in fake.cli_calls)
        assert monitor._started is True
    finally:
        p.stop_fxs_monitor()
        p.disconnect()


def test_fxs_monitor_stop_is_idempotent(fake):
    p = RealReproductionPlatform(adapter=fake)
    p.start_fxs_monitor()
    p.stop_fxs_monitor()
    p.stop_fxs_monitor()  # second stop must not raise
    p.disconnect()
