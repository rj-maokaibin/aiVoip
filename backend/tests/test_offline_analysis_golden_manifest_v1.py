from __future__ import annotations

from pathlib import Path

import yaml


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "golden_cases" / "OFFLINE_ANALYSIS_20260814_001" / "manifest.yaml"


def test_offline_golden_manifest_cannot_claim_live_acquisition_coverage():
    manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    classification = manifest["classification"]
    assert classification["kind"] == "OFFLINE_ANALYSIS_GOLDEN_E2E"
    assert classification["source_mode"] == "IMPORTED_EVIDENCE"
    assert classification["live_acquisition_covered"] is False
    assert "packet_analysis" in classification["validates"]
    assert "canonical_report_semantics" in classification["validates"]


def test_offline_golden_fixture_is_content_addressed():
    manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    source = manifest["source"]
    assert source["filename"] == "tcpdump-2026-08-14(2).pcap"
    assert source["sha256"] == "b038aa7c9a0644581f2815f654fcdee4620860796382265b178823fccba2e3f0"
    assert source["fixture_env"] == "VOIP_OFFLINE_GOLDEN_001_PCAP"
    assert source["pcm_profile"] == "ruijie_aim_diag_v1"


def test_ground_truth_has_explicit_answer_leakage_prohibition():
    manifest = yaml.safe_load(MANIFEST.read_text(encoding="utf-8"))
    rule = str(manifest["ground_truth"]["leakage_rule"])
    assert "不得进入生产 Analyzer" in rule
    assert "FindingComposer" in rule
    assert "Diagnosis" in rule
    assert "AI Prompt" in rule
