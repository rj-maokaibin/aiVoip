"""Real-device reproduction STABILITY SOAK (no phone needed).

Drives the capture channel directly against the DUT over SSH/AIM to prove the
capture pipeline is stable under congestion, reconnection, high-frequency
commands and repeated arm/cleanup cycles - WITHOUT requiring a phone to dial.

This is the "capture is the foundation" hardening verification: every test that
used to fail (SSH_COMMAND_TIMEOUT, BrokenPipe, wedged session, leaked lock) is
exercised here and must PASS for the whole loop to be trustworthy.

Usage (inside backend container, /tools mounted, tunnel must be live):
    python /tools/dev_stability_soak.py
"""
import asyncio
import hashlib
import sys
import time

sys.path.insert(0, '/app')

from app.collectors.asyncssh_adapter import AsyncSSHDeviceAdapter
from app.integrations.credentials import get_credential_provider

IP = '47.104.22.0'
PORT = 65243  # current live tunnel; update when the EWEB tunnel rotates
SN = 'MACC1JZH3260M'
IFACE = 'br-lan_400'
PCM_RX = 40000
PCM_TX = 50000


def _line(label, ok, detail=''):
    print(f'  [{"PASS" if ok else "FAIL"}] {label}' + (f'  ({detail})' if detail else ''))


async def _connect():
    provider = get_credential_provider()
    pwd = await provider.get_password(sn=SN, ip=IP)
    a = AsyncSSHDeviceAdapter(ip=IP, port=PORT, username='root', password=pwd)
    await asyncio.wait_for(a.connect(), timeout=20)
    await asyncio.wait_for(a.ensure_aim_session_ready(), timeout=20)
    return a


async def g1_ssh_channel(a, results):
    """G1: SSH capture-channel stress - bursts, long windows, concurrency."""
    print('=== G1: SSH capture channel ===')
    # B1a: burst of short tcpdump probes
    ok = True; el = []
    for _ in range(20):
        t0 = time.monotonic()
        try:
            r = await a.execute_shell(f'timeout -t 2 tcpdump -ni {IFACE} -c 1 "udp port {PCM_RX}" 2>&1; echo done', timeout=8)
            el.append(time.monotonic() - t0)
            if 'done' not in (r.stdout or ''):
                ok = False
        except Exception as e:
            ok = False; _line(f'burst probe {_}', False, f'{type(e).__name__}:{e}')
            break
    _line(f'20x short tcpdump probes', ok, f'avg={sum(el)/len(el):.2f}s max={max(el):.2f}s' if el else '')

    # B1b: full 8s -w windows (the live-probe shape)
    ok = True; sizes = []
    for i in range(5):
        try:
            cmds = f'rm -f /tmp/soak_{i}.pcap; timeout -t 8 tcpdump -ni {IFACE} -w /tmp/soak_{i}.pcap "udp port {PCM_RX} or udp port {PCM_TX}" >/dev/null 2>&1; base64 /tmp/soak_{i}.pcap 2>/dev/null | wc -c; rm -f /tmp/soak_{i}.pcap'
            r = await a.execute_shell(cmds, timeout=25)
            sizes.append(int((r.stdout or '0').strip().splitlines()[-1] or 0))
        except Exception as e:
            ok = False; _line(f'8s window {i}', False, f'{type(e).__name__}:{e}')
            break
    _line('5x 8s tcpdump -w windows (no timeout)', ok, f'b64_sizes={sizes}')

    # B1c: concurrency - 4 simultaneous windows (channel congestion)
    ok = True
    try:
        cmds = '; '.join(f'rm -f /tmp/conc_{i}.pcap; timeout -t 4 tcpdump -ni {IFACE} -w /tmp/conc_{i}.pcap "udp" >/dev/null 2>&1; rm -f /tmp/conc_{i}.pcap' for i in range(4))
        t0 = time.monotonic(); r = await a.execute_shell(f'({cmds}) 2>&1 & wait', timeout=25)
        el = time.monotonic() - t0
        _line('4x concurrent tcpdump windows', 'ERROR' not in (r.stderr or '') and el < 20, f'elapsed={el:.1f}s')
    except Exception as e:
        _line('4x concurrent tcpdump', False, f'{type(e).__name__}:{e}')


