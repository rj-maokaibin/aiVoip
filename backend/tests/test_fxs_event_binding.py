from __future__ import annotations

from pathlib import Path
import tempfile
from uuid import uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.contracts.enums import ReproductionState
from app.db.base import Base
from app.db.models import Case, CaseDevice, ReproductionAttempt, ReproductionEventRecord, ReproductionSession
from app.integrations.storage import FilesystemObjectStorage
from app.reproduction.capture_pipeline import ReproductionCapturePipeline
from app.reproduction.fxs_event_monitor import FxsEventMonitor, FxsEvent
from app.reproduction.mock_platform import MockReproductionPlatform
from app.reproduction.orchestrator import ReproductionOrchestrator
from app.reproduction.profile import ReproductionProfileRegistry


def _engine():
    eng = create_engine('sqlite+pysqlite:///:memory:')
    Base.metadata.create_all(eng)
    return eng


def _case_device(db: Session):
    case = Case(case_no='FXSB-1', summary='fxs event binding', status='ANALYZING')
    db.add(case); db.flush()
    device = CaseDevice(case_id=case.id, ip='198.51.100.30', ssh_port=22, sn='SN-FXSB', username='admin', device_info={})
    db.add(device); db.flush()
    return case, device


_TMP = tempfile.TemporaryDirectory(prefix='voip-fxsb-tests-')


def _orch(monitor: FxsEventMonitor | None):
    root = Path(__file__).resolve().parents[2] / 'profiles'
    base = Path(_TMP.name) / uuid4().hex
    pipe = ReproductionCapturePipeline(root=base / 'capture', storage=FilesystemObjectStorage(base / 'objects'))
    return ReproductionOrchestrator(
        registry=ReproductionProfileRegistry(root),
        platform=MockReproductionPlatform(),
        capture_pipeline=pipe,
        fxs_event_monitor=monitor,
    )


def _monitor(*, ms_seq=(1000, 2000, 3000), commands=None):
    counter = [0]
    def clock():
        i = min(counter[0], len(ms_seq) - 1)
        counter[0] += 1
        return ms_seq[i]
    return FxsEventMonitor(
        read_aim_chunk=lambda: None,
        write_aim=(commands.append if commands is not None else lambda _: None),
        relative_ms=clock,
    )


def _session_at_watching(db, orch, case):
    session = orch.create_session(db, case_id=case.id, profile_id='AUDIO_NOISE')
    orch.start(db, session=session)
    assert session.state == ReproductionState.WATCHING.value
    return session


def test_offhook_event_starts_attempt_and_onhook_ends_it():
    eng = _engine()
    with Session(eng) as db:
        case, _ = _case_device(db)
        commands: list[str] = []
        orch = _orch(_monitor(commands=commands))
        session = _session_at_watching(db, orch, case)

        offhook = FxsEvent(timestamp='2026-08-13 22:52:53.878000', line=0, event='OFFHOOK')
        attempt = orch.record_fxs_event(db, session=session, event=offhook)
        assert attempt is not None
        assert attempt.start_anchor_type == 'FXS_OFFHOOK'
        assert session.state == ReproductionState.ACTIVITY_DETECTED.value

        dtmf = FxsEvent(timestamp='2026-08-13 22:52:54.778000', line=0, event='DTMF', digit='1')
        orch.record_fxs_event(db, session=session, event=dtmf)
        rec = db.scalar(select(ReproductionEventRecord).where(ReproductionEventRecord.event_type == 'FXS_DTMF'))
        assert rec is not None and rec.payload_json.get('digit') == '1'

        onhook = FxsEvent(timestamp='2026-08-13 22:52:58.758000', line=0, event='ONHOOK')
        ended = orch.record_fxs_event(db, session=session, event=onhook)
        assert ended is not None
        assert ended.status == 'INVALID'
        assert ended.end_anchor_type == 'FXS_ONHOOK'
        assert session.state == ReproductionState.WATCHING.value


def test_onhook_without_active_attempt_is_ignored():
    eng = _engine()
    with Session(eng) as db:
        case, _ = _case_device(db)
        orch = _orch(_monitor())
        session = _session_at_watching(db, orch, case)
        onhook = FxsEvent(timestamp='2026-08-13 22:52:58.758000', line=0, event='ONHOOK')
        assert orch.record_fxs_event(db, session=session, event=onhook) is None
        assert session.state == ReproductionState.WATCHING.value


def test_offhook_when_not_watching_is_ignored():
    eng = _engine()
    with Session(eng) as db:
        case, _ = _case_device(db)
        orch = _orch(_monitor())
        session = orch.create_session(db, case_id=case.id, profile_id='AUDIO_NOISE')
        # CREATED state, not watching.
        offhook = FxsEvent(timestamp='2026-08-13 22:52:53.878000', line=0, event='OFFHOOK')
        assert orch.record_fxs_event(db, session=session, event=offhook) is None


def test_dtmf_is_attributed_to_attempt_and_call():
    """DTMF recorded during dialing carries attempt_id; after bind_call it
    carries call_id. DTMF during CAPTURING is recorded, not dropped."""
    from app.contracts.enums import AttemptStatus, ReproductionCallStatus

    eng = _engine()
    with Session(eng) as db:
        case, _ = _case_device(db)
        orch = _orch(_monitor(ms_seq=(1000, 2000, 3000, 4000, 5000, 6000, 7000, 8000)))
        session = _session_at_watching(db, orch, case)

        # OFFHOOK -> attempt 1, then a dial digit.
        orch.record_fxs_event(db, session=session, event=FxsEvent(timestamp='t', line=0, event='OFFHOOK'))
        orch.record_fxs_event(db, session=session, event=FxsEvent(timestamp='t', line=0, event='DTMF', digit='3'))
        dial_rec = db.scalar(select(ReproductionEventRecord).where(
            ReproductionEventRecord.event_type == 'FXS_DTMF',
            ReproductionEventRecord.payload_json['digit'].as_string() == '3'))
        # attempt_id must be set (was NULL before the fix).
        assert dial_rec.attempt_id is not None
        # No call yet -> call_id None, in_call False.
        assert dial_rec.call_id is None
        assert dial_rec.payload_json['in_call'] is False

        # Bind the call (simulating SIP_INVITE observed by the signal observer).
        call = orch.bind_call(db, session=session, relative_ms=3500, binding_event='SIP_INVITE')
        assert call is not None
        assert session.state == ReproductionState.CAPTURING.value

        # A digit pressed AFTER binding is in-call: recorded (not dropped),
        # attributed to both attempt and call, in_call True.
        orch.record_fxs_event(db, session=session, event=FxsEvent(timestamp='t', line=0, event='DTMF', digit='5'))
        in_call_rec = db.scalar(select(ReproductionEventRecord).where(
            ReproductionEventRecord.event_type == 'FXS_DTMF',
            ReproductionEventRecord.payload_json['digit'].as_string() == '5'))
        assert in_call_rec is not None, 'DTMF during CAPTURING must be recorded, not dropped'
        assert in_call_rec.attempt_id is not None
        assert in_call_rec.call_id == call.id
        assert in_call_rec.payload_json['in_call'] is True
