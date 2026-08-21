from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from app.golden.offline_analysis_e2e import GoldenCheck
from app.reports.evidence_card import attach_evidence_cards
from app.services.evidence_report_source_artifacts import artifact_matches_finding


def _project_real_analyzer_artifacts_to_cards(bundle:dict)->dict:
    """Golden-only projection using production binding/Card rules.

    The external PCAP and production Analyzer outputs remain the source of truth.
    No manifest expected value enters this projection. Raw analyzer Artifact metadata
    is mapped onto production Findings using the same fail-closed matching function
    used by the report persistence service, then the production Evidence Card builder
    is invoked. This validates traceability without leaking Golden answers.
    """
    report=bundle.get("report") or {};raw_artifacts=bundle.get("artifacts") or []
    for finding in report.get("findings",[]) or []:
        tr=finding.get("time_range") or {}
        fake_finding=SimpleNamespace(
            finding_type=finding.get("type"),scope_json=finding.get("scope") or {},
            start_time=tr.get("start"),end_time=tr.get("end"),representative_time=tr.get("representative"),
        )
        refs=[]
        for index,artifact in enumerate(raw_artifacts):
            atype=str(artifact.get("type") or "").upper()
            if atype not in {"AUDIO_CLIP","PERIODIC_AUDIO_CLIP","WAVEFORM_JSON","SPECTROGRAM_JSON","PCM_WAV","AUDIO_WAV","PERIODIC_METRICS_JSON"}:continue
            fake_artifact=SimpleNamespace(type=atype,metadata_json=artifact.get("metadata") or {})
            if not artifact_matches_finding(fake_artifact,fake_finding):continue
            refs.append({
                "artifact_id":f"offline-artifact-{index:03d}","type":atype,"filename":artifact.get("filename"),
                "content_type":artifact.get("content_type"),"role":"FINDING","metadata":artifact.get("metadata") or {},
            })
        finding["artifact_refs"]=refs
    attach_evidence_cards(report)
    return report


