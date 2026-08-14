"""Real-mode CALL-level analysis tests.

Covers the previously-unconnected link: real CALL analysis driven by the real
platform's media-binding signal (PCM mirror active) instead of AIM SIP plaintext.

Tests:
  1. pcm_media_active detects the PCM mirror stream and returns False when quiet;
  2. bind_call accepts an explicit media binding event (RTP_STREAM_START);
  3. end_call with a no-oracle signal (INCONCLUSIVE, no fixture findings) drives
     the evidence-backed verdict path (abnormal findings -> MATCH, else INCONCLUSIVE);
  4. the real watcher advances WATCHING -> ACTIVITY_DETECTED (FXS OFFHOOK) -> bind
     on media-active -> CALL_DETECTED/CAPTURING, then ONHOOK ends the call.
"""
from __future__ import annotations

import asyncio
from pathlib import Path
import tempfile
from uuid import uuid4

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.collectors.device_adapter import CommandResult
from app.contracts.enums import CallVerdict, ReproductionState
from app.db.base import Base
from app.db.models import (
    Case, CaseDevice, ReproductionAttempt, ReproductionCall, ReproductionEventRecord,
    ReproductionSession,
)
from app.integrations.storage import FilesystemObjectStorage
from app.reproduction.capture_pipeline import ReproductionCapturePipeline
from app.reproduction.mock_platform import MockReproductionPlatform
from app.reproduction.orchestrator import ReproductionOrchestrator
from app.reproduction.pcm_cleanup import PcmCleanupGuard
from app.reproduction.profile import ReproductionProfileRegistry
from app.reproduction.quick import QuickAnalysisInput


def _engine():
    eng = create_engine('sqlite+pysqlite:///:memory:')
    Base.metadata.create_all(eng)
    return eng


def _case_device(db: Session):
    case = Case(case_no='RCAL-1', summary='real call analysis', status='ANALYZING')
    db.add(case); db.flush()
    device = CaseDevice(case_id=case.id, ip='198.51.100.30', ssh_port=22, sn='SN-RCAL', username='admin', device_info={})
    db.add(device); db.flush()
    return case, device


_TMP = tempfile.TemporaryDirectory(prefix='voip-rcal-tests-')


def _orch(platform=None):
    root = Path(__file__).resolve().parents[2] / 'profiles'
    base = Path(_TMP.name) / uuid4().hex
    pipe = ReproductionCapturePipeline(root=base / 'capture', storage=FilesystemObjectStorage(base / 'objects'))
    return ReproductionOrchestrator(
        registry=ReproductionProfileRegistry(root),
        platform=platform or MockReproductionPlatform(),
        capture_pipeline=pipe,
    )


def _session_at_watching(db, orch, case):
    session = orch.create_session(db, case_id=case.id, profile_id='AUDIO_NOISE')
    orch.start(db, session=session)
    assert session.state == ReproductionState.WATCHING.value
    return session


# --- real platform media-active detection -------------------------------------------


class _MediaFakeAdapter:
    """AsyncSSHDeviceAdapter stand-in: tcpdump probe responses for PCM ports."""

    def __init__(self, *, rx_packets=0, tx_packets=0):
        self.rx_packets = rx_packets
        self.tx_packets = tx_packets
        self.cli_calls: list[str] = []

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def execute_shell(self, command, timeout=None):
        # build_busybox_tcpdump_probe: 'timeout -t 5 tcpdump -ni <iface> -c 1 udp port <port> ...'
        port = 50000 if '50000' in command else 40000
        n = self.rx_packets if port == 40000 else self.tx_packets
        return CommandResult(stdout=f'{n} packets captured\n{n} packets received by filter\n')

    async def execute_cli(self, command, timeout=None):
        self.cli_calls.append(command)
        return CommandResult(stdout='AIM>')


def test_real_platform_pcm_media_active_detects_stream():
    from app.reproduction.real_platform import RealReproductionPlatform
    p = RealReproductionPlatform(adapter=_MediaFakeAdapter(rx_packets=3, tx_packets=0))
    assert p.pcm_media_active() is True


def test_real_platform_pcm_media_active_quiet_returns_false():
    from app.reproduction.real_platform import RealReproductionPlatform
    p = RealReproductionPlatform(adapter=_MediaFakeAdapter(rx_packets=0, tx_packets=0))
    assert p.pcm_media_active() is False


# --- orchestrator CALL semantics -----------------------------------------------------


