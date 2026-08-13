from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from app.analyzers.packet import PacketIntelligenceEngine
from app.analyzers.pcm import PcmIntelligenceEngine, load_pcm_profile
from app.contracts.enums import EvidenceCompleteness, EvidenceKind, EvidenceLevel, EvidenceScope, RunStatus
from app.core.config import settings
from app.db.models import AnalyzerRun, Evidence, ReproductionCall, ReproductionSession
from app.integrations.storage import reproduction_object_storage
from app.reproduction.pcap_codec import MockPcapAdapter
from app.services.evidence import create_evidence


def _utcnow(): return datetime.now(timezone.utc)


class LiveReproductionAnalyzer:
    analyzer_name='REPRODUCTION_LIVE_ANALYZER'
    analyzer_version='1.0.0-c2'

    def __init__(self, *, storage=None):
        self.storage=storage or reproduction_object_storage()

    def run(self, db:Session, *, session:ReproductionSession, call:ReproductionCall, pcap_path:Path, input_evidence:Evidence) -> dict:
        profile_path=settings.profile_root/'pcm'/'ruijie_aim_diag_v1.yaml'
        if not profile_path.exists(): profile_path=Path(__file__).resolve().parents[3]/'profiles'/'pcm'/'ruijie_aim_diag_v1.yaml'
        pcm_profile=load_pcm_profile(profile_path)
        packet=PacketIntelligenceEngine(MockPcapAdapter()).analyze_pcap(pcap_path)
        pcm=PcmIntelligenceEngine(pcm_profile).analyze_pcap(pcap_path)
        findings=[]
        if packet.get('summary',{}).get('call_count',0)>0: findings.append('SIP_CALL_LIVE')
        if packet.get('summary',{}).get('rtp_stream_count',0)>0: findings.append('RTP_BASIC_LIVE')
        if pcm.get('summary',{}).get('total_packets',0)>0: findings.append('PCM_STREAM_HEALTH')
        summary={'mode':'LIVE','findings':findings,'packet_summary':packet.get('summary') or {},'pcm_summary':pcm.get('summary') or {}}
        run=AnalyzerRun(case_id=call.case_id,analyzer_name=self.analyzer_name,analyzer_version=self.analyzer_version,
            config_version='LIVE_C2',config_snapshot={'mode':'LIVE','parser':'mock-pcap-adapter/1.0.0','pcm_profile':pcm_profile.snapshot()},
            scope=EvidenceScope.CALL.value,status=RunStatus.SUCCESS.value,input_evidence_ids=[input_evidence.id],output_evidence_ids=[],summary_json=summary,
            started_at=_utcnow(),finished_at=_utcnow())
        db.add(run); db.flush()
        raw=json.dumps({'summary':summary},ensure_ascii=False,separators=(',',':')).encode(); key=f'cases/{call.case_id}/reproductions/{session.id}/analysis/{run.id}/live.json'; self.storage.put_bytes(key,raw,'application/json')
        ev=create_evidence(db,case_id=call.case_id,device_id=session.device_id,evidence_type='LIVE_ANALYZER_FINDINGS',source='REPRODUCTION_LIVE_ANALYZER',
            filename='live.json',object_key=key,size_bytes=len(raw),sha256=hashlib.sha256(raw).hexdigest(),content_type='application/json',kind=EvidenceKind.DERIVED,
            scope=EvidenceScope.CALL,level=EvidenceLevel.L2,completeness=EvidenceCompleteness.COMPLETE,session_id=session.id,attempt_id=call.attempt_id,call_id=call.id,
            producer_type='ANALYZER',producer_id=self.analyzer_name,producer_version=self.analyzer_version,metadata={'findings':findings},parent_evidence_ids=[input_evidence.id])
        run.output_evidence_ids=[ev.id]; run.result_object_key=key; db.flush()
        return {'analyzer_run_id':run.id,'evidence_id':ev.id,**summary}
