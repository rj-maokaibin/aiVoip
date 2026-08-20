from __future__ import annotations

import json
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analyzers.media.candidate_decision import apply_candidate_decisions
from app.contracts.evidence_report import EvidenceReportScope
from app.db.models import AnalyzerRun, Case, CaseDevice, Evidence, ReproductionCall, ReproductionSession, VoiceRuntimeContextSnapshot
from app.reports.diagnostic_contract import build_diagnostic_contract_snapshot

REPORT_ANALYZERS = {"packet_intelligence", "pcm_intelligence", "media_intelligence"}
TERMINAL_ANALYZER_STATES = {"SUCCESS", "PARTIAL_SUCCESS", "FAILED", "UNAVAILABLE", "TIMEOUT"}


def scope_value(value: EvidenceReportScope | str) -> str:
    return value.value if isinstance(value, EvidenceReportScope) else str(value).upper()


def resolve_scope(db: Session, *, scope_type: EvidenceReportScope | str, scope_id: str) -> dict:
    scope_type=scope_value(scope_type)
    if scope_type==EvidenceReportScope.CALL.value:
        call=db.get(ReproductionCall,scope_id)
        if not call: raise ValueError("CALL_NOT_FOUND")
        return {"case":db.get(Case,call.case_id),"session":db.get(ReproductionSession,call.session_id),"call":call}
    if scope_type==EvidenceReportScope.SESSION.value:
        session=db.get(ReproductionSession,scope_id)
        if not session: raise ValueError("SESSION_NOT_FOUND")
        return {"case":db.get(Case,session.case_id),"session":session,"call":None}
    if scope_type==EvidenceReportScope.CASE.value:
        case=db.get(Case,scope_id)
        if not case: raise ValueError("CASE_NOT_FOUND")
        session=db.scalar(select(ReproductionSession).where(ReproductionSession.case_id==case.id).order_by(ReproductionSession.created_at.desc()).limit(1))
        call=db.scalar(select(ReproductionCall).where(ReproductionCall.session_id==session.id).order_by(ReproductionCall.call_no.desc()).limit(1)) if session else None
        return {"case":case,"session":session,"call":call}
    raise ValueError("REPORT_SCOPE_INVALID")


def case_dict(case: Case) -> dict:
    return {"id":case.id,"case_no":case.case_no,"summary":case.summary,"status":case.status}


def session_dict(session: ReproductionSession | None) -> dict | None:
    if not session: return None
    return {"id":session.id,"state":session.state,"profile_key":session.profile_key,"profile_version":session.profile_version,
            "capture_stage":session.capture_stage,"capture_completeness":session.capture_completeness,"evidence_sufficiency":session.evidence_sufficiency,
            "started_at":session.started_at.isoformat() if session.started_at else None,"ended_at":session.ended_at.isoformat() if session.ended_at else None}


def call_dict(call: ReproductionCall | None) -> dict | None:
    if not call: return None
    return {"id":call.id,"call_no":call.call_no,"external_call_ref":call.external_call_ref,"status":call.status,"verdict":call.verdict,"role":call.role,
            "started_at":call.started_at.isoformat() if call.started_at else None,"ended_at":call.ended_at.isoformat() if call.ended_at else None,
            "incomplete":call.ended_at is None}


def environment_snapshot(db: Session, case: Case, session: ReproductionSession | None) -> dict:
    devices=list(db.scalars(select(CaseDevice).where(CaseDevice.case_id==case.id).order_by(CaseDevice.created_at.asc())))
    context=db.scalar(select(VoiceRuntimeContextSnapshot).where(VoiceRuntimeContextSnapshot.session_id==session.id).limit(1)) if session else None
    return {"devices":[{"id":d.id,"sn":d.sn,"ip":d.ip,"platform_id":d.platform_id,"device_info":d.device_info or {}} for d in devices],
            "voice_runtime_context":context.snapshot_json if context else None,
            "reproduction_profile":{"key":session.profile_key,"version":session.profile_version,"checksum":session.profile_checksum} if session else None}


def scoped_evidences(db: Session, *, scope_type: str, scope: dict) -> list[Evidence]:
    case=scope["case"]; session=scope.get("session"); call=scope.get("call")
    stmt=select(Evidence).where(Evidence.case_id==case.id)
    if scope_type==EvidenceReportScope.CALL.value and call:
        stmt=stmt.where((Evidence.call_id==call.id)|((Evidence.call_id.is_(None))&(Evidence.session_id==call.session_id)))
    elif scope_type==EvidenceReportScope.SESSION.value and session:
        stmt=stmt.where(Evidence.session_id==session.id)
    return list(db.scalars(stmt.order_by(Evidence.created_at.asc())))