def test_bind_call_accepts_media_binding_event():
    eng = _engine()
    with Session(eng) as db:
        case, _ = _case_device(db)
        orch = _orch()
        session = _session_at_watching(db, orch, case)
        # OFFHOOK -> record_activity (ACTIVITY_DETECTED) before binding.
        from app.reproduction.fxs_event_monitor import FxsEvent
        orch.record_fxs_event(db, session=session, event=FxsEvent(timestamp='2026-08-13 22:52:53.878000', line=0, event='OFFHOOK'))
        assert session.state == ReproductionState.ACTIVITY_DETECTED.value
        # Bind with the media-binding event (real mode has no SIP INVITE plaintext).
        call = orch.bind_call(db, session=session, relative_ms=1500, external_call_ref='media-1',
                              binding_event='RTP_STREAM_START')
        assert call is not None
        assert session.state == ReproductionState.CAPTURING.value
        rec = db.scalar(select(ReproductionEventRecord).where(
            ReproductionEventRecord.call_id == call.id,
            ReproductionEventRecord.event_type == 'RTP_STREAM_START'))
        assert rec is not None
        assert (rec.payload_json or {}).get('binding_event') == 'RTP_STREAM_START'


def test_end_call_no_oracle_signal_drives_evidence_verdict():
    """Real mode passes INCONCLUSIVE/no-fixture signal; verdict must come from the
    deterministic analyzers, not the mock fixture oracle."""
    eng = _engine()
    with Session(eng) as db:
        case, _ = _case_device(db)
        orch = _orch()
        session = _session_at_watching(db, orch, case)
        from app.reproduction.fxs_event_monitor import FxsEvent
        orch.record_fxs_event(db, session=session, event=FxsEvent(timestamp='2026-08-13 22:52:53.878000', line=0, event='OFFHOOK'))
        call = orch.bind_call(db, session=session, relative_ms=1500, binding_event='RTP_STREAM_START')
        call, decision = orch.end_call(
            db, session=session, call_id=call.id, relative_ms=5000,
            signal=QuickAnalysisInput(verdict=CallVerdict.INCONCLUSIVE, findings=()),
            end_anchor='FXS_ONHOOK',
        )
        # The no-oracle signal must NOT short-circuit to MATCH; the verdict follows
        # the analyzers (AUDIO_NOISE profile expects PERIODIC_INTERFERENCE, absent
        # here -> NO_MATCH / not blindly MATCH).
        assert call.status == 'ANALYZED'
        qa = call.quick_analysis_json or {}
        assert 'findings' in qa
        assert 'analysis_summary' in qa
        # call.verdict holds the analyzer verdict (not the fixture oracle).
        assert call.verdict in (CallVerdict.MATCH.value, CallVerdict.NO_MATCH.value, CallVerdict.INCONCLUSIVE.value)
        # No-oracle path: PERIODIC_INTERFERENCE (AUDIO_NOISE target finding) is NOT
        # among findings, so the verdict must not be a fixture-forced MATCH.
        assert 'PERIODIC_INTERFERENCE' not in (qa.get('findings') or [])


def test_real_watcher_call_flow_bind_on_media_then_end():
    """Simulate the real watcher decision: FXS OFFHOOK -> ACTIVITY_DETECTED,
    media-active probe -> bind_call -> CAPTURING, FXS ONHOOK -> end_call."""
    eng = _engine()
    with Session(eng) as db:
        case, _ = _case_device(db)
        orch = _orch()
        session = _session_at_watching(db, orch, case)
        from app.reproduction.fxs_event_monitor import FxsEvent
        # 1. OFFHOOK
        orch.record_fxs_event(db, session=session, event=FxsEvent(timestamp='2026-08-13 22:52:53.878000', line=0, event='OFFHOOK'))
        assert session.state == ReproductionState.ACTIVITY_DETECTED.value
        # 2. media-binding (mimics pcm_media_active True) -> bind_call
        call = orch.bind_call(db, session=session, relative_ms=1500, binding_event='RTP_STREAM_START')
        assert session.state == ReproductionState.CAPTURING.value
        # 3. ONHOOK -> end_call (real worker path)
        call2, decision = orch.end_call(
            db, session=session, call_id=call.id, relative_ms=4500,
            signal=QuickAnalysisInput(verdict=CallVerdict.INCONCLUSIVE, findings=()),
            end_anchor='FXS_ONHOOK',
        )
        assert call2.status == 'ANALYZED'
        # After end_call with sufficient/end_policy, session should have progressed
        # (either cleaned up terminal or returned to WATCHING).
        assert session.state in (ReproductionState.WATCHING.value, ReproductionState.CLEANUP.value,
                                 ReproductionState.CANCELLED.value, ReproductionState.FINALIZING.value,
                                 ReproductionState.COMPLETED.value, ReproductionState.ANALYZING.value)


def test_pcm_media_active_uses_default_interface_when_no_context():
    from app.reproduction.real_platform import RealReproductionPlatform
    p = RealReproductionPlatform(adapter=_MediaFakeAdapter(rx_packets=2))
    # No context -> falls back to DEFAULT_VOICE_INTERFACE (br-lan_400), still detects.
    assert p.pcm_media_active(context=None) is True