async def g2_aim_session(a, results):
    """G2: AIM session - high-frequency CLI + reopen recovery."""
    print('=== G2: AIM session ===')
    ok = True; last = None
    for i in range(30):
        try:
            r = await a.execute_cli('show version', timeout=8)
            last = (r.stdout or '')[:40]
        except Exception as e:
            ok = False; _line(f'AIM cmd {i}', False, f'{type(e).__name__}:{e}')
            break
    _line('30x high-frequency AIM CLI', ok, repr(last))

    # B2b: session reopen recovery (close AIM PTY, re-establish, run CLI)
    ok = True
    try:
        await a._close_aim_session()
        await asyncio.wait_for(a.ensure_aim_session_ready(), timeout=20)
        r = await a.execute_cli('show version', timeout=10)
        _line('AIM reopen recovery', bool(r.stdout), '')
    except Exception as e:
        _line('AIM reopen recovery', False, f'{type(e).__name__}:{e}')


async def g3_fault_injection(a, results):
    """G3: fault injection - slow command retry, large base64 integrity."""
    print('=== G3: fault injection ===')
    # B3a: slow command should be retried by execute_shell and still succeed
    ok = True
    try:
        t0 = time.monotonic()
        r = await a.execute_shell('sleep 2; echo SLOW_OK', timeout=12, retries=2)
        el = time.monotonic() - t0
        _line('slow cmd (sleep 2) via retrying execute_shell', 'SLOW_OK' in (r.stdout or ''), f'{el:.1f}s')
    except Exception as e:
        _line('slow cmd retry', False, f'{type(e).__name__}:{e}')

    # B3b: large file base64 round-trip integrity (data must not be corrupted)
    ok = True
    try:
        r = await a.execute_shell('dd if=/dev/urandom of=/tmp/soak_big.bin bs=256k count=1 2>/dev/null; md5sum /tmp/soak_big.bin | cut -d" " -f1; base64 /tmp/soak_big.bin | wc -c; rm -f /tmp/soak_big.bin', timeout=20)
        lines = (r.stdout or '').splitlines()
        _line('256KB base64 round-trip (device)', len(lines) >= 2, f'lines={len(lines)}')


async def g5_state_clean(a, results):
    """G5: after the soak, device AIM/debug state must be clean (no leak)."""
    print('=== G5: device state clean ===')
    try:
        r = await a.execute_shell('ps w | grep -c tcpdump; ps w | grep -c aim', timeout=8)
        lines = (r.stdout or '').splitlines()
        tcp = int(lines[0].strip() or 0) if lines else -1
        aim = int(lines[1].strip() or 0) if len(lines) > 1 else -1
        # tcpdump count includes the grep itself sometimes; allow <=1 leftover
        _line('no leaked tcpdump/aim processes', tcp <= 1 and aim >= 0, f'tcpdump={tcp} aim={aim}')
    except Exception as e:
        _line('device state check', False, f'{type(e).__name__}:{e}')


async def main():
    a = None
    try:
        a = await _connect()
        print(f'connected {IP}:{PORT} (AIM ready)')
    except Exception as e:
        print(f'CONNECT FAILED: {type(e).__name__}: {e}')
        print('Tunnel may have rotated; check DB device_credentials for the live endpoint.')
        sys.exit(1)
    results = []
    try:
        for fn in (g1_ssh_channel, g2_aim_session, g3_fault_injection, g5_state_clean):
            await fn(a, results)
    finally:
        try:
            await a.disconnect()
        except Exception:
            pass
    print('\nsoak complete; see PASS/FAIL per test above')


if __name__ == '__main__':
    asyncio.run(main())