def evidence_dict(e: Evidence) -> dict:
    meta=e.metadata_json or {}
    payload_available=bool(meta.get("payload_available", True)) and str(e.completeness or "").upper() not in {"UNAVAILABLE","CORRUPTED"}
    retention_status=meta.get("retention_status")
    # For completeness calculations an expired payload is deliberately not
    # represented as PCAP/PCM_RX/PCM_TX. The original type remains available for
    # provenance and UI explanation.
    effective_type=e.type if payload_available else "EXPIRED_RAW_EVIDENCE"
    return {"id":e.id,"type":effective_type,"original_type":e.type,"source":e.source,"kind":e.kind,"scope":e.source_scope,"level":e.level,"completeness":e.completeness,
            "filename":e.filename,"sha256":e.sha256,"size_bytes":e.size_bytes,"session_id":e.session_id,"call_id":e.call_id,
            "payload_available":payload_available,"retention_status":retention_status,"retention_expired_at":meta.get("retention_expired_at"),
            "time_range_start":e.time_range_start.isoformat() if e.time_range_start else None,"time_range_end":e.time_range_end.isoformat() if e.time_range_end else None}


def latest_analyzer_runs(db: Session, *, case_id: str, evidence_ids: set[str], case_scope: bool) -> dict[str, AnalyzerRun]:
    rows=list(db.scalars(select(AnalyzerRun).where(AnalyzerRun.case_id==case_id,AnalyzerRun.analyzer_name.in_(REPORT_ANALYZERS)).order_by(AnalyzerRun.created_at.desc())))
    selected={}
    for run in rows:
        if run.analyzer_name in selected: continue
        inputs=set(run.input_evidence_ids or [])
        if case_scope or not inputs or bool(inputs&evidence_ids): selected[run.analyzer_name]=run
    return selected


def analyzer_state(run: AnalyzerRun | None) -> dict:
    if not run: return {"status":"UNAVAILABLE","terminal":True,"reason":"ANALYZER_RUN_NOT_FOUND"}
    return {"run_id":run.id,"status":run.status,"terminal":run.status in TERMINAL_ANALYZER_STATES,"analyzer_version":run.analyzer_version,
            "config_version":run.config_version,"config_checksum":run.config_checksum,"result_object_key":run.result_object_key,
            "error_code":run.error_code,"error_message":run.error_message}


def _normalize_packet_result(payload: dict) -> dict:
    """Keep report consumers compatible across RTP Analyzer field migrations.

    The canonical V1 report accepts the newer short jitter names while preserving
    the earlier RFC3550-qualified aliases already used by the report composer.
    No metric value is recalculated or upgraded here.
    """
    for stream in payload.get("rtp_streams", []) or []:
        pairs=(
            ("avg_rfc3550_jitter_ms","avg_jitter_ms"),
            ("p95_rfc3550_jitter_ms","p95_jitter_ms"),
            ("max_rfc3550_jitter_ms","max_jitter_ms"),
        )
        for legacy,current in pairs:
            if stream.get(legacy) is None and stream.get(current) is not None:
                stream[legacy]=stream.get(current)
            if stream.get(current) is None and stream.get(legacy) is not None:
                stream[current]=stream.get(legacy)
        if stream.get("loss_rate") is None and stream.get("loss_rate_percent") is not None:
            stream["loss_rate"]=stream.get("loss_rate_percent")
    return payload


def load_analyzer_results(storage, runs: dict[str, AnalyzerRun]) -> tuple[dict[str,dict|None],dict[str,dict]]:
    results={}; states={}
    for name in sorted(REPORT_ANALYZERS):
        run=runs.get(name); state=analyzer_state(run); states[name]=state
        if not run or run.status not in {"SUCCESS","PARTIAL_SUCCESS"} or not run.result_object_key:
            results[name]=None; continue
        try:
            payload=json.loads(storage.get_bytes(run.result_object_key).decode("utf-8"))
            if name=="packet_intelligence" and isinstance(payload,dict):
                payload=_normalize_packet_result(payload)
            results[name]=payload
            if results[name] and results[name].get("degraded_reason"): state["degraded_reason"]=results[name]["degraded_reason"]
        except Exception as exc:
            results[name]=None; state.update({"status":"FAILED","terminal":True,"error_code":type(exc).__name__,"error_message":str(exc)})
    # CandidateDecision is a deterministic evidence-normalization stage. Raw
    # detector candidates remain auditable, while only promoted candidates are
    # exposed to the Finding composer as user-visible anomalies.
    results=apply_candidate_decisions(results)

    # PR7 compatibility projection. Analyzer output contracts remain unchanged;
    # the in-memory report path receives one canonical DiagnosticEvent /
    # CandidateDecision snapshot. The snapshot is temporarily carried inside an
    # Analyzer summary so the existing Report Composer signature does not change.
    snapshot=build_diagnostic_contract_snapshot(results=results,analyzer_states=states)
    media=results.get("media_intelligence")
    pcm=results.get("pcm_intelligence")
    if isinstance(media,dict):
        media.setdefault("summary",{})["__diagnostic_contract_snapshot"]=snapshot
    elif isinstance(pcm,dict):
        pcm.setdefault("summary",{})["__diagnostic_contract_snapshot"]=snapshot
    return results,states
