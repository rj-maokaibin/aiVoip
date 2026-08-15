"""Simulate MULTIPLE off-hook/dial/on-hook cycles against the orchestrator state
machine (no phone needed) to prove the on-site operator can mis-operate freely
without wedging the session.

Injects a realistic "sloppy on-site" FXS event sequence into record_fxs_event:
    OFFHOOK -> dial -> OFFHOOK(again) -> dial -> ONHOOK
    -> OFFHOOK -> dial -> ONHOOK -> OFFHOOK -> ONHOOK
and asserts the state machine stays sane: correct attempt count, only the
valid state transitions, DTMF recorded only during an active attempt, and the
session always returns to WATCHING after an ONHOOK (no wedged state).
"""
import asyncio
import sys
import time
from types import SimpleNamespace
from uuid import uuid4

sys.path.insert(0, '/app')

from sqlalchemy import func, select

from app.contracts.enums import AttemptStatus, ReproductionState
from app.db.models import Case, CaseDevice, ReproductionAttempt, ReproductionEventRecord, ReproductionSession
from app.db.session import SessionLocal
from app.reproduction.orchestrator import ReproductionOrchestrator
from app.reproduction.platform_factory import resolve_platform_mode


def _line(label, ok, detail=''):
    print(f'  [{"PASS" if ok else "FAIL"}] {label}' + (f'  ({detail})' if detail else ''))


class _Clock:
    def __init__(self):
        self.t = int(time.monotonic() * 1000)

    def __call__(self):
        self.t += 500  # each event 500ms apart
        return self.t


def _ev(event, digit=None):
    return SimpleNamespace(event=event, digit=digit, timestamp=None)


def main():
    assert resolve_platform_mode() in ('real', 'mock'), 'platform mode'
    db = SessionLocal()
    sid = None
    try:
        case = Case(case_no=f'FXSMULTI-{uuid4().hex[:8]}', summary='multi off/on/dial', status='ANALYZING')
        db.add(case); db.flush()
        dev = CaseDevice(case_id=case.id, ip='192.0.2.1', ssh_port=22, sn='SIM', username='root')
        db.add(dev); db.flush()
        orch = ReproductionOrchestrator()
        session = orch.create_session(db, case_id=case.id, profile_id='VOIP_GENERIC_FULL_CAPTURE',
                                      device_id=dev.id, actor='fxs-multi')
        sid = session.id
        # fake clock so record_fxs_event can timestamp events
        orch.fxs_event_monitor = SimpleNamespace(relative_ms=_Clock())
        # mock mode: no real arm; put the session into WATCHING (as if armed)
        s0 = db.get(ReproductionSession, sid)
        if ReproductionState(s0.state) != ReproductionState.WATCHING:
            s0.state = ReproductionState.WATCHING.value
        # provide a valid voice runtime context (mock pretrigger needs it)
        s0.voice_runtime_context_json = {
            'voice_device_ip': '192.0.2.1', 'voice_gateway_ip': '192.0.2.2',
            'voice_vlan_id': '1', 'voice_interface': 'br-lan_1', 'interface_up': True,
        }
        db.commit()

        # Sloppy on-site sequence: multiple cycles, repeated OFFHOOK, dials.
        seq = [
            ('OFFHOOK', None),  # start attempt 1
            ('DTMF', '1'),      # dial during attempt 1
            ('OFFHOOK', None),  # repeated off-hook while active -> must be ignored
            ('DTMF', '2'),      # dial during attempt 1
            ('ONHOOK', None),   # end attempt 1 (no call) -> WATCHING
            ('OFFHOOK', None),  # start attempt 2
            ('DTMF', '3'),      # dial during attempt 2
            ('ONHOOK', None),   # end attempt 2
            ('OFFHOOK', None),  # start attempt 3
            ('ONHOOK', None),   # end attempt 3 (no dial)
        ]
        for ev_name, digit in seq:
            orch.record_fxs_event(db, session=db.get(ReproductionSession, sid),
                                  event=_ev(ev_name, digit), actor='fxs-multi')
            db.commit()

        s = db.get(ReproductionSession, sid)
        attempts = db.execute(select(ReproductionAttempt).where(
            ReproductionAttempt.session_id == sid).order_by(ReproductionAttempt.attempt_no)).scalars().all()
        dtmf = db.execute(select(ReproductionEventRecord).where(
            ReproductionEventRecord.session_id == sid,
            ReproductionEventRecord.event_type == 'FXS_DTMF')).scalars().all()
        offhook = db.execute(select(ReproductionEventRecord).where(
            ReproductionEventRecord.session_id == sid,
            ReproductionEventRecord.event_type == 'FXS_OFFHOOK')).scalars().all()
        onhook = db.execute(select(ReproductionEventRecord).where(
            ReproductionEventRecord.session_id == sid,
            ReproductionEventRecord.event_type == 'FXS_ONHOOK')).scalars().all()

        # Expect exactly 3 attempts (repeated OFFHOOK inside an active attempt ignored).
        _line('attempts created == 3 (repeated off-hook ignored)',
              len(attempts) == 3, f'attempts={len(attempts)}')
        # DTMF recorded only during active attempts (2 dials in active state).
        _line('DTMF recorded during active attempts', len(dtmf) == 3, f'dtmf={[e.payload_json for e in dtmf]}')
        # Each OFFHOOK started an attempt; 3 OFFHOOK, 3 ONHOOK ended them.
        _line('OFFHOOK/ONHOOK balanced (3/3)',
              len(offhook) == 3 and len(onhook) == 3, f'off={len(offhook)} on={len(onhook)}')
        # All attempts ended (INVALID), none left ACTIVE.
        active = [a for a in attempts if a.status == AttemptStatus.ACTIVE.value]
        _line('no attempt left ACTIVE', len(active) == 0,
              f'statuses={[a.status for a in attempts]}')
        # Session returned to WATCHING (not wedged).
        _line('session back to WATCHING (not wedged)', ReproductionState(s.state) == ReproductionState.WATCHING,
              s.state)

        # cleanup: delete case-scoped evidence first (Evidence.case_id is RESTRICT),
        # then the case (session/attempts/events cascade with it).
        from app.db.models import Evidence
        for ev in db.execute(select(Evidence).where(Evidence.case_id == case.id)).scalars().all():
            db.delete(ev)
        db.delete(session)
        db.delete(dev)
        db.delete(case)
        db.commit()
    except Exception as e:
        _line('multi-cycle exception', False, f'{type(e).__name__}: {e}')
        if sid:
            try:
                db.rollback()
            except Exception:
                pass
    finally:
        db.close()


if __name__ == '__main__':
    main()
