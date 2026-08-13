from __future__ import annotations

from pathlib import Path
import tempfile
from uuid import uuid4

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.contracts.enums import CleanupStatus, ReproductionState
from app.db.base import Base
from app.db.models import Case, CaseDevice, CleanupRun, ReproductionSession
from app.integrations.storage import FilesystemObjectStorage
from app.reproduction.capture_pipeline import ReproductionCapturePipeline
from app.reproduction.mock_platform import MockReproductionPlatform
from app.reproduction.orchestrator import ReproductionOrchestrator
from app.reproduction.pcm_cleanup import PcmCleanupGuard
from app.reproduction.profile import ReproductionProfileRegistry


def _engine():
    eng = create_engine('sqlite+pysqlite:///:memory:')
    Base.metadata.create_all(eng)
    return eng


def _case_device(db: Session):
    case = Case(case_no='PCMB-1', summary='pcm guard binding', status='ANALYZING')
    db.add(case); db.flush()
    device = CaseDevice(case_id=case.id, ip='198.51.100.20', ssh_port=22, sn='SN-PCMB', username='admin', device_info={})
    db.add(device); db.flush()
    return case, device


_TMP = tempfile.TemporaryDirectory(prefix='voip-pcmb-tests-')


def _guard(*, probes, commands):
    return PcmCleanupGuard(
        probe_packets=lambda interface, port: probes.pop(0),
        execute_aim=commands.append,
    )


def _orch(guard: PcmCleanupGuard):
    root = Path(__file__).resolve().parents[2] / 'profiles'
    base = Path(_TMP.name) / uuid4().hex
    pipe = ReproductionCapturePipeline(root=base / 'capture', storage=FilesystemObjectStorage(base / 'objects'))
    return ReproductionOrchestrator(
        registry=ReproductionProfileRegistry(root),
        platform=MockReproductionPlatform(),
        capture_pipeline=pipe,
        pcm_cleanup_guard=guard,
    )


def test_cleanup_with_guard_skips_off_when_pcm_streams_are_quiet():
    eng = _engine()
    with Session(eng) as db:
        case, _ = _case_device(db)
        commands: list[str] = []
        orch = _orch(_guard(probes=[0, 0], commands=commands))
        session = orch.create_session(db, case_id=case.id, profile_id='AUDIO_NOISE')
        orch.start(db, session=session)
        orch.cancel(db, session=session)

        assert commands == []
        assert session.state == ReproductionState.CANCELLED.value
        assert session.cleanup_status == CleanupStatus.CLEANUP_VERIFIED.value
        run = db.query(CleanupRun).filter(CleanupRun.session_id == session.id).order_by(CleanupRun.run_no.desc()).first()
        meta = run.action_results_json['pcm_guard']
        assert meta['PCM_RX']['off_already_executed'] is False
        assert meta['PCM_TX']['off_already_executed'] is False


def test_cleanup_with_guard_executes_off_once_then_verifies_quiet():
    eng = _engine()
    with Session(eng) as db:
        case, _ = _case_device(db)
        commands: list[str] = []
        orch = _orch(_guard(probes=[3, 0, 2, 0], commands=commands))
        session = orch.create_session(db, case_id=case.id, profile_id='AUDIO_NOISE')
        orch.start(db, session=session)
        orch.cancel(db, session=session)

        assert commands == [
            'voip dsp diag set 192.0.2.1 40000 1 pcm_rx off',
            'voip dsp diag set 192.0.2.1 50000 1 pcm_tx off',
        ]
        assert session.state == ReproductionState.CANCELLED.value
        assert session.cleanup_status == CleanupStatus.CLEANUP_VERIFIED.value
        run = db.query(CleanupRun).filter(CleanupRun.session_id == session.id).order_by(CleanupRun.run_no.desc()).first()
        meta = run.action_results_json['pcm_guard']
        assert meta['PCM_RX']['off_executed'] is True
        assert meta['PCM_TX']['off_executed'] is True


def test_retry_never_sends_a_second_off_for_a_channel_that_already_executed_off():
    eng = _engine()
    with Session(eng) as db:
        case, _ = _case_device(db)
        commands: list[str] = []
        # First cleanup: PCM_RX stays active after its single OFF (quiet_verified=False),
        # PCM_TX is quiet (1 probe, skipped). Retry restores off_already_executed from the
        # previous CleanupRun and must NOT issue a second PCM_RX OFF.
        probes = [2, 1, 0,   # first run: RX before/after, TX before
                  1, 0]      # retry: RX before (blocked), TX before (quiet)
        orch = _orch(_guard(probes=probes, commands=commands))
        session = orch.create_session(db, case_id=case.id, profile_id='AUDIO_NOISE')
        orch.start(db, session=session)
        orch.cancel(db, session=session)

        assert session.state == ReproductionState.CLEANUP_FAILED.value
        assert session.cleanup_status == CleanupStatus.CLEANUP_FAILED.value

        orch.retry_cleanup(db, session=session, actor='watchdog')

        assert commands == ['voip dsp diag set 192.0.2.1 40000 1 pcm_rx off']
        assert session.state == ReproductionState.CLEANUP_FAILED.value
        assert session.cleanup_status == CleanupStatus.CLEANUP_FAILED.value
        run = db.query(CleanupRun).filter(CleanupRun.session_id == session.id).order_by(CleanupRun.run_no.desc()).first()
        meta = run.action_results_json['pcm_guard']
        assert meta['PCM_RX']['retry_blocked'] is True
        assert meta['PCM_RX']['off_already_executed'] is True
        assert meta['PCM_RX']['off_executed'] is False
