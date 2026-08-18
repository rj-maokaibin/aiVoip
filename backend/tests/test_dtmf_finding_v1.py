from app.analyzers.pcm.dtmf_quality import dtmf_quality_events
from app.reports.finding_composer import compose_findings


def test_dtmf_quality_detects_low_confidence_and_short_gap_without_inventing_missing_digits():
    events = [
        {"digit":"1","start_seconds":0.0,"end_seconds":0.10,"duration_ms":100.0,"confidence":0.40},
        {"digit":"2","start_seconds":0.12,"end_seconds":0.22,"duration_ms":100.0,"confidence":0.90},
    ]
    quality = dtmf_quality_events(events)
    assert [x["type"] for x in quality] == ["DTMF_LOW_CONFIDENCE", "DTMF_SHORT_INTERDIGIT_GAP"]
    assert quality[0]["digit"] == "1"
    assert quality[1]["gap_ms"] == 20.0
    assert all("missing" not in str(x).lower() for x in quality)


def test_dtmf_quality_event_becomes_evidence_finding_not_root_cause():
    pcm = {
        "streams":[{
            "tap":{"name":"pcm_rx","direction":"RX"},
            "sessions":[{
                "session_index":0,"start_time":10.0,"end_time":12.0,"hum":{"level":"LOW"},
                "gap_events":[],"silence_events":[],"click_pop_events":[],
                "dtmf_quality_events":[{
                    "type":"DTMF_LOW_CONFIDENCE","severity":"MEDIUM","digit":"5",
                    "start_seconds":0.5,"end_seconds":0.6,"duration_ms":100.0,"confidence":0.42,"threshold":0.55,
                }],
            }],
        }]
    }
    findings = compose_findings(pcm=pcm, source_run_ids={"pcm_intelligence":"run-pcm"})
    assert len(findings) == 1
    finding = findings[0]
    assert finding["type"] == "DTMF_ABNORMAL"
    assert finding["severity"] == "MEDIUM"
    assert finding["evidence_level"] == "L3"
    assert finding["time_range"]["start"] == 10.5
    assert "不推断具体丢号" in finding["observation"]
    assert "最终根因" in finding["root_cause_boundary"]
    assert finding["event_refs"] == [{"source":"pcm.dtmf_quality_events","index":0}]
