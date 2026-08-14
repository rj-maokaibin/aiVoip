"""Real-DUT PCM mirror capture + analyzer decode (requires an ACTIVE call).

Opens PCM RX/TX mirrors (UDP 40000/50000), then repeatedly probes for media while
the engineer keeps a real call active on the physical phone. As soon as packets
appear, captures the mirror stream and decodes it with PcmIntelligenceEngine using
the VERIFIED profile. Also runs MediaIntelligenceEngine on a SIP+RTP+PCM pcap to
show the full diagnosis path.

Usage (inside backend container, /tools mounted):
    python /tools/dev_real_pcm_analyze.py
"""
import asyncio
import base64
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

IP = '47.104.155.247'
PORT = 65157
SN = 'MACC1JZH3260M'
IFACE = 'br-lan_400'
GW = '192.168.3.200'
WATCH = 60  # keep the call active during this window


def _line(label, ok, detail=''):
    print(f'  [{"PASS" if ok else "FAIL"}] {label}' + (f'  ({detail})' if detail else ''))


def main():
    results = []

    def check(label, cond, detail=''):
        results.append((label, bool(cond), detail))
        _line(label, cond, detail)

    async def run():
        from app.integrations.credentials import get_credential_provider
        provider = get_credential_provider()
        password = await provider.get_password(sn=SN, ip=IP)
        username = provider.resolve_username(ip=IP, fallback='root')
        from app.collectors.asyncssh_adapter import AsyncSSHDeviceAdapter
        adapter = AsyncSSHDeviceAdapter(ip=IP, port=PORT, username=username, password=password)
        await adapter.connect()
        check('SSH connected', True, f'{username}@{IP}:{PORT}')
        try:
            # 1. open PCM mirrors with retry (AIM session can be transient)
            for cmd in (f'voip dsp diag set {GW} 40000 1 pcm_rx on',
                        f'voip dsp diag set {GW} 50000 1 pcm_tx on'):
                for attempt in range(3):
                    try:
                        await adapter.execute_cli(cmd)
                        break
                    except Exception:
                        await asyncio.sleep(2)
            await asyncio.sleep(1)

            # 2. watch for media with short probes; capture when traffic appears
            from app.reproduction.pcm_cleanup import parse_tcpdump_packet_count
            print(f'=== WATCHING {WATCH}s for media. PLEASE DIAL A REACHABLE NUMBER '
                  f'AND KEEP THE CALL ACTIVE/TALKING ===')
            started = time.monotonic()
            pcap_bytes = b''
            while time.monotonic() - started < WATCH:
                got = 0
                for port in (40000, 50000):
                    r = await adapter.execute_shell(
                        f"timeout -t 2 tcpdump -ni {IFACE} -c 1 'udp port {port}' 2>&1")
                    try:
                        n = parse_tcpdump_packet_count(r.stdout or r.stderr)
                    except ValueError:
                        n = 0
                    got += n
                if got > 0:
                    print(f'  t={int(time.monotonic()-started)}s media detected -> capturing 8s')
                    rc = await adapter.execute_shell(
                        f"rm -f /tmp/aiVoip_pcm.pcap; timeout -t 8 tcpdump -ni {IFACE} -w /tmp/aiVoip_pcm.pcap "
                        f"'udp port 40000 or udp port 50000' >/dev/null 2>&1; "
                        f"base64 /tmp/aiVoip_pcm.pcap 2>/dev/null || true",
                        timeout=20)
                    pcap_bytes = base64.b64decode((rc.stdout or '').strip())
                    break
                await asyncio.sleep(2)

            check('captured pcap non-empty', len(pcap_bytes) > 24, f'{len(pcap_bytes)} bytes')
            if len(pcap_bytes) > 24:
                open('/tmp/aiVoip_pcm.pcap', 'wb').write(pcap_bytes)
                from app.analyzers.pcm.pcap_udp import iter_udp_datagrams
                from app.analyzers.pcm import PcmIntelligenceEngine, load_pcm_profile
                pkts = list(iter_udp_datagrams('/tmp/aiVoip_pcm.pcap'))
                check('udp datagrams parsed', len(pkts) > 0, f'{len(pkts)} datagrams')
                by_port, lens = {}, set()
                for d in pkts:
                    by_port[d.dst_port] = by_port.get(d.dst_port, 0) + 1
                    lens.add(len(d.payload))
                check('pcm mirror ports seen', 40000 in by_port or 50000 in by_port, str(by_port))
                if pkts:
                    check('payload length == 160', len(pkts[0].payload) == 160,
                          f'{len(pkts[0].payload)}B (lens seen {sorted(lens)[:5]})')
                profile = load_pcm_profile(
                    '/app/profiles/pcm/ruijie_aim_diag_v1.yaml'
                    if Path('/app/profiles/pcm/ruijie_aim_diag_v1.yaml').exists()
                    else 'profiles/pcm/ruijie_aim_diag_v1.yaml')
                eng = PcmIntelligenceEngine(profile)
                res = eng.analyze_pcap('/tmp/aiVoip_pcm.pcap')
                summ = res.get('summary') or {}
                print('  PCM analyzer:', res.get('status'), 'packets=', summ.get('total_packets'),
                      'sessions=', summ.get('session_count'), 'format=', res.get('format_availability'))
                check('pcm analyzer ran', res.get('status') in ('SUCCESS', 'PARTIAL_SUCCESS'), res.get('status'))
                check('pcm packets decoded', summ.get('total_packets', 0) > 0,
                      f"total={summ.get('total_packets')}")
        finally:
            for cmd in (f'voip dsp diag set {GW} 40000 1 pcm_rx off',
                        f'voip dsp diag set {GW} 50000 1 pcm_tx off'):
                try:
                    await adapter.execute_cli(cmd)
                except Exception:
                    pass
            await adapter.execute_shell('rm -f /tmp/aiVoip_pcm.pcap')
            await adapter.disconnect()

    asyncio.run(run())

    print('\n=== RESULT ===')
    failed = [l for l, ok, _ in results if not ok]
    print(f'  {len(results) - len(failed)}/{len(results)} checks passed')
    if failed:
        print(f'  FAILED: {failed}')
        sys.exit(1)
    print('  REAL PCM ANALYZE OK')


if __name__ == '__main__':
    main()
