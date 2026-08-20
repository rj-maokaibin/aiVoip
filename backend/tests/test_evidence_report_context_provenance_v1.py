from types import SimpleNamespace

from app.contracts.evidence_report import AnalysisMode
from app.services.evidence_report import _analysis_context_evidences, _runtime_binding_ids


def test_context_binding_uses_only_current_analyzer_input_evidence_ids():
    evidences = [
        {
            "id": "old-offline-pcap",
            "type": "PCAP",
            "source": "USER_UPLOAD",
            "session_id": None,
            "call_id": None,
        },
        {
            "id": "current-runtime-pcap",
            "type": "PCAP",
            "source": "REPRODUCTION",
            "session_id": "session-current",
            "call_id": "call-current",
        },
        {
            "id": "current-pcm-rx",
            "type": "PCM_RX",
            "source": "REPRODUCTION",
            "session_id": "session-current",
            "call_id": "call-current",
        },
    ]
    runs = {
        "packet_intelligence": SimpleNamespace(input_evidence_ids=["current-runtime-pcap"]),
        "pcm_intelligence": SimpleNamespace(input_evidence_ids=["current-pcm-rx"]),
    }

    selected, input_ids = _analysis_context_evidences(evidences, runs)

    assert input_ids == ["current-pcm-rx", "current-runtime-pcap"]
    assert {x["id"] for x in selected} == {"current-runtime-pcap", "current-pcm-rx"}
    assert "old-offline-pcap" not in {x["id"] for x in selected}


def test_context_binding_falls_back_to_scoped_evidence_when_runs_have_no_input_ids():
    evidences = [{"id": "case-pcap", "type": "PCAP"}]
    runs = {"packet_intelligence": SimpleNamespace(input_evidence_ids=[])}

    selected, input_ids = _analysis_context_evidences(evidences, runs)

    assert selected == evidences
    assert input_ids == []


def test_offline_report_never_persists_historical_runtime_session_or_call_foreign_keys():
    session = SimpleNamespace(id="historical-session")
    call = SimpleNamespace(id="historical-call")

    session_id, call_id = _runtime_binding_ids(
        {"analysis_mode": AnalysisMode.OFFLINE_IMPORTED.value},
        session,
        call,
    )

    assert session_id is None
    assert call_id is None


def test_reproduction_report_preserves_real_runtime_session_and_call_foreign_keys():
    session = SimpleNamespace(id="runtime-session")
    call = SimpleNamespace(id="runtime-call")

    session_id, call_id = _runtime_binding_ids(
        {"analysis_mode": AnalysisMode.REPRODUCTION.value},
        session,
        call,
    )

    assert session_id == "runtime-session"
    assert call_id == "runtime-call"
