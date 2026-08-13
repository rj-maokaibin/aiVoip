"""EC-02 real-device E2E: drive RealReproductionPlatform against the actual APF1250.

Connects via AsyncSSHDeviceAdapter (legacy KEX built in), constructs the real
platform adapter, and exercises the full lifecycle the orchestrator depends on:

  resolve_voice_context -> arm (PCM RX/TX ON + full debug + PCAP probe)
  -> snapshot -> cleanup (PCM OFF via PcmCleanupGuard + debug OFF)

The sequence is deliberately the safe, verified one: arm is fully reversible,
cleanup uses the guard so PCM OFF is executed at most once per active stream, and
readiness/quiet is verified via tcpdump probes. Credentials come from DEV_* env
vars (never printed).

Usage:
    DEV_HOST=... DEV_PORT=... DEV_USER=... DEV_PASSWORD=... python3 tools/dev_real_platform_e2e.py
"""
import asyncio
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from app.collectors.asyncssh_adapter import AsyncSSHDeviceAdapter
from app.reproduction.real_platform import RealReproductionPlatform


class _Device:
    """Minimal CaseDevice stand-in: the platform only reads device_info."""
    device_info = {}


def _line(label, ok, detail=''):
    mark = 'PASS' if ok else 'FAIL'
    print(f'  [{mark}] {label}' + (f'  ({detail})' if detail else ''))


async def main():
    adapter = AsyncSSHDeviceAdapter(
        ip=os.environ['DEV_HOST'], port=int(os.environ['DEV_PORT']),
        username=os.environ['DEV_USER'], password=os.environ['DEV_PASSWORD'],
    )
    platform = RealReproductionPlatform(adapter=adapter)
    # Connect on the platform's bridge loop so the asyncssh connection and all
    # transport calls share the same event loop.
    platform.connect()
    results = []

    def check(label, cond, detail=''):
        results.append((label, bool(cond), detail))
        _line(label, cond, detail)

    try:
        print('=== 1. resolve_voice_context (real dev_config/ip-link) ===')
        ctx = platform.resolve_voice_context(_Device())
        print(f'      vlan={ctx.voice_vlan_id} iface={ctx.voice_interface} gw={ctx.voice_gateway_ip} '
              f'resolver={ctx.resolver_id}')
        check('voice_vlan_id == 400', ctx.voice_vlan_id == '400', ctx.voice_vlan_id)
        check('voice_interface == br-lan_400', ctx.voice_interface == 'br-lan_400', ctx.voice_interface)
        check('voice_gateway_ip == 192.168.3.200', ctx.voice_gateway_ip == '192.168.3.200', ctx.voice_gateway_ip)
        check('interface_up', ctx.interface_up is True)

        print('=== 2. arm (PCM RX/TX ON + full debug + PCAP probe) ===')
        snap = platform.arm(
            session_id='e2e-real',
            device=_Device(),
            actions=['START_PCM_RX', 'START_PCM_TX', 'ENABLE_BASIC_VOIP_DEBUG',
                     'ENABLE_DTMF_DEBUG', 'ENABLE_DSP_DEBUG', 'ENABLE_SIP_PACKET_LOG',
                     'START_VOICE_PCAP'],
        )
        for ch in ('PCM_RX', 'PCM_TX', 'DEBUG', 'PCAP', 'LOG'):
            d = snap.get(ch) or {}
            print(f'      {ch}: status={d.get("status")} enabled={d.get("enabled")} '
                  f'advancing={d.get("advancing")} pcap_header_valid={d.get("pcap_header_valid")}')
        check('arm PCM_RX STARTING/enabled', snap.get('PCM_RX', {}).get('enabled') is True,
              snap.get('PCM_RX', {}).get('status'))
        check('arm PCM_TX STARTING/enabled', snap.get('PCM_TX', {}).get('enabled') is True,
              snap.get('PCM_TX', {}).get('status'))
        check('arm DEBUG enabled', snap.get('DEBUG', {}).get('enabled') is True)
        check('arm PCAP header valid', snap.get('PCAP', {}).get('pcap_header_valid') is True,
              snap.get('PCAP', {}).get('status'))
        check('arm LOG healthy', snap.get('LOG', {}).get('status') == 'HEALTHY')

        print('=== 3. snapshot (real has no in-memory state) ===')
        snap_empty = platform.snapshot('e2e-real')
        check('snapshot empty for real platform', snap_empty == {})

        print('=== 4. cleanup (PCM OFF via guard + debug OFF) ===')
        cleaned = platform.cleanup(
            session_id='e2e-real',
            device=_Device(),
            actions=['STOP_PCM_RX', 'STOP_PCM_TX', 'DISABLE_BASIC_VOIP_DEBUG',
                     'DISABLE_DTMF_DEBUG', 'DISABLE_DSP_DEBUG', 'DISABLE_SIP_PACKET_LOG'],
        )
        for ch in ('PCM_RX', 'PCM_TX', 'DEBUG'):
            d = cleaned.get(ch) or {}
            print(f'      {ch}: status={d.get("status")} quiet_verified={d.get("quiet_verified")} '
                  f'off_executed={d.get("off_executed")} packets_after={d.get("packets_after")}')
        # Both PCM channels must be quiet after cleanup (either skipped quiet or OFF verified).
        for ch in ('PCM_RX', 'PCM_TX'):
            d = cleaned.get(ch) or {}
            check(f'cleanup {ch} quiet', d.get('quiet_verified') is True and d.get('status') == 'STOPPED',
                  f'packets_after={d.get("packets_after")}')
        check('cleanup DEBUG off_verified', cleaned.get('DEBUG', {}).get('off_verified') is True)

    finally:
        platform.disconnect()

    print('\n=== RESULT ===')
    failed = [l for l, ok, _ in results if not ok]
    total = len(results)
    print(f'  {total - len(failed)}/{total} checks passed')
    if failed:
        print(f'  FAILED: {failed}')
        sys.exit(1)
    print('  REAL PLATFORM E2E OK')


if __name__ == '__main__':
    asyncio.run(main())
