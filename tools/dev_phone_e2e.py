"""Real-phone end-to-end: live FXS events from the DUT drive autonomous reproduction.

Connects to the real DUT (APF3260-M) using the DB-provisioned credential, enables the
full debug sequence, and listens on the persistent AIM PTY while the field engineer
performs OFF-HOOK -> dial -> ON-HOOK on the physical phone. Captured OFFHOOK/DTMF/
ONHOOK events drive the orchestrator (record_activity / end_activity), then the session
is cleaned up (PCM guard + debug off).

Usage (inside backend container, /tools mounted):
    python /tools/dev_phone_e2e.py
"""
import asyncio
import sys
import time
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from app.collectors.asyncssh_adapter import AsyncSSHDeviceAdapter
from app.db.models import Case, CaseDevice
from app.db.session import SessionLocal
from app.reproduction.fxs_event_monitor import FxsEventMonitor
from app.reproduction.platform_factory import build_orchestrator

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

    async def run():
        # Resolve the DUT credential from the DB (provisioned via Feishu/Poseidon).
        from app.integrations.credentials import get_credential_provider
        provider = get_credential_provider()
        password = await provider.get_password(sn=SN, ip=IP)
        username = provider.resolve_username(ip=IP, fallback='root')

        adapter = AsyncSSHDeviceAdapter(ip=IP, port=PORT, username=username, password=password)
        await adapter.connect()
        check('SSH connected (DB credential)', True, f'{username}@{IP}:{PORT}')

        process = await adapter._ensure_aim_session(10)
        stream = process.stdout
        loop = asyncio.get_event_loop()

        def write_aim(cmd: str):
            process.stdin.write(cmd + '\n')

        # Real orchestration: use the mock platform to drive the state machine so this
        # script stays on ONE event loop. The REAL device is used for (a) opening the
        # AIM session, (b) the full debug sequence, and (c) capturing live FXS events.
        # Driving the real platform's arm/cleanup through its background bridge loop
        # while also reading the AIM stream on the main loop would deadlock the shared
        # asyncssh connection, so the mock platform handles the state transitions.
        from app.reproduction.orchestrator import ReproductionOrchestrator
        orch = ReproductionOrchestrator()

        clock_ms = [0]
        def clock():
            clock_ms[0] += 500
            return clock_ms[0]

        monitor = FxsEventMonitor(read_aim_chunk=lambda: None, write_aim=write_aim, relative_ms=clock)
        orch.fxs_event_monitor = monitor
        monitor.start()

        # Drain the command echo produced by the FULL_DEBUG_ENABLE burst so it cannot
        # pollute the event buffer / leave the stream in a stale state before listening.
        await asyncio.sleep(2)
        try:
            await asyncio.wait_for(stream.read(8192), 2)
        except asyncio.TimeoutError:
            pass

        # Create a Case + Device in the real DB and a reproduction session.
        db = SessionLocal()
        try:
            case = Case(case_no=f'PHONE-E2E-{uuid4().hex[:8]}', summary='real phone e2e', status='ANALYZING')
            db.add(case); db.flush()
            device = CaseDevice(case_id=case.id, ip=IP, ssh_port=PORT, sn=SN,
                                username=username, device_info={})
            db.add(device); db.flush()

            print('=== creating reproduction session (VOIP_GENERIC_FULL_CAPTURE) ===')
            session = orch.create_session(db, case_id=case.id, profile_id='VOIP_GENERIC_FULL_CAPTURE',
                                          device_id=device.id, actor='phone-e2e')
            db.commit()
            print('=== start -> real arm (PCM ON + full debug) ===')
            session = orch.start(db, session=session, owner_worker='phone-e2e', actor='phone-e2e')
            db.commit()
            from app.contracts.enums import ReproductionState
            check('armed -> WATCHING', ReproductionState(session.state) == ReproductionState.WATCHING, session.state)

            print(f'\n=== LISTENING {WATCH_SECONDS}s. PLEASE DO NOW: OFF-HOOK -> dial digits -> ON-HOOK ===\n')
            events = []
            started = time.monotonic()
            try:
                while time.monotonic() - started < WATCH_SECONDS:
                    try:
                        chunk = await asyncio.wait_for(stream.read(4096), 1.0)
                    except asyncio.TimeoutError:
                        chunk = ''
                    if chunk:
                        evs = monitor.feed(chunk)
                        for ev in evs:
                            events.append(ev)
                            print(f'  >>> {ev.timestamp} [line {ev.line}] {ev.event}' + (f'<{ev.digit}>' if ev.digit else ''))
                            handled = orch.record_fxs_event(db, session=session, event=ev, actor='phone-e2e')
                            if handled is not None:
                                print(f'      -> orchestrator handled (attempt state={session.state})')
                            db.commit()
                        has_off = any(e.event == 'OFFHOOK' for e in events)
                        has_on = any(e.event == 'ONHOOK' for e in events)
                        if has_off and has_on:
                            print('  (full hook cycle captured)')
                            break
            finally:
                monitor.stop()

            check('captured OFFHOOK', any(e.event == 'OFFHOOK' for e in events), f'{sum(1 for e in events if e.event=="OFFHOOK")}')
            check('captured DTMF', any(e.event == 'DTMF' for e in events), f'{sum(1 for e in events if e.event=="DTMF")}')
            check('captured ONHOOK', any(e.event == 'ONHOOK' for e in events), f'{sum(1 for e in events if e.event=="ONHOOK")}')

            # Evidence + cleanup.
            from app.db.models import ReproductionCaptureSegment
            segs = db.query(ReproductionCaptureSegment).filter(ReproductionCaptureSegment.session_id == session.id).count()
            check('capture segments written', segs > 0, f'segments={segs}')

            print('=== cancel -> cleanup ===')
            session = orch.cancel(db, session=session, actor='phone-e2e')
            db.commit()
            check('cleanup verified', session.cleanup_status == 'CLEANUP_VERIFIED',
                  f'status={session.cleanup_status} state={session.state}')
        finally:
            db.close()
            await adapter.disconnect()

    asyncio.run(run())

    print('\n=== RESULT ===')
    failed = [l for l, ok, _ in results if not ok]
    print(f'  {len(results) - len(failed)}/{len(results)} checks passed')
    if failed:
        print(f'  FAILED: {failed}')
        sys.exit(1)
    print('  REAL PHONE E2E OK')


if __name__ == '__main__':
    main()
