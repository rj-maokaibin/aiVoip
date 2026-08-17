from types import SimpleNamespace

import numpy as np

from app.analyzers.pcm.signal import basic_stats
from app.contracts.evidence_report import REPORT_SCHEMA_VERSION
from app.reports.evidence_brief import build_report_payload, render_report_html
from app.reports.evidence_visuals import render_rtp_timeline_png, render_spectrum_png, render_waveform_png
from app.reports.finding_composer import compose_findings, derive_first_observable_layer
from app.services.evidence_boundary import apply_first_observable_boundaries
from app.services.evidence_report import report_idempotency_key
from app.services.evidence_report_aggregation import _ab_comparison


def test_pcm_level_contract_exposes_rms_and_peak_dbfs_without_claiming_spl():
    samples=np.array([32767,-32768,16384,-16384],dtype=np.int16)
    result=basic_stats(samples)
    assert result["dbfs"]==result["rms_dbfs"]
    assert result["peak_dbfs"]==0.0
    assert result["level_unit"]=="dBFS"
    assert "dB SPL" in result["level_boundary"]


def test_first_observable_boundary_requires_complete_upstream_control():
    observed=derive_first_observable_layer([
        {"layer":"RTP_DOWNSTREAM","available":True,"abnormal":False},
        {"layer":"PCM_RX","available":True,"abnormal":True},
        {"layer":"RTP_UPSTREAM","available":True,"abnormal":True},
    ])
    assert observed["status"]=="OBSERVED_BOUNDARY"
    assert observed["first_observable_layer"]=="PCM_RX"
    missing=derive_first_observable_layer([
        {"layer":"RTP_DOWNSTREAM","available":False,"abnormal":False},
        {"layer":"PCM_RX","available":True,"abnormal":True},
    ])
    assert missing["status"]=="UNKNOWN"
    assert missing["reason"]=="UPSTREAM_EVIDENCE_MISSING"


def test_periodic_boundary_postprocessor_never_upgrades_to_root_cause():
    payload={"findings":[{
        "type":"LOCAL_CAPTURE_PERIODIC_INTERFERENCE","interpretation":"周期干扰相关。","correlation":{},
        "metrics":{"downstream_rtp":{"level":"LOW"},"pcm_rx":{"level":"HIGH"},"upstream_rtp":{"level":"HIGH"}},
    }]}
    apply_first_observable_boundaries(payload)
    finding=payload["findings"][0]
    assert finding["correlation"]["first_observable_boundary"]["first_observable_layer"]=="PCM_RX"
    assert "不声明物理信号起源" in finding["correlation"]["role_boundary"]
    assert "根因" in finding["interpretation"]


def test_finding_composer_keeps_periodic_physical_boundary_explicit():
    pcm={"streams":[{"tap":{"name":"pcm_rx","direction":"RX"},"sessions":[{
        "session_index":0,"start_time":1.0,"end_time":3.0,"gap_events":[],"silence_events":[],"click_pop_events":[],
        "hum":{"level":"HIGH","dominant_family":"50Hz","score":0.1},"signal":{"rms_dbfs":-20,"peak_dbfs":-3},
    }]}]}
    findings=compose_findings(pcm=pcm,source_run_ids={"pcm_intelligence":"run-pcm"})
    assert findings[0]["type"]=="PERIODIC_LOW_FREQUENCY_INTERFERENCE"
    assert findings[0]["evidence_level"]=="L2"
    assert "不能单独确认" in findings[0]["root_cause_boundary"]
    assert "SLIC" in findings[0]["root_cause_boundary"]


def test_canonical_report_contains_d112_sections_and_complete_pcm_direction_check():
    results={
        "packet_intelligence":{"summary":{"packet_count":100,"sip_message_count":6,"call_count":1,"rtp_stream_count":2,"rtcp_report_count":0},"calls":[],"rtp_streams":[],"anomalies":[]},
        "pcm_intelligence":{"summary":{"total_packets":20},"format":{},"streams":[
            {"tap":{"name":"pcm_rx","direction":"RX"},"packet_count":10,"sessions":[]},
            {"tap":{"name":"pcm_tx","direction":"TX"},"packet_count":10,"sessions":[]},
        ]},
        "media_intelligence":{"summary":{},"cross_layer_events":[],"periodic_interference_paths":[]},
    }
    states={name:{"status":"SUCCESS","run_id":name,"analyzer_version":"1","config_version":"v"} for name in results}
    payload=build_report_payload(case={"id":"c","case_no":"C-1","summary":"noise","status":"OPEN"},scope_type="CALL",scope_id="call-1",
        session=None,call={"id":"call-1"},environment={},evidences=[{"type":"PCAP"}],analyzer_states=states,results=results,report_version=1,
        generated_at="2026-08-18T00:00:00+00:00")
    assert payload["schema_version"]==REPORT_SCHEMA_VERSION
    assert payload["completeness"]["state"]=="COMPLETE"
    assert payload["evidence_boundary"]["root_cause_authority"]=="PRELIMINARY_EVIDENCE_ONLY"
    html=render_report_html(payload)
    expected=["0. 当前状态 / 快速导航","1. 当前初步结论","2. 当前重点问题","3. 证据完整度","4. 最新一次复现结果","5. 多次复现汇总","6. A/B 对比","7. 历次 Reproduction Session","8. 正常项 / 排除性证据","9. 完整技术证据","10. Evidence Bundle / 附件","11. 报告版本与审计记录"]
    offsets=[html.index(x) for x in expected]
    assert offsets==sorted(offsets)


def test_report_idempotency_is_stable_and_forced_version_is_distinct():
    states={"packet_intelligence":{"run_id":"r1","analyzer_version":"1","config_version":"p1"}}
    a=report_idempotency_key("CALL","call-1","hash",states)
    b=report_idempotency_key("CALL","call-1","hash",states)
    forced=report_idempotency_key("CALL","call-1","hash",states,forced_version=2)
    assert a==b
    assert forced!=a


def test_ab_comparison_requires_repeatability_and_absolute_rate_difference():
    groups=[
        {"environment_fingerprint":"A","call_count":3,"finding_groups":[{"finding_signature":"sig","finding_type":"X","title":"问题X","reproduction_rate":0.0}]},
        {"environment_fingerprint":"B","call_count":3,"finding_groups":[{"finding_signature":"sig","finding_type":"X","title":"问题X","reproduction_rate":1.0}]},
    ]
    comp=_ab_comparison(groups)[0]["differences"][0]
    assert comp["repeatability_requirement_met"] is True
    assert comp["significant_by_v1_rule"] is True
    assert "不独立确认因果" in comp["interpretation_boundary"]


def test_png_renderer_is_deterministic_and_emits_valid_png():
    waveform={"duration_seconds":1.0,"bins":[{"t":0.0,"min":-10,"max":10},{"t":1.0,"min":-20,"max":20}]}
    a=render_waveform_png(waveform); b=render_waveform_png(waveform)
    assert a==b and a.startswith(b"\x89PNG\r\n\x1a\n")
    assert render_spectrum_png({"peaks":[{"frequency_hz":50,"energy_ratio":0.5}]}).startswith(b"\x89PNG")
    assert render_rtp_timeline_png([{"start_time":0,"end_time":1,"events":[{"start_time":0.5}]}]).startswith(b"\x89PNG")
