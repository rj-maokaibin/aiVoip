"""Diagnosis reasoner consumes autonomous-reproduction CALL_QUICK evidence tests.

Closes the gap where reproduction CALL_QUICK evidence was collected in the case
snapshot but never reasoned over by the deterministic diagnosis reasoner.
"""
from __future__ import annotations

from app.diagnosis.reasoner import DeterministicDiagnosisReasoner

R = DeterministicDiagnosisReasoner()


def _repro_snap(*, summary='电流音', findings=None, verdict='MATCH', role='TARGET',
                input_evidence_ids=('ev1',)):
    result = {
        'summary': {
            'mode': 'CALL_QUICK', 'verdict': verdict, 'role': role,
            'findings': findings or [],
            'media_summary': {'decoded_rtp_track_count': 1},
            'packet_summary': {}, 'pcm_summary': {},
        },
        'analysis': {},
    }
    analyzers = {
        'REPRODUCTION_CALL_QUICK_EVIDENCE': {
            'run_id': 'run-repro-1', 'status': 'SUCCESS', 'version': '2.0.0-c2',
            'result': result, 'summary': result['summary'],
            'input_evidence_ids': list(input_evidence_ids),
        },
    }
    # Reproduction evidence exists (CALL_QUICK_FINDINGS / CALL_PCAP) so the
    # reasoner passes the "no evidence" early-return and reaches reproduction.
    return {'case': {'id': 'c', 'summary': summary}, 'devices': [{'id': 'd'}],
            'evidences': [{'id': 'ev1', 'type': 'CALL_QUICK_FINDINGS', 'source': 'REPRODUCTION_CALL_QUICK',
                           'filename': 'call_quick.json', 'sha256': 'x', 'metadata': {}}],
            'analyzers': analyzers, 'fingerprint': 'x'}


def test_periodic_interference_repro_raises_hum_hypothesis():
    d = R.reason(_repro_snap(findings=['PERIODIC_INTERFERENCE', 'ACTIVE_MEDIA_WINDOW', 'CALL_CLASSIFICATION']))
    codes = {h.code for h in d.hypotheses}
    assert 'LOCAL_CAPTURE_PERIODIC_INTERFERENCE' in codes
    h = next(h for h in d.hypotheses if h.code == 'LOCAL_CAPTURE_PERIODIC_INTERFERENCE')
    assert h.status == 'SUPPORTED'
    # AUDIO_NOISE symptom keeps full confidence; check known note present.
    assert any('自动复现CALL_QUICK' in k for k in d.known)


def test_burst_loss_repro_raises_packet_loss_hypothesis():
    d = R.reason(_repro_snap(summary='通话卡顿断音', findings=['RTP_BURST_LOSS']))
    codes = {h.code for h in d.hypotheses}
    assert 'RTP_PACKET_LOSS_PATH' in codes


def test_one_way_audio_repro_raises_hypothesis():
    d = R.reason(_repro_snap(summary='单通', findings=['ONE_WAY_RTP_MEDIA']))
    codes = {h.code for h in d.hypotheses}
    assert 'ONE_WAY_AUDIO_PATH' in codes


def test_sip_call_failed_repro_raises_hypothesis():
    d = R.reason(_repro_snap(summary='呼叫失败', findings=['SIP_CALL_FAILED']))
    codes = {h.code for h in d.hypotheses}
    assert 'SIP_CALL_SETUP_FAILURE' in codes


def test_target_match_without_finding_requests_media_analysis():
    d = R.reason(_repro_snap(findings=['ACTIVE_MEDIA_WINDOW', 'CALL_CLASSIFICATION']))
    # verdict MATCH but no mapped abnormal finding -> request media analysis on the
    # reproduction input evidence.
    assert any(a.action_type == 'RUN_MEDIA_ANALYSIS' for a in d.plan)


def test_no_reproduction_analyzer_falls_through_to_basic():
    d = R.reason({'case': {'id': 'c', 'summary': 'test'}, 'devices': [{'id': 'd'}],
                  'evidences': [], 'analyzers': {}, 'fingerprint': 'x'})
    assert d.conclusion_state == 'NEED_MORE_EVIDENCE'
    assert d.plan[0].action_type == 'COLLECT_PROFILE'
