from types import SimpleNamespace

from app.contracts.evidence_report import AnalysisMode
from app.db.models import ReproductionCall, ReproductionSession
from app.services.evidence_report import (
    _analysis_context_evidences,
    _case_runtime_scope_from_evidence,
    _runtime_binding_ids,
)


class _FakeDb:
    def __init__(self, rows):
        self.rows = rows

    def get(self, model, row_id):
        return self.rows.get((model, row_id))


def test_context_binding_uses_only_packet_call_source_analyzer_input_evidence_ids():
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
            "id": "unrelated-pcm-input",
            "type": "PCM_RX",
            "source": "USER_UPLOAD",
            "session_id": None,
            "call_id": None,
        },
    ]
    runs = {
        "packet_intelligence": SimpleNamespace(input_evidence_ids=["current-runtime-pcap"]),
        "pcm_intelligence": SimpleNamespace(input_evidence_ids=["unrelated-pcm-input"]),
    }
    results = {
        "packet_intelligence": {"calls": [{"call_id": "sip-current"}]},
        "pcm_intelligence": {"streams": []},
        "media_intelligence": None,
    }

    selected, input_ids, analyzer_name = _analysis_context_evidences(evidences, runs, results)

    assert analyzer_name == "packet_intelligence"
    assert input_ids == ["current-runtime-pcap"]
    assert [x["id"] for x in selected] == ["current-runtime-pcap"]


def test_context_binding_uses_media_run_inputs_when_call_falls_back_to_media_packet_result():
    evidences = [
        {"id": "old-packet-input", "type": "PCAP", "session_id": "old-session", "call_id": "old-call"},
        {"id": "current-offline-pcap", "type": "PCAP", "session_id": None, "call_id": None},
    ]
    runs = {
        "packet_intelligence": SimpleNamespace(input_evidence_ids=["old-packet-input"]),
        "media_intelligence": SimpleNamespace(input_evidence_ids=["current-offline-pcap"]),
    }
    results = {
        "packet_intelligence": {"calls": []},
        "pcm_intelligence": None,
        "media_intelligence": {"packet": {"calls": [{"call_id": "sip-media-fallback"}]}},
    }

    selected, input_ids, analyzer_name = _analysis_context_evidences(evidences, runs, results)

    assert analyzer_name == "media_intelligence"
    assert input_ids == ["current-offline-pcap"]
    assert [x["id"] for x in selected] == ["current-offline-pcap"]


def test_context_binding_falls_back_to_scoped_evidence_when_source_run_has_no_input_ids():
    evidences = [{"id": "case-pcap", "type": "PCAP"}]
    runs = {"packet_intelligence": SimpleNamespace(input_evidence_ids=[])}
    results = {"packet_intelligence": {"calls": [{"call_id": "sip-1"}]}}

    selected, input_ids, analyzer_name = _analysis_context_evidences(evidences, runs, results)

    assert selected == evidences
    assert input_ids == []
    assert analyzer_name == "packet_intelligence"


def test_case_runtime_scope_uses_current_packet_evidence_call_not_latest_case_call():
    current_session=SimpleNamespace(id="session-current",case_id="case-1")
    current_call=SimpleNamespace(id="call-current",case_id="case-1",session_id="session-current")
    historical_session=SimpleNamespace(id="session-latest-but-unrelated",case_id="case-1")
    historical_call=SimpleNamespace(id="call-latest-but-unrelated",case_id="case-1",session_id=historical_session.id)
    db=_FakeDb({
        (ReproductionCall,"call-current"):current_call,
        (ReproductionSession,"session-current"):current_session,
    })
    evidence=[{"type":"PCAP","session_id":"session-current","call_id":"call-current"}]

    session,call,meta=_case_runtime_scope_from_evidence(db,case_id="case-1",scope_type="CASE",context_evidences=evidence,
                                                        fallback_session=historical_session,fallback_call=historical_call)

    assert session is current_session
    assert call is current_call
    assert meta["source"]=="PACKET_EVIDENCE_CALL"
    assert meta["status"]=="RESOLVED"


def test_case_unbound_packet_keeps_latest_runtime_only_as_suppressible_history():
    historical_session=SimpleNamespace(id="historical-session",case_id="case-1")
    historical_call=SimpleNamespace(id="historical-call",case_id="case-1",session_id="historical-session")
    db=_FakeDb({})

    session,call,meta=_case_runtime_scope_from_evidence(db,case_id="case-1",scope_type="CASE",
        context_evidences=[{"type":"PCAP","session_id":None,"call_id":None}],
        fallback_session=historical_session,fallback_call=historical_call)

    assert session is historical_session
    assert call is historical_call
    assert meta["status"]=="SUPPRESSED_BY_OFFLINE_CONTEXT"


def test_case_packet_call_and_session_binding_mismatch_fails_closed():
    call_session=SimpleNamespace(id="session-from-call",case_id="case-1")
    bound_call=SimpleNamespace(id="call-1",case_id="case-1",session_id="session-from-call")
    db=_FakeDb({
        (ReproductionCall,"call-1"):bound_call,
        (ReproductionSession,"session-from-call"):call_session,
    })

    session,call,meta=_case_runtime_scope_from_evidence(db,case_id="case-1",scope_type="CASE",
        context_evidences=[{"type":"PCAP","session_id":"different-session","call_id":"call-1"}],
        fallback_session=None,fallback_call=None)

    assert session is None
    assert call is None
    assert meta["status"]=="BINDING_MISMATCH"


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
