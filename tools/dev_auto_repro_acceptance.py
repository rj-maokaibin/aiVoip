"""Real-DUT autonomous reproduction acceptance WITHOUT a physical phone.

Drives the full orchestrator reproduction lifecycle on the real APF1250 using the
real platform, but feeds FXS activity from a canned real-device event stream
(2026-08-13 OFFHOOK/DTMF/ONHOOK sample) instead of a physical phone. This verifies
every real link except the physical hook/dial action itself:

  create session (VOIP_GENERIC_FULL_CAPTURE, real arm_barrier)
  -> start  (real arm: PCM ON + full debug + PCAP probe) -> WATCHING
  -> inject OFFHOOK -> record_activity (real pretrigger tcpdump) -> attempt
  -> inject DTMF
  -> inject ONHOOK -> end_activity_without_call
  -> cancel (real cleanup via PcmCleanupGuard)

Credentials come from DEV_* env vars. Runs inside the backend container against the
real postgres DB so the resulting session/attempt/evidence are the real artifacts.
"""
import asyncio
import os
import sys
from pathlib import Path
from uuid import uuid4

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))

from app.contracts.enums import ReproductionState, AttemptStatus
from app.core.config import settings
from app.db.session import SessionLocal
from app.db.models import Case, CaseDevice
from app.reproduction.fxs_event_monitor import FxsEvent, FxsEventMonitor
from app.reproduction.platform_factory import build_orchestrator
from app.collectors.asyncssh_adapter import AsyncSSHDeviceAdapter

# Real-device FXS event stream captured 2026-08-13 (OFFHOOK -> DTMF<1> -> ONHOOK).
REAL_FXS_STREAM = [
    ('2026-08-13 22:52:53.878000', 'OFFHOOK', None),
    ('2026-08-13 22:52:54.778000', 'DTMF', '1'),
    ('2026-08-13 22:52:58.758000', 'ONHOOK', None),
]


def _line(label, ok, detail=''):
    print(f'  [{"PASS" if ok else "FAIL"}] {label}' + (f'  ({detail})' if detail else ''))


def main():
    results = []

    def check(label, cond, detail=''):
        results.append((label, bool(cond), detail))
        _line(label, cond, detail)

    device = CaseDevice(
        case_id=None, ip=os.environ['DEV_HOST'], ssh_port=int(os.environ['DEV_PORT']),
        sn=f'APF-{uuid4().hex[:8]}', username=os.environ['DEV_USER'], device_info={},
    )
    # Credentials: the platform resolves from secret.yaml via LocalSecretCredentialProvider.
    adapter = AsyncSSHDeviceAdapter(
        ip=os.environ['DEV_HOST'], port=int(os.environ['DEV_PORT']),
        username=os.environ['DEV_USER'], password=os.environ['DEV_PASSWORD'],
    )
    orch, close = build_orchestrator(adapter=adapter, connect=True)
    platform = orch.platform
    print('platform:', platform.platform_id, platform.version)

    # Canned event clock: relative ms advancing per event.
    clock_vals = [1000, 2000, 3000]
    counter = [0]
    def clock():
        i = min(counter[0], len(clock_vals) - 1)
        counter[0] += 1
        return clock_vals[i]

    monitor = FxsEventMonitor(read_aim_chunk=lambda: None, write_aim=lambda _: None, relative_ms=clock)
    orch.fxs_event_monitor = monitor

    db = SessionLocal()
    try:
        case = Case(case_no=f'REAL-ACCEPT-{uuid4().hex[:8]}', summary='real auto-repro acceptance (no phone)', status='ANALYZING')
        db.add(case); db.flush()
        device.case_id = case.id
        db.add(device); db.flush()

        print('=== 1. create_session (real profile) ===')
        session = orch.create_session(db, case_id=case.id, profile_id='VOIP_GENERIC_FULL_CAPTURE', device_id=device.id, actor='acceptance')
        check('session created', session is not None and session.profile_key == 'VOIP_GENERIC_FULL_CAPTURE', session.state)
        check('platform_profile real', session.platform_profile_id == 'ruijie-voip-aim-real', session.platform_profile_id)

        print('=== 2. start -> real arm -> WATCHING ===')
        session = orch.start(db, session=session, owner_worker='acceptance', actor='acceptance')
        db.commit()
        check('reached WATCHING', ReproductionState(session.state) == ReproductionState.WATCHING, session.state)
        ctx = orch._runtime_context(session)
        check('voice context resolved', ctx.voice_interface == 'br-lan_400' and ctx.voice_gateway_ip == '192.168.3.200',
              f'{ctx.voice_interface}/{ctx.voice_gateway_ip}')

        print('=== 3. inject OFFHOOK -> record_activity (real pretrigger) ===')
        ev = FxsEvent(timestamp=REAL_FXS_STREAM[0][0], line=0, event=REAL_FXS_STREAM[0][1], digit=REAL_FXS_STREAM[0][2])
        attempt = orch.record_fxs_event(db, session=session, event=ev, actor='acceptance')
        db.commit()
        check('attempt created', attempt is not None, f'no={attempt.attempt_no if attempt else None}')
        check('session ACTIVITY_DETECTED', ReproductionState(session.state) == ReproductionState.ACTIVITY_DETECTED, session.state)

        print('=== 4. inject DTMF ===')
        ev = FxsEvent(timestamp=REAL_FXS_STREAM[1][0], line=0, event=REAL_FXS_STREAM[1][1], digit=REAL_FXS_STREAM[1][2])
        orch.record_fxs_event(db, session=session, event=ev, actor='acceptance')
        db.commit()
        from app.db.models import ReproductionEventRecord
        dtmf = db.query(ReproductionEventRecord).filter(
            ReproductionEventRecord.session_id == session.id, ReproductionEventRecord.event_type == 'FXS_DTMF').count()
        check('DTMF recorded', dtmf >= 1, f'count={dtmf}')

        print('=== 5. inject ONHOOK -> end_activity_without_call ===')
        ev = FxsEvent(timestamp=REAL_FXS_STREAM[2][0], line=0, event=REAL_FXS_STREAM[2][1], digit=REAL_FXS_STREAM[2][2])
        ended = orch.record_fxs_event(db, session=session, event=ev, actor='acceptance')
        db.commit()
        check('activity ended', ended is not None, f'state={session.state}')

        print('=== 6. cancel -> real cleanup (guard) ===')
        session = orch.cancel(db, session=session, actor='acceptance')
        db.commit()
        check('cleanup verified + terminal', session.cleanup_status == 'CLEANUP_VERIFIED'
              and session.state in ('CANCELLED', 'COMPLETED'), f'status={session.cleanup_status} state={session.state}')

        # Evidence artifact check.
        from app.db.models import ReproductionCaptureSegment
        segs = db.query(ReproductionCaptureSegment).filter(ReproductionCaptureSegment.session_id == session.id).count()
        check('capture segments written', segs > 0, f'segments={segs}')
        pcap = db.query(ReproductionCaptureSegment).filter(
            ReproductionCaptureSegment.session_id == session.id, ReproductionCaptureSegment.channel == 'PCAP').count()
        check('pcap segments present', pcap > 0, f'pcap={pcap}')

    finally:
        try:
            close()
        except Exception:
            pass
        db.rollback()
        db.close()

    print('\n=== RESULT ===')
    failed = [l for l, ok, _ in results if not ok]
    print(f'  {len(results) - len(failed)}/{len(results)} checks passed')
    if failed:
        print(f'  FAILED: {failed}')
        sys.exit(1)
    print('  REAL AUTO-REPRO ACCEPTANCE (no phone) OK')


if __name__ == '__main__':
    main()
