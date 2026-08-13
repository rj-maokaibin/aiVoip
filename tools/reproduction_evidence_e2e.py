#!/usr/bin/env python3
from __future__ import annotations

import json
import shutil
import sys
import tempfile
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))

from app.contracts.enums import CallVerdict
from app.db.base import Base
from app.db.models import AnalyzerRun, Case, CaseDevice, Evidence, EvidenceFinalizeRun, ReproductionCaptureSegment
from app.integrations.storage import FilesystemObjectStorage
from app.reproduction.capture_pipeline import ReproductionCapturePipeline
from app.reproduction.mock_platform import MockReproductionPlatform
from app.reproduction.orchestrator import ReproductionOrchestrator
from app.reproduction.profile import ReproductionProfileRegistry
from app.reproduction.quick import QuickAnalysisInput


def setup(db,no):
    c=Case(case_no=no,summary='M6.2 C2 evidence E2E',status='ANALYZING'); db.add(c); db.flush()
    d=CaseDevice(case_id=c.id,ip='198.51.100.10',ssh_port=22,sn='MOCK-'+no,username='admin',device_info={}); db.add(d); db.flush()
    return c,d


def one_call(db,orch,session,findings):
    orch.record_activity(db,session=session,relative_ms=100)
    call=orch.bind_call(db,session=session,relative_ms=300)
    return orch.end_call(db,session=session,call_id=call.id,relative_ms=2500,signal=QuickAnalysisInput(CallVerdict.MATCH,findings=tuple(findings)))


def main():
    eng=create_engine('sqlite+pysqlite:///:memory:'); Base.metadata.create_all(eng)
    results=[]
    with tempfile.TemporaryDirectory(prefix='voip-c2-e2e-') as td:
        base=Path(td)
        with Session(eng) as db:
            for idx,(pid,findings,expected) in enumerate([
                ('AUDIO_NOISE',('ACTIVE_MEDIA_WINDOW','PERIODIC_INTERFERENCE','PCM_RTP_CORRELATION'),'PERIODIC_INTERFERENCE'),
                ('AUDIO_STUTTER',('ACTIVE_MEDIA_WINDOW','RTP_BURST_LOSS'),'RTP_BURST_LOSS'),
                ('ONE_WAY_AUDIO',('CALL_MEDIA_DIRECTION','ONE_WAY_RTP_MEDIA'),'ONE_WAY_RTP_MEDIA'),
                ('ECHO',('ECHO_PATH',),'ECHO_PATH'),
                ('DTMF_LOSS',('DTMF_PATH',),'DTMF_PATH'),
            ],1):
                c,_=setup(db,f'C2-E2E-{idx}')
                pipe=ReproductionCapturePipeline(root=base/f'capture-{idx}',storage=FilesystemObjectStorage(base/f'objects-{idx}'))
                orch=ReproductionOrchestrator(registry=ReproductionProfileRegistry(ROOT/'profiles'),platform=MockReproductionPlatform(),capture_pipeline=pipe)
                s=orch.create_session(db,case_id=c.id,profile_id=pid); orch.start(db,session=s)
                call,decision=one_call(db,orch,s,findings)
                assert s.state=='COMPLETED' and decision.sufficient and expected in call.quick_analysis_json['findings']
                assert call.live_summary_json and call.live_summary_json['mode']=='LIVE'
                assert call.quick_analysis_json['input_evidence_ids'] and call.quick_analysis_json['output_evidence_ids']
                segs=list(db.scalars(select(ReproductionCaptureSegment).where(ReproductionCaptureSegment.session_id==s.id)))
                assert any(x.retained and x.evidence_id for x in segs)
                final=db.scalar(select(EvidenceFinalizeRun).where(EvidenceFinalizeRun.session_id==s.id))
                assert final and final.status=='SUCCESS' and final.manifest_sha256
                run=db.get(AnalyzerRun,call.quick_analysis_json['analyzer_run_id'])
                assert run and run.input_evidence_ids==call.quick_analysis_json['input_evidence_ids']
                results.append({'scenario':pid,'status':'PASS','expected_finding':expected,'session_state':s.state,'segment_count':len(segs)})
    payload={'status':'PASS','scenarios':results,'passed':len(results),'total':len(results)}
    out=ROOT/'.reproduction-evidence-e2e.json'; out.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(payload,ensure_ascii=False))

if __name__=='__main__': main()
