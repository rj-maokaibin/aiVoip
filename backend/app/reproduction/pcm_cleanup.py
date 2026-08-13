from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from typing import Callable


PcmPacketProbe = Callable[[str, int], int]
AimCommandExecutor = Callable[[str], None]
ShellCommandExecutor = Callable[[str], str]


_TCPDUMP_CAPTURED = re.compile(r'(?m)^\s*(\d+) packets captured\s*$')


def build_busybox_tcpdump_probe(*, voice_interface: str, port: int, seconds: int = 5) -> str:
    """Build the device-specific read-only PCM probe used by the EC-02 contract."""
    if not voice_interface or not re.fullmatch(r'[A-Za-z0-9_.:-]+', voice_interface):
        raise ValueError(f'INVALID_VOICE_INTERFACE:{voice_interface!r}')
    if not 1 <= port <= 65535:
        raise ValueError(f'INVALID_UDP_PORT:{port}')
    if not 1 <= seconds <= 60:
        raise ValueError(f'INVALID_PROBE_SECONDS:{seconds}')
    return (
        f'timeout -t {seconds} tcpdump -ni {shlex.quote(voice_interface)} '
        f"-c 1 'udp port {port}' 2>&1"
    )


def parse_tcpdump_packet_count(output: str) -> int:
    """Return tcpdump's capture count; BusyBox timeout status is intentionally ignored."""
    matches = _TCPDUMP_CAPTURED.findall(output)
    if len(matches) != 1:
        raise ValueError('PCM_TCPDUMP_CAPTURE_COUNT_MISSING')
    return int(matches[0])


class BusyboxTcpdumpPcmProbe:
    """Read-only UDP packet probe for the observed APF1250 BusyBox environment."""

    def __init__(self, *, execute_shell: ShellCommandExecutor, seconds: int = 5):
        self._execute_shell = execute_shell
        self._seconds = seconds

    def __call__(self, voice_interface: str, port: int) -> int:
        output = self._execute_shell(
            build_busybox_tcpdump_probe(
                voice_interface=voice_interface, port=port, seconds=self._seconds
            )
        )
        return parse_tcpdump_packet_count(output)


@dataclass(frozen=True)
class PcmCleanupChannelResult:
    channel: str
    port: int
    packets_before: int
    packets_after: int
    off_executed: bool
    quiet_verified: bool
    retry_blocked: bool

    def as_dict(self) -> dict[str, int | bool | str]:
        return {
            'channel': self.channel,
            'port': self.port,
            'packets_before': self.packets_before,
            'packets_after': self.packets_after,
            'off_executed': self.off_executed,
            'quiet_verified': self.quiet_verified,
            'retry_blocked': self.retry_blocked,
        }


class PcmCleanupGuard:
    """Verify PCM state before cleanup and never repeat a non-idempotent OFF command.

    ``off_already_executed`` must be restored from the previous CleanupRun when a watchdog
    retries after a worker restart. A stream that remains active after its only permitted OFF
    is deliberately left unverified for operator investigation instead of risking AIM exit.
    """

    _CHANNELS = {
        'PCM_RX': (40000, 'pcm_rx'),
        'PCM_TX': (50000, 'pcm_tx'),
    }

    def __init__(self, *, probe_packets: PcmPacketProbe, execute_aim: AimCommandExecutor):
        self._probe_packets = probe_packets
        self._execute_aim = execute_aim

    def cleanup_channel(
        self,
        *,
        channel: str,
        voice_interface: str,
        voice_gateway_ip: str,
        off_already_executed: bool = False,
    ) -> PcmCleanupChannelResult:
        try:
            port, direction = self._CHANNELS[channel]
        except KeyError as exc:
            raise ValueError(f'UNKNOWN_PCM_CHANNEL:{channel}') from exc

        packets_before = self._packet_count(voice_interface, port)
        if packets_before == 0:
            return PcmCleanupChannelResult(
                channel=channel,
                port=port,
                packets_before=0,
                packets_after=0,
                off_executed=False,
                quiet_verified=True,
                retry_blocked=False,
            )
        if off_already_executed:
            return PcmCleanupChannelResult(
                channel=channel,
                port=port,
                packets_before=packets_before,
                packets_after=packets_before,
                off_executed=False,
                quiet_verified=False,
                retry_blocked=True,
            )

        self._execute_aim(f'voip dsp diag set {voice_gateway_ip} {port} 1 {direction} off')
        packets_after = self._packet_count(voice_interface, port)
        return PcmCleanupChannelResult(
            channel=channel,
            port=port,
            packets_before=packets_before,
            packets_after=packets_after,
            off_executed=True,
            quiet_verified=packets_after == 0,
            retry_blocked=False,
        )

    def _packet_count(self, voice_interface: str, port: int) -> int:
        count = self._probe_packets(voice_interface, port)
        if isinstance(count, bool) or not isinstance(count, int) or count < 0:
            raise ValueError(f'INVALID_PCM_PACKET_COUNT:{port}:{count!r}')
        return count