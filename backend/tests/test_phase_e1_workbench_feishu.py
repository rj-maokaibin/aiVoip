from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import (
    CaptureChannelHealth, Case, CaseDevice, DiagnosisRun, Hypothesis,
    ReproductionSession, ReproductionCall,
)
from app.integrations.feishu.cards import FeishuCaseCardBuilder


def _engine():
    eng = create_engine('sqlite+pysqlite:///:memory:')
    Base.metadata.create_all(eng)
    return eng


def test_feishu_case_card_is_single_case_summary_and_never_invents_confirmation():
    eng = _engine()
    with Session(eng) as db:
        case = Case(case_no='E1-001', summary='电流音', status='ANALYZING')
        db.add(case); db.flush()
        dev = CaseDevice(case_id=case.id, ip='192.0.2.10', ssh_port=22, sn='SN-E1', username='admin')
        db.add(dev)
        dr = DiagnosisRun(case_id=case.id, status='RUNNING', cycle=2)
        db.add(dr); db.flush()
        h = Hypothesis(case_id=case.id, diagnosis_run_id=dr.id, code='LOCAL_CAPTURE_PERIODIC_INTERFERENCE', title='本地采集周期干扰', fault_domain='LOCAL_AUDIO_PATH', status='SUPPORTED', confidence=9600)
        db.add(h)
        session = ReproductionSession(
            case_id=case.id, device_id=dev.id, profile_key='AUDIO_NOISE', profile_version='1.0.0', profile_checksum='a'*64,
            effective_profile_snapshot={}, state='WATCHING', capture_stage='BASE', cleanup_required=True, cleanup_status='REQUIRED',
            capture_completeness='COMPLETE', evidence_sufficiency='NOT_EVALUATED',
        )
        db.add(session); db.flush()
        db.add(ReproductionCall(session_id=session.id, case_id=case.id, call_no=1, status='ANALYZED', verdict='NO_MATCH', role='CONTROL'))
        db.flush()
        built = FeishuCaseCardBuilder().build(db, case.id)
        text = str(built.card)
        assert 'E1-001' in text and 'WATCHING' in text and 'CONTROL / TARGET' in text
        assert '支持' in text
        assert '已确认 · 本地采集周期干扰' not in text
        assert '停止自动复现' in text
        assert '请等待 FXS_MONITOR_READY' in text


def test_feishu_card_only_invites_operation_after_fxs_runtime_ready():
    eng = _engine()
    with Session(eng) as db:
        case = Case(case_no='E1-READY', summary='ready gate', status='ANALYZING')
        db.add(case); db.flush()
        dev = CaseDevice(case_id=case.id, ip='192.0.2.10', ssh_port=22, sn='SN-READY', username='admin')
        db.add(dev); db.flush()
        session = ReproductionSession(
            case_id=case.id, device_id=dev.id, profile_key='AUDIO_NOISE',
            profile_version='1.0.0', profile_checksum='b' * 64,
            effective_profile_snapshot={}, state='WATCHING', capture_stage='BASE',
            cleanup_required=True, cleanup_status='REQUIRED', capture_completeness='PARTIAL',
            evidence_sufficiency='NOT_EVALUATED',
        )
        db.add(session); db.flush()
        db.add(CaptureChannelHealth(
            session_id=session.id, channel='DEBUG', status='HEALTHY', packet_count=0,
            health_json={'runtime_ready': True, 'reader_alive': True,
                         'debug_enable_acknowledged': True},
        ))
        db.flush()

        text = str(FeishuCaseCardBuilder().build(db, case.id).card)
        assert '可以开始现场复现：FXS 监听已就绪' in text
        assert '请等待 FXS_MONITOR_READY' not in text


def test_feishu_card_turns_green_after_resolved_case():
    eng = _engine()
    with Session(eng) as db:
        case = Case(case_no='E1-002', summary='DTMF', status='RESOLVED')
        db.add(case); db.flush()
        card = FeishuCaseCardBuilder().build(db, case.id).card
        assert card['header']['template'] == 'green'

def test_phase_e1_read_contract_routes_are_declared_in_api_modules():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1] / 'app' / 'api' / 'v1'
    reproduction = (root / 'reproduction.py').read_text()
    experiments = (root / 'experiments.py').read_text()
    feishu = (root / 'feishu.py').read_text()
    assert "'/cases/{case_id}/reproductions'" in reproduction
    assert "'/cases/{case_id}/experiments'" in experiments
    assert "'/cases/{case_id}/fix-actions'" in experiments
    assert "'/cases/{case_id}/fix-verifications'" in experiments
    assert "'/cases/{case_id}/feishu/card-preview'" in feishu

def test_feishu_notification_policy_keeps_routine_calls_silent_and_alerts_target_cleanup_fix():
    from app.integrations.feishu.policy import FeishuNotificationPolicy
    p = FeishuNotificationPolicy()
    assert p.decide('REPRODUCTION_CALL_CHANGED', {'verdict':'NO_MATCH'}).notify is False
    assert p.decide('TARGET_CONFIRMED', {}).notify is True
    assert p.decide('CLEANUP_ALERT', {}).priority == 'CRITICAL'
    assert p.decide('FXS_MONITOR_READY', {}).notify is True
    assert p.decide('FXS_MONITOR_FAILED', {}).priority == 'CRITICAL'
    assert p.decide('FIX_VERIFICATION_UPDATED', {'status':'FIX_VERIFIED'}).notify is True
    assert p.decide('REPRODUCTION_STATE_CHANGED', {'state':'WATCHING'}).notify is False
