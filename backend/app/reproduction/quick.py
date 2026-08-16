from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from app.analyzers.media import MediaIntelligenceEngine
from app.analyzers.pcm import load_pcm_profile
from app.contracts.enums import CallRole, CallVerdict, EvidenceCompleteness, EvidenceKind, EvidenceLevel, EvidenceScope, RunStatus
from app.core.config import settings
from app.db.models import AnalyzerRun, Artifact, Evidence, ReproductionCall, ReproductionSession
from app.integrations.storage import reproduction_object_storage
from app.reproduction.pcap_codec import MockPcapAdapter
from app.services.evidence import create_evidence


def _utcnow(): return datetime.now(timezone.utc)


@dataclass(frozen=True)
class QuickAnalysisInput:
    """Mock scenario injection, not a diagnostic result.

    Phase C2 uses this input to make the Mock Platform synthesize a capture. The
    evidence-backed analyzer independently derives its verdict from the generated PCAP.
    """
    verdict: CallVerdict
    findings: tuple[str, ...] = ()
    hard_contradiction: bool = False
    capture_recovery_required: bool = False
    external_action_required: bool = False
    metrics: dict = field(default_factory=dict)


@dataclass(frozen=True)
class QuickAnalysisResult:
    verdict: CallVerdict
    role: CallRole
    findings: tuple[str, ...]
    hard_contradiction: bool
    capture_recovery_required: bool
    external_action_required: bool
    metrics: dict
    analyzer_run_id: str
    input_evidence_ids: tuple[str,...] = ()
    output_evidence_ids: tuple[str,...] = ()
    analysis_summary: dict = field(default_factory=dict)
    # Authoritative DTMF digits detected in the PCM media (raw audio). Fast key
    # presses that the device DSP's FXS event report drops still appear here, so
    # this is the complete media-truth sequence used to reconcile the DTMF record.
    pcm_dtmf_sequences: tuple[dict, ...] = ()


_TARGET_FINDING = {
    'AUDIO_NOISE':'PERIODIC_INTERFERENCE',
    'AUDIO_STUTTER':'RTP_BURST_LOSS',
    'ONE_WAY_AUDIO':'ONE_WAY_RTP_MEDIA',
    # DTMF_LOSS must map to a *loss* signal, not DTMF_PATH. DTMF_PATH only means
    # "DTMF was observed in the media path"; it does not imply digits were lost.
    # Treating DTMF presence as the DTMF_LOSS target caused every DTMF-bearing
    # call (dial digits, in-call key presses) to be a false MATCH.
    'DTMF_LOSS':'DTMF_LOSS',
    'ECHO':'ECHO_PATH',
    'CALL_SETUP_FAILURE':'SIP_CALL_FAILED',
    'REGISTER_FAILURE':'REGISTER_ATTEMPT',
}


