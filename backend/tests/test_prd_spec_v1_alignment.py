from types import SimpleNamespace

from app.contracts.evidence_report import P0_FINDING_TYPES, P0_MEASUREMENT_CAPABILITIES
from app.reports.prd_spec_v1_alignment import (
    COMPLETENESS_DIMENSIONS,
    build_evidence_completeness,
    finalize_report_contract,
    scalar_media_metrics,
)
from app.services.evidence_report_aggregation import _ab_comparison, _normal_baseline_comparison


FROZEN_SCHEMA_FIELDS = {
    "schema", "report_id", "scope_type", "scope_id", "version", "status",
    "case", "environment", "capture_quality", "packet_summary",
    "signaling_summary", "media_flows", "pcm_summary", "findings",
    "normal_evidence", "artifacts", "preliminary_assessment",
    "evidence_boundary", "traceability", "generated_at",
}


def _complete_payload() -> dict:
    return {
        "schema_version": "preliminary-evidence-report-v1",
        "composer_version": "evidence-brief-composer-v4",
        "report_version": 1,
        "generated_at": "2026-08-22T00:00:00+00:00",
        "scope": {"type": "CALL", "id": "CALL-1"},
        "case": {"id": "CASE-1"},
        "environment": {"dut_model": "DUT-A"},
        "environment_fingerprint": "ENV-A",
        "completeness": {
            "state": "COMPLETE",
            "capture": {"pcap": True, "pcm_rx": True, "pcm_tx": True, "debug": True},
            "analyzers": {"media": {"available": True}},
        },
        "packet_summary": {
            "available": True,
            "sip_message_count": 10,
            "rtp_stream_count": 1,
            "calls": [{"call_id": "sip-1"}],
            "streams": [{
                "loss_rate": 0.0,
                "p95_jitter_ms": 1.5,
                "max_delta_ms": 20.0,
            }],
        },
        "pcm_summary": {
            "available": True,
            "streams": [
                {"tap": {"name": "pcm_rx"}, "sessions": [{"rms_dbfs": -20.0, "peak_dbfs": -5.0, "hum": {"score": 0.01}}]},
                {"tap": {"name": "pcm_tx"}, "sessions": [{"rms_dbfs": -21.0, "peak_dbfs": -6.0, "hum": {"score": 0.02}}]},
            ],
        },
        "findings": [],
        "normal_and_exclusion_evidence": [{"text": "双向 RTP 正常"}],
        "artifacts": [],
        "preliminary_assessment": {"summary": "normal"},
        "evidence_boundary": {"statement": "preliminary only"},
        "analyzers": {"packet_intelligence": {"status": "SUCCESS"}},
        "input_snapshot_hash": "abc",
    }


