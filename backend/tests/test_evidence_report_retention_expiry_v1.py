from app.reports.evidence_brief import build_completeness
from app.services.evidence_report_scope import evidence_dict
from types import SimpleNamespace


def test_expired_raw_pcap_does_not_count_as_available_capture():
    evidence = SimpleNamespace(
        id='ev-1', type='PCAP', source='TEST', kind='RAW', source_scope='CALL', level='L1',
        completeness='UNAVAILABLE', filename='call.pcap', sha256='a'*64, size_bytes=123,
        session_id='s1', call_id='c1', time_range_start=None, time_range_end=None,
        metadata_json={'retention_status':'EXPIRED','retention_expired_at':'2026-08-18T00:00:00Z','payload_available':False},
    )
    item = evidence_dict(evidence)
    assert item['original_type'] == 'PCAP'
    assert item['type'] == 'EXPIRED_RAW_EVIDENCE'
    assert item['payload_available'] is False
    out = build_completeness(
        evidences=[item],
        analyzer_states={
            'packet_intelligence': {'status':'SUCCESS'},
            'pcm_intelligence': {'status':'SUCCESS'},
            'media_intelligence': {'status':'SUCCESS'},
        },
        scope_type='CALL',
        results={'pcm_intelligence': {'streams': []}},
    )
    assert out['capture']['pcap'] is False
    assert out['state'] == 'PARTIAL'
    assert 'pcap' in out['missing_required_evidence']
