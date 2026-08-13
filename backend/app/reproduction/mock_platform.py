from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from app.contracts.enums import CaptureChannel, ChannelHealth
from app.core.errors import AppError
from app.db.models import CaseDevice
from app.reproduction.mock_signal import MockCallCaptureBuilder


def _utcnow():
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class VoiceRuntimeContext:
    voice_vlan_id: str
    voice_interface: str
    voice_device_ip: str | None
    voice_gateway_ip: str
    interface_up: bool
    resolver_id: str = 'MOCK_VOICE_CONTEXT_V1'
    resolver_version: str = '1.0.0'

    def as_dict(self) -> dict[str, Any]:
        return {
            'voice_vlan_id': self.voice_vlan_id,
            'voice_interface': self.voice_interface,
            'voice_device_ip': self.voice_device_ip,
            'voice_gateway_ip': self.voice_gateway_ip,
            'interface_up': self.interface_up,
            'resolver_id': self.resolver_id,
            'resolver_version': self.resolver_version,
        }


@dataclass
class MockChannelState:
    status: ChannelHealth = ChannelHealth.UNKNOWN
    packet_count: int = 0
    advancing: bool = False
    enabled: bool = False
    details: dict[str, Any] = field(default_factory=dict)


class MockReproductionPlatform:
    """Deterministic platform simulator for Phase C.

    It never opens SSH, never runs shell/AIM, and never contains real DUT commands.
    EC-02 will later provide a production adapter behind the same abstract behavior.
    """

    platform_id='mock-voip-platform'
    version='1.0.0'

    def __init__(self):
        self._state: dict[str, dict[CaptureChannel, MockChannelState]] = {}
        self.capture_builder=MockCallCaptureBuilder()

    def resolve_voice_context(self, device: CaseDevice) -> VoiceRuntimeContext:
        info=device.device_info or {}
        raw=info.get('mock_voice_context') or {
            'voice_vlans': ['100'],
            'voice_interface': 'br-lan_100',
            'voice_device_ip': '192.0.2.10',
            'voice_gateway_ip': '192.0.2.1',
            'interface_up': True,
        }
        vlans=list(raw.get('voice_vlans') or [])
        if not vlans:
            raise AppError('VOICE_CONTEXT_NOT_FOUND', details={'device_id':device.id})
        if len(vlans) != 1:
            raise AppError('VOICE_CONTEXT_INVALID', details={'voice_vlan_count':len(vlans)})
        vlan=str(vlans[0])
        interface=str(raw.get('voice_interface') or '')
        expected=f'br-lan_{vlan}'
        if interface != expected:
            raise AppError('VOICE_INTERFACE_MISSING', details={'expected':expected,'observed':interface or None})
        if not bool(raw.get('interface_up',False)):
            raise AppError('VOICE_INTERFACE_MISSING', details={'interface':interface,'reason':'INTERFACE_DOWN'})
        gateway=str(raw.get('voice_gateway_ip') or '')
        try:
            ipaddress.ip_address(gateway)
        except ValueError as exc:
            raise AppError('VOICE_GATEWAY_CONFIG_INVALID', details={'voice_gateway_ip':gateway or None}) from exc
        device_ip=raw.get('voice_device_ip')
        if device_ip:
            try:
                ipaddress.ip_address(str(device_ip))
            except ValueError:
                device_ip=None
        return VoiceRuntimeContext(
            voice_vlan_id=vlan,
            voice_interface=interface,
            voice_device_ip=str(device_ip) if device_ip else None,
            voice_gateway_ip=gateway,
            interface_up=True,
        )

    def _session_state(self, session_id: str) -> dict[CaptureChannel, MockChannelState]:
        if session_id not in self._state:
            self._state[session_id]={channel:MockChannelState() for channel in CaptureChannel}
        return self._state[session_id]

    def arm(self, *, session_id: str, device: CaseDevice, actions: list[str]) -> dict[str, dict[str, Any]]:
        cfg=(device.device_info or {}).get('mock_capture') or {}
        st=self._session_state(session_id)
        # PCAP
        if 'START_VOICE_PCAP' in actions:
            ok=not bool(cfg.get('pcap_fail',False))
            st[CaptureChannel.PCAP]=MockChannelState(
                status=ChannelHealth.HEALTHY if ok else ChannelHealth.FAILED,
                packet_count=int(cfg.get('pcap_packets',20 if ok else 0)),
                advancing=ok and not bool(cfg.get('pcap_stalled',False)), enabled=ok,
                details={'pcap_header_valid':ok},
            )
        # PCM streams are represented by actual observed packet counts in the mocked PCAP data-plane.
        if 'START_PCM_RX' in actions:
            ok=not bool(cfg.get('pcm_rx_fail',False))
            st[CaptureChannel.PCM_RX]=MockChannelState(
                status=ChannelHealth.HEALTHY if ok else ChannelHealth.UNAVAILABLE,
                packet_count=int(cfg.get('pcm_rx_packets',5 if ok else 0)),
                advancing=ok and not bool(cfg.get('pcm_rx_stalled',False)), enabled=ok,
                details={'dst_port':40000},
            )
        if 'START_PCM_TX' in actions:
            ok=not bool(cfg.get('pcm_tx_fail',False))
            st[CaptureChannel.PCM_TX]=MockChannelState(
                status=ChannelHealth.HEALTHY if ok else ChannelHealth.UNAVAILABLE,
                packet_count=int(cfg.get('pcm_tx_packets',5 if ok else 0)),
                advancing=ok and not bool(cfg.get('pcm_tx_stalled',False)), enabled=ok,
                details={'dst_port':50000},
            )
        debug_actions={'ENABLE_BASIC_VOIP_DEBUG','ENABLE_DTMF_DEBUG','ENABLE_DSP_DEBUG','ENABLE_SIP_PACKET_LOG'}
        if any(a in actions for a in debug_actions):
            ok=not bool(cfg.get('debug_fail',False))
            st[CaptureChannel.DEBUG]=MockChannelState(
                status=ChannelHealth.HEALTHY if ok else ChannelHealth.FAILED,
                packet_count=0, advancing=ok, enabled=ok,
                details={'reader_alive':ok,'heartbeat':ok},
            )
        st[CaptureChannel.LOG]=MockChannelState(status=ChannelHealth.HEALTHY,packet_count=0,advancing=True,enabled=True)
        return self.snapshot(session_id)

    def degrade_channel(self, session_id: str, channel: CaptureChannel, *, unavailable: bool = False) -> None:
        st=self._session_state(session_id)
        current=st[channel]
        current.status=ChannelHealth.UNAVAILABLE if unavailable else ChannelHealth.DEGRADED
        current.advancing=False
        if unavailable:
            current.enabled=False
            current.packet_count=0

    def snapshot(self, session_id: str) -> dict[str, dict[str, Any]]:
        return {
            channel.value: {
                'status': state.status.value,
                'packet_count': state.packet_count,
                'advancing': state.advancing,
                'enabled': state.enabled,
                **state.details,
            }
            for channel,state in self._session_state(session_id).items()
        }


    def build_pretrigger_capture(self, *, context: VoiceRuntimeContext, start_ms: int, end_ms: int):
        return self.capture_builder.pretrigger(
            start_ms=start_ms,end_ms=end_ms,device_ip=context.voice_device_ip or '192.0.2.10',gateway_ip=context.voice_gateway_ip,
        )

    def build_live_probe(self, *, context: VoiceRuntimeContext, start_ms: int, call_id: str):
        return self.capture_builder.live_probe(start_ms=start_ms,device_ip=context.voice_device_ip or '192.0.2.10',gateway_ip=context.voice_gateway_ip,call_id=call_id)

    def build_call_capture(self, *, context: VoiceRuntimeContext, start_ms: int, end_ms: int, call_id: str, profile_id: str, signal):
        return self.capture_builder.build(
            start_ms=start_ms,end_ms=end_ms,device_ip=context.voice_device_ip or '192.0.2.10',gateway_ip=context.voice_gateway_ip,
            call_id=call_id,target_findings=signal.findings,verdict=signal.verdict.value,profile_id=profile_id,
        )

    def cleanup(self, *, session_id: str, device: CaseDevice, actions: list[str]) -> dict[str, dict[str, Any]]:
        cfg=(device.device_info or {}).get('mock_cleanup') or {}
        st=self._session_state(session_id)
        # PCM/debug are stopped first while PCAP remains alive for reverse validation.
        if 'STOP_PCM_RX' in actions:
            leak=bool(cfg.get('pcm_rx_leak',False))
            st[CaptureChannel.PCM_RX]=MockChannelState(
                status=ChannelHealth.DEGRADED if leak else ChannelHealth.STOPPED,
                packet_count=1 if leak else 0, advancing=leak, enabled=leak,
                details={'dst_port':40000,'quiet_verified':not leak},
            )
        if 'STOP_PCM_TX' in actions:
            leak=bool(cfg.get('pcm_tx_leak',False))
            st[CaptureChannel.PCM_TX]=MockChannelState(
                status=ChannelHealth.DEGRADED if leak else ChannelHealth.STOPPED,
                packet_count=1 if leak else 0, advancing=leak, enabled=leak,
                details={'dst_port':50000,'quiet_verified':not leak},
            )
        if any(a.startswith('DISABLE_') for a in actions):
            leak=bool(cfg.get('debug_leak',False))
            st[CaptureChannel.DEBUG]=MockChannelState(
                status=ChannelHealth.DEGRADED if leak else ChannelHealth.STOPPED,
                advancing=leak, enabled=leak,
                details={'off_verified':not leak},
            )
        # Reverse-validation snapshot happens before PCAP stop.
        reverse=self.snapshot(session_id)
        if 'STOP_VOICE_PCAP' in actions:
            leak=bool(cfg.get('pcap_leak',False))
            st[CaptureChannel.PCAP]=MockChannelState(
                status=ChannelHealth.DEGRADED if leak else ChannelHealth.STOPPED,
                packet_count=0, advancing=leak, enabled=leak,
                details={'closed_verified':not leak},
            )
        return {'reverse_validation':reverse,'final':self.snapshot(session_id)}
