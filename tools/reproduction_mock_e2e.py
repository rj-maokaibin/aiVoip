#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend'))

from app.contracts.enums import CallVerdict
from app.db.base import Base
from app.db.models import Case, CaseDevice
from app.reproduction.bundle import build_reproduction_evidence_bundle
from app.reproduction.mock_platform import MockReproductionPlatform
from app.reproduction.capture_pipeline import ReproductionCapturePipeline
from app.integrations.storage import FilesystemObjectStorage
from app.reproduction.orchestrator import ReproductionOrchestrator
from app.reproduction.profile import ReproductionProfileRegistry
from app.reproduction.quick import QuickAnalysisInput


def setup(db,no,device_info=None):
    c=Case(case_no=no,summary='M6.2 mock E2E',status='ANALYZING'); db.add(c); db.flush()
    d=CaseDevice(case_id=c.id,ip='198.51.100.10',ssh_port=22,sn='MOCK-'+no,username='admin',device_info=device_info or {}); db.add(d); db.flush()
    return c,d


def main():
    eng=create_engine('sqlite+pysqlite:///:memory:'); Base.metadata.create_all(eng)
    registry=ReproductionProfileRegistry(ROOT/'profiles')
    results=[]
    with tempfile.TemporaryDirectory(prefix='voip-c1-e2e-') as td:
        base=Path(td)
        def orch(name):
            pipe=ReproductionCapturePipeline(root=base/f'capture-{name}',storage=FilesystemObjectStorage(base/f'objects-{name}'))
            return ReproductionOrchestrator(registry=registry,platform=MockReproductionPlatform(),capture_pipeline=pipe)
        with Session(eng) as db:
            # Scenario 1: CONTROL then TARGET, evidence sufficiency => cleanup => COMPLETED.
            c,_=setup(db,'M62-E2E-1'); o=orch('s1')
            s=o.create_session(db,case_id=c.id,profile_id='AUDIO_NOISE'); o.start(db,session=s)
            a=o.record_activity(db,session=s,relative_ms=100); call=o.bind_call(db,session=s,relative_ms=300)
            o.end_call(db,session=s,call_id=call.id,relative_ms=3000,signal=QuickAnalysisInput(CallVerdict.NO_MATCH,findings=('ACTIVE_MEDIA_WINDOW',)))
            o.record_activity(db,session=s,relative_ms=5000); call2=o.bind_call(db,session=s,relative_ms=5200)
            _,dec=o.end_call(db,session=s,call_id=call2.id,relative_ms=9000,signal=QuickAnalysisInput(CallVerdict.MATCH,findings=('ACTIVE_MEDIA_WINDOW','PERIODIC_INTERFERENCE','PCM_RTP_CORRELATION')))
            bundle=build_reproduction_evidence_bundle(db,s)
            assert s.state=='COMPLETED' and dec.sufficient and [x['role'] for x in bundle['calls']]==['CONTROL','TARGET']
            results.append({'scenario':'CONTROL_TARGET_AUDIO_NOISE','status':'PASS','session_state':s.state})

            # Scenario 2: Generic partial capability downgrade remains explicit.
            c2,_=setup(db,'M62-E2E-2',{'mock_capture':{'pcm_rx_fail':True,'pcm_tx_fail':True}})
            o2=orch('s2')
            s2=o2.create_session(db,case_id=c2.id,profile_id='VOIP_GENERIC_FULL_CAPTURE'); o2.start(db,session=s2)
            assert s2.state=='WATCHING' and s2.capture_completeness=='PARTIAL'
            results.append({'scenario':'GENERIC_PARTIAL_CAPTURE','status':'PASS','session_state':s2.state})

            # Scenario 3: cleanup leak prevents lock release.
            c3,_=setup(db,'M62-E2E-3',{'mock_cleanup':{'pcm_tx_leak':True}})
            o3=orch('s3')
            s3=o3.create_session(db,case_id=c3.id,profile_id='AUDIO_STUTTER'); o3.start(db,session=s3); o3.cancel(db,session=s3)
            assert s3.state=='CLEANUP_FAILED'
            results.append({'scenario':'CLEANUP_REVERSE_VALIDATION','status':'PASS','session_state':s3.state})

    payload={'status':'PASS','scenarios':results,'passed':len(results),'total':len(results)}
    out=ROOT/'.reproduction-mock-e2e.json'; out.write_text(json.dumps(payload,ensure_ascii=False,indent=2),encoding='utf-8')
    print(json.dumps(payload,ensure_ascii=False))

if __name__=='__main__': main()
