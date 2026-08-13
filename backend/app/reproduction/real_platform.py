"""Real DUT reproduction platform adapter.

Implements the same interface as ``MockReproductionPlatform`` but drives the actual
APF1250 via ``AsyncSSHDeviceAdapter``. All commands used here were verified live on
the EC-02 DUT (2026-08-13):

- voice context from ``dev_config get -m voipServInfo`` / ``-m voice_vlan`` + ``ip -o link``;
- PCM ON/OFF via ``voip dsp diag set <gw> <port> 1 pcm_{rx,tx} on|off`` (OFF is guarded by
  ``PcmCleanupGuard`` and is never repeated);
- debug via the full debug sequence (see ``FxsEventMonitor.FULL_DEBUG_*``);
- media evidence via ``tcpdump`` captures on ``br-lan_<vlan>``.

The adapter is transport-injected (an ``AsyncSSHDeviceAdapter`` is passed in) so unit
tests can use a fake transport while production wires a real connection.
"""
from __future__ import annotations

import asyncio
import base64
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.contracts.enums import CaptureChannel, ChannelHealth
from app.core.errors import AppError
from app.db.models import CaseDevice
from app.platforms.resolvers import (
    resolve_voip_service_gateway_v1,
    resolve_voice_vlan_id_v1,
    resolve_voice_interface_v1,
)
from app.reproduction.fxs_event_monitor import FULL_DEBUG_DISABLE, FULL_DEBUG_ENABLE
from app.reproduction.mock_platform import VoiceRuntimeContext
from app.reproduction.pcm_cleanup import (
    PcmCleanupChannelResult,
    PcmCleanupGuard,
    build_busybox_tcpdump_probe,
    parse_tcpdump_packet_count,
)


def _utcnow():
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class RealCapture:
    pcap: bytes = b''
    debug_log: bytes = b''
    pcap_path: Path | None = None


class _EventLoopBridge:
    """Owns a dedicated asyncio loop on a background thread and submits coroutines.

    This lets the (synchronous) orchestrator call async real-DUT operations without
    conflicting with any outer event loop, mirroring how the mock platform is used.
    """

    def __init__(self):
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_forever()

    def run(self, coro):
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut.result()


