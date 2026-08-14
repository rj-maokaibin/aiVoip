"""Real-call CALL-level end-to-end: FXS + media-binding -> bind_call -> CALL_QUICK.

Drives the REAL platform through the full CALL state machine that the watcher
(_watch_real) implements, against the live DUT:

  arm (PCM ON + full debug) -> WATCHING
  -> FXS OFFHOOK -> record_activity (ACTIVITY_DETECTED)
  -> PCM mirror media-active -> bind_call(RTP_STREAM_START) -> CAPTURING
  -> FXS ONHOOK -> end_call(no-oracle signal) -> CALL_QUICK verdict/findings
  -> cleanup (PCM OFF + debug off)

Usage (inside backend container, /tools mounted):
    python /tools/dev_real_call_e2e.py
"""
import asyncio
import sys
import time
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

SN = 'MACC1JZH3260M'
IP = '47.104.155.247'
PORT = 65157
WATCH = 120


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
        from app.reproduction.quick import QuickAnalysisInput
        from app.contracts.enums import CallVerdict
        from app.db.models import Case, CaseDevice
        from app.db.session import SessionLocal
        from app.reproduction.platform_factory import build_orchestrator

        adapter = AsyncSSHDeviceAdapter(ip=IP, port=PORT, username=username, password=password)
        orch, close = build_orchestrator(adapter=adapter, connect=True)
        platform = orch.platform
        check('platform is REAL', type(platform).__name__ == 'RealReproductionPlatform', type(platform).__name__)

        db = SessionLocal()
        try:
            case = Case(case_no=f'RCALL-{uuid4().hex[:8]}', summary='real call e2e', status='ANALYZING')
            db.add(case); db.flush()
            device = CaseDevice(case_id=case.id, ip=IP, ssh_port=PORT, sn=SN, username=username, device_info={})
            db.add(device); db.flush()

            print('=== create + start -> REAL arm ===')
            session = orch.create_session(db, case_id=case.id, profile_id='VOIP_GENERIC_FULL_CAPTURE',
                                          device_id=device.id, actor='real-call-e2e')
            db.commit()
            session = orch.start(db, session=session, owner_worker='real-call-e2e', actor='real-call-e2e')
            db.commit()
            from app.contracts.enums import ReproductionState
            check('armed -> WATCHING', ReproductionState(session.state) == ReproductionState.WATCHING, session.state)

            # Start FXS event streaming (bridge-loop reader).
            monitor = platform.start_fxs_monitor()
            orch.fxs_event_monitor = monitor

            print(f'=== LISTENING {WATCH}s. PLEASE DO: OFF-HOOK -> dial -> TALK -> ON-HOOK ===')
            events = []
            call = None
            calls_ended = 0
            active_call_id = None
            last_media_probe = 0.0
            started = time.monotonic()
            try:
                while time.monotonic() - started < WATCH:
                    row = db.get(type(session), session.id)
                    state = ReproductionState(row.state)
                    if state not in {ReproductionState.WATCHING, ReproductionState.ACTIVITY_DETECTED,
                                     ReproductionState.CALL_DETECTED, ReproductionState.CAPTURING}:
                        break
                    # FXS events
                    for ev in monitor.poll():
                        events.append(ev)
                        print(f'  >>> {ev.event}' + (f'<{ev.digit}>' if ev.digit else ''), ev.timestamp)
                        handled = orch.record_fxs_event(db, session=session, event=ev, actor='real-call-e2e')
                        if handled is not None:
                            print(f'      -> orchestrator handled (state={row.state})')
                        db.commit()
                        # ONHOOK while call bound -> end_call
                        if ev.event == 'ONHOOK' and active_call_id is not None:
                            now = ReproductionState(db.get(type(session), session.id).state)
                            if now in {ReproductionState.CALL_DETECTED, ReproductionState.CAPTURING}:
                                # The call is over: stop the AIM reader so cleanup's
                                # execute_cli prompt reads do not compete with it.
                                try:
                                    platform.stop_fxs_monitor()
                                except Exception:
                                    pass
                                rel = monitor.relative_ms()
                                call, decision = orch.end_call(
                                    db, session=session, call_id=active_call_id, relative_ms=rel,
                                    signal=QuickAnalysisInput(verdict=CallVerdict.INCONCLUSIVE, findings=()),
                                    end_anchor='FXS_ONHOOK', actor='real-call-e2e',
                                )
                                active_call_id = None
                                calls_ended += 1
                                db.commit()
                                print(f'      -> end_call done verdict={call.verdict} role={call.role}')
                    # Periodic media probe -> bind on media-active
                    now = time.monotonic()
                    if now - last_media_probe >= 3.0:
                        last_media_probe = now
                        cur = ReproductionState(db.get(type(session), session.id).state)
                        if cur == ReproductionState.ACTIVITY_DETECTED and active_call_id is None:
                            ctx = platform.resolve_voice_context(device)
                            if platform.pcm_media_active(context=ctx):
                                print('      (media active -> bind_call)')
                                rel = monitor.relative_ms()
                                call = orch.bind_call(
                                    db, session=session, relative_ms=rel,
                                    external_call_ref=platform.media_binding_call_ref(),
                                    binding_event='RTP_STREAM_START', actor='real-call-e2e',
                                )
                                active_call_id = call.id
                                db.commit()
                                print(f'      -> bind_call id={call.id[:8]} state={row.state}')
                    if not events:
                        await asyncio.sleep(0.3)
            finally:
                platform.stop_fxs_monitor()

            check('captured OFFHOOK', any(e.event == 'OFFHOOK' for e in events),
                  f'{sum(1 for e in events if e.event=="OFFHOOK")}')
            check('captured ONHOOK', any(e.event == 'ONHOOK' for e in events),
                  f'{sum(1 for e in events if e.event=="ONHOOK")}')
            check('call bound via media', active_call_id is not None or calls_ended > 0,
                  f'bound_calls={calls_ended}')
            check('call ended + CALL_QUICK', calls_ended > 0, f'calls_ended={calls_ended}')

            if call is not None:
                print('  CALL_QUICK summary:')
                qa = call.quick_analysis_json or {}
                print('    verdict =', call.verdict, ' role =', call.role)
                print('    findings =', (qa.get('findings') or [])[:12])
                print('    media_summary =', ((qa.get('analysis_summary') or {}).get('media_summary') or {}))
                check('verdict present', call.verdict in ('MATCH', 'NO_MATCH', 'INCONCLUSIVE'), call.verdict)
                check('findings present', bool(qa.get('findings')), str((qa.get('findings') or [])[:8]))

            print('=== cancel -> REAL cleanup ===')
            session = orch.cancel(db, session=session, actor='real-call-e2e')
            db.commit()
            check('cleanup verified', session.cleanup_status == 'CLEANUP_VERIFIED',
                  f'{session.cleanup_status} state={session.state}')
        finally:
            db.close()
            close()

    asyncio.run(run())

    print('\n=== RESULT ===')
    failed = [l for l, ok, _ in results if not ok]
    print(f'  {len(results) - len(failed)}/{len(results)} checks passed')
    if failed:
        print(f'  FAILED: {failed}')
        sys.exit(1)
    print('  REAL CALL E2E OK')


if __name__ == '__main__':
    main()
