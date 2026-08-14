"""Real-call PCM + PCAP verification on the APF3260-M (DB-provisioned credential).

During a real connected call (engineer dials a reachable number and keeps talking):
  1. open PCM RX (UDP 40000) + PCM TX (UDP 50000) via AIM,
  2. capture on br-lan_400 (udp port 40000 or 50000) for a window,
  3. expect real RTP/UDP packets on 40000/50000 while the call is active,
  4. single PCM OFF each (guard: probe quiet -> skip; active -> OFF once),
  5. debug off, root prompt intact.

Single loop (no platform bridge) so the shared asyncssh connection never deadlocks.
"""
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from app.collectors.asyncssh_adapter import AsyncSSHDeviceAdapter
from app.reproduction.fxs_event_monitor import FULL_DEBUG_ENABLE, FULL_DEBUG_DISABLE
from app.reproduction.pcm_cleanup import parse_tcpdump_packet_count

IP = '47.104.155.247'
PORT = 65157
SN = 'MACC1JZH3260M'
GW = '192.168.3.200'
IFACE = 'br-lan_400'
WATCH = 25  # capture window (engineer keeps the call active during this)


async def cli(adapter, cmd):
    r = await adapter.execute_cli(cmd)
    return r.stdout or r.stderr


async def probe(adapter, port, seconds=5):
    cmd = f"timeout -t {seconds} tcpdump -ni {IFACE} -c 1 'udp port {port}' 2>&1"
    r = await adapter.execute_shell(cmd)
    out = r.stdout or r.stderr
    try:
        return parse_tcpdump_packet_count(out)
    except ValueError:
        print('  RAW probe:', out[:160])
        return -1


def main():
    results = []

    def check(label, ok, detail=''):
        results.append((label, bool(ok), detail))
        print(f'  [{"PASS" if ok else "FAIL"}] {label}' + (f'  ({detail})' if detail else ''))

    async def run():
        from app.integrations.credentials import get_credential_provider
        provider = get_credential_provider()
        password = await provider.get_password(sn=SN, ip=IP)
        username = provider.resolve_username(ip=IP, fallback='root')

        adapter = AsyncSSHDeviceAdapter(ip=IP, port=PORT, username=username, password=password)
        await adapter.connect()
        check('SSH connected (DB credential)', True, f'{username}@{IP}:{PORT}')
        try:
            process = await adapter._ensure_aim_session(10)
            stream = process.stdout

            def write(cmd: str):
                process.stdin.write(cmd + '\n')

            # 1. full debug (so FXS/call state also streams; needed for call awareness)
            print('=== enabling full debug ===')
            for cmd in FULL_DEBUG_ENABLE:
                write(cmd)
            await asyncio.sleep(2)
            try:
                await asyncio.wait_for(stream.read(8192), 2)
            except asyncio.TimeoutError:
                pass

            # 2. open PCM RX/TX
            print('=== pcm_rx on / pcm_tx on ===')
            await cli(adapter, f'voip dsp diag set {GW} 40000 1 pcm_rx on')
            await cli(adapter, f'voip dsp diag set {GW} 50000 1 pcm_tx on')

            # 3. baseline probe (before call): expect 0
            b_rx = await probe(adapter, 40000)
            b_tx = await probe(adapter, 50000)
            print(f'  baseline before call: RX={b_rx} TX={b_tx}')

            # 4. capture window — engineer keeps a real call active
            print(f'\n=== CAPTURE {WATCH}s. PLEASE DIAL A REACHABLE NUMBER AND '
                  f'KEEP THE CALL ACTIVE (talking) ===\n')
            start = time.monotonic()
            count_rx = 0
            count_tx = 0
            saw_rtp = False
            samples = []
            while time.monotonic() - start < WATCH:
                try:
                    chunk = await asyncio.wait_for(stream.read(8192), 1.0)
                except asyncio.TimeoutError:
                    chunk = ''
                # count PCM packets via repeated short probes on both ports
                if int(time.monotonic() - start) % 4 == 0 and (not samples or time.monotonic() - samples[-1] > 3):
                    n = await probe(adapter, 40000, seconds=2)
                    m = await probe(adapter, 50000, seconds=2)
                    if n > 0:
                        count_rx += n
                    if m > 0:
                        count_tx += m
                    samples.append(time.monotonic())
                    print(f'  t={int(time.monotonic()-start)}s probe: RX={n} TX={m}')
                    if n > 0 or m > 0:
                        saw_rtp = True

            print('\n=== capture done ===')
            check('PCM RX traffic during call', count_rx > 0, f'{count_rx} packets')
            check('PCM TX traffic during call', count_tx > 0, f'{count_tx} packets')
            check('RTP/UDP media observed', saw_rtp)

            # 5. cleanup: single OFF per channel (guard semantics)
            print('=== cleanup: PCM off (single) + debug off ===')
            a_rx = await probe(adapter, 40000)
            a_tx = await probe(adapter, 50000)
            if a_rx > 0:
                print('  RX active -> single off')
                await cli(adapter, f'voip dsp diag set {GW} 40000 1 pcm_rx off')
            if a_tx > 0:
                print('  TX active -> single off')
                await cli(adapter, f'voip dsp diag set {GW} 50000 1 pcm_tx off')
            q_rx = await probe(adapter, 40000)
            q_tx = await probe(adapter, 50000)
            check('PCM quiet after cleanup', q_rx == 0 and q_tx == 0, f'RX={q_rx} TX={q_tx}')
            for cmd in FULL_DEBUG_DISABLE:
                write(cmd)
            await asyncio.sleep(1)
            try:
                await asyncio.wait_for(stream.read(4096), 1.5)
            except asyncio.TimeoutError:
                pass
            try:
                await adapter.execute_cli('sys show bind-if')
                check('root prompt intact', True)
            except Exception as exc:
                check('root prompt intact', False, str(exc)[:80])
        finally:
            try:
                await adapter.disconnect()
            except Exception:
                pass

    asyncio.run(run())

    print('\n=== RESULT ===')
    failed = [l for l, ok, _ in results if not ok]
    print(f'  {len(results) - len(failed)}/{len(results)} checks passed')
    if failed:
        print(f'  FAILED: {failed}')
        sys.exit(1)
    print('  PCM/PCAP REAL CALL VERIFY OK')


if __name__ == '__main__':
    main()
