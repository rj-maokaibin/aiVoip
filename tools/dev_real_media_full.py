"""Real-call full media diagnosis: capture PCM mirror + RTP, run MediaIntelligenceEngine.

During an ACTIVE call (engineer dials and talks, ideally presses a few DTMF keys)
this captures the PCM mirror streams (UDP 40000/50000) plus RTP on the voice
interface, then runs MediaIntelligenceEngine (PCM decode + RTP + audio quality) and
saves the real pcap as a golden artifact under /tmp.

Usage (inside backend container, /tools mounted):
    python /tools/dev_real_media_full.py
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
WATCH = 60


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
            for cmd in (f'voip dsp diag set {GW} 40000 1 pcm_rx on',
                        f'voip dsp diag set {GW} 50000 1 pcm_tx on'):
                for attempt in range(3):
                    try:
                        await adapter.execute_cli(cmd)
                        break
                    except Exception:
                        await asyncio.sleep(2)
            await asyncio.sleep(1)
            from app.reproduction.pcm_cleanup import parse_tcpdump_packet_count
            print(f'=== WATCHING {WATCH}s. DIAL + TALK + PRESS A FEW DTMF KEYS ===')
            started = time.monotonic()
            pcap_bytes = b''
            while time.monotonic() - started < WATCH:
                got = 0
                for port in (40000, 50000):
                    r = await adapter.execute_shell(
                        f"timeout -t 2 tcpdump -ni {IFACE} -c 1 'udp port {port}' 2>&1")
                    try:
                        got += parse_tcpdump_packet_count(r.stdout or r.stderr)
                    except ValueError:
                        pass
                if got > 0:
                    print(f'  t={int(time.monotonic()-started)}s media -> capturing 10s')
                    rc = await adapter.execute_shell(
                        f"rm -f /tmp/aiVoip_media.pcap; timeout -t 10 tcpdump -ni {IFACE} -w /tmp/aiVoip_media.pcap "
                        f"'udp' >/dev/null 2>&1; base64 /tmp/aiVoip_media.pcap 2>/dev/null || true",
                        timeout=25)
                    pcap_bytes = base64.b64decode((rc.stdout or '').strip())
                    break
                await asyncio.sleep(2)
            check('captured pcap', len(pcap_bytes) > 24, f'{len(pcap_bytes)} bytes')
            if len(pcap_bytes) > 24:
                p = Path('/tmp/aiVoip_media.pcap')
                p.write_bytes(pcap_bytes)
                # golden copy outside container /tmp for later inspection
                from app.db.session import SessionLocal
                from app.integrations.storage import reproduction_object_storage
                try:
                    st = reproduction_object_storage()
                    st.put_bytes('golden/real_call_20260814.pcap', pcap_bytes, 'application/vnd.tcpdump.pcap')
                    print('  golden saved: golden/real_call_20260814.pcap')
                except Exception as e:
                    print('  golden save skipped:', type(e).__name__, str(e)[:80])
                from app.analyzers.pcm.pcap_udp import iter_udp_datagrams
                from app.analyzers.pcm import load_pcm_profile
                from app.analyzers.media.engine import MediaIntelligenceEngine
                from app.analyzers.packet import TSharkAdapter
                pkts = list(iter_udp_datagrams(p))
                check('udp datagrams', len(pkts) > 0, f'{len(pkts)}')
                profile = load_pcm_profile(
                    '/app/profiles/pcm/ruijie_aim_diag_v1.yaml'
                    if Path('/app/profiles/pcm/ruijie_aim_diag_v1.yaml').exists()
                    else 'profiles/pcm/ruijie_aim_diag_v1.yaml')
                engine = MediaIntelligenceEngine(profile, TSharkAdapter())
                res = engine.analyze_pcap(p, '/tmp/media_full_out')
                summ = res.get('summary') or {}
                print('  Media status:', res.get('status'), 'degraded:', res.get('degraded_reason'))
                print('  summary:', summ)
                check('media analyzer ran', res.get('status') in ('SUCCESS', 'PARTIAL_SUCCESS'), res.get('status'))
                check('rtp stream surfaced', summ.get('rtp_stream_count', 0) >= 1,
                      f"rtp={summ.get('rtp_stream_count')} tracks={summ.get('decoded_rtp_track_count')}")
                check('pcm sessions surfaced', summ.get('pcm_session_count', 0) >= 1,
                      f"pcm_sessions={summ.get('pcm_session_count')}")
                check('dtmf or audio events surfaced',
                      summ.get('dtmf_event_count', 0) > 0 or summ.get('timeline_event_count', 0) > 0,
                      f"dtmf={summ.get('dtmf_event_count')} timeline={summ.get('timeline_event_count')}")
        finally:
            for cmd in (f'voip dsp diag set {GW} 40000 1 pcm_rx off',
                        f'voip dsp diag set {GW} 50000 1 pcm_tx off'):
                try:
                    await adapter.execute_cli(cmd)
                except Exception:
                    pass
            await adapter.execute_shell('rm -f /tmp/aiVoip_media.pcap')
            await adapter.disconnect()

    asyncio.run(run())

    print('\n=== RESULT ===')
    failed = [l for l, ok, _ in results if not ok]
    print(f'  {len(results) - len(failed)}/{len(results)} checks passed')
    if failed:
        print(f'  FAILED: {failed}')
        sys.exit(1)
    print('  REAL MEDIA FULL OK')


if __name__ == '__main__':
    main()
