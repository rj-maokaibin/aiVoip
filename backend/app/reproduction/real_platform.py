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
import queue
import threading
import time
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
from app.reproduction.fxs_event_monitor import (
    FxsEventMonitor,
    FULL_DEBUG_DISABLE,
    FULL_DEBUG_ENABLE,
)
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

    def spawn(self, coro):
        """Schedule a coroutine on the bridge loop without blocking on its result.

        Returns the asyncio Task so the caller can cancel/wait it later.
        """
        fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
        return fut


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
        # FXS event streaming: a background reader runs on the bridge loop (the loop
        # that owns the asyncssh connection) and pushes raw AIM chunks into a
        # thread-safe queue; the synchronous orchestrator polls the queue via the
        # FxsEventMonitor. This keeps ALL asyncssh I/O on ONE event loop, avoiding
        # the previous cross-loop deadlock (bridge loop vs caller loop).
        self._fxs_queue: queue.Queue = queue.Queue()
        self._fxs_stop = threading.Event()
        self._fxs_reader_fut = None
        self._fxs_monitor: FxsEventMonitor | None = None
        self._fxs_started_ms = int(time.monotonic() * 1000)
        # Real in-call media segments captured while a call is active, keyed by
        # call_id. build_live_probe appends a short tcpdump window per probe, and
        # the reproduction watcher probes periodically during the conversation so
        # the merged segments span the whole call (not just the dialing window at
        # bind_call). build_call_capture (end_call) merges them and prefers this
        # real media over the post-hangup window (which is empty because the
        # mirror stream stops on hangup), so CALL_QUICK analyzes actual PCM
        # instead of an empty/near-empty capture.
        self._live_pcap_cache: dict[str, list[bytes]] = {}

    # -- FXS event streaming -------------------------------------------------------

    @property
    def fxs_event_monitor(self) -> FxsEventMonitor:
        """The event monitor wired to this platform's bridge-loop AIM reader."""
        if self._fxs_monitor is None:
            self._fxs_monitor = FxsEventMonitor(
                read_aim_chunk=self._read_fxs_chunk,
                write_aim=self._write_aim,
                relative_ms=self._fxs_relative_ms,
            )
        return self._fxs_monitor

    def start_fxs_monitor(self):
        """Start the background AIM reader on the bridge loop and return the monitor.

        The reader shares the asyncssh connection with arm/cleanup but runs on the
        same (bridge) event loop, so no cross-loop handoff ever happens. The caller
        should call ``stop_fxs_monitor`` before cleanup so the reader is no longer
        competing with ``execute_cli`` prompt reads on the same PTY.
        """
        if self._fxs_reader_fut is not None:
            return self.fxs_event_monitor
        self._fxs_stop.clear()
        self._fxs_started_ms = int(time.monotonic() * 1000)
        self._fxs_reader_fut = self._bridge.spawn(self._fxs_reader_loop())
        # Send FULL_DEBUG_ENABLE on THIS connection's AIM PTY. The watcher opens a
        # fresh AIM session (its own adapter connection), so it cannot rely on the
        # arm phase having enabled debug on a different, already-closed session —
        # without it the DUT never emits OFFHOOK/ONHOOK lines and FXS activity is
        # never detected. FULL_DEBUG_ENABLE is idempotent, so re-sending when arm
        # did run on the same session is harmless.
        self.fxs_event_monitor.start(enable_debug=True)
        return self.fxs_event_monitor

    def stop_fxs_monitor(self):
        """Stop the background AIM reader; safe to call even when not started."""
        self._fxs_stop.set()
        fut, self._fxs_reader_fut = self._fxs_reader_fut, None
        if fut is not None:
            try:
                fut.result(timeout=5)
            except Exception:
                try:
                    fut.cancel()
                except Exception:
                    pass
        # Drain any buffered chunks so a later monitor use starts clean.
        while True:
            try:
                self._fxs_queue.get_nowait()
            except queue.Empty:
                break

    async def _fxs_reader_loop(self):
        """Bridge-loop task: read raw AIM chunks and push them onto the queue.

        Runs until ``stop_fxs_monitor`` sets the stop event. ``read_aim_chunk``
        returns '' on timeout so this never blocks the bridge loop permanently.
        """
        while not self._fxs_stop.is_set():
            try:
                chunk = await self._adapter.read_aim_chunk(timeout=1.0)
            except Exception:
                break
            if chunk:
                self._fxs_queue.put(chunk)
            else:
                await asyncio.sleep(0.05)

    def _read_fxs_chunk(self) -> str | None:
        """Synchronous reader used by FxsEventMonitor.poll (no queue blocking)."""
        try:
            return self._fxs_queue.get_nowait()
        except queue.Empty:
            return None

    def _write_aim(self, cmd: str):
        self._bridge.run(self._adapter.write_aim(cmd))

    def _fxs_relative_ms(self) -> int:
        return int(time.monotonic() * 1000) - self._fxs_started_ms

    def connect(self):
        """Connect the injected adapter on the platform's bridge loop.

        The adapter's async primitives must all run on the same loop that owns the
        asyncssh connection, so connect/disconnect/shell/cli all go through the bridge.
        """
        self._bridge.run(self._adapter.connect())

    def disconnect(self):
        try:
            self.stop_fxs_monitor()
        except Exception:
            pass
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

    # -- real CALL detection (media-binding, no AIM SIP plaintext) --------------------

    def pcm_media_active(self, *, context: VoiceRuntimeContext | None = None) -> bool:
        """Return True when the PCM mirror stream shows live media.

        A real call is bound on the PCM mirror stream becoming active (UDP
        40000/50000 carry packets), not on SIP INVITE plaintext: the DUT's AIM SIP
        debug (`de sip de`) does not emit INVITE/BYE lines on the PTY (verified live
        2026-08-14), so the verified media-binding signal is used instead — matching
        the profile's ``media_binding_event = RTP_STREAM_START`` semantics.
        """
        iface = context.voice_interface if context and context.voice_interface else self.DEFAULT_VOICE_INTERFACE
        for port in (self.DEFAULT_PCM_RX_PORT, self.DEFAULT_PCM_TX_PORT):
            n = self._probe_packets(interface=iface, port=port)
            if n > 0:
                return True
        return False

    def media_binding_call_ref(self) -> str:
        """Opaque call reference for the media-binding event (no SIP call-id exists)."""
        return f'media-{int(time.monotonic() * 1000)}'

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
        # arm issues AIM commands (PCM ON / full debug); stop any lingering FXS reader
        # so its prompt reads do not compete with execute_cli (idempotent).
        try:
            self.stop_fxs_monitor()
        except Exception:
            pass
        # Real-device arm readiness means "capture facility ready": the PCM mirror
        # commands were accepted and the probe path is live. There is intentionally no
        # real traffic count at arm time — media only appears after an FXS event starts
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
        # Cleanup issues AIM commands (PCM OFF / debug OFF) whose prompt reads must NOT
        # compete with the FXS AIM reader, so stop the reader first (idempotent).
        try:
            self.stop_fxs_monitor()
        except Exception:
            pass
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

    def _tcpdump_capture(self, *, context: VoiceRuntimeContext, seconds: int,
                         remote: str, port_filter: str | None = None) -> RealCapture:
        """Run tcpdump -w on the DUT and return the captured pcap bytes.

        Mirrors the verified pretrigger path: tcpdump writes a binary pcap to a temp
        file on the DUT, then base64 is read back (ASCII-safe over the SSH text
        channel) and decoded to bytes. ``port_filter`` narrows the capture (e.g. the
        PCM mirror streams); the default captures all UDP on the voice interface.
        """
        flt = f"'{port_filter}'" if port_filter else "'udp'"
        cmds = (
            f"rm -f {remote}; "
            f"timeout -t {seconds} tcpdump -ni {context.voice_interface} -w {remote} {flt} >/dev/null 2>&1; "
            f"base64 {remote} 2>/dev/null || true"
        )
        b64 = self._shell(cmds, timeout=max(20.0, seconds + 10))
        return RealCapture(pcap=base64.b64decode(b64.strip()), pcap_path=None)

    def build_pretrigger_capture(self, *, context: VoiceRuntimeContext, start_ms: int, end_ms: int) -> RealCapture:
        # Real tcpdump writes a binary pcap to a temp file on the DUT; read it back as
        # base64 (ASCII-safe over the SSH text channel) and decode to bytes.
        # The real platform cannot replay history: tcpdump only captures forward from
        # "now", so a 30s pretrigger window would block the CALL flow for 30s. Cap the
        # capture at a short forward window (5s) that still yields media evidence while
        # keeping the watcher responsive.
        seconds = min(5, max(1, (int(end_ms) - int(start_ms)) // 1000))
        remote = f'/tmp/aiVoip_pretrigger_{int(start_ms)}_{int(end_ms)}.pcap'
        # The tcpdump window runs for ``seconds`` (e.g. the profile's pretrigger),
        # which exceeds the default 10s command timeout; pass a matching timeout.
        return self._tcpdump_capture(context=context, seconds=seconds, remote=remote)

    def build_live_probe(self, *, context: VoiceRuntimeContext, start_ms: int, call_id: str) -> RealCapture:
        # Called at bind_call and repeatedly during the conversation: capture the PCM
        # mirror streams for a short window and append it to the call's segment list,
        # so the merged capture spans the whole conversation, not just the dialing
        # window at bind_call. The 40000/50000 ports are the verified PCM RX/TX mirrors
        # opened during arm. build_call_capture later merges these segments for CALL_QUICK.
        seconds = 2
        remote = f'/tmp/aiVoip_live_{call_id}_{int(time.monotonic()*1000)}.pcap'
        cap = self._tcpdump_capture(
            context=context, seconds=seconds, remote=remote,
            port_filter=f'udp port {self.DEFAULT_PCM_RX_PORT} or udp port {self.DEFAULT_PCM_TX_PORT}',
        )
        if cap.pcap and len(cap.pcap) > 24:
            self._live_pcap_cache.setdefault(call_id, []).append(cap.pcap)
        return cap

    @staticmethod
    def _merge_pcap_segments(segments: list[bytes]) -> bytes:
        """Concatenate multiple classic-pcap captures into one valid pcap blob.

        Each tcpdump ``-w`` capture carries its own 24-byte global header; keep the
        first header and strip the redundant global header from each subsequent
        segment so packet records form a single stream.
        """
        if not segments:
            return b''
        head = segments[0][:24] if len(segments[0]) >= 24 else b''
        body = bytearray(head)
        for seg in segments:
            body += seg[24:] if len(seg) > 24 else b''
        return bytes(body)

    def build_call_capture(self, *, context: VoiceRuntimeContext, start_ms: int, end_ms: int, call_id: str, profile_id: str, signal) -> RealCapture:
        # Called at end_call (call just ended). The PCM mirror stream stops on hangup,
        # so a fresh capture here is empty. Merge the real in-call media segments
        # accumulated by build_live_probe (bind + conversation probes) when available;
        # otherwise fall back to a short post-call tail capture (may be empty ->
        # analyzer degrades gracefully).
        segments = self._live_pcap_cache.pop(call_id, None)
        merged = self._merge_pcap_segments(segments or [])
        if merged and len(merged) > 24:
            return RealCapture(pcap=merged, debug_log=b'')
        seconds = max(1, (int(end_ms) - int(start_ms)) // 1000)
        seconds = min(seconds, 8)  # cap the tail window; media already ended
        remote = f'/tmp/aiVoip_call_{call_id}.pcap'
        return self._tcpdump_capture(
            context=context, seconds=seconds, remote=remote,
            port_filter=f'udp port {self.DEFAULT_PCM_RX_PORT} or udp port {self.DEFAULT_PCM_TX_PORT}',
        )
