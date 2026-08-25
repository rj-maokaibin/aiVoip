from __future__ import annotations

import copy
import hashlib
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from app.analyzers.media import MediaIntelligenceEngine
from app.analyzers.media.candidate_artifacts import gate_candidate_audio_artifacts, sanitize_gated_media_pcm
from app.analyzers.media.candidate_decision import CANDIDATE_DECISION_VERSION, apply_candidate_decisions
from app.analyzers.packet import TSharkAdapter
from app.analyzers.pcm import load_pcm_profile
from app.diagnosis.reasoner import DeterministicDiagnosisReasoner
from app.reports.actionable_summary import attach_actionable_summary
from app.reports.evidence_brief import build_report_payload
from app.services.evidence_boundary import apply_first_observable_boundaries
from app.services.evidence_report_context import resolve_report_analysis_context


GATED_MEDIA_CONTRACT_VERSION = "0.5.0"


@dataclass(frozen=True, slots=True)
class GoldenCheck:
    name: str
    passed: bool
    actual: Any
    expected: Any
    category: str
    blocking: bool = True


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _diagnosis_dict(decision) -> dict:
    return {
        "state": decision.conclusion_state,
        "summary": decision.summary,
        "known": decision.known,
        "unknown": decision.unknown,
        "excluded": decision.excluded,
        "hypotheses": [
            {
                "code": item.code,
                "title": item.title,
                "status": item.status,
                "confidence": item.confidence,
                "fault_domain": item.fault_domain,
            }
            for item in decision.hypotheses
        ],
        "plan": [item.to_dict() for item in sorted(decision.plan, key=lambda item: item.priority)],
    }


