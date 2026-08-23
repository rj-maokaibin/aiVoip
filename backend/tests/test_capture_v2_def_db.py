from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db import models as _existing_models  # noqa
from app.db.base import Base
from app.capture_v2.db_models import (AttemptDataPlaneVerification, CaptureAttempt, CaptureEvent, CaptureSession,
    CoverageInterval,CoverageTrack,CoverageWindow,EvidenceAsset,QualitySnapshot,ReadinessSnapshot,SignalAvailability)
from app.capture_v2.enums import CaptureHealth, CaptureSessionState
from app.capture_v2.fxs.attempt_service import AttemptSemanticRepository
from app.capture_v2.fxs.sanitizer import FxsEventSanitizer, RawFxsEvent
from app.capture_v2.readiness.data_plane import AttemptDataPlaneVerifier, ChannelExpectation
from app.capture_v2.report.evidence_first import EvidenceAssetRepository, EvidenceFirstReportBuilder, FindingEvidenceRequest


def factory():
    engine=create_engine('sqlite+pysqlite:///:memory:',connect_args={'check_same_thread':False},poolclass=StaticPool)
    Base.metadata.create_all(engine,tables=[CaptureSession.__table__,CaptureEvent.__table__,ReadinessSnapshot.__table__,CaptureAttempt.__table__,AttemptDataPlaneVerification.__table__,CoverageWindow.__table__,CoverageTrack.__table__,CoverageInterval.__table__,QualitySnapshot.__table__,SignalAvailability.__table__,EvidenceAsset.__table__])
    F=sessionmaker(bind=engine,expire_on_commit=False)
    with F() as db, db.begin():
        db.add(CaptureSession(id='S',reproduction_session_id='R',device_id='D',state=CaptureSessionState.PREPARING.value,
            health_status=CaptureHealth.HEALTHY.value,capture_profile_id='p',capture_profile_version='1',platform_profile_id='mt7621',platform_profile_version='1',effective_profile={}))
    return F


def test_raw_hook_glitch_is_audited_but_capture_attempt_is_classified_glitch():
    F=factory(); repo=AttemptSemanticRepository(F); san=FxsEventSanitizer(hook_glitch_max_ms=100,post_onhook_rebound_window_ms=500)
    t0=datetime(2026,8,20,tzinfo=timezone.utc)
    for ev in [RawFxsEvent(t0,'ONHOOK'),RawFxsEvent(t0+timedelta(milliseconds=20),'OFFHOOK'),RawFxsEvent(t0+timedelta(milliseconds=40),'ONHOOK')]:
        repo.append_raw_event(capture_session_id='S',source_ts=ev.source_ts,event=ev.event,digit=ev.digit,line=0)
        for action in san.on_raw(ev): repo.apply(capture_session_id='S',action=action)
    with F() as db:
        raw=[e for e in db.query(CaptureEvent).all() if e.entity_type=='FXS_RAW']; assert len(raw)==3
        a=db.query(CaptureAttempt).one(); assert a.state=='CLASSIFIED_GLITCH'; assert a.classification=='FXS_HOOK_GLITCH'


def test_sip_expectation_timer_is_trigger_relative_not_offhook_relative():
    F=factory(); t0=datetime(2026,8,20,tzinfo=timezone.utc)
    with F() as db, db.begin():
        db.add(CaptureAttempt(id='A',capture_session_id='S',attempt_no=1,state='CONFIRMED',candidate_start_source_ts=t0,confirmed_start_source_ts=t0))
    v=AttemptDataPlaneVerifier(F)
    v.create_expectation(capture_attempt_id='A',expectation=ChannelExpectation('SIP',3),expectation_created_at=t0+timedelta(seconds=7))
    assert v.expire_due(capture_attempt_id='A',now=t0+timedelta(seconds=8))==()
    assert v.expire_due(capture_attempt_id='A',now=t0+timedelta(seconds=11))==('SIP',)


def test_evidence_report_refuses_conclusion_when_required_audio_missing():
    F=factory(); assets=EvidenceAssetRepository(F); graph=assets.create(capture_session_id='S',asset_type='RTP_GRAPH',title='RTP loss')
    report=EvidenceFirstReportBuilder(F).build(capture_session_id='S',quality={},findings=[FindingEvidenceRequest(
        finding_id='F1',title='电流音',conclusion='RTP loss caused audio defect',confidence='HIGH',
        required_asset_types=('RTP_GRAPH','ABNORMAL_AUDIO'),evidence_asset_ids=(graph,),why=('loss aligned',))])
    f=report['findings'][0]; assert f['supported'] is False; assert f['conclusion']=='EVIDENCE_INSUFFICIENT_FOR_CONCLUSION'; assert 'ABNORMAL_AUDIO' in f['missing_evidence_types']
