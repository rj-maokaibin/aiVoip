from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import AIProposalRecord, Case, Evidence
from app.diagnosis.ai_proposal import run_ai_shadow


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
        object_key=f'cases/{case_id}/{name}', size_bytes=1, sha256='d' * 64,
    )
    db.add(row)
    db.flush()
    return row


class FakeGateway:
    model = 'fake-shadow-model'

    def __init__(self, response: dict):
        self.response = response

    def enabled(self):
        return True

    def enhance(self, snapshot, baseline):
        return self.response


def _proposal(real_evidence_id: str, hallucinated: str) -> dict:
    return {
        'schema_version': 'ai-proposal-v2',
        'intent': 'DIAGNOSIS_ENHANCEMENT',
        'hypotheses': [{
            'code': 'AI_ONE_WAY_AUDIO', 'title': 'candidate', 'fault_domain': 'RTP',
            'confidence': 0.6, 'rationale': 'structured summary only',
            'supporting_evidence_ids': [real_evidence_id, hallucinated],
            'contradicting_evidence_ids': [], 'missing_evidence': [],
        }],
        'claims': [{
            'claim_id': 'claim-1', 'claim_type': 'OBSERVATION',
            'statement': 'media direction candidate', 'subject': 'RTP',
            'predicate': 'direction', 'value': 'RX', 'status': 'PROPOSED',
            'evidence_level': 'L5',
            'evidence': [{
                'evidence_id': hallucinated, 'relation': 'SUPPORT', 'direction': 'RX',
                'call_id': None, 'time_start_ms': None, 'time_end_ms': None, 'note': '',
            }],
            'missing_evidence': [],
        }],
        'known': [], 'unknown': [], 'excluded': [],
        'next_question_key': 'NOT_A_REAL_QUESTION',
        'recommended_action': None,
        'user_explanation': 'AI candidate only, not root cause.',
    }


def test_sanitize_drops_hallucinated_evidence_and_unregistered_question():
    eng = _engine()
    with Session(eng) as db:
        case = _case(db, 'AI-SAN-1')
        evidence = _evidence(db, case.id, 'one-way.pcap')
        hallucinated = 'c9' + '1' * 42  # not a real UUID in the case
        baseline = {'hypotheses': [], 'known': [], 'unknown': [], 'excluded': []}

        row = run_ai_shadow(
            db, case_id=case.id, diagnosis_run_id=None,
            snapshot={'fingerprint': 'c' * 64}, deterministic_baseline=baseline,
            gateway=FakeGateway(_proposal(evidence.id, hallucinated)),
        )

        assert row.status == 'ACCEPTED', row.validation_errors
        assert row.validation_errors == []
        hyp = row.validated_output_json['hypotheses'][0]
        assert hyp['supporting_evidence_ids'] == [evidence.id]
        assert hallucinated not in hyp['supporting_evidence_ids']
        claim = row.validated_output_json['claims'][0]
        assert claim['evidence'] == []
        assert row.validated_output_json['next_question_key'] is None
        assert db.scalar(select(AIProposalRecord).where(AIProposalRecord.id == row.id)) is row


def test_sanitize_keeps_real_evidence_reference_grounded():
    eng = _engine()
    with Session(eng) as db:
        case = _case(db, 'AI-SAN-2')
        evidence = _evidence(db, case.id, 'real.pcap')
        baseline = {'hypotheses': [], 'known': [], 'unknown': [], 'excluded': []}
        proposal = _proposal(evidence.id, 'x9' + '2' * 42)
        proposal['next_question_key'] = None
        # claim references the real evidence id -> must be preserved
        proposal['claims'][0]['evidence'] = [{
            'evidence_id': evidence.id, 'relation': 'SUPPORT', 'direction': 'RX',
            'call_id': None, 'time_start_ms': None, 'time_end_ms': None, 'note': '',
        }]

        row = run_ai_shadow(
            db, case_id=case.id, diagnosis_run_id=None,
            snapshot={'fingerprint': 'b' * 64}, deterministic_baseline=baseline,
            gateway=FakeGateway(proposal),
        )

        assert row.status == 'ACCEPTED', row.validation_errors
        claim = row.validated_output_json['claims'][0]
        assert claim['evidence'][0]['evidence_id'] == evidence.id
