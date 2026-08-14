"""Full real-DUT end-to-end verification (APF3260-M), one coherent flow.

Verifies the complete autonomous-reproduction chain on the real device:
  1. SSH connect with the DB-provisioned (Poseidon) credential,
  2. voice runtime context (VLAN 400 / br-lan_400 / gateway),
  3. full debug on,
  4. FXS events from a real hook/dial/hangup cycle (stage A),
  5. PCM RX/TX real traffic during a connected call (stage B),
  6. clean shutdown (single PCM OFF per channel via guard semantics, debug off),
  7. evidence artifacts persisted in the DB.

Single event loop throughout (no platform bridge) so the shared asyncssh connection
never deadlocks. The engineer performs two phone actions at the prompts.
"""
import asyncio
import sys
import time
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from app.collectors.asyncssh_adapter import AsyncSSHDeviceAdapter
from app.reproduction.fxs_event_monitor import FxsEventMonitor, FULL_DEBUG_ENABLE, FULL_DEBUG_DISABLE
from app.reproduction.pcm_cleanup import parse_tcpdump_packet_count

IP = '47.104.155.247'
PORT = 65157
SN = 'MACC1JZH3260M'
GW = '192.168.3.200'
IFACE = 'br-lan_400'
FXS_WATCH = 60
PCM_WATCH = 30

report = {}


async def cli(adapter, cmd):
    r = await adapter.execute_cli(cmd)
    return r.stdout or r.stderr


async def shell(adapter, cmd):
    r = await adapter.execute_shell(cmd)
    return r.stdout or r.stderr


async def probe(adapter, port, seconds=5):
    out = await shell(adapter, f"timeout -t {seconds} tcpdump -ni {IFACE} -c 1 'udp port {port}' 2>&1")
    try:
        return parse_tcpdump_packet_count(out)
    except ValueError:
        return -1


