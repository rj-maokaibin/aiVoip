from fastapi import APIRouter, Depends, Header, HTTPException, Query
from sqlalchemy.orm import Session
from app.api.deps import ENGINEER_ROLES, READ_ROLES, REVIEWER_ROLES, get_db, require_roles
from app.db.models import Case, KnowledgeItem
from app.schemas.knowledge import KnowledgeCreateRequest, KnowledgeOut, KnowledgeVerifyRequest
from app.knowledge.service import bootstrap_knowledge, find_similar_cases, search_knowledge_items
from app.services.audit import audit
from app.services.idempotency import begin_idempotent, complete_idempotent

router=APIRouter(tags=['knowledge'])

@router.post('/knowledge/bootstrap')
def bootstrap(actor:str='system',db:Session=Depends(get_db),idempotency_key:str|None=Header(default=None,alias='Idempotency-Key'),identity=Depends(require_roles(*REVIEWER_ROLES))):
    handle=begin_idempotent(db,scope='POST:/api/v1/knowledge/bootstrap',key=idempotency_key,payload={})
    if handle.replay is not None: return handle.replay
    result=bootstrap_knowledge(db,actor=identity.actor_id)
    complete_idempotent(db,handle,response=result,status_code=200,resource_type='knowledge_bootstrap'); db.commit(); return result

@router.post('/knowledge',response_model=KnowledgeOut)
def create(req:KnowledgeCreateRequest,db:Session=Depends(get_db),idempotency_key:str|None=Header(default=None,alias='Idempotency-Key'),identity=Depends(require_roles(*ENGINEER_ROLES))):
    payload=req.model_dump(mode='json',exclude={'actor','verified'})
    handle=begin_idempotent(db,scope='POST:/api/v1/knowledge',key=idempotency_key,payload=payload)
    if handle.replay is not None: return handle.replay
    row=KnowledgeItem(type=req.type,title=req.title,summary=req.summary,content_json=req.content_json,tags_json=req.tags,source_ref=req.source_ref,verified=0,created_by=identity.actor_id)
    db.add(row); db.flush(); audit(db,actor=identity.actor_id,event_type='KNOWLEDGE_CREATED',target_type='knowledge_item',target_id=row.id,detail={'type':req.type,'requested_verified':req.verified,'status':'PENDING_REVIEW'})
    response=KnowledgeOut.model_validate(row).model_dump(mode='json')
    complete_idempotent(db,handle,response=response,status_code=200,resource_type='knowledge_item',resource_id=row.id)
    db.commit(); db.refresh(row); return row

@router.get('/knowledge/search')
def search(q:str=Query(min_length=1,max_length=2000),limit:int=10,db:Session=Depends(get_db),_identity=Depends(require_roles(*READ_ROLES))):
    return search_knowledge_items(db,q,limit=max(1,min(limit,50)))

@router.get('/cases/{case_id}/similar-cases')
def similar_cases(case_id:str,limit:int=5,db:Session=Depends(get_db),_identity=Depends(require_roles(*READ_ROLES))):
    if not db.get(Case,case_id): raise HTTPException(404,'CASE_NOT_FOUND')
    return find_similar_cases(db,case_id,limit=max(1,min(limit,20)))


@router.post('/knowledge/{item_id}/verify',response_model=KnowledgeOut)
def verify(item_id:str,req:KnowledgeVerifyRequest,db:Session=Depends(get_db),idempotency_key:str|None=Header(default=None,alias='Idempotency-Key'),identity=Depends(require_roles(*REVIEWER_ROLES))):
    from datetime import datetime, timezone
    row=db.get(KnowledgeItem,item_id)
    if not row: raise HTTPException(404,'KNOWLEDGE_NOT_FOUND')
    handle=begin_idempotent(db,scope=f'POST:/api/v1/knowledge/{item_id}/verify',key=idempotency_key,payload={'item_id':item_id})
    if handle.replay is not None: return handle.replay
    if row.created_by==identity.actor_id: raise HTTPException(409,'KNOWLEDGE_SELF_REVIEW_NOT_ALLOWED')
    row.verified=1; row.verified_by=identity.actor_id; row.verified_at=datetime.now(timezone.utc)
    audit(db,actor=identity.actor_id,event_type='KNOWLEDGE_VERIFIED',target_type='knowledge_item',target_id=row.id,detail={'created_by':row.created_by})
    response=KnowledgeOut.model_validate(row).model_dump(mode='json')
    complete_idempotent(db,handle,response=response,status_code=200,resource_type='knowledge_item',resource_id=row.id)
    db.commit(); db.refresh(row); return row