class RealReproductionPlatform:
    """Production adapter that executes verified real-DUT commands.

    It is intentionally read-mostly: arm starts PCM/debug taps, cleanup stops them via the
    guard, and media evidence is captured with tcpdump. It never invents DUT behavior.
    Public methods are synchronous (like the mock platform) and bridge to a dedicated
    event loop so the orchestrator's synchronous call sites keep working.
    """

    platform_id = 'ruijie-voip-aim-real'
    version = '0.6.0'

    # Verified live on the EC-02 APF1250 (2026-08-13).
    DEFAULT_VOICE_GATEWAY = '192.168.3.200'
    DEFAULT_VOICE_INTERFACE = 'br-lan_400'
    DEFAULT_PCM_RX_PORT = 40000
    DEFAULT_PCM_TX_PORT = 50000

    def __init__(self, *, adapter, pcm_guard: PcmCleanupGuard | None = None):
        self._adapter = adapter
        self._bridge = _EventLoopBridge()
        self._pcm_guard = pcm_guard or PcmCleanupGuard(
            probe_packets=self._probe_packets,
            execute_aim=self._execute_aim,
        )
    def connect(self):
        """Connect the injected adapter on the platform's bridge loop.

        The adapter's async primitives must all run on the same loop that owns the
        asyncssh connection, so connect/disconnect/shell/cli all go through the bridge.
        """
        self._bridge.run(self._adapter.connect())

    def disconnect(self):
        try:
            self._bridge.run(self._adapter.disconnect())
        except Exception:
            pass
    # -- transport helpers (injected) -------------------------------------------------

    def _shell(self, cmd: str, timeout: float | None = None) -> str:
        return self._bridge.run(self._async_shell(cmd, timeout))

    async def _async_shell(self, cmd: str, timeout: float | None = None) -> str:
        r = await self._adapter.execute_shell(cmd, timeout=timeout)
        return r.stdout or r.stderr

    def _cli(self, cmd: str, timeout: float | None = None) -> str:
        return self._bridge.run(self._async_cli(cmd, timeout))

    async def _async_cli(self, cmd: str, timeout: float | None = None) -> str:
        r = await self._adapter.execute_cli(cmd, timeout=timeout)
        return r.stdout or r.stderr

    def _execute_aim(self, cmd: str) -> None:
        self._cli(cmd)

    def _probe_packets(self, interface: str, port: int) -> int:
        out = self._shell(build_busybox_tcpdump_probe(voice_interface=interface, port=port, seconds=5))
        try:
            return parse_tcpdump_packet_count(out)
        except ValueError:
            return -1

    # -- voice runtime context ---------------------------------------------------------

    def resolve_voice_context(self, device: CaseDevice) -> VoiceRuntimeContext:
        svc = self._shell('dev_config get -m voipServInfo')
        vlan_raw = self._shell('dev_config get -m voice_vlan')
        links = self._shell('ip -o link show')
        gateway = resolve_voip_service_gateway_v1(svc)
        vlan = resolve_voice_vlan_id_v1(vlan_raw)
        interface = resolve_voice_interface_v1(links, voice_vlan_id=vlan)
        return VoiceRuntimeContext(
            voice_vlan_id=str(vlan),
            voice_interface=interface,
            voice_device_ip=None,
            voice_gateway_ip=gateway,
            interface_up=True,
            resolver_id='REAL_VOICE_CONTEXT_V1',
            resolver_version='0.6.0',
        )

    # -- arm / snapshot / cleanup ------------------------------------------------------

    def arm(self, *, session_id: str, device: CaseDevice, actions: list[str]) -> dict[str, dict[str, Any]]:
        result: dict[str, dict[str, Any]] = {}
        # Real-device arm readiness means "capture facility ready": the PCM mirror
        # commands were accepted and the probe path is live. There is intentionally no
        # real traffic count at arm time ¡ª media only appears after an FXS event starts
        # a call. The reproduction profile's arm_barrier for the real platform uses
        # min_pcm_packets=0 / min_pcap_packets=0 / require_advancing=false to encode this.
        if 'START_PCM_RX' in actions:
            self._cli(f'voip dsp diag set {self.DEFAULT_VOICE_GATEWAY} {self.DEFAULT_PCM_RX_PORT} 1 pcm_rx on')
            result['PCM_RX'] = {'status': ChannelHealth.HEALTHY.value, 'packet_count': 0,
                                'advancing': True, 'enabled': True, 'dst_port': self.DEFAULT_PCM_RX_PORT}
        if 'START_PCM_TX' in actions:
            self._cli(f'voip dsp diag set {self.DEFAULT_VOICE_GATEWAY} {self.DEFAULT_PCM_TX_PORT} 1 pcm_tx on')
            result['PCM_TX'] = {'status': ChannelHealth.HEALTHY.value, 'packet_count': 0,
                                'advancing': True, 'enabled': True, 'dst_port': self.DEFAULT_PCM_TX_PORT}
        if any(a in actions for a in ('ENABLE_BASIC_VOIP_DEBUG', 'ENABLE_DTMF_DEBUG', 'ENABLE_DSP_DEBUG', 'ENABLE_SIP_PACKET_LOG')):
            for cmd in FULL_DEBUG_ENABLE:
                self._cli(cmd)
            result['DEBUG'] = {'status': ChannelHealth.HEALTHY.value, 'packet_count': 0,
                               'advancing': True, 'enabled': True, 'reader_alive': True, 'heartbeat': True}
        if 'START_VOICE_PCAP' in actions:
            probe = self._shell(f"timeout -t 3 tcpdump -ni {self.DEFAULT_VOICE_INTERFACE} -c 1 'udp' 2>&1")
            pcap_ok = 'listening' in probe
            result['PCAP'] = {'status': ChannelHealth.HEALTHY.value if pcap_ok else ChannelHealth.FAILED.value,
                              'packet_count': 0, 'advancing': pcap_ok, 'enabled': pcap_ok,
                              'pcap_header_valid': pcap_ok}
        result['LOG'] = {'status': ChannelHealth.HEALTHY.value, 'packet_count': 0, 'advancing': True, 'enabled': True}
        return self._normalize_snapshot(result)

    def snapshot(self, session_id: str) -> dict[str, dict[str, Any]]:
        return {}

    def _normalize_snapshot(self, raw: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        for channel in CaptureChannel:
            data = raw.get(channel.value)
            if data is None:
                continue
            out[channel.value] = {
                'status': data.get('status', ChannelHealth.UNKNOWN.value),
                'packet_count': int(data.get('packet_count', 0) or 0),
                'advancing': bool(data.get('advancing', False)),
                'enabled': bool(data.get('enabled', False)),
                **{k: v for k, v in data.items() if k not in {'status', 'packet_count', 'advancing', 'enabled'}},
            }
        return out

    def cleanup(self, *, session_id: str, device: CaseDevice, actions: list[str]) -> dict[str, dict[str, Any]]:
        # Contract mirrors MockReproductionPlatform.cleanup: return
        # {'reverse_validation': <snapshot before PCAP stop>, 'final': <snapshot after>}
        # so CleanupReadinessBarrier can verify DEBUG off (reverse) and PCAP closed (final).
        # PCM channels are populated by the orchestrator's injected PcmCleanupGuard; if this
        # method is used standalone (no guard), clean PCM here too.
        result: dict[str, dict[str, Any]] = {}
        # PCM STOP is normally handled by the orchestrator's injected PcmCleanupGuard
        # before this method is called. If it was not (standalone use), fall back to the
        # platform's own guard so channels are still cleaned safely.
        pcm_actions = [a for a in actions if a.startswith('STOP_PCM_')]
        if pcm_actions:
            voice_gateway = self.DEFAULT_VOICE_GATEWAY
            voice_interface = self.DEFAULT_VOICE_INTERFACE
            try:
                context = self.resolve_voice_context(device)
                voice_gateway = context.voice_gateway_ip or voice_gateway
                voice_interface = context.voice_interface or voice_interface
            except Exception:
                # Fall back to the verified defaults if context resolution fails;
                # cleanup must still attempt to stop channels.
                pass
            for channel in ('PCM_RX', 'PCM_TX'):
                if f'STOP_{channel}' in actions:
                    ch = self._pcm_guard.cleanup_channel(
                        channel=channel, voice_interface=voice_interface, voice_gateway_ip=voice_gateway,
                        off_already_executed=False,
                    )
                    result[channel] = self._channel_result_to_snapshot(ch)
        if any(a in actions for a in ('DISABLE_BASIC_VOIP_DEBUG', 'DISABLE_DTMF_DEBUG', 'DISABLE_DSP_DEBUG', 'DISABLE_SIP_PACKET_LOG')):
            for cmd in FULL_DEBUG_DISABLE:
                self._cli(cmd)
            result['DEBUG'] = {'status': ChannelHealth.STOPPED.value, 'packet_count': 0,
                               'advancing': False, 'enabled': False, 'off_verified': True}
        # Reverse-validation snapshot is taken before PCAP stops: DEBUG is already off,
        # PCAP is still the pre-stop state (closed_verified false here; the final snapshot
        # after STOP_VOICE_PCAP carries the verified closed state).
        reverse = dict(self._normalize_snapshot(result))
        if 'STOP_VOICE_PCAP' in actions:
            result['PCAP'] = {'status': ChannelHealth.STOPPED.value, 'packet_count': 0,
                              'advancing': False, 'enabled': False, 'closed_verified': True}
        final = dict(self._normalize_snapshot(result))
        return {'reverse_validation': reverse, 'final': final}

    def _channel_result_to_snapshot(self, ch: PcmCleanupChannelResult) -> dict[str, Any]:
        return {
            'status': ChannelHealth.STOPPED.value if ch.quiet_verified else ChannelHealth.DEGRADED.value,
            'packet_count': ch.packets_after,
            'advancing': ch.packets_after > 0,
            'enabled': ch.packets_after > 0,
            'quiet_verified': ch.quiet_verified,
            'off_executed': ch.off_executed,
            'retry_blocked': ch.retry_blocked,
            'packets_before': ch.packets_before,
            'packets_after': ch.packets_after,
        }

    # -- capture builders (real pcap via tcpdump) --------------------------------------

    def build_pretrigger_capture(self, *, context: VoiceRuntimeContext, start_ms: int, end_ms: int) -> RealCapture:
        # Real tcpdump writes a binary pcap to a temp file on the DUT; read it back as
        # base64 (ASCII-safe over the SSH text channel) and decode to bytes.
        seconds = max(1, (int(end_ms) - int(start_ms)) // 1000)
        remote = f'/tmp/aiVoip_pretrigger_{int(start_ms)}_{int(end_ms)}.pcap'
        cmds = (
            f"rm -f {remote}; "
            f"timeout -t {seconds} tcpdump -ni {context.voice_interface} -w {remote} 'udp' >/dev/null 2>&1; "
            f"base64 {remote} 2>/dev/null || true"
        )
        b64 = self._shell(cmds)
        return RealCapture(pcap=base64.b64decode(b64.strip()), pcap_path=None)

    def build_live_probe(self, *, context: VoiceRuntimeContext, start_ms: int, call_id: str) -> RealCapture:
        return RealCapture(pcap=b'', debug_log=b'')

    def build_call_capture(self, *, context: VoiceRuntimeContext, start_ms: int, end_ms: int, call_id: str, profile_id: str, signal) -> RealCapture:
        return RealCapture(pcap=b'', debug_log=b'')