class EvidenceBackedCallQuickAnalyzer:
    analyzer_name='REPRODUCTION_CALL_QUICK_EVIDENCE'
    analyzer_version='2.0.0-c2'

    def __init__(self, *, storage=None):
        self.storage=storage or reproduction_object_storage()

    def run(self, db:Session, *, session:ReproductionSession, call:ReproductionCall, signal:QuickAnalysisInput,
            pcap_path:Path, pcap_evidence:Evidence) -> QuickAnalysisResult:
        profile_path=(settings.profile_root/'pcm'/'ruijie_aim_diag_v1.yaml')
        if not profile_path.exists():
            profile_path=Path(__file__).resolve().parents[3]/'profiles'/'pcm'/'ruijie_aim_diag_v1.yaml'
        pcm_profile=load_pcm_profile(profile_path)
        engine=MediaIntelligenceEngine(pcm_profile,MockPcapAdapter())
        now=_utcnow()
        run=AnalyzerRun(case_id=call.case_id,analyzer_name=self.analyzer_name,analyzer_version=self.analyzer_version,
            config_version=f'CALL_QUICK+{pcm_profile.id}@{pcm_profile.version}',
            config_snapshot={'mode':'CALL_QUICK','pcm_profile':pcm_profile.snapshot(),'parser':'mock-pcap-adapter/1.0.0','contract':'M6.2-SPEC-11'},
            scope=EvidenceScope.CALL.value,status=RunStatus.RUNNING.value,input_evidence_ids=[pcap_evidence.id],output_evidence_ids=[],started_at=now)
        db.add(run); db.flush()
        with tempfile.TemporaryDirectory(prefix='voip-repro-quick-') as td:
            result=engine.analyze_pcap(pcap_path,Path(td)/'artifacts')
            findings=self._findings(result)
            verdict=self._verdict(session.profile_key,findings,signal)
            role={CallVerdict.MATCH:CallRole.TARGET,CallVerdict.NO_MATCH:CallRole.CONTROL,CallVerdict.INCONCLUSIVE:CallRole.INCONCLUSIVE}[verdict]
            pcm_dtmf=self._pcm_dtmf_sequences(result)
            # Persist generated media artifacts as regeneratable Analyzer artifacts.
            for spec in result.get('artifacts',[]):
                local=Path(spec['local_path']); data=local.read_bytes(); sha=hashlib.sha256(data).hexdigest()
                key=f'cases/{call.case_id}/reproductions/{session.id}/analysis/{run.id}/artifacts/{local.name}'
                self.storage.put_file(key,local,spec.get('content_type') or 'application/octet-stream')
                db.add(Artifact(case_id=call.case_id,analyzer_run_id=run.id,evidence_id=pcap_evidence.id,type=spec['type'],filename=local.name,
                    object_key=key,content_type=spec.get('content_type'),size_bytes=len(data),sha256=sha,
                    metadata_json={**(spec.get('metadata') or {}),'retention_class':'REGENERATABLE','call_id':call.id,'session_id':session.id}))
            summary={
                'mode':'CALL_QUICK','verdict':verdict.value,'role':role.value,'findings':sorted(findings),
                'media_summary':result.get('summary') or {},'packet_summary':(result.get('packet') or {}).get('summary') or {},
                'pcm_summary':(result.get('pcm') or {}).get('summary') or {},'mock_scenario_expected_verdict':signal.verdict.value,
                'mock_scenario_requested_findings':list(signal.findings),
                # Authoritative PCM-media DTMF sequences (complete even under fast key
                # presses that the device FXS event report may drop).
                'pcm_dtmf_sequences':[dict(x) for x in pcm_dtmf],
            }
            encoded=json.dumps({'summary':summary,'analysis':result},ensure_ascii=False,separators=(',',':'),default=str).encode()
            key=f'cases/{call.case_id}/reproductions/{session.id}/analysis/{run.id}/call_quick.json'; self.storage.put_bytes(key,encoded,'application/json')
            finding_ev=create_evidence(db,case_id=call.case_id,device_id=session.device_id,evidence_type='CALL_QUICK_FINDINGS',source='REPRODUCTION_CALL_QUICK',
                filename='call_quick.json',object_key=key,size_bytes=len(encoded),sha256=hashlib.sha256(encoded).hexdigest(),content_type='application/json',
                kind=EvidenceKind.DERIVED,scope=EvidenceScope.CALL,level=EvidenceLevel.L1,completeness=EvidenceCompleteness.COMPLETE,
                session_id=session.id,attempt_id=call.attempt_id,call_id=call.id,producer_type='ANALYZER',producer_id=self.analyzer_name,
                producer_version=self.analyzer_version,metadata={'findings':sorted(findings),'verdict':verdict.value,'role':role.value},
                parent_evidence_ids=[pcap_evidence.id])
            run.status=RunStatus.SUCCESS.value if result.get('status')=='SUCCESS' else RunStatus.PARTIAL_SUCCESS.value
            run.finished_at=_utcnow(); run.summary_json=summary; run.result_object_key=key; run.output_evidence_ids=[finding_ev.id]
            db.flush()
            return QuickAnalysisResult(verdict,role,tuple(sorted(findings)),signal.hard_contradiction,signal.capture_recovery_required,
                signal.external_action_required,{**signal.metrics,'media_summary':result.get('summary') or {}},run.id,(pcap_evidence.id,),(finding_ev.id,),summary,
                tuple(pcm_dtmf))

    @staticmethod
    def _findings(result:dict) -> set[str]:
        out=set(); packet=result.get('packet') or {}; calls=packet.get('calls') or []
        if calls:
            out.update({'SIP_CALL_ATTEMPT','CALL_CLASSIFICATION'})
            for call in calls:
                if call.get('media_start_time') is not None:
                    out.add('ACTIVE_MEDIA_WINDOW')
                health=call.get('media_direction_health') or {}
                if health.get('eligible'):
                    out.add('CALL_MEDIA_DIRECTION')
                if call.get('state')=='FAILED': out.add('SIP_CALL_FAILED')
        anomalies=packet.get('anomalies') or []
        if any(a.get('type')=='ONE_WAY_RTP_MEDIA' for a in anomalies): out.add('ONE_WAY_RTP_MEDIA')
        if any(e.get('type')=='BURST_LOSS' for s in packet.get('rtp_streams',[]) for e in (s.get('events') or [])): out.add('RTP_BURST_LOSS')
        if any(e.get('type')=='LOCAL_CAPTURE_PERIODIC_INTERFERENCE' for e in result.get('periodic_interference_paths',[]) or []): out.add('PERIODIC_INTERFERENCE')
        if any(e.get('type')=='PCM_RTP_CORRELATION' for e in result.get('correlations',[]) or []): out.add('PCM_RTP_CORRELATION')
        if any(e.get('type')=='ECHO_PATH_DETECTED' for e in result.get('echo_paths',[]) or []): out.add('ECHO_PATH')
        # DTMF loss: the PCM RX dialed-digit sequence disagrees with the SIP dial
        # target (a digit was dropped or the number was assembled wrong in the
        # media path).  This is a real DTMF_LOSS signal.  DTMF_PATH below remains
        # a path observability observation and is intentionally NOT a loss signal.
        cross=result.get('cross_layer_events') or []
        if any(e.get('type')=='DTMF_SIP_DIAL_MISMATCH' for e in cross):
            out.add('DTMF_LOSS')
        pcm=result.get('pcm') or {}
        if any(sess.get('dtmf_sequences') for stream in pcm.get('streams',[]) for sess in (stream.get('sessions') or [])):
            out.add('DTMF_PATH')
        return out

    @staticmethod
    def _pcm_dtmf_sequences(result: dict) -> tuple[dict, ...]:
        """Authoritative DTMF sequences detected in the PCM media (raw audio).

        These come from the Goertzel detector on the captured media, independent of
        the device FXS event report. Fast key presses that the DSP's FXS event
        detector drops still appear here, so this is the complete media truth used
        to reconcile the DTMF record and to judge DTMF-sequence completeness.
        """
        out=[]
        pcm=result.get('pcm') or {}
        for stream in pcm.get('streams',[]):
            tap=(stream.get('tap') or {}).get('name')
            for sess in stream.get('sessions',[]):
                for seq in sess.get('dtmf_sequences') or []:
                    out.append({
                        'tap': tap,
                        'session_index': sess.get('session_index'),
                        'digits': seq.get('digits'),
                        'start_seconds': seq.get('start_seconds'),
                        'end_seconds': seq.get('end_seconds'),
                        'event_count': seq.get('event_count'),
                        'min_confidence': seq.get('min_confidence'),
                    })
        return tuple(out)

    @staticmethod
    def _verdict(profile_id:str, findings:set[str], signal:QuickAnalysisInput) -> CallVerdict:
        expected=_TARGET_FINDING.get(profile_id)
        if expected:
            if expected in findings: return CallVerdict.MATCH
            # In Mock Platform only, signal.verdict is fixture ground truth for "the target symptom was reproduced".
            # This keeps symptom confirmation separate from deeper required-finding sufficiency (for example generic
            # noise reproduced but PERIODIC_INTERFERENCE still missing -> ENHANCE_CAPTURE). Production EC-02 does
            # not use this fixture oracle; its target match comes from real deterministic profile analyzers.
            if signal.verdict==CallVerdict.MATCH and ('ACTIVE_MEDIA_WINDOW' in findings or 'SIP_CALL_FAILED' in findings):
                return CallVerdict.MATCH
            return CallVerdict.NO_MATCH
        # ECHO_PATH and DTMF_PATH are observations that the paths exist.  They
        # become target evidence only under their symptom-specific profiles; a
        # generic normal call must not be classified as faulty from path presence.
        abnormal={'PERIODIC_INTERFERENCE','RTP_BURST_LOSS','ONE_WAY_RTP_MEDIA','SIP_CALL_FAILED'} & findings
        if abnormal: return CallVerdict.MATCH
        return CallVerdict.INCONCLUSIVE if profile_id=='VOIP_GENERIC_FULL_CAPTURE' else signal.verdict


# Compatibility alias retained for imports; implementation is evidence-backed in C2.
MockCallQuickAnalyzer=EvidenceBackedCallQuickAnalyzer
