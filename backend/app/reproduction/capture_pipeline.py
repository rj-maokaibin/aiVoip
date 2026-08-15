from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.contracts.enums import (
    CaptureChannel, CaptureSegmentStatus, EvidenceCompleteness, EvidenceFinalizeStatus,
    EvidenceKind, EvidenceLevel, EvidenceRelationType, EvidenceScope, RetentionClass,
)
from app.core.config import settings
from app.db.models import (
    EvidenceFinalizeRun, ReproductionAttempt, ReproductionCall, ReproductionCaptureSegment,
    ReproductionCaptureState, ReproductionSession,
)
from app.integrations.storage import reproduction_object_storage
from app.reproduction.pcap_codec import merge_classic_pcaps
from app.services.evidence import create_evidence


def _utcnow(): return datetime.now(timezone.utc)

def _sha256(path: Path) -> str:
    h=hashlib.sha256()
    with path.open('rb') as fh:
        for chunk in iter(lambda:fh.read(1024*1024),b''): h.update(chunk)
    return h.hexdigest()


class ReproductionCapturePipeline:
    """File-backed segmented capture pipeline for Phase C2.

    Raw segment files are written locally, evicted while in ring mode, frozen on the
    earliest anchor, then persisted as immutable RAW Evidence. Call/session merged
    captures are DERIVED Evidence with explicit lineage to retained raw segments.
    """
    version='1.0.0-c2'

    def __init__(self, *, root: Path|None=None, storage=None):
        self.root=Path(root or settings.reproduction_capture_root)
        self.storage=storage or reproduction_object_storage()

    def state(self, db:Session, session:ReproductionSession, *, pretrigger_ms:int, segment_ms:int) -> ReproductionCaptureState:
        row=db.scalar(select(ReproductionCaptureState).where(ReproductionCaptureState.session_id==session.id))
        if row is None:
            row=ReproductionCaptureState(session_id=session.id,pretrigger_ms=pretrigger_ms,segment_ms=segment_ms)
            db.add(row); db.flush()
        return row

    def _session_dir(self, session_id:str) -> Path:
        p=self.root/session_id; p.mkdir(parents=True,exist_ok=True); return p

    def _write_segment(self, db:Session, *, session:ReproductionSession, channel:CaptureChannel, start_ms:int, end_ms:int,
                       data:bytes, suffix:str, content_type:str, attempt_id:str|None=None, call_id:str|None=None,
                       retention:RetentionClass=RetentionClass.TEMP_RING, metadata:dict|None=None) -> ReproductionCaptureSegment:
        if end_ms < start_ms: raise ValueError('SEGMENT_TIME_INVALID')
        no=(db.scalar(select(func.count(ReproductionCaptureSegment.id)).where(
            ReproductionCaptureSegment.session_id==session.id,ReproductionCaptureSegment.channel==channel.value)) or 0)+1
        d=self._session_dir(session.id)/channel.value.lower(); d.mkdir(parents=True,exist_ok=True)
        path=d/f'{no:06d}_{start_ms}_{end_ms}{suffix}'; tmp=path.with_suffix(path.suffix+'.tmp'); tmp.write_bytes(data); tmp.replace(path)
        digest=_sha256(path)
        row=ReproductionCaptureSegment(session_id=session.id,attempt_id=attempt_id,call_id=call_id,channel=channel.value,
            segment_no=no,start_ms=int(start_ms),end_ms=int(end_ms),local_path=str(path),content_type=content_type,
            size_bytes=path.stat().st_size,sha256=digest,status=CaptureSegmentStatus.ACTIVE.value,frozen=False,retained=False,
            retention_class=retention.value,metadata_json={'pipeline_version':self.version,**(metadata or {})})
        db.add(row); db.flush()
        st=db.scalar(select(ReproductionCaptureState).where(ReproductionCaptureState.session_id==session.id))
        if st: st.total_bytes=int(st.total_bytes or 0)+row.size_bytes
        return row

    def append_pcap(self, db:Session, **kwargs) -> ReproductionCaptureSegment:
        return self._write_segment(db,channel=CaptureChannel.PCAP,suffix='.pcap',content_type='application/vnd.tcpdump.pcap',**kwargs)

    def append_log(self, db:Session, **kwargs) -> ReproductionCaptureSegment:
        return self._write_segment(db,channel=CaptureChannel.DEBUG,suffix='.log',content_type='text/plain',**kwargs)

    def evict_ring(self, db:Session, *, session:ReproductionSession, current_end_ms:int):
        st=db.scalar(select(ReproductionCaptureState).where(ReproductionCaptureState.session_id==session.id))
        if not st or st.preserve_mode: return []
        cutoff=int(current_end_ms)-int(st.pretrigger_ms); evicted=[]
        rows=list(db.scalars(select(ReproductionCaptureSegment).where(
            ReproductionCaptureSegment.session_id==session.id,
            ReproductionCaptureSegment.retained.is_(False),ReproductionCaptureSegment.frozen.is_(False),
            ReproductionCaptureSegment.end_ms < cutoff,
        )))
        for row in rows:
            path=Path(row.local_path)
            if path.exists(): path.unlink()
            row.status=CaptureSegmentStatus.EVICTED.value; row.retention_class=RetentionClass.TEMP_RING.value
            evicted.append(row.id)
        return evicted

    def freeze(self, db:Session, *, session:ReproductionSession, anchor_ms:int, attempt_id:str|None=None):
        profile=session.effective_profile_snapshot or {}; ring=profile.get('ring') or {}
        st=self.state(db,session,pretrigger_ms=int(ring.get('pretrigger_seconds',30))*1000,segment_ms=int(ring.get('segment_seconds',5))*1000)
        cutoff=int(anchor_ms)-int(st.pretrigger_ms); st.preserve_mode=True; st.freeze_anchor_ms=int(anchor_ms)
        rows=list(db.scalars(select(ReproductionCaptureSegment).where(
            ReproductionCaptureSegment.session_id==session.id,
            ReproductionCaptureSegment.status!=CaptureSegmentStatus.EVICTED.value,
            ReproductionCaptureSegment.end_ms >= cutoff,
        )))
        for row in rows:
            row.frozen=True; row.status=CaptureSegmentStatus.FROZEN.value
            if attempt_id and not row.attempt_id: row.attempt_id=attempt_id
            self._retain_raw_segment(db,session,row)
        db.flush(); return rows

    def preserve_new_segment(self, db:Session, *, session:ReproductionSession, row:ReproductionCaptureSegment):
        st=db.scalar(select(ReproductionCaptureState).where(ReproductionCaptureState.session_id==session.id))
        if st and st.preserve_mode:
            row.frozen=True; row.status=CaptureSegmentStatus.FROZEN.value
            self._retain_raw_segment(db,session,row)
        return row

    def reset_after_attempt(self, db:Session, *, session:ReproductionSession, invalid:bool=False):
        st=db.scalar(select(ReproductionCaptureState).where(ReproductionCaptureState.session_id==session.id))
        if st:
            st.preserve_mode=False; st.freeze_anchor_ms=None
        if invalid:
            rows=list(db.scalars(select(ReproductionCaptureSegment).where(
                ReproductionCaptureSegment.session_id==session.id,ReproductionCaptureSegment.frozen.is_(True),
                ReproductionCaptureSegment.call_id.is_(None),
            )))
            for row in rows:
                row.retention_class=RetentionClass.SHORT_ATTEMPT.value
        db.flush()

    def _retain_raw_segment(self, db:Session, session:ReproductionSession, row:ReproductionCaptureSegment):
        if row.evidence_id:
            row.retained=True; row.status=CaptureSegmentStatus.RETAINED.value; return row.evidence_id
        path=Path(row.local_path)
        if not path.exists():
            row.status=CaptureSegmentStatus.CORRUPTED.value
            return None
        object_key=f'cases/{session.case_id}/reproductions/{session.id}/segments/{row.channel.lower()}/{path.name}'
        self.storage.put_file(object_key,path,row.content_type)
        ev=create_evidence(db,case_id=session.case_id,device_id=session.device_id,evidence_type=f'{row.channel}_SEGMENT',
            source='REPRODUCTION_COLLECTOR',filename=path.name,object_key=object_key,size_bytes=row.size_bytes,sha256=row.sha256,
            content_type=row.content_type,kind=EvidenceKind.RAW,scope=EvidenceScope.ATTEMPT if row.attempt_id else EvidenceScope.SESSION,
            level=EvidenceLevel.L1,completeness=EvidenceCompleteness.COMPLETE,session_id=session.id,attempt_id=row.attempt_id,
            call_id=row.call_id,producer_type='REPRODUCTION_CAPTURE_PIPELINE',producer_id='SEGMENT_WRITER',producer_version=self.version,
            metadata={'retention_class':RetentionClass.PERMANENT_RAW.value,'segment_id':row.id,'start_ms':row.start_ms,'end_ms':row.end_ms})
        row.evidence_id=ev.id; row.retained=True; row.status=CaptureSegmentStatus.RETAINED.value; row.retention_class=RetentionClass.PERMANENT_RAW.value
        return ev.id

    def _overlap_segments(self, db:Session, session_id:str, *, channel:CaptureChannel, start_ms:int, end_ms:int):
        return list(db.scalars(select(ReproductionCaptureSegment).where(
            ReproductionCaptureSegment.session_id==session_id,ReproductionCaptureSegment.channel==channel.value,
            ReproductionCaptureSegment.retained.is_(True),ReproductionCaptureSegment.start_ms <= end_ms,
            ReproductionCaptureSegment.end_ms >= start_ms,
        ).order_by(ReproductionCaptureSegment.segment_no)))

    def build_call_capture(self, db:Session, *, session:ReproductionSession, call:ReproductionCall) -> tuple[Path, object]:
        attempt=db.get(ReproductionAttempt,call.attempt_id) if call.attempt_id else None
        start=int(attempt.start_anchor_ms if attempt and attempt.start_anchor_ms is not None else 0)
        end=int(attempt.end_anchor_ms if attempt and attempt.end_anchor_ms is not None else start+1000)
        rows=self._overlap_segments(db,session.id,channel=CaptureChannel.PCAP,start_ms=start,end_ms=end)
        # The CALL_FINAL segment is the platform's merged in-call media (for the real
        # platform it already includes the pretrigger via platform.cache_pretrigger,
        # so the dialing DTMF/silence reach CALL_QUICK without merging here). Prefer
        # the final segment exclusively to avoid contaminating the analysis with a
        # prior call's overlapping windows (the mock platform's final pcap is also
        # self-contained).
        final_rows=[x for x in rows if x.call_id==call.id and (x.metadata_json or {}).get('mock_final_call')]
        if final_rows:
            rows=final_rows
        else:
            # In non-mock/file-streaming paths prefer segments explicitly bound to
            # this call when such binding exists; otherwise fall back to the time
            # overlap set for reconstructed anchors.
            bound_rows=[x for x in rows if x.call_id==call.id]
            if bound_rows:
                rows=bound_rows
        if not rows: raise ValueError('CALL_CAPTURE_SEGMENTS_MISSING')
        for row in rows: self._retain_raw_segment(db,session,row)
        out=self._session_dir(session.id)/'calls'/call.id/'call.pcap'; out.parent.mkdir(parents=True,exist_ok=True)
        merge_classic_pcaps([x.local_path for x in rows],out)
        digest=_sha256(out); key=f'cases/{session.case_id}/reproductions/{session.id}/calls/{call.id}/call.pcap'; self.storage.put_file(key,out,'application/vnd.tcpdump.pcap')
        parent_ids=[x.evidence_id for x in rows if x.evidence_id]
        ev=create_evidence(db,case_id=session.case_id,device_id=session.device_id,evidence_type='CALL_PCAP',source='REPRODUCTION_COLLECTOR',
            filename='call.pcap',object_key=key,size_bytes=out.stat().st_size,sha256=digest,content_type='application/vnd.tcpdump.pcap',
            kind=EvidenceKind.DERIVED,scope=EvidenceScope.CALL,level=EvidenceLevel.L1,completeness=EvidenceCompleteness.COMPLETE,
            session_id=session.id,attempt_id=call.attempt_id,call_id=call.id,producer_type='REPRODUCTION_CAPTURE_PIPELINE',producer_id='CALL_WINDOW_MERGER',
            producer_version=self.version,metadata={'retention_class':RetentionClass.PERMANENT_DERIVED.value,'segment_count':len(rows)},
            parent_evidence_ids=parent_ids,relation_type=EvidenceRelationType.DERIVED_FROM)
        return out,ev

    def finalize_session(self, db:Session, *, session:ReproductionSession) -> dict:
        existing=db.scalar(select(EvidenceFinalizeRun).where(EvidenceFinalizeRun.session_id==session.id,EvidenceFinalizeRun.status==EvidenceFinalizeStatus.SUCCESS.value).order_by(EvidenceFinalizeRun.run_no.desc()))
        if existing:
            return {'status':existing.status,'evidence_ids':existing.evidence_ids_json or [],'manifest_object_key':existing.manifest_object_key,'idempotent':True}
        run_no=(db.scalar(select(func.count(EvidenceFinalizeRun.id)).where(EvidenceFinalizeRun.session_id==session.id)) or 0)+1
        run=EvidenceFinalizeRun(session_id=session.id,run_no=run_no,status=EvidenceFinalizeStatus.RUNNING.value,started_at=_utcnow()); db.add(run); db.flush()
        try:
            evidence_ids=[]; manifest={'schema_version':1,'pipeline_version':self.version,'session_id':session.id,'case_id':session.case_id,'objects':[]}
            for channel,suffix,ctype in [(CaptureChannel.PCAP,'.pcap','application/vnd.tcpdump.pcap'),(CaptureChannel.DEBUG,'.log','text/plain')]:
                rows=list(db.scalars(select(ReproductionCaptureSegment).where(
                    ReproductionCaptureSegment.session_id==session.id,ReproductionCaptureSegment.channel==channel.value,
                    ReproductionCaptureSegment.retained.is_(True)).order_by(ReproductionCaptureSegment.segment_no)))
                if channel==CaptureChannel.PCAP:
                    rows=[x for x in rows if not (x.metadata_json or {}).get('mock_probe_only')]
                # Robustness: skip segments whose raw file is gone (e.g. wiped by a
                # container recreate before the persistence fix) instead of crashing
                # the whole finalize / reconcile run on FileNotFoundError.
                rows=[x for x in rows if x.local_path and Path(x.local_path).exists()]
                if not rows: continue
                for row in rows: self._retain_raw_segment(db,session,row)
                out=self._session_dir(session.id)/'final'/f'session_{channel.value.lower()}{suffix}'; out.parent.mkdir(parents=True,exist_ok=True)
                if channel==CaptureChannel.PCAP: merge_classic_pcaps([x.local_path for x in rows],out)
                else:
                    with out.open('wb') as fh:
                        for row in rows: fh.write(Path(row.local_path).read_bytes()); fh.write(b'\n')
                digest=_sha256(out); key=f'cases/{session.case_id}/reproductions/{session.id}/raw/session_{channel.value.lower()}{suffix}'
                self.storage.put_file(key,out,ctype); parents=[x.evidence_id for x in rows if x.evidence_id]
                ev=create_evidence(db,case_id=session.case_id,device_id=session.device_id,evidence_type=f'SESSION_{channel.value}',source='REPRODUCTION_COLLECTOR',
                    filename=out.name,object_key=key,size_bytes=out.stat().st_size,sha256=digest,content_type=ctype,
                    kind=EvidenceKind.DERIVED,scope=EvidenceScope.SESSION,level=EvidenceLevel.L1,completeness=EvidenceCompleteness.COMPLETE,
                    session_id=session.id,producer_type='REPRODUCTION_CAPTURE_PIPELINE',producer_id='SESSION_FINALIZER',producer_version=self.version,
                    metadata={'retention_class':RetentionClass.PERMANENT_DERIVED.value,'segment_count':len(rows)},parent_evidence_ids=parents)
                evidence_ids.append(ev.id); manifest['objects'].append({'evidence_id':ev.id,'type':ev.type,'object_key':key,'sha256':digest,'size_bytes':ev.size_bytes,'parents':parents})
            segs=list(db.scalars(select(ReproductionCaptureSegment).where(ReproductionCaptureSegment.session_id==session.id).order_by(ReproductionCaptureSegment.channel,ReproductionCaptureSegment.segment_no)))
            manifest['segments']=[{'id':x.id,'channel':x.channel,'segment_no':x.segment_no,'start_ms':x.start_ms,'end_ms':x.end_ms,'sha256':x.sha256,'retained':x.retained,'evidence_id':x.evidence_id,'retention_class':x.retention_class} for x in segs]
            raw=json.dumps(manifest,ensure_ascii=False,sort_keys=True,separators=(',',':')).encode(); msha=hashlib.sha256(raw).hexdigest(); mkey=f'cases/{session.case_id}/reproductions/{session.id}/metadata/evidence_manifest.json'; self.storage.put_bytes(mkey,raw,'application/json')
            run.status=EvidenceFinalizeStatus.SUCCESS.value; run.evidence_ids_json=evidence_ids; run.manifest_object_key=mkey; run.manifest_sha256=msha; run.finished_at=_utcnow()
            st=db.scalar(select(ReproductionCaptureState).where(ReproductionCaptureState.session_id==session.id))
            if st: st.finalized=True; st.manifest_json=manifest
            db.flush(); return {'status':run.status,'evidence_ids':evidence_ids,'manifest_object_key':mkey,'manifest_sha256':msha,'idempotent':False}
        except Exception as exc:
            run.status=EvidenceFinalizeStatus.FAILED.value; run.error_code=type(exc).__name__; run.error_message=str(exc); run.finished_at=_utcnow(); db.flush(); raise