def build_offline_analysis_bundle(
    *,
    pcap_path: str | Path,
    pcm_profile_path: str | Path,
    output_dir: str | Path,
    tshark_binary: str = "tshark",
) -> dict:
    """Replay production analysis without exposing Golden truth to production code."""
    pcap_path = Path(pcap_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    profile = load_pcm_profile(Path(pcm_profile_path))
    engine = MediaIntelligenceEngine(profile, TSharkAdapter(binary=tshark_binary))
    raw_media = engine.analyze_pcap(pcap_path, output_dir)

    # Mirror media_tasks.py persistence semantics: CandidateDecision is part of the
    # persisted 0.5.0 Media contract, raw PCM click/silence events are moved behind
    # *_candidates, and only promoted candidate clips remain main AUDIO_CLIPs.
    raw_media["base_engine_version"] = raw_media.get("version") or engine.analyzer_version
    raw_media["version"] = GATED_MEDIA_CONTRACT_VERSION
    raw_media["candidate_decision_version"] = CANDIDATE_DECISION_VERSION

    standalone_pcm = copy.deepcopy(raw_media.get("pcm") or {})
    results: dict[str, dict | None] = {
        "packet_intelligence": raw_media.get("packet") or {},
        "pcm_intelligence": standalone_pcm,
        "media_intelligence": raw_media,
    }
    apply_candidate_decisions(results)
    media = sanitize_gated_media_pcm(results["media_intelligence"] or {})
    media = gate_candidate_audio_artifacts(media)
    results["media_intelligence"] = media
    packet = results["packet_intelligence"] or {}
    pcm = results["pcm_intelligence"] or {}

    evidence = {
        "id": "golden-source",
        "type": "PCAP",
        "original_type": "PCAP",
        "source": "USER_UPLOAD",
        "filename": pcap_path.name,
        "sha256": sha256_file(pcap_path),
        "session_id": None,
        "call_id": None,
        "payload_available": True,
    }
    resolved = resolve_report_analysis_context(
        scope_type="CASE",
        session=None,
        runtime_call=None,
        evidences=[evidence],
        results=results,
    )
    context = resolved["analysis_context"]
    display_call = resolved["display_call"]
    analyzer_states = {
        "packet_intelligence": {
            "run_id": "golden-packet-run",
            "status": packet.get("status"),
            "analyzer_version": packet.get("version"),
            "config_version": "golden",
        },
        "pcm_intelligence": {
            "run_id": "golden-pcm-run",
            "status": pcm.get("status", media.get("status")),
            "analyzer_version": pcm.get("version"),
            "config_version": "golden",
        },
        "media_intelligence": {
            "run_id": "golden-media-run",
            "status": media.get("status"),
            "analyzer_version": media.get("version"),
            "config_version": "golden",
        },
    }
    report = build_report_payload(
        case={"id": "offline-golden-001", "case_no": "OFFLINE-GOLDEN-001", "summary": "持续电流音/周期底噪"},
        scope_type="CASE",
        scope_id="offline-golden-001",
        session=None,
        call=None,
        display_call=display_call,
        analysis_context=context,
        environment=None,
        evidences=[evidence],
        analyzer_states=analyzer_states,
        results=results,
        report_version=1,
    )
    apply_first_observable_boundaries(report)

    decision = DeterministicDiagnosisReasoner().reason({
        "case": {"id": "offline-golden-001", "summary": "持续电流音/周期底噪"},
        "devices": [],
        "evidences": [{"id": "golden-source", "type": "PCAP", "filename": pcap_path.name}],
        "analyzers": {
            "media_intelligence": {
                "run_id": "golden-media-run",
                "status": media.get("status"),
                "version": media.get("version"),
                "result": media,
            }
        },
        "fingerprint": "offline-golden-001",
    })
    diagnosis = _diagnosis_dict(decision)
    attach_actionable_summary(report, diagnosis)
    return {
        "schema_version": "offline-analysis-replay-bundle-v1",
        "source": {"filename": pcap_path.name, "sha256": evidence["sha256"]},
        "packet": packet,
        "pcm": pcm,
        "media": media,
        "analysis_context": context,
        "display_call": display_call,
        "report": report,
        "artifacts": media.get("artifacts", []) or [],
        "diagnosis": diagnosis,
    }


def validate_offline_analysis_bundle(bundle: dict, manifest: dict) -> list[GoldenCheck]:
    checks: list[GoldenCheck] = []
    expected = manifest.get("expected") or {}

    def add(name: str, passed: bool, actual: Any, wanted: Any, category: str) -> None:
        checks.append(GoldenCheck(name, bool(passed), actual, wanted, category))

    source_expected = manifest.get("source") or {}
    add(
        "source.sha256",
        bundle.get("source", {}).get("sha256") == source_expected.get("sha256"),
        bundle.get("source", {}).get("sha256"),
        source_expected.get("sha256"),
        "SOURCE",
    )

    context = bundle.get("analysis_context") or {}
    for key, wanted in (expected.get("analysis_context") or {}).items():
        add(f"analysis_context.{key}", context.get(key) == wanted, context.get(key), wanted, "CALL_BINDING")

    packet = bundle.get("packet") or {}
    calls = packet.get("calls", []) or []
    call_exp = expected.get("call") or {}
    raw_count = int(call_exp.get("raw_sip_leg_count") or 0)
    add("call.raw_sip_leg_count", len(calls) == raw_count, len(calls), raw_count, "CALL_BINDING")
    diagnostic_count = 1 if bundle.get("display_call") else 0
    add("call.diagnostic_call_count", diagnostic_count == int(call_exp.get("diagnostic_call_count") or 0), diagnostic_count, call_exp.get("diagnostic_call_count"), "CALL_BINDING")

    selected_sip_call_id = call_exp.get("selected_sip_call_id")
    call = next((x for x in calls if x.get("call_id") == selected_sip_call_id), None)
    add("call.selected_sip_call_id", call is not None, call.get("call_id") if call else None, selected_sip_call_id, "CALL_BINDING")
    other_leg = (call_exp.get("other_sip_leg") or {}).get("call_id")
    if other_leg:
        add("call.other_sip_leg_present", any(x.get("call_id") == other_leg for x in calls), [x.get("call_id") for x in calls], other_leg, "CALL_BINDING")
    if call:
        target = str(call.get("callee") or "")
        wanted_number = str(call_exp.get("dialed_number") or "")
        add("call.dialed_number", wanted_number in target, target, wanted_number, "CALL_BINDING")
        allowed_states = set(call_exp.get("allowed_states") or [])
        add("call.state", call.get("state") in allowed_states, call.get("state"), sorted(allowed_states), "CALL_BINDING")
        media_status = (call.get("media_direction_health") or {}).get("status")
        add("call.media_direction", media_status == call_exp.get("media_direction_status"), media_status, call_exp.get("media_direction_status"), "RTP")

    display_call = bundle.get("display_call") or {}
    report_exp = expected.get("report") or {}
    add("report.display_call_required", bool(display_call) == bool(report_exp.get("display_call_required")), bool(display_call), bool(report_exp.get("display_call_required")), "REPORT")
    if display_call:
        add("call.selected_display_call_id", display_call.get("id") == call_exp.get("selected_display_call_id"), display_call.get("id"), call_exp.get("selected_display_call_id"), "CALL_BINDING")
        add("report.display_call_id", display_call.get("id") == report_exp.get("display_call_id"), display_call.get("id"), report_exp.get("display_call_id"), "REPORT")
        add("report.sip_call_id", display_call.get("sip_call_id") == report_exp.get("sip_call_id"), display_call.get("sip_call_id"), report_exp.get("sip_call_id"), "REPORT")
        add("report.dialed_number", str(display_call.get("dialed_number")) == str(report_exp.get("dialed_number")), display_call.get("dialed_number"), report_exp.get("dialed_number"), "REPORT")

    rtp_exp = (expected.get("rtp") or {}).get("primary_uplink") or {}
    streams = packet.get("rtp_streams", []) or []
    primary = next((s for s in streams if s.get("src_ip") == rtp_exp.get("src_ip") and int(s.get("src_port") or 0) == int(rtp_exp.get("src_port") or 0) and s.get("dst_ip") == rtp_exp.get("dst_ip") and int(s.get("dst_port") or 0) == int(rtp_exp.get("dst_port") or 0)), None)
    add("rtp.primary_uplink.exists", primary is not None, primary.get("stream_id") if primary else None, rtp_exp, "RTP")
    if primary:
        add("rtp.primary_uplink.codec", str(primary.get("codec") or "").upper() == str(rtp_exp.get("codec") or "").upper(), primary.get("codec"), rtp_exp.get("codec"), "RTP")
        add("rtp.primary_uplink.lost_packets", int(primary.get("lost_packets") or 0) == int(rtp_exp.get("lost_packets") or 0), primary.get("lost_packets"), rtp_exp.get("lost_packets"), "RTP")
        high_delta = [e for e in primary.get("events", []) or [] if e.get("type") == "HIGH_DELTA"]
        add("rtp.primary_uplink.high_delta_count", len(high_delta) == int(rtp_exp.get("high_delta_count") or 0), len(high_delta), rtp_exp.get("high_delta_count"), "RTP")
        actual_deltas = sorted(float((e.get("details") or {}).get("delta_ms") or 0.0) for e in high_delta)
        ranges = sorted(list(rtp_exp.get("high_delta_ms") or []), key=lambda x: float(x["min"]))
        add(
            "rtp.primary_uplink.high_delta_values",
            len(actual_deltas) == len(ranges) and all(float(r["min"]) <= value <= float(r["max"]) for value, r in zip(actual_deltas, ranges)),
            actual_deltas,
            ranges,
            "RTP",
        )

    packet_anomaly_types = [str(x.get("type")) for x in packet.get("anomalies", []) or []]
    for forbidden in (expected.get("rtp") or {}).get("forbidden_anomaly_types", []) or []:
        add(f"rtp.forbidden.{forbidden}", forbidden not in packet_anomaly_types, packet_anomaly_types, f"must not contain {forbidden}", "NEGATIVE_CONTROL")

    media = bundle.get("media") or {}
    cross = media.get("cross_layer_events", []) or []
    dtmf_exp = expected.get("dtmf") or {}
    dtmf = next((e for e in cross if e.get("type") == dtmf_exp.get("required_event_type") and str((e.get("details") or {}).get("pcm_digits")) == str(dtmf_exp.get("pcm_digits")) and str((e.get("details") or {}).get("sip_target")) == str(dtmf_exp.get("sip_target"))), None)
    add("dtmf.sip_dial_match", dtmf is not None, (dtmf or {}).get("details"), dtmf_exp, "CROSS_LAYER")

    periodic_exp = expected.get("periodic_interference") or {}
    periodic = [e for e in media.get("periodic_interference_paths", []) or [] if e.get("type") == periodic_exp.get("required_event_type")]
    add("periodic.required_event", bool(periodic), len(periodic), f">=1 {periodic_exp.get('required_event_type')}", "CROSS_LAYER")
    if periodic:
        best = max(periodic, key=lambda e: float(((e.get("details") or {}).get("strength") or {}).get("pcm_rx") or 0.0))
        details = best.get("details") or {}
        pcm_rep = ((details.get("pcm_rx") or {}).get("representative") or {})
        up_rep = ((details.get("upstream_rtp") or {}).get("representative") or {})
        pcm_ac20 = float((pcm_rep.get("autocorrelation") or {}).get("20ms") or 0.0)
        up_ac20 = float((up_rep.get("autocorrelation") or {}).get("20ms") or 0.0)
        comb_hits = int(((details.get("pcm_rx") or {}).get("comb") or {}).get("hit_count") or 0)
        add("periodic.pcm_rx_ac20", pcm_ac20 >= float(periodic_exp.get("pcm_rx_ac20_min") or 0), pcm_ac20, f">={periodic_exp.get('pcm_rx_ac20_min')}", "CROSS_LAYER")
        add("periodic.upstream_ac20", up_ac20 >= float(periodic_exp.get("upstream_ac20_min") or 0), up_ac20, f">={periodic_exp.get('upstream_ac20_min')}", "CROSS_LAYER")
        add("periodic.comb_hits", comb_hits >= int(periodic_exp.get("odd_50hz_comb_hits_min") or 0), comb_hits, f">={periodic_exp.get('odd_50hz_comb_hits_min')}", "CROSS_LAYER")
        if periodic_exp.get("downstream_control_required"):
            add("periodic.downstream_control", details.get("downstream_rtp") is not None, details.get("downstream_rtp") is not None, True, "CROSS_LAYER")

    decision_exp = expected.get("candidate_decision") or {}
    decisions = media.get("candidate_decisions", []) or []
    reason = decision_exp.get("required_negative_control_reason")
    add("candidate.required_negative_control", any(d.get("status") == "REJECTED_NEGATIVE_CONTROL" and d.get("reason_code") == reason for d in decisions), [{"status": d.get("status"), "reason_code": d.get("reason_code"), "time": d.get("candidate_time")} for d in decisions], reason, "NEGATIVE_CONTROL")
    click_window = decision_exp.get("forbidden_promoted_click_window") or {}
    if click_window:
        offenders = [d for d in decisions if d.get("candidate_type") == "CLICK_POP" and d.get("status") == "PROMOTED" and float(click_window["start"]) <= float(d.get("candidate_time") or 0.0) <= float(click_window["end"])]
        add("candidate.dtmf_click_not_promoted", not offenders, offenders, "no promoted CLICK_POP in DTMF window", "NEGATIVE_CONTROL")
    required_silence_reason = decision_exp.get("silence_promote_reason_required")
    bad_silence = [d for d in decisions if d.get("candidate_type") == "UNEXPECTED_SILENCE" and d.get("status") == "PROMOTED" and d.get("reason_code") != required_silence_reason]
    add("candidate.silence_promotion_grounded", not bad_silence, bad_silence, f"all promoted silence reason={required_silence_reason}", "NEGATIVE_CONTROL")

    report = bundle.get("report") or {}
    findings = report.get("findings", []) or []
    finding_types = [str(x.get("type")) for x in findings]
    for forbidden in report_exp.get("forbidden_finding_types", []) or []:
        add(f"report.forbidden_finding.{forbidden}", forbidden not in finding_types, finding_types, f"must not contain {forbidden}", "REPORT")
    report_click_window = report_exp.get("forbidden_click_window") or {}
    if report_click_window:
        offenders = [f for f in findings if f.get("type") == "CLICK_POP" and float(report_click_window["start"]) <= float((f.get("time_range") or {}).get("representative") or 0.0) <= float(report_click_window["end"])]
        add("report.dtmf_click_not_visible", not offenders, offenders, "no CLICK_POP Finding in DTMF window", "REPORT")

    artifacts = bundle.get("artifacts", []) or []
    required_sources = set((expected.get("artifacts") or {}).get("periodic_audio_sources_required", []) or [])
    periodic_sources = {str((a.get("metadata") or {}).get("source")) for a in artifacts if a.get("type") == "PERIODIC_AUDIO_CLIP"}
    add("artifacts.periodic_audio_sources", required_sources.issubset(periodic_sources), sorted(periodic_sources), sorted(required_sources), "ARTIFACT")
    candidate_clip_violations = []
    for artifact in artifacts:
        meta = artifact.get("metadata") or {}
        if str(meta.get("event_type") or "").upper() not in {"CLICK_POP", "SILENCE", "UNEXPECTED_SILENCE"} or not meta.get("pcm_tap"):
            continue
        if artifact.get("type") == "AUDIO_CLIP" and (meta.get("candidate_artifact_status") != "PROMOTED" or not meta.get("candidate_id")):
            candidate_clip_violations.append({"filename": artifact.get("filename"), "type": artifact.get("type"), "metadata": meta})
    add("artifacts.candidate_audio_quarantine", not candidate_clip_violations, candidate_clip_violations, "raw Click/Silence clips must be CANDIDATE_AUDIO_CLIP unless promoted with provenance", "ARTIFACT")

    diagnosis = bundle.get("diagnosis") or {}
    hypotheses = diagnosis.get("hypotheses", []) or []
    diag_exp = expected.get("diagnosis") or {}
    required_hyp = next((h for h in hypotheses if h.get("code") == diag_exp.get("required_hypothesis_code")), None)
    add("diagnosis.required_hypothesis", bool(required_hyp and required_hyp.get("status") == diag_exp.get("required_hypothesis_status")), required_hyp, {"code": diag_exp.get("required_hypothesis_code"), "status": diag_exp.get("required_hypothesis_status")}, "DIAGNOSIS")
    forbidden_confirmed = set(diag_exp.get("forbidden_confirmed_hypothesis_codes", []) or [])
    confirmed_offenders = [h for h in hypotheses if h.get("status") == "CONFIRMED" and h.get("code") in forbidden_confirmed]
    add("diagnosis.no_specific_hardware_confirmation", not confirmed_offenders, confirmed_offenders, "no specific hardware root CONFIRMED", "DIAGNOSIS")

    return checks


def validation_payload(bundle: dict, manifest: dict) -> dict:
    checks = validate_offline_analysis_bundle(bundle, manifest)
    return {
        "schema_version": "offline-analysis-golden-result-v1",
        "golden_case": manifest.get("id"),
        "classification": manifest.get("classification"),
        "source": bundle.get("source"),
        "passed": all(item.passed for item in checks if item.blocking),
        "checks_passed": sum(1 for item in checks if item.passed),
        "checks_total": len(checks),
        "checks": [asdict(item) for item in checks],
        "analysis_context": bundle.get("analysis_context"),
        "display_call": bundle.get("display_call"),
        "media_summary": (bundle.get("media") or {}).get("summary"),
        "finding_summary": [{"type": f.get("type"), "severity": f.get("severity"), "scope": f.get("scope"), "time_range": f.get("time_range")} for f in (bundle.get("report") or {}).get("findings", []) or []],
        "diagnosis": bundle.get("diagnosis"),
    }
