from app.reports.v2.semantic_validator import validate_report_semantics


def _base_report():
    return {
        "call_reconstruction": {
            "invite_time": 1.0,
            "established_time": 2.0,
            "call_end_time": None,
            "termination": {"observed": False},
        },
        "timeline": {
            "media_observation_window": {"start": 2.1, "end": 4.0, "source": "RTP_OBSERVATION"}
        },
        "rtp_streams": [{"packet_count": 10}],
        "visibility": {"end_to_end_media": "PARTIAL"},
        "claims": {"end_to_end_media_complete": False},
        "findings": [],
        "correlation_clusters": [],
        "problem_count": 0,
        "recommendations": [],
        "artifacts": [],
        "artifact_failures": [],
        "preliminary_assessment": {"root_cause_status": "UNCONFIRMED"},
        "events": [],
    }


def _rules(report):
    return {item["rule"] for item in validate_report_semantics(report)["violations"]}


def test_complete_validator_passes_safe_empty_report():
    result = validate_report_semantics(_base_report())
    assert result["status"] == "PASS"
    assert result["ruleset"] == "preliminary-evidence-v2-r001-r015"


def test_r005_rejects_recommendation_that_references_absent_severity():
    report = _base_report()
    report["recommendations"] = [{"target_severity": "HIGH", "action": "review"}]
    assert "R005" in _rules(report)


def test_r006_accepts_structured_audio_render_failure_but_rejects_silent_unbound_source():
    report = _base_report()
    report["findings"] = [{
        "finding_id": "F1", "class": "ABNORMAL", "severity": "MEDIUM",
        "evidence_refs": ["E1"], "requires_audio_clip": True, "audio_source_available": True,
    }]
    report["problem_count"] = 1
    assert "R006" in _rules(report)

    report["artifact_failures"] = [{
        "artifact_requirement": "AUDIO_CLIP", "status": "FAILED",
        "reason_code": "UNSUPPORTED_CODEC", "source_available": True,
        "finding_refs": ["F1"],
    }]
    assert "R006" not in _rules(report)


def test_r008_uses_top_level_end_to_end_visibility_contract():
    report = _base_report()
    report["claims"] = {"end_to_end_media_complete": True}
    assert "R008" in _rules(report)


def test_r010_prevents_preliminary_root_cause_confirmation():
    report = _base_report()
    report["preliminary_assessment"] = {"root_cause_status": "CONFIRMED"}
    assert "R010" in _rules(report)


def test_r011_requires_evidence_for_abnormal_finding():
    report = _base_report()
    report["findings"] = [{"finding_id": "F1", "class": "ABNORMAL", "severity": "MEDIUM"}]
    report["problem_count"] = 1
    assert "R011" in _rules(report)


def test_r012_requires_provenance_on_critical_artifact():
    report = _base_report()
    report["artifacts"] = [{"artifact_id": "A1", "critical": True, "sha256": "abc"}]
    assert "R012" in _rules(report)


def test_r013_checks_relative_time_against_absolute_anchors():
    report = _base_report()
    report["events"] = [{"event_id": "EV1", "timestamp": 3.0, "relative_to_invite": 1.5}]
    assert "R013" in _rules(report)

    report["events"][0]["relative_to_invite"] = 2.0
    assert "R013" not in _rules(report)