def _report(**overrides):
    values = {
        "id": "REPORT-1",
        "case_id": "CASE-1",
        "session_id": "SESSION-1",
        "call_id": "CALL-1",
        "scope_type": "CALL",
        "scope_id": "CALL-1",
        "version": 1,
        "status": "COMPOSING",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def test_fr014_frozen_completeness_has_exact_seven_dimensions():
    result = build_evidence_completeness(_complete_payload())
    assert tuple(result["dimensions"]) == COMPLETENESS_DIMENSIONS
    assert result["state"] == "COMPLETE"
    assert result["missing"] == []


def test_fr014_missing_debug_fails_closed_and_downgrades_report():
    payload = _complete_payload()
    payload["completeness"]["capture"]["debug"] = False
    report = _report()

    finalize_report_contract(report, payload)

    assert payload["capture_quality"]["dimensions"]["DEBUG"]["available"] is False
    assert payload["capture_quality"]["state"] == "PARTIAL"
    assert payload["status"] == "PARTIAL_COMPLETE"
    assert report.status == "PARTIAL_COMPLETE"


def test_spec_section_5_canonical_schema_fields_are_materialized():
    payload = _complete_payload()
    report = _report()

    finalize_report_contract(report, payload)

    assert FROZEN_SCHEMA_FIELDS <= set(payload)
    assert payload["schema"] == "preliminary-evidence-report-v1"
    assert payload["report_id"] == "REPORT-1"
    assert payload["scope_type"] == "CALL"
    assert payload["scope_id"] == "CALL-1"
    assert payload["version"] == 1
    assert payload["status"] == "COMPLETE"
    assert payload["normal_evidence"] == payload["normal_and_exclusion_evidence"]
    assert payload["traceability"]["input_snapshot_hash"] == "abc"


def test_fr018_scalar_media_dimensions_cover_network_pcm_level_and_spectrum():
    metrics = scalar_media_metrics(_complete_payload())
    assert metrics["rtp_loss_rate_mean"] == 0.0
    assert metrics["rtp_p95_jitter_ms_mean"] == 1.5
    assert metrics["rtp_max_delta_ms_mean"] == 20.0
    assert metrics["pcm_rms_dbfs_mean"] == -20.5
    assert metrics["pcm_peak_dbfs_mean"] == -5.5
    assert metrics["spectrum_periodic_score_mean"] == 0.015


def test_fr018_ab_exposes_all_frozen_compare_dimensions():
    group_a = {
        "environment_fingerprint": "ENV-A",
        "call_count": 2,
        "finding_groups": [],
        "metric_summary": {
            "rtp_loss_rate_mean": 0.0,
            "rtp_p95_jitter_ms_mean": 1.0,
            "rtp_max_delta_ms_mean": 20.0,
            "pcm_rms_dbfs_mean": -20.0,
            "pcm_peak_dbfs_mean": -5.0,
            "spectrum_periodic_score_mean": 0.01,
        },
        "evidence_boundaries": ["A boundary"],
    }
    group_b = {
        "environment_fingerprint": "ENV-B",
        "call_count": 2,
        "finding_groups": [],
        "metric_summary": {
            "rtp_loss_rate_mean": 1.0,
            "rtp_p95_jitter_ms_mean": 5.0,
            "rtp_max_delta_ms_mean": 80.0,
            "pcm_rms_dbfs_mean": -30.0,
            "pcm_peak_dbfs_mean": -10.0,
            "spectrum_periodic_score_mean": 0.2,
        },
        "evidence_boundaries": ["B boundary"],
    }

    result = _ab_comparison([group_a, group_b])[0]

    assert result["dimensions"] == {
        "reproduction_rate": "AVAILABLE",
        "finding": "AVAILABLE",
        "network_media": "AVAILABLE",
        "pcm": "AVAILABLE",
        "digital_level": "AVAILABLE",
        "spectrum": "AVAILABLE",
        "evidence_boundary": "AVAILABLE",
    }
    assert all(item["status"] == "COMPARABLE" for item in result["metric_differences"])


def test_fr019_baseline_requires_exact_environment_and_normal_evidence():
    payload = _complete_payload()
    payload["environment_fingerprint"] = "ENV-A"
    wrong_env = SimpleNamespace(
        id="R-B", scope_id="CALL-B", environment_fingerprint="ENV-B",
        snapshot_json={"finding_count": 0, "normal_evidence": [{"text": "ok"}]},
    )

    unmatched = _normal_baseline_comparison(None, payload=payload, reports=[wrong_env], current_scope_id="CALL-1")
    assert unmatched["status"] == "NOT_MATCHED"
    assert unmatched["reason"] == "NO_EXACT_ENVIRONMENT_NORMAL_BASELINE"

    normal = SimpleNamespace(
        id="R-A", scope_id="CALL-A", environment_fingerprint="ENV-A",
        snapshot_json={**_complete_payload(), "finding_count": 0},
    )
    matched = _normal_baseline_comparison(None, payload=payload, reports=[normal], current_scope_id="CALL-1")
    assert matched["status"] == "MATCHED"
    assert matched["baseline_report_ids"] == ["R-A"]
    assert matched["minimum_match_rule"] == "EXACT_ENVIRONMENT_FINGERPRINT_AND_ZERO_FINDING_NORMAL_EVIDENCE"


def test_spec_section_28_concrete_p0_gate_includes_current_deterministic_types():
    assert "SIP_CONFLICTING_FINAL_RESPONSE" in P0_FINDING_TYPES
    assert "PAYLOAD_CHANGE" in P0_FINDING_TYPES
    assert {"RTP_RFC3550_JITTER", "RTP_PTIME", "PCM_RMS_DBFS", "PCM_PEAK_DBFS", "EVIDENCE_COMPLETENESS_7D"} <= P0_MEASUREMENT_CAPABILITIES
