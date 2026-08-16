import hashlib
import math
import wave
from pathlib import Path

import numpy as np
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.contracts.enums import EvidenceCompleteness, EvidenceKind, EvidenceLevel, EvidenceScope
from app.db.base import Base
from app.db.models import AnalyzerRun, Case, Evidence
from app.diagnosis.reasoner import DeterministicDiagnosisReasoner
from app.diagnosis.snapshot import CaseEvidenceSnapshotBuilder
from app.services.analysis import create_field_audio_analysis_job
from app.services.evidence import create_evidence


def test_field_audio_worker_persists_result_and_resumes_reasoning(monkeypatch, tmp_path):
    import app.workers.attachment_tasks as tasks
    engine=create_engine('sqlite+pysqlite:///:memory:',poolclass=StaticPool,connect_args={'check_same_thread':False})
    Base.metadata.create_all(engine)
    rate=8000; samples=(np.sin(2*math.pi*1000*np.arange(rate*2)/rate)*9000).astype('<i2')
    source=tmp_path/'field.wav'
    with wave.open(str(source),'wb') as out:
        out.setnchannels(1); out.setsampwidth(2); out.setframerate(rate); out.writeframes(samples.tobytes())
    objects={'cases/c/evidence/field.wav':source.read_bytes()}

    class Storage:
        def put_bytes(self,key,data,content_type='application/octet-stream'): objects[key]=data
        def get_bytes(self,key): return objects[key]

    storage=Storage()
    monkeypatch.setattr(tasks,'SessionLocal',lambda:Session(engine))
    monkeypatch.setattr(tasks,'ObjectStorage',lambda:storage)
    monkeypatch.setattr(tasks,'materialize_evidence',lambda evidence,path,**kwargs: Path(path).write_bytes(objects[evidence.object_key]))
    monkeypatch.setattr('app.workers.diagnosis_tasks.notify_case_changed',lambda case_id:None)
    with Session(engine) as db:
        case=Case(id='c',case_no='CASE-MULTI-1',summary='电话有电流音',status='NEW',created_by='test')
        db.add(case); db.flush()
        evidence=create_evidence(db,evidence_id='audio',case_id=case.id,evidence_type='FIELD_AUDIO_WAV',
            source='FEISHU_ATTACHMENT',filename='field.wav',object_key='cases/c/evidence/field.wav',
            size_bytes=len(objects['cases/c/evidence/field.wav']),sha256=hashlib.sha256(objects['cases/c/evidence/field.wav']).hexdigest(),
            kind=EvidenceKind.RAW,scope=EvidenceScope.CASE,level=EvidenceLevel.L1,
            completeness=EvidenceCompleteness.COMPLETE,content_type='audio/wav')
        db.commit()
        job=create_field_audio_analysis_job(db,case_id=case.id,evidence_id=evidence.id)
        job_id=job.id
    outcome=tasks.analyze_field_audio.run(job_id,'audio')
    assert outcome['status']=='SUCCESS'
    with Session(engine) as db:
        run=db.scalar(select(AnalyzerRun).where(AnalyzerRun.analyzer_name=='field_audio_intelligence'))
        assert run is not None and run.status=='SUCCESS'
        snapshot=CaseEvidenceSnapshotBuilder(storage).build(db,'c')
    decision=DeterministicDiagnosisReasoner().reason(snapshot)
    assert any('现场录音已分析' in item for item in decision.known)
    assert not any(action.action_type=='RUN_FIELD_AUDIO_ANALYSIS' for action in decision.plan)
