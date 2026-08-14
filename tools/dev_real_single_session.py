"""Real-device SINGLE-SESSION full chain: real arm + real events + real cleanup.

This is the production-shaped end-to-end that the previous phone E2E could not do:
the FULL state machine is driven by the real ``RealReproductionPlatform`` (not the
mock), and FXS events are consumed through the platform's bridge-loop AIM reader
queue -- so there is only ONE event loop owning the asyncssh connection.

Flow:
    connect (bridge loop) -> orchestrator.start() (REAL arm: PCM RX/TX ON + full
    debug + PCAP probe) -> start_fxs_monitor (bridge-loop reader) -> physical phone
    OFF-HOOK / dial / ON-HOOK drives the state machine -> orchestrator.cancel()
    (REAL cleanup: PCM OFF guarded + debug OFF + PCAP close).

Usage (inside backend container, /tools mounted):
    python /tools/dev_real_single_session.py
"""
import asyncio
import sys
import time
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from app.db.models import Case, CaseDevice
from app.db.session import SessionLocal
from app.reproduction.platform_factory import build_orchestrator, resolve_platform_mode

SN = 'MACC1JZH3260M'
IP = '47.104.155.247'
PORT = 65157
WATCH_SECONDS = 90


def _line(label, ok, detail=''):
    print(f'  [{"PASS" if ok else "FAIL"}] {label}' + (f'  ({detail})' if detail else ''))


def main():
    results = []

    def check(label, cond, detail=''):
        results.append((label, bool(cond), detail))
        _line(label, cond, detail)

    assert resolve_platform_mode() == 'real', 'REPRODUCTION_PLATFORM_MODE must be real'

    # Resolve the DUT credential from the DB (provisioned via Feishu/Poseidon).
    from app.integrations.credentials import get_credential_provider
    provider = get_credential_provider()
    password = asyncio.run(provider.get_password(sn=SN, ip=IP))
    username = provider.resolve_username(ip=IP, fallback='root')

    from app.collectors.asyncssh_adapter import AsyncSSHDeviceAdapter
    adapter = AsyncSSHDeviceAdapter(ip=IP, port=PORT, username=username, password=password)
    orch, close = build_orchestrator(adapter=adapter, connect=True)
    platform = orch.platform
    check('platform is REAL (not mock)', type(platform).__name__ == 'RealReproductionPlatform',
          type(platform).__name__)

    db = SessionLocal()
    try:
        case = Case(case_no=f'REAL-SINGLE-{uuid4().hex[:8]}', summary='real single-session full chain', status='ANALYZING')
        db.add(case); db.flush()
        device = CaseDevice(case_id=case.id, ip=IP, ssh_port=PORT, sn=SN,
                            username=username, device_info={})
        db.add(device); db.flush()

        print('=== create reproduction session (VOIP_GENERIC_FULL_CAPTURE) ===')
        session = orch.create_session(db, case_id=case.id, profile_id='VOIP_GENERIC_FULL_CAPTURE',
                                      device_id=device.id, actor='real-single-session')
        db.commit()

        print('=== start -> REAL arm (PCM ON + full debug + PCAP probe) ===')
        session = orch.start(db, session=session, owner_worker='real-single-session', actor='real-single-session')
        db.commit()
        from app.contracts.enums import ReproductionState
        check('armed -> WATCHING', ReproductionState(session.state) == ReproductionState.WATCHING, session.state)
        # arm snapshot was persisted by the real platform: PCM_RX/TX enabled + debug on.
        from app.db.models import CaptureChannelHealth
        chans = {c.channel: c for c in db.query(CaptureChannelHealth).filter(
            CaptureChannelHealth.session_id == session.id)}
        for ch in ('PCM_RX', 'PCM_TX'):
            row = chans.get(ch)
            healthy = row is not None and row.status == 'HEALTHY'
            hjson = (row.health_json or {}) if row is not None else {}
            check(f'real arm {ch} healthy', healthy, f'status={getattr(row, "status", None)} enabled={hjson.get("enabled")}')
        check('real arm DEBUG healthy', chans.get('DEBUG') is not None and chans['DEBUG'].status == 'HEALTHY',
              f'status={getattr(chans.get("DEBUG"), "status", None)}')

        print(f'\n=== LISTENING {WATCH_SECONDS}s. PLEASE DO NOW: OFF-HOOK -> dial digits -> ON-HOOK ===\n')
        # Start the bridge-loop AIM reader; all asyncssh I/O stays on the bridge loop.
        monitor = platform.start_fxs_monitor()
        orch.fxs_event_monitor = monitor
        events = []
        started = time.monotonic()
        try:
            while time.monotonic() - started < WATCH_SECONDS:
                for ev in monitor.poll():
                    events.append(ev)
                    print(f'  >>> {ev.timestamp} [line {ev.line}] {ev.event}' + (f'<{ev.digit}>' if ev.digit else ''))
                    handled = orch.record_fxs_event(db, session=session, event=ev, actor='real-single-session')
                    if handled is not None:
                        print(f'      -> orchestrator handled (attempt state={session.state})')
                    db.commit()
                has_off = any(e.event == 'OFFHOOK' for e in events)
                has_on = any(e.event == 'ONHOOK' for e in events)
                if has_off and has_on:
                    print('  (full hook cycle captured)')
                    break
                time.sleep(0.2)
        finally:
            platform.stop_fxs_monitor()

        check('captured OFFHOOK', any(e.event == 'OFFHOOK' for e in events),
              f'{sum(1 for e in events if e.event=="OFFHOOK")}')
        check('captured DTMF', any(e.event == 'DTMF' for e in events),
              f'{sum(1 for e in events if e.event=="DTMF")}')
        check('captured ONHOOK', any(e.event == 'ONHOOK' for e in events),
              f'{sum(1 for e in events if e.event=="ONHOOK")}')

        # Evidence + REAL cleanup via orchestrator.cancel (guard + debug off).
        from app.db.models import ReproductionCaptureSegment
        segs = db.query(ReproductionCaptureSegment).filter(ReproductionCaptureSegment.session_id == session.id).count()
        check('capture segments written', segs > 0, f'segments={segs}')

        print('=== cancel -> REAL cleanup (PCM OFF guarded + debug OFF) ===')
        session = orch.cancel(db, session=session, actor='real-single-session')
        db.commit()
        check('cleanup verified', session.cleanup_status == 'CLEANUP_VERIFIED',
              f'status={session.cleanup_status} state={session.state}')
    finally:
        db.close()
        close()

    print('\n=== RESULT ===')
    failed = [l for l, ok, _ in results if not ok]
    print(f'  {len(results) - len(failed)}/{len(results)} checks passed')
    if failed:
        print(f'  FAILED: {failed}')
        sys.exit(1)
    print('  REAL SINGLE-SESSION FULL CHAIN OK')


if __name__ == '__main__':
    main()
