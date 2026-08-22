from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "golden_cases" / "OFFLINE_ANALYSIS_20260814_001" / "manifest.yaml"


def _manifest() -> dict:
    return yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))


def test_offline_golden_manifest_cannot_claim_live_acquisition_coverage():
    manifest = _manifest()
    classification = manifest["classification"]
    assert classification["kind"] == "OFFLINE_ANALYSIS_GOLDEN_E2E"
    assert classification["source_mode"] == "IMPORTED_EVIDENCE"
    assert classification["live_acquisition_covered"] is False
    assert "packet_analysis" in classification["validates"]
    assert "canonical_report_semantics" in classification["validates"]


def test_offline_golden_fixture_is_content_addressed():
    source = _manifest()["source"]
    assert source["filename"] == "tcpdump-2026-08-14(2).pcap"
    assert source["sha256"] == "b038aa7c9a0644581f2815f654fcdee4620860796382265b178823fccba2e3f0"
    assert source["fixture_env"] == "VOIP_OFFLINE_GOLDEN_001_PCAP"
    assert source["pcm_profile"] == "ruijie_aim_diag_v1"


def test_ground_truth_has_explicit_answer_leakage_prohibition():
    rule = str(_manifest()["ground_truth"]["leakage_rule"])
    assert "不得进入生产 Analyzer" in rule
    assert "FindingComposer" in rule
    assert "Diagnosis" in rule
    assert "AI Prompt" in rule


def test_b2bua_truth_keeps_raw_legs_but_selects_one_dut_diagnostic_call():
    expected = _manifest()["expected"]
    call = expected["call"]
    context = expected["analysis_context"]
    assert call["raw_sip_leg_count"] == 2
    assert call["diagnostic_call_count"] == 1
    assert call["selected_sip_call_id"] == "00ad1c804c33b255@192.168.3.200"
    assert call["other_sip_leg"]["role"] == "PBX_B2BUA_INTERNAL_LEG"
    assert context["selection_rule"] == "PCM_SOURCE_DEVICE_IDENTITY_MATCH"
    assert context["subject_device_ip"] == "192.168.150.4"
    assert context["reviewability"] == "FULLY_REVIEWABLE"


def test_pcm_and_dtmf_truth_are_subject_bound_not_answer_leaked():
    expected = _manifest()["expected"]
    assert expected["pcm"]["source_device_ip"] == "192.168.150.4"
    assert expected["pcm"]["taps"]["pcm_rx"]["packet_count"] == 6525
    assert expected["pcm"]["taps"]["pcm_tx"]["packet_count"] == 6525
    assert expected["dtmf"]["expected_match_count"] == 1
    assert expected["dtmf"]["call_id"] == expected["call"]["selected_sip_call_id"]
