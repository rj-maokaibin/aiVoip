from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.api.deps import ENGINEER_ROLES, READ_ROLES, REVIEWER_ROLES, get_db, require_roles
from app.db.models import RuleDefinition, RuleVersion
from app.schemas.rules import RuleActivateRequest, RuleReplayRequest, RuleUpsertRequest, RuleVersionOut
from app.services.idempotency import begin_idempotent, complete_idempotent
from app.services.rules import activate_rule_version, bootstrap_rules, replay_rule, upsert_rule_version

router=APIRouter(tags=['rules'])

@router.post('/rules/bootstrap')
def bootstrap(actor:str='system',db:Session=Depends(get_db),idempotency_key:str|None=Header(default=None,alias='Idempotency-Key'),identity=Depends(require_roles(*REVIEWER_ROLES))):
    actual_actor=identity.actor_id
    handle=begin_idempotent(db,scope='POST:/api/v1/rules/bootstrap',key=idempotency_key,payload={'activate':True})
    if handle.replay is not None: return handle.replay
    try:
        result=bootstrap_rules(db,actor=actual_actor,activate=True)
        complete_idempotent(db,handle,response=result,status_code=200,resource_type='rule_bootstrap'); db.commit(); return result
    except Exception as exc: db.rollback(); raise HTTPException(400,str(exc))

@router.get('/rules')
def list_rules(db:Session=Depends(get_db),_identity=Depends(require_roles(*READ_ROLES))):
    rows=list(db.scalars(select(RuleDefinition).order_by(RuleDefinition.rule_key.asc())))
    return [{'id':r.id,'rule_key':r.rule_key,'name':r.name,'fault_domain':r.fault_domain,'enabled':bool(r.enabled),'active_version':r.active_version,'updated_at':r.updated_at} for r in rows]

@router.post('/rules',response_model=RuleVersionOut)
def upsert(req:RuleUpsertRequest,db:Session=Depends(get_db),idempotency_key:str|None=Header(default=None,alias='Idempotency-Key'),identity=Depends(require_roles(*ENGINEER_ROLES))):
    if req.activate: raise HTTPException(409,'RULE_ACTIVATION_REQUIRES_SEPARATE_APPROVAL')
    payload={'rule':req.rule,'change_note':req.change_note,'activate':False}
    handle=begin_idempotent(db,scope='POST:/api/v1/rules',key=idempotency_key,payload=payload)
    if handle.replay is not None: return handle.replay
    try:
        _,v=upsert_rule_version(db,req.rule,actor=identity.actor_id,change_note=req.change_note,activate=False)
        response=RuleVersionOut.model_validate(v).model_dump(mode='json')
        complete_idempotent(db,handle,response=response,status_code=200,resource_type='rule_version',resource_id=v.id)
        db.commit(); db.refresh(v); return v
    except Exception as exc: db.rollback(); raise HTTPException(400,str(exc))

@router.get('/rules/{rule_key}/versions',response_model=list[RuleVersionOut])
def versions(rule_key:str,db:Session=Depends(get_db),_identity=Depends(require_roles(*READ_ROLES))):
    d=db.scalar(select(RuleDefinition).where(RuleDefinition.rule_key==rule_key))
    if not d: raise HTTPException(404,'RULE_NOT_FOUND')
    return list(db.scalars(select(RuleVersion).where(RuleVersion.rule_definition_id==d.id).order_by(RuleVersion.created_at.desc())))

@router.post('/rules/{rule_key}/versions/{version}/activate',response_model=RuleVersionOut)
def activate(rule_key:str,version:str,req:RuleActivateRequest,db:Session=Depends(get_db),idempotency_key:str|None=Header(default=None,alias='Idempotency-Key'),identity=Depends(require_roles(*REVIEWER_ROLES))):
    d=db.scalar(select(RuleDefinition).where(RuleDefinition.rule_key==rule_key))
    if not d: raise HTTPException(404,'RULE_NOT_FOUND')
    v=db.scalar(select(RuleVersion).where(RuleVersion.rule_definition_id==d.id,RuleVersion.version==version))
    if not v: raise HTTPException(404,'RULE_VERSION_NOT_FOUND')
    handle=begin_idempotent(db,scope=f'POST:/api/v1/rules/{rule_key}/versions/{version}/activate',key=idempotency_key,payload={'rule_key':rule_key,'version':version})
    if handle.replay is not None: return handle.replay
    activate_rule_version(db,d,v,actor=identity.actor_id)
    response=RuleVersionOut.model_validate(v).model_dump(mode='json')
    complete_idempotent(db,handle,response=response,status_code=200,resource_type='rule_version',resource_id=v.id)
    db.commit(); db.refresh(v); return v

@router.post('/rules/versions/{rule_version_id}/replay')
def replay(rule_version_id:str,req:RuleReplayRequest,db:Session=Depends(get_db),idempotency_key:str|None=Header(default=None,alias='Idempotency-Key'),identity=Depends(require_roles(*ENGINEER_ROLES))):
    handle=begin_idempotent(db,scope=f'POST:/api/v1/rules/versions/{rule_version_id}/replay',key=idempotency_key,payload={'case_id':req.case_id})
    if handle.replay is not None: return handle.replay
    try:
        row=replay_rule(db,case_id=req.case_id,rule_version_id=rule_version_id,actor=identity.actor_id)
        response={'id':row.id,'status':row.status,'matched':bool(row.matched),'input_fingerprint':row.input_fingerprint,'result':row.result_json}
        complete_idempotent(db,handle,response=response,status_code=200,resource_type='rule_replay_run',resource_id=row.id)
        db.commit(); db.refresh(row); return response
    except ValueError as exc: db.rollback(); raise HTTPException(404,str(exc))
