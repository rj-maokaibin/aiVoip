from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from app.analyzers.media.engine import MediaIntelligenceEngine
from app.analyzers.packet.tshark import TSharkAdapter
from app.analyzers.pcm.profile import load_pcm_profile
from app.diagnosis.reasoner import DeterministicDiagnosisReasoner
from tools.fullstack_fixture import build_periodic_fixture


def test_fullstack_synthetic_fixture_reaches_periodic_supported(tmp_path: Path):
    pcap = tmp_path / 'periodic.pcap'
    build_periodic_fixture(pcap, seconds=2.2)
    profile = load_pcm_profile(Path('profiles/pcm/ruijie_aim_diag_v1.yaml'))
    result = MediaIntelligenceEngine(profile, TSharkAdapter(binary='__missing_tshark__')).analyze_pcap(pcap, tmp_path / 'media')
    assert result['summary']['rtp_stream_count'] == 2
    assert result['summary']['periodic_interference_count'] >= 1
    snap = {
        'case': {'id':'c','summary':'现场持续电流音'},
        'devices': [],
        'evidences': [{'id':'e','type':'PCAP','filename':'periodic.pcap'}],
        'analyzers': {'media_intelligence': {'run_id':'r','status':result['status'],'version':result['version'],'result':result}},
        'fingerprint':'fixture',
    }
    decision = DeterministicDiagnosisReasoner().reason(snap)
    target = next(h for h in decision.hypotheses if h.code == 'LOCAL_CAPTURE_PERIODIC_INTERFERENCE')
    assert target.status == 'SUPPORTED'
    assert target.confidence >= 0.90
    assert not any(h.status == 'CONFIRMED' for h in decision.hypotheses)
