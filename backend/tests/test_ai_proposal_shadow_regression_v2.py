from __future__ import annotations

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
        object_key=f'cases/{case_id}/{name}', size_bytes=1, sha256='b' * 64,
    )
    db.add(row)
    db.flush()
    return row


def _proposal_v2(evidence_id: str) -> dict:
    """Exact ai-proposal-v2 output contract the gateway system prompt must produce."""
    return {
        'schema_version': 'ai-proposal-v2',
        'intent': 'DIAGNOSIS_ENHANCEMENT',
        'hypotheses': [{
            'code': 'AI_ONE_WAY_AUDIO',
            'title': '候选：媒体单向不可达',
            'fault_domain': 'RTP',
            'confidence': 0.7,
            'rationale': '结构化媒体摘要显示方向不对称，仍需确定性证据确认。',
            'supporting_evidence_ids': [evidence_id],
            'contradicting_evidence_ids': [],
            'missing_evidence': ['反向 RTP 统计'],
        }],
        'claims': [{
            'claim_id': 'claim-1',
            'claim_type': 'OBSERVATION',
            'statement': 'RX 方向存在单向媒体流候选',
            'subject': 'RTP',
            'predicate': 'direction',
            'value': 'RX',
            'status': 'PROPOSED',
            'evidence_level': 'L5',
            'evidence': [{
                'evidence_id': evidence_id,
                'relation': 'SUPPORT',
                'direction': 'RX',
                'call_id': None,
                'time_start_ms': None,
                'time_end_ms': None,
                'note': '',
            }],
            'missing_evidence': [],
        }],
        'known': [], 'unknown': ['反向 RTP 是否到达'], 'excluded': [],
        'next_question_key': None, 'recommended_action': None,
        'user_explanation': '这是 AI 候选解释，不是已确认根因。',
    }


class FakeGateway:
    model = 'fake-shadow-model'

    def __init__(self, response: dict):
        self.response = response

    def enabled(self):
        return True

    def enhance(self, snapshot, baseline):
        return self.response


def test_ai_proposal_v2_schema_accepts_exact_contract_fields():
    """Lock the ai-proposal-v2 contract so the gateway prompt stays correct."""
    eng = _engine()
    with Session(eng) as db:
        case = _case(db, 'AI-V2-1')
        evidence = _evidence(db, case.id, 'one-way.pcap')
        baseline = {'hypotheses': [], 'known': [], 'unknown': [], 'excluded': []}

        row = run_ai_shadow(
            db, case_id=case.id, diagnosis_run_id=None,
            snapshot={'fingerprint': 'f' * 64}, deterministic_baseline=baseline,
            gateway=FakeGateway(_proposal_v2(evidence.id)),
        )

        assert row.status == 'ACCEPTED', row.validation_errors
        assert row.schema_version == 'ai-proposal-v2'
        assert row.intent == 'DIAGNOSIS_ENHANCEMENT'
        assert row.validation_errors == []
        hyp = row.validated_output_json['hypotheses'][0]
        assert hyp['confidence'] == 0.7
        assert hyp['status'] == 'OPEN'
        assert hyp['confirmable'] is False
        assert hyp['evidence_level'] == 'L5'
        claim = row.validated_output_json['claims'][0]
        assert claim['evidence_level'] == 'L5'
        assert claim['status'] == 'PROPOSED'
        # persisted to DB without psycopg "cannot adapt dict" failure
        assert db.scalar(select(AIProposalRecord).where(AIProposalRecord.id == row.id)) is row


def test_schema_version_dict_is_coerced_to_string_when_persisting():
    """Regression: a dict schema_version must not crash the INSERT into a VARCHAR column."""
    eng = _engine()
    with Session(eng) as db:
        case = _case(db, 'AI-V2-2')
        evidence = _evidence(db, case.id, 'one-way.pcap')
        baseline = {'hypotheses': [], 'known': [], 'unknown': [], 'excluded': []}
        proposal = _proposal_v2(evidence.id)
        proposal['schema_version'] = {
            'intent': 'DIAGNOSIS_ENHANCEMENT', 'version': 'ai-proposal-v2',
        }

        row = run_ai_shadow(
            db, case_id=case.id, diagnosis_run_id=None,
            snapshot={'fingerprint': 'a' * 64}, deterministic_baseline=baseline,
            gateway=FakeGateway(proposal),
        )

        # dict schema_version fails validation -> REJECTED, but must persist cleanly
        assert row.status == 'REJECTED'
        assert row.schema_version == 'ai-proposal-v2'
        assert isinstance(row.schema_version, str)
        assert row.validation_errors
        assert db.scalar(select(AIProposalRecord).where(AIProposalRecord.id == row.id)) is row
