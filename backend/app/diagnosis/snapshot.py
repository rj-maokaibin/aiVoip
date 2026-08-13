from __future__ import annotations
import hashlib
import json
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.db.models import AnalyzerRun, Case, CaseDevice, Evidence
from app.integrations.storage import ObjectStorage

SUCCESS={'SUCCESS','PARTIAL_SUCCESS','SUCCEEDED'}  # SUCCEEDED accepted only for legacy stored runs

class CaseEvidenceSnapshotBuilder:
    def __init__(self, storage:ObjectStorage|None=None): self.storage=storage or ObjectStorage()

    def build(self, db:Session, case_id:str) -> dict:
        case=db.get(Case,case_id)
        if not case: raise ValueError('CASE_NOT_FOUND')
        devices=list(db.scalars(select(CaseDevice).where(CaseDevice.case_id==case_id).order_by(CaseDevice.created_at.asc())))
        evidences=list(db.scalars(select(Evidence).where(Evidence.case_id==case_id).order_by(Evidence.created_at.asc())))
        runs=list(db.scalars(select(AnalyzerRun).where(AnalyzerRun.case_id==case_id,AnalyzerRun.status.in_(SUCCESS)).order_by(AnalyzerRun.created_at.asc())))
        latest={}
        for run in runs:
            latest[run.analyzer_name]=run
        analyzer_results={}
        for name,run in latest.items():
            result=None
            if run.result_object_key:
                try: result=json.loads(self.storage.get_bytes(run.result_object_key))
                except Exception: result=None
            analyzer_results[name]={
                'run_id':run.id,'status':run.status,'version':run.analyzer_version,'config_version':run.config_version,
                'summary':run.summary_json or {},'result':result,
            }
        data={
            'case':{'id':case.id,'case_no':case.case_no,'summary':case.summary,'status':case.status},
            'devices':[{'id':d.id,'ip':d.ip,'ssh_port':d.ssh_port,'sn':d.sn,'platform_id':d.platform_id} for d in devices],
            'evidences':[{'id':e.id,'type':e.type,'source':e.source,'filename':e.filename,'sha256':e.sha256,'metadata':e.metadata_json or {}} for e in evidences],
            'analyzers':analyzer_results,
        }
        data['fingerprint']=self.fingerprint(data)
        return data

    @staticmethod
    def fingerprint(snapshot:dict) -> str:
        stable={
            'evidences':[(e['id'],e['sha256'],e['type']) for e in snapshot.get('evidences',[])],
            'analyzers':[(k,v.get('run_id'),v.get('status'),v.get('version')) for k,v in sorted(snapshot.get('analyzers',{}).items())],
        }
        return hashlib.sha256(json.dumps(stable,sort_keys=True,separators=(',',':')).encode()).hexdigest()