def validate_extended_offline_truth(bundle: dict, manifest: dict) -> list[GoldenCheck]:
    checks: list[GoldenCheck] = []
    expected = manifest.get("expected") or {}

    def add(name: str, passed: bool, actual: Any, wanted: Any, category: str) -> None:
        checks.append(GoldenCheck(name, bool(passed), actual, wanted, category))

    pcm = bundle.get("pcm") or {};pcm_exp = expected.get("pcm") or {};pcm_summary = pcm.get("summary") or {};pcm_format = pcm.get("format") or {}
    add("pcm.total_packets", int(pcm_summary.get("total_packets") or 0) == int(pcm_exp.get("total_packets") or 0), pcm_summary.get("total_packets"), pcm_exp.get("total_packets"), "PCM")
    for key, wanted in (pcm_exp.get("format") or {}).items():
        actual = pcm_format.get(key)
        if key == "endian":passed = str(actual or "").lower() in {str(wanted).lower(), "le" if str(wanted).lower() == "little" else str(wanted).lower()}
        else:passed = actual == wanted
        add(f"pcm.format.{key}", passed, actual, wanted, "PCM")

    by_tap = {str((s.get("tap") or {}).get("name")): s for s in pcm.get("streams", []) or []};source_ips: set[str] = set()
    for tap_name, tap_exp in (pcm_exp.get("taps") or {}).items():
        stream = by_tap.get(tap_name);add(f"pcm.tap.{tap_name}.exists", stream is not None, bool(stream), True, "PCM")
        if not stream:continue
        tap = stream.get("tap") or {}
        add(f"pcm.tap.{tap_name}.direction", str(tap.get("direction") or "").upper() == str(tap_exp.get("direction") or "").upper(), tap.get("direction"), tap_exp.get("direction"), "PCM")
        add(f"pcm.tap.{tap_name}.packet_count", int(stream.get("packet_count") or 0) == int(tap_exp.get("packet_count") or 0), stream.get("packet_count"), tap_exp.get("packet_count"), "PCM")
        for endpoint in stream.get("source_endpoints", []) or []:
            if endpoint.get("ip"):source_ips.add(str(endpoint["ip"]))
    expected_source_ip = pcm_exp.get("source_device_ip")
    if expected_source_ip:add("pcm.source_device_ip", source_ips == {str(expected_source_ip)}, sorted(source_ips), [str(expected_source_ip)], "PCM_PROVENANCE")

    rtp_all_exp = expected.get("rtp") or {};rtp_streams = (bundle.get("packet") or {}).get("rtp_streams", []) or []

    def find_rtp(exp: dict) -> dict | None:
        return next((s for s in rtp_streams if s.get("src_ip") == exp.get("src_ip") and int(s.get("src_port") or 0) == int(exp.get("src_port") or 0) and s.get("dst_ip") == exp.get("dst_ip") and int(s.get("dst_port") or 0) == int(exp.get("dst_port") or 0)), None)

    for label in ("primary_uplink", "primary_downlink"):
        flow_exp = rtp_all_exp.get(label) or {}
        if not flow_exp:continue
        stream = find_rtp(flow_exp)
        add(f"rtp.{label}.exists", stream is not None, bool(stream), True, "RTP_ACCOUNTING")
        if not stream:continue
        if flow_exp.get("codec") is not None:add(f"rtp.{label}.codec", str(stream.get("codec") or "").upper() == str(flow_exp.get("codec") or "").upper(), stream.get("codec"), flow_exp.get("codec"), "RTP_ACCOUNTING")
        field_map = {
            "observed_packet_count": "packet_count",
            "unique_packet_count": "unique_packet_count",
            "duplicate_packets": "duplicate_packets",
            "expected_packets": "expected_packets",
            "lost_packets": "lost_packets",
        }
        for expected_field, actual_field in field_map.items():
            if expected_field not in flow_exp:continue
            actual_value = int(stream.get(actual_field) or 0);wanted_value = int(flow_exp.get(expected_field) or 0)
            add(f"rtp.{label}.{expected_field}", actual_value == wanted_value, actual_value, wanted_value, "RTP_ACCOUNTING")
        observed = int(stream.get("packet_count") or 0);unique = int(stream.get("unique_packet_count") or 0);duplicates = int(stream.get("duplicate_packets") or 0)
        expected_packets = int(stream.get("expected_packets") or 0);lost = int(stream.get("lost_packets") or 0)
        add(f"rtp.{label}.accounting.observed_equals_unique_plus_duplicates", observed == unique + duplicates, {"observed":observed,"unique":unique,"duplicates":duplicates}, "observed = unique + duplicates", "RTP_ACCOUNTING")
        add(f"rtp.{label}.accounting.expected_equals_unique_plus_loss", expected_packets == unique + lost, {"expected":expected_packets,"unique":unique,"lost":lost}, "expected = unique + lost", "RTP_ACCOUNTING")
        max_delta_range = flow_exp.get("max_arrival_delta_ms") or {}
        if max_delta_range:
            actual_delta = float(stream.get("max_delta_ms") or 0.0)
            add(f"rtp.{label}.max_arrival_delta_ms", float(max_delta_range.get("min") or 0.0) <= actual_delta <= float(max_delta_range.get("max") or 0.0), actual_delta, max_delta_range, "RTP_ACCOUNTING")
        duplicate_exp = list(flow_exp.get("duplicate_events") or [])
        if duplicate_exp:
            duplicate_actual = list(stream.get("duplicate_events") or [])
            duplicate_exp.sort(key=lambda e: int(e.get("sequence") or -1));duplicate_actual.sort(key=lambda e: int(e.get("sequence") or -1))
            add(f"rtp.{label}.duplicate_event_count", len(duplicate_actual) == len(duplicate_exp), len(duplicate_actual), len(duplicate_exp), "RTP_DUPLICATE_FRAME")
            if len(duplicate_actual) == len(duplicate_exp):
                for index,(actual_event,expected_event) in enumerate(zip(duplicate_actual,duplicate_exp),start=1):
                    for field in ("sequence","first_frame_number","duplicate_frame_number","rtp_timestamp"):
                        add(f"rtp.{label}.duplicate.{index}.{field}", int(actual_event.get(field) or -1) == int(expected_event.get(field) or -2), actual_event.get(field), expected_event.get(field), "RTP_DUPLICATE_FRAME")
                    delta_range = expected_event.get("arrival_delta_ms") or {};delta=float(actual_event.get("arrival_delta_ms") or 0.0)
                    add(f"rtp.{label}.duplicate.{index}.arrival_delta_ms", float(delta_range.get("min") or 0.0) <= delta <= float(delta_range.get("max") or 0.0), delta, delta_range, "RTP_DUPLICATE_FRAME")
                    add(f"rtp.{label}.duplicate.{index}.same_rtp_timestamp", actual_event.get("rtp_timestamp") == actual_event.get("first_rtp_timestamp"), {"first":actual_event.get("first_rtp_timestamp"),"duplicate":actual_event.get("rtp_timestamp")}, "same RTP timestamp", "RTP_DUPLICATE_FRAME")

    rtp_exp = rtp_all_exp.get("primary_uplink") or {};primary = find_rtp(rtp_exp) if rtp_exp else None
    actual_high_delta = [e for e in (primary or {}).get("events", []) or [] if e.get("type") == "HIGH_DELTA"]
    expected_high_delta = list(rtp_exp.get("high_delta_events") or [])
    actual_high_delta.sort(key=lambda e: float((e.get("details") or {}).get("delta_ms") or 0.0));expected_high_delta.sort(key=lambda e: float((e.get("delta_ms") or {}).get("min") or 0.0))
    add("rtp.high_delta.frame_event_count", len(actual_high_delta) == len(expected_high_delta), len(actual_high_delta), len(expected_high_delta), "RTP_FRAME")
    if len(actual_high_delta) == len(expected_high_delta):
        for index, (actual_event, expected_event) in enumerate(zip(actual_high_delta, expected_high_delta), start=1):
            details = actual_event.get("details") or {};delta_range = expected_event.get("delta_ms") or {};delta = float(details.get("delta_ms") or 0.0)
            add(f"rtp.high_delta.{index}.delta_ms", float(delta_range.get("min") or 0.0) <= delta <= float(delta_range.get("max") or 0.0), delta, delta_range, "RTP_FRAME")
            for field in ("previous_frame_number", "current_frame_number", "previous_sequence", "current_sequence"):
                add(f"rtp.high_delta.{index}.{field}", int(details.get(field) or -1) == int(expected_event.get(field) or -2), details.get(field), expected_event.get(field), "RTP_FRAME")
            prev_seq = details.get("previous_sequence");curr_seq = details.get("current_sequence")
            if prev_seq is not None and curr_seq is not None:add(f"rtp.high_delta.{index}.sequence_continuity", ((int(curr_seq) - int(prev_seq)) & 0xFFFF) == 1, {"previous": prev_seq, "current": curr_seq}, "current sequence is previous + 1", "RTP_FRAME")

    semantic_exp = rtp_exp.get("high_delta_semantics") or {};allowed_classifications = set(semantic_exp.get("allowed_classifications") or [])
    for index, actual_event in enumerate(actual_high_delta, start=1):
        details = actual_event.get("details") or {}
        if semantic_exp.get("required_sequence_continuous"):add(f"rtp.high_delta.{index}.semantic.sequence_continuous", details.get("sequence_continuous") is True, details.get("sequence_continuous"), True, "RTP_SEMANTIC")
        required_loss_semantics = semantic_exp.get("required_loss_semantics")
        if required_loss_semantics:add(f"rtp.high_delta.{index}.semantic.loss", details.get("loss_semantics") == required_loss_semantics, details.get("loss_semantics"), required_loss_semantics, "RTP_SEMANTIC")
        if allowed_classifications:add(f"rtp.high_delta.{index}.semantic.classification", details.get("classification") in allowed_classifications, details.get("classification"), sorted(allowed_classifications), "RTP_SEMANTIC")
        if semantic_exp.get("catch_up_required"):
            catch_up = details.get("catch_up") or {};add(f"rtp.high_delta.{index}.semantic.catch_up", catch_up.get("status") in {"PARTIAL", "FULL"} and catch_up.get("observed") is True, catch_up, "PARTIAL or FULL catch-up observed", "RTP_SEMANTIC")

    media = bundle.get("media") or {};dtmf_exp = expected.get("dtmf") or {};dtmf_matches = [e for e in media.get("cross_layer_events", []) or [] if e.get("type") == dtmf_exp.get("required_event_type")]
    add("dtmf.match_count", len(dtmf_matches) == int(dtmf_exp.get("expected_match_count") or 0), len(dtmf_matches), dtmf_exp.get("expected_match_count"), "CROSS_LAYER")
    if dtmf_matches:
        matched_call_ids = [str((e.get("details") or {}).get("call_id") or (e.get("scope") or {}).get("call_id") or "") for e in dtmf_matches]
        add("dtmf.subject_call_id", matched_call_ids == [str(dtmf_exp.get("call_id"))], matched_call_ids, [str(dtmf_exp.get("call_id"))], "CROSS_LAYER")

    report = _project_real_analyzer_artifacts_to_cards(bundle);report_exp = expected.get("report") or {};findings = report.get("findings", []) or [];finding_types = [str(f.get("type")) for f in findings]
    for required in report_exp.get("required_finding_types", []) or []:add(f"report.required_finding.{required}", required in finding_types, finding_types, f"must contain {required}", "REPORT")

    packet_summary = report.get("packet_summary") or {};summary_rows = packet_summary.get("streams") or []
    for label in ("primary_uplink", "primary_downlink"):
        flow_exp = rtp_all_exp.get(label) or {}
        if not flow_exp:continue
        row = next((s for s in summary_rows if str(s.get("source")) == f"{flow_exp.get('src_ip')}:{flow_exp.get('src_port')}" and str(s.get("destination")) == f"{flow_exp.get('dst_ip')}:{flow_exp.get('dst_port')}"), None)
        add(f"report.rtp.{label}.exists", row is not None, bool(row), True, "REPORT_SEMANTIC")
        if not row:continue
        add(f"report.rtp.{label}.packet_count_semantics", row.get("packet_count_semantics") == "UNIQUE_EFFECTIVE_RTP_PACKETS", row.get("packet_count_semantics"), "UNIQUE_EFFECTIVE_RTP_PACKETS", "REPORT_SEMANTIC")
        if "unique_packet_count" in flow_exp:add(f"report.rtp.{label}.effective_packet_count", int(row.get("packet_count") or 0) == int(flow_exp.get("unique_packet_count") or 0), row.get("packet_count"), flow_exp.get("unique_packet_count"), "REPORT_SEMANTIC")
        if "observed_packet_count" in flow_exp:add(f"report.rtp.{label}.observed_packet_count", int(row.get("observed_packet_count") or 0) == int(flow_exp.get("observed_packet_count") or 0), row.get("observed_packet_count"), flow_exp.get("observed_packet_count"), "REPORT_SEMANTIC")
        if "duplicate_packets" in flow_exp:add(f"report.rtp.{label}.duplicate_packets", int(row.get("duplicate_packets") or 0) == int(flow_exp.get("duplicate_packets") or 0), row.get("duplicate_packets"), flow_exp.get("duplicate_packets"), "REPORT_SEMANTIC")

    high_delta_report_exp = report_exp.get("high_delta_primary_stream_finding") or {}
    if high_delta_report_exp:
        primary_stream_id = (primary or {}).get("stream_id");finding = next((f for f in findings if f.get("type") == "HIGH_DELTA" and (f.get("scope") or {}).get("rtp_stream_id") == primary_stream_id), None)
        add("report.high_delta.primary_stream.exists", finding is not None, (finding or {}).get("scope"), primary_stream_id, "REPORT_SEMANTIC")
        if finding:
            metrics = finding.get("metrics") or {};semantic = finding.get("semantic_summary") or {}
            add("report.high_delta.primary_stream.occurrence_count", int(finding.get("occurrence_count") or 0) == int(high_delta_report_exp.get("occurrence_count") or 0), finding.get("occurrence_count"), high_delta_report_exp.get("occurrence_count"), "REPORT_SEMANTIC")
            add("report.high_delta.primary_stream.event_count", int(metrics.get("event_count") or 0) == int(high_delta_report_exp.get("occurrence_count") or 0), metrics.get("event_count"), high_delta_report_exp.get("occurrence_count"), "REPORT_SEMANTIC")
            add("report.high_delta.primary_stream.sequence", metrics.get("all_sequence_continuous") == bool(high_delta_report_exp.get("all_sequence_continuous")), metrics.get("all_sequence_continuous"), high_delta_report_exp.get("all_sequence_continuous"), "REPORT_SEMANTIC")
            add("report.high_delta.primary_stream.loss_interpretation", semantic.get("loss_interpretation") == high_delta_report_exp.get("loss_interpretation"), semantic.get("loss_interpretation"), high_delta_report_exp.get("loss_interpretation"), "REPORT_SEMANTIC")
            add("report.high_delta.primary_stream.frame_seq_events", len(metrics.get("events") or []) == int(high_delta_report_exp.get("occurrence_count") or 0) and all(e.get("previous_frame_number") is not None and e.get("current_frame_number") is not None and e.get("previous_sequence") is not None and e.get("current_sequence") is not None for e in metrics.get("events") or []), metrics.get("events"), "all aggregated events retain Frame/Seq evidence", "REPORT_SEMANTIC")

    # PR5: validate real Analyzer Artifact metadata -> production binding -> production
    # Evidence Card. Expected values are read only here, after production output exists.
    card_exp = report_exp.get("evidence_cards") or {};primary_stream_id=(primary or {}).get("stream_id")
    high_card_finding=next((f for f in findings if f.get("type")=="HIGH_DELTA" and (f.get("scope") or {}).get("rtp_stream_id")==primary_stream_id),None)
    periodic_finding=next((f for f in findings if f.get("type")=="LOCAL_CAPTURE_PERIODIC_INTERFERENCE"),None)
    for label,finding in (("high_delta_primary_stream",high_card_finding),("local_periodic_interference",periodic_finding)):
        wanted=card_exp.get(label) or {}
        if not wanted:continue
        card=(finding or {}).get("evidence_card") or {}
        add(f"report.evidence_card.{label}.exists", bool(card), bool(card), True, "EVIDENCE_CARD")
        if not card:continue
        audio=card.get("audio_evidence") or {}
        if wanted.get("audio_status"):add(f"report.evidence_card.{label}.audio_status", audio.get("status")==wanted.get("audio_status"), audio.get("status"), wanted.get("audio_status"), "EVIDENCE_CARD")
        if wanted.get("packet_ref_count") is not None:add(f"report.evidence_card.{label}.packet_refs", len(card.get("packet_refs") or [])==int(wanted.get("packet_ref_count")), len(card.get("packet_refs") or []), wanted.get("packet_ref_count"), "EVIDENCE_CARD")
        if wanted.get("next_action_required"):add(f"report.evidence_card.{label}.next_action", bool(str(card.get("next_action") or "").strip()), card.get("next_action"), "non-empty", "EVIDENCE_CARD")
        if wanted.get("root_cause_boundary_required"):add(f"report.evidence_card.{label}.root_boundary", bool(str(card.get("root_cause_boundary") or "").strip()), card.get("root_cause_boundary"), "non-empty", "EVIDENCE_CARD")
        required_sources=set(wanted.get("required_audio_sources") or [])
        if required_sources:
            actual_sources={str(x.get("source")) for x in audio.get("clips",[]) or [] if x.get("source") not in (None,{})}
            add(f"report.evidence_card.{label}.audio_sources", required_sources.issubset(actual_sources), sorted(actual_sources), sorted(required_sources), "EVIDENCE_CARD")

    report_context = report.get("analysis_context") or {};bundle_context = bundle.get("analysis_context") or {}
    add("report.analysis_context_consistency", report_context == bundle_context, report_context, bundle_context, "REPORT")
    report_call = report.get("display_call") or report.get("call");add("report.call_consistency", report_call == bundle.get("display_call"), report_call, bundle.get("display_call"), "REPORT")
    return checks
