from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
CONTRACT = ROOT / "golden_cases" / "pr2_field_20260814_candidate_decision.json"
RELEASE_GATE = ROOT / "tools" / "voip_ai_release_gate.sh"
EVIDENCE_GATE = ROOT / "tools" / "evidence_report_release_gate.py"
FIELD_GATE = ROOT / "tools" / "pr2_field_candidate_gate.py"


def test_pr2_field_contract_freezes_source_identity_and_candidate_expectations():
    payload = json.loads(CONTRACT.read_text(encoding="utf-8"))

    assert payload["schema_version"] == "pr2-field-candidate-golden-v1"
    assert payload["source"]["sha256"] == "b038aa7c9a0644581f2815f654fcdee4620860796382265b178823fccba2e3f0"
    assert payload["source"]["size_bytes"] == 4544926
    assert payload["profiles"]["analyzer_profile_version"] == "1.2.0"

    expected = payload["expected"]
    assert expected["dtmf_sequence"] == "601"
    assert expected["raw_pcm_click_negative_control"]["reason_code"] == "NEGCTRL_DTMF_TRANSIENT"
    assert expected["pcm_tx_silence"]["active_media_candidate_count"] == 8
    assert expected["pcm_tx_silence"]["promoted_count"] == 0
    assert expected["pcm_tx_silence"]["suppressed_count"] == 8
    assert expected["pcm_tx_silence"]["required_reason_code"] == "NEGCTRL_MATCHED_RTP_SOURCE_SILENCE"
    assert expected["pcm_tx_rtp_correlation"]["expected_lag_ms"] == 44.0
    assert expected["pcm_tx_rtp_correlation"]["alignment_rule"] == "rtp_window = pcm_window - correlation_lag"
    assert "UNEXPECTED_SILENCE" in expected["report_invariants"]["must_not_contain_finding_types"]


def test_controlled_release_gate_can_inject_external_field_pcap_without_committing_payload():
    release_text = RELEASE_GATE.read_text(encoding="utf-8")
    evidence_text = EVIDENCE_GATE.read_text(encoding="utf-8")
    field_text = FIELD_GATE.read_text(encoding="utf-8")

    assert "VOIP_PR2_FIELD_PCAP" in release_text
    assert "--field-pcap" in release_text
    assert "--field-pcap" in evidence_text
    assert "PR2_FIELD_CANDIDATE_GOLDEN" in evidence_text
    assert "SOURCE_SHA256" in field_text
    assert "PCM_TX_SILENCE_DECISIONS" in field_text
    assert "PCM_TX_RTP_CORRELATION" in field_text