async def main():
    from app.integrations.credentials import get_credential_provider
    provider = get_credential_provider()
    password = await provider.get_password(sn=SN, ip=IP)
    username = provider.resolve_username(ip=IP, fallback='root')

    adapter = AsyncSSHDeviceAdapter(ip=IP, port=PORT, username=username, password=password)
    await adapter.connect()
    report['ssh'] = {'connected': True, 'username': username, 'ip': IP, 'port': PORT,
                     'credential_source': 'device_credentials(poseidon)'}
    print('[1/7] SSH connected (DB Poseidon credential) OK')
    try:
        # --- 2. voice runtime context ---
        print('[2/7] resolving voice runtime context...')
        vlan = await shell(adapter, 'dev_config get -m voice_vlan')
        svc = await shell(adapter, 'dev_config get -m voipServInfo')
        links = await shell(adapter, 'ip -o link show br-lan_400 2>/dev/null')
        iface_up = 'br-lan_400' in links and 'UP' in links
        report['voice_context'] = {
            'vlan_raw': vlan.strip()[:120],
            'gateway_raw': svc.strip()[:120],
            'br-lan_400_up': iface_up,
        }
        print(f'  VLAN={vlan.strip()[:60]}')
        print(f'  GW={svc.strip()[:80]}')
        print(f'  br-lan_400 up={iface_up}')

        aim = await shell(adapter, 'ps w | grep -E "[a]im" | head -2')
        report['aim_process'] = aim.strip()[:120]
        print('  aim process present:', 'aim' in aim)

        process = await adapter._ensure_aim_session(10)
        stream = process.stdout

        def write(cmd: str):
            process.stdin.write(cmd + '\n')

        # --- 3. full debug on ---
        print('[3/7] enabling full debug...')
        for cmd in FULL_DEBUG_ENABLE:
            write(cmd)
        await asyncio.sleep(2)
        try:
            await asyncio.wait_for(stream.read(8192), 2)
        except asyncio.TimeoutError:
            pass

        # --- 4. Stage A: FXS events from a real hook cycle ---
        print(f'\n[4/7] STAGE A: LISTENING {FXS_WATCH}s for FXS events.')
        print('      >>> DO NOW: OFF-HOOK -> dial a few digits -> ON-HOOK <<<\n')
        clock = [0]
        def relclock():
            clock[0] += 500
            return clock[0]
        monitor = FxsEventMonitor(read_aim_chunk=lambda: None, write_aim=write, relative_ms=relclock)
        monitor.start()
        events = []
        start = time.monotonic()
        while time.monotonic() - start < FXS_WATCH:
            try:
                chunk = await asyncio.wait_for(stream.read(8192), 1.0)
            except asyncio.TimeoutError:
                chunk = ''
            except Exception:
                break
            if chunk:
                for ev in monitor.feed(chunk):
                    events.append(ev)
                    print(f'  >>> FXS {ev.event}' + (f'<{ev.digit}>' if ev.digit else '') +
                          f'  @ {ev.timestamp}')
            if any(e.event == 'OFFHOOK' for e in events) and any(e.event == 'ONHOOK' for e in events):
                print('  (full hook cycle captured)')
                break
        monitor.stop()
        offs = [e for e in events if e.event == 'OFFHOOK']
        dtmfs = [e for e in events if e.event == 'DTMF']
        ons = [e for e in events if e.event == 'ONHOOK']
        report['fxs_events'] = {
            'offhook': len(offs), 'dtmf': len(dtmfs), 'onhook': len(ons),
            'sample': [{'event': e.event, 'digit': e.digit, 'ts': e.timestamp} for e in events[:10]],
        }
        print(f'  FXS: OFFHOOK={len(offs)} DTMF={len(dtmfs)} ONHOOK={len(ons)}')

        # --- 5. Stage B: PCM real traffic during a connected call ---
        print(f'\n[5/7] STAGE B: CAPTURE {PCM_WATCH}s while a real call is ACTIVE.')
        print('      >>> DO NOW: DIAL A REACHABLE NUMBER AND KEEP TALKING <<<\n')
        print('  opening PCM RX/TX...')
        await cli(adapter, f'voip dsp diag set {GW} 40000 1 pcm_rx on')
        await cli(adapter, f'voip dsp diag set {GW} 50000 1 pcm_tx on')
        baseline_rx = await probe(adapter, 40000)
        baseline_tx = await probe(adapter, 50000)
        print(f'  baseline (pre-call): RX={baseline_rx} TX={baseline_tx}')
        pcm_rx = 0
        pcm_tx = 0
        last = 0
        start = time.monotonic()
        while time.monotonic() - start < PCM_WATCH:
            try:
                chunk = await asyncio.wait_for(stream.read(8192), 1.0)
            except asyncio.TimeoutError:
                chunk = ''
            if time.monotonic() - last > 4:
                n = await probe(adapter, 40000, seconds=2)
                m = await probe(adapter, 50000, seconds=2)
                if n > 0:
                    pcm_rx += n
                if m > 0:
                    pcm_tx += m
                last = time.monotonic()
                print(f'  t={int(time.monotonic()-start)}s probe: RX={n} TX={m}')
        report['pcm'] = {'rx_packets': pcm_rx, 'tx_packets': pcm_tx,
                         'baseline': {'rx': baseline_rx, 'tx': baseline_tx}}
        print(f'  PCM during call: RX={pcm_rx} TX={pcm_tx}')

        # --- 6. cleanup ---
        print('[6/7] cleanup: single PCM OFF per channel + debug off...')
        a_rx = await probe(adapter, 40000)
        a_tx = await probe(adapter, 50000)
        if a_rx > 0:
            await cli(adapter, f'voip dsp diag set {GW} 40000 1 pcm_rx off')
        if a_tx > 0:
            await cli(adapter, f'voip dsp diag set {GW} 50000 1 pcm_tx off')
        q_rx = await probe(adapter, 40000)
        q_tx = await probe(adapter, 50000)
        report['cleanup'] = {'rx_quiet': q_rx == 0, 'tx_quiet': q_tx == 0, 'rx_after': q_rx, 'tx_after': q_tx}
        print(f'  PCM quiet after cleanup: RX={q_rx} TX={q_tx}')
        for cmd in FULL_DEBUG_DISABLE:
            write(cmd)
        await asyncio.sleep(1)
        try:
            await asyncio.wait_for(stream.read(4096), 1.5)
        except asyncio.TimeoutError:
            pass
        try:
            await adapter.execute_cli('sys show bind-if')
            report['root_prompt'] = 'intact'
            print('  root prompt intact')
        except Exception as exc:
            report['root_prompt'] = f'broken:{exc}'
            print('  root prompt BROKEN')

        # --- 7. evidence in DB ---
        print('[7/7] checking DB evidence...')
        from app.db.session import SessionLocal
        from app.db.models import ReproductionSession, ReproductionCaptureSegment, Case
        from sqlalchemy import select
        db = SessionLocal()
        try:
            case = db.scalar(select(Case).where(Case.case_no.like('PHONE-E2E-%')).order_by(Case.created_at.desc()))
            segs = 0
            if case:
                sess = db.scalar(select(ReproductionSession).where(ReproductionSession.case_id == case.id))
                if sess:
                    segs = db.query(ReproductionCaptureSegment).filter(
                        ReproductionCaptureSegment.session_id == sess.id).count()
                    report['evidence'] = {'segments': segs, 'session_state': sess.state}
                    print(f'  session={sess.state} segments={segs}')
        finally:
            db.close()
    finally:
        try:
            await adapter.disconnect()
        except Exception:
            pass


def _verdict() -> dict:
    s = report.get('ssh', {}).get('connected', False)
    ctx = report.get('voice_context', {}).get('br-lan_400_up', False)
    fxs = report.get('fxs_events', {})
    pcm = report.get('pcm', {})
    cl = report.get('cleanup', {})
    return {
        'overall': all([s, ctx, fxs.get('offhook', 0) > 0, fxs.get('onhook', 0) > 0,
                        pcm.get('rx_packets', 0) > 0 or pcm.get('tx_packets', 0) > 0,
                        cl.get('rx_quiet'), cl.get('tx_quiet')]),
        'ssh': s, 'voice_context': ctx,
        'fxs_cycle': fxs.get('offhook', 0) > 0 and fxs.get('onhook', 0) > 0,
        'pcm_traffic': pcm.get('rx_packets', 0) > 0 or pcm.get('tx_packets', 0) > 0,
        'cleanup_quiet': cl.get('rx_quiet') and cl.get('tx_quiet'),
    }


if __name__ == '__main__':
    asyncio.run(main())
    v = _verdict()
    print('\n================ FULL-FLOW VERDICT ================')
    for k, ok in v.items():
        print(f'  [{"PASS" if ok else "FAIL"}] {k}')
    print('  overall:', 'PASS' if v['overall'] else 'FAIL')
    Path('/tmp/full_flow_report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    print('  report saved: /tmp/full_flow_report.json')
