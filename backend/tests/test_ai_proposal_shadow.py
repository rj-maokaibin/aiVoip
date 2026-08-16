from __future__ import annotations

from copy import deepcopy

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import AIProposalRecord, Case, Evidence
from app.diagnosis.ai_proposal import AIProposalValidator, run_ai_shadow


def _engine():
    eng = create_engine('sqlite+pysqlite:///:memory:')
    Base.metadata.create_all(eng)
    return eng


def _case(db: Session, no: str) -> Case:
    row = Case(case_no=no, summary='one-way audio', status='ANALYZING')
    db.add(row)
    db.flush()
    return row


def _evidence(db: Session, case_id: str, name: str) -> Evidence:
    row = Evidence(
        case_id=case_id, type='PCAP', source='UPLOAD', filename=name,
        object_key=f'cases/{case_id}/{name}', size_bytes=1, sha256='a' * 64,
    )
    db.add(row)
    db.flush()
    return row


def _proposal(evidence_id: str) -> dict:
    return {
        'schema_version': 'ai-proposal-v1',
        'intent': 'DIAGNOSIS_ENHANCEMENT',
        'hypotheses': [{
            'code': 'AI_ONE_WAY_AUDIO', 'title': '候选：媒体单向不可达',
            'fault_domain': 'RTP', 'confidence': 0.95,
            'rationale': '结构化媒体摘要显示方向不对称，仍需确定性证据确认。',
            'supporting_evidence_ids': [evidence_id],
            'contradicting_evidence_ids': [], 'missing_evidence': ['反向 RTP 统计'],
        }],
        'known': [], 'unknown': ['反向 RTP 是否到达'], 'excluded': [],
        'next_question_key': None, 'recommended_action': None,
        'user_explanation': '这是 AI 候选解释，不是已确认根因。',
    }


class FakeGateway:
    model = 'fake-shadow-model'

    def __init__(self, response: dict | None = None, error: Exception | None = None):
        self.response = response or {}
        self.error = error

    def enabled(self):
        return True

    def enhance(self, snapshot, baseline):
        if self.error:
            raise self.error
        return self.response


def test_shadow_accepts_valid_proposal_but_never_changes_formal_baseline():
    eng = _engine()
    with Session(eng) as db:
        case = _case(db, 'AI-S-1')
        evidence = _evidence(db, case.id, 'one-way.pcap')
        baseline = {'hypotheses': [{'code': 'DET_RTP_DIRECTION'}],
                    'known': ['deterministic fact'], 'unknown': [], 'excluded': []}
        original = deepcopy(baseline)

        row = run_ai_shadow(
            db, case_id=case.id, diagnosis_run_id=None,
            snapshot={'fingerprint': 'f' * 64}, deterministic_baseline=baseline,
            gateway=FakeGateway(_proposal(evidence.id)),
        )

        assert row.status == 'ACCEPTED'
        hypothesis = row.validated_output_json['hypotheses'][0]
        assert hypothesis['confidence'] == 0.75
        assert hypothesis['status'] == 'OPEN'
        assert hypothesis['confirmable'] is False
        assert hypothesis['evidence_level'] == 'L5'
        assert row.diff_json['formal_result_changed'] is False
        assert baseline == original
        assert db.scalar(select(AIProposalRecord).where(AIProposalRecord.id == row.id)) is row


def test_validator_rejects_cross_case_or_missing_evidence_reference():
    eng = _engine()
    with Session(eng) as db:
        case_a = _case(db, 'AI-S-2A')
        case_b = _case(db, 'AI-S-2B')
        foreign = _evidence(db, case_b.id, 'foreign.pcap')
        validated, errors = AIProposalValidator().validate(
            db, case_id=case_a.id, raw=_proposal(foreign.id),
            deterministic_baseline={'excluded': []},
        )
        assert validated is None
        assert {'code': 'EVIDENCE_NOT_IN_CASE', 'evidence_id': foreign.id} in errors


def test_validator_rejects_executable_command_content():
    eng = _engine()
    with Session(eng) as db:
        case = _case(db, 'AI-S-3')
        evidence = _evidence(db, case.id, 'safe.pcap')
        proposal = _proposal(evidence.id)
        proposal['user_explanation'] = '请执行 ssh root@device'
        validated, errors = AIProposalValidator().validate(
            db, case_id=case.id, raw=proposal, deterministic_baseline={'excluded': []},
        )
        assert validated is None
        assert any(x['code'] == 'COMMAND_OR_TEMPLATE_FORBIDDEN' for x in errors)


def test_shadow_gateway_failure_is_persisted_as_degraded():
    eng = _engine()
    with Session(eng) as db:
        case = _case(db, 'AI-S-4')
        row = run_ai_shadow(
            db, case_id=case.id, diagnosis_run_id=None,
            snapshot={'fingerprint': 'e' * 64}, deterministic_baseline={'hypotheses': []},
            gateway=FakeGateway(error=TimeoutError('timeout')),
        )
        assert row.status == 'DEGRADED'
        assert row.validated_output_json is None
        assert 'TimeoutError' in row.gateway_error
