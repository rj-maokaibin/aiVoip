from __future__ import annotations
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.db.models import Case, CaseRelation, Hypothesis, KnowledgeItem
from app.contracts.enums import HypothesisState, KnowledgeStatus
from app.diagnosis.types import EvidenceRef
from .similarity import CaseSignature, CaseSimilarity, tokenize


def _signature(db:Session,case:Case)->CaseSignature:
    hs=list(db.scalars(select(Hypothesis).where(Hypothesis.case_id==case.id,Hypothesis.status.in_([HypothesisState.SUPPORTED.value,HypothesisState.STRONGLY_SUPPORTED.value,HypothesisState.CONFIRMED.value]))))
    version_tokens={x for x in tokenize(case.summary) if x.startswith(('r3','r4','v1','v2','release','reyeeos'))}
    return CaseSignature(case.id,case.summary,{h.code for h in hs},{h.fault_domain for h in hs},version_tokens)


def find_similar_cases(db:Session,case_id:str,*,limit:int=5,min_score:float=0.18):
    case=db.get(Case,case_id)
    if not case: return []
    target=_signature(db,case); algo=CaseSimilarity(); found=[]
    candidates=list(db.scalars(select(Case).where(Case.id!=case_id,Case.status.in_(['ROOT_CAUSE_CONFIRMED','RESOLVED','CLOSED'])).order_by(Case.updated_at.desc()).limit(300)))
    for other in candidates:
        sig=_signature(db,other); score,details=algo.score(target,sig)
        if score<min_score: continue
        hs=list(db.scalars(select(Hypothesis).where(Hypothesis.case_id==other.id,Hypothesis.status.in_([HypothesisState.SUPPORTED.value,HypothesisState.STRONGLY_SUPPORTED.value,HypothesisState.CONFIRMED.value])).order_by(Hypothesis.confidence.desc())))
        found.append({'case_id':other.id,'case_no':other.case_no,'summary':other.summary,'status':other.status,'score':round(score,4),'details':details,'hypotheses':[{'code':h.code,'title':h.title,'status':h.status,'confidence':h.confidence/10000.0} for h in hs[:5]]})
    found.sort(key=lambda x:x['score'],reverse=True)
    return found[:limit]


def persist_case_relations(db:Session,case_id:str,similar:list[dict]):
    for item in similar:
        row=db.scalar(select(CaseRelation).where(CaseRelation.case_id==case_id,CaseRelation.related_case_id==item['case_id'],CaseRelation.relation_type=='SIMILAR'))
        if not row:
            row=CaseRelation(case_id=case_id,related_case_id=item['case_id'],relation_type='SIMILAR')
            db.add(row)
        row.score=int(round(item['score']*10000)); row.details_json=item['details']
    db.flush()


def enrich_decision_with_history(decision,similar:list[dict]):
    if not similar: return decision
    decision.summary={**decision.summary,'similar_cases':similar[:5]}
    current={h.code:h for h in decision.hypotheses}
    for item in similar:
        score=float(item['score'])
        for hist in item.get('hypotheses',[]):
            h=current.get(hist['code'])
            if not h: continue
            # Historical cases are L4 only: modest confidence boost, never change to CONFIRMED.
            boost=min(0.08,score*0.08)
            h.confidence=min(0.98,h.confidence+boost)
            h.evidence.append(EvidenceRef('HISTORICAL_CASE',item['case_id'],'L4','SUPPORT',min(0.35,score),f'历史Case {item["case_no"]} 存在相同假设，仅作为经验佐证。',{'similarity':score,'historical_status':hist['status']}))
    decision.known.append(f'发现 {len(similar)} 个达到阈值的历史相似Case；仅作为L4经验佐证，不替代当前Case直接证据。')
    return decision


def search_knowledge_items(db:Session,query:str,*,limit:int=10):
    qtokens=tokenize(query); rows=list(db.scalars(select(KnowledgeItem).where(KnowledgeItem.status==KnowledgeStatus.ACTIVE.value,KnowledgeItem.verified==1).order_by(KnowledgeItem.updated_at.desc()).limit(500)))
    scored=[]
    for row in rows:
        tokens=tokenize(row.title+' '+row.summary+' '+' '.join(row.tags_json or [])); overlap=len(qtokens&tokens)/max(1,len(qtokens|tokens))
        if overlap>0: scored.append((overlap,row))
    scored.sort(key=lambda x:x[0],reverse=True)
    return [{'id':r.id,'type':r.type,'title':r.title,'summary':r.summary,'verified':bool(r.verified),'score':round(s,4),'source_ref':r.source_ref,'tags':r.tags_json or []} for s,r in scored[:limit]]


def bootstrap_knowledge(db:Session,*,root=None,actor:str='system'):
    from pathlib import Path
    import yaml
    from app.core.config import settings
    root=Path(root or settings.knowledge_root)
    count=0; ids=[]
    if not root.exists(): return {'count':0,'ids':[]}
    for path in sorted(root.glob('*.yaml')):
        data=yaml.safe_load(path.read_text(encoding='utf-8')) or {}
        for raw in data.get('items',[]) or []:
            source_ref=f'bootstrap:{path.name}:{raw.get("title")}'
            existing=db.scalar(select(KnowledgeItem).where(KnowledgeItem.source_ref==source_ref))
            if existing: continue
            row=KnowledgeItem(type=str(raw.get('type','GUIDE')),title=str(raw['title']),summary=str(raw['summary']),content_json=raw.get('content_json'),tags_json=list(raw.get('tags') or []),source_ref=source_ref,verified=1 if raw.get('verified') else 0,verified_by=actor if raw.get('verified') else None,verified_at=__import__('datetime').datetime.now(__import__('datetime').timezone.utc) if raw.get('verified') else None,created_by=actor)
            db.add(row); db.flush(); count+=1; ids.append(row.id)
    db.commit(); return {'count':count,'ids':ids}
