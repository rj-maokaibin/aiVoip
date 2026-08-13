from __future__ import annotations
from datetime import datetime, timezone
from pathlib import Path
from sqlalchemy import select
from sqlalchemy.orm import Session
from app.core.config import settings
from app.contracts.enums import HypothesisState, RuleVersionStatus, RunStatus, normalize_hypothesis_state
from app.db.models import RuleDefinition, RuleReplayRun, RuleVersion
from app.rules.compiler import compile_rule, load_rule_yaml
from app.rules.engine import RuleEngine
from app.services.audit import audit


def utcnow(): return datetime.now(timezone.utc)


def upsert_rule_version(db:Session, data:dict, *, actor:str|None=None, change_note:str|None=None, activate:bool=False):
    if int(data.get('dsl_version',0)) != 2:
        raise ValueError('RULE_DSL_VERSION_2_REQUIRED')
    rule=compile_rule(data)
    definition=db.scalar(select(RuleDefinition).where(RuleDefinition.rule_key==rule.key))
    if not definition:
        definition=RuleDefinition(rule_key=rule.key,name=rule.name,fault_domain=rule.fault_domain,enabled=1)
        db.add(definition); db.flush()
    else:
        definition.name=rule.name; definition.fault_domain=rule.fault_domain
    version=db.scalar(select(RuleVersion).where(RuleVersion.rule_definition_id==definition.id,RuleVersion.version==rule.version))
    if version:
        if version.checksum!=rule.checksum: raise ValueError('RULE_VERSION_IMMUTABLE')
    else:
        version=RuleVersion(rule_definition_id=definition.id,version=rule.version,checksum=rule.checksum,status=RuleVersionStatus.DRAFT.value,content_json=rule.source,created_by=actor,change_note=change_note)
        db.add(version); db.flush()
    if activate:
        activate_rule_version(db,definition,version,actor=actor)
    audit(db,event_type='RULE_VERSION_UPSERTED',actor=actor,target_type='rule_version',target_id=version.id,detail={'rule_key':rule.key,'version':rule.version,'checksum':rule.checksum,'activate':activate})
    return definition,version


def activate_rule_version(db:Session,definition:RuleDefinition,version:RuleVersion,*,actor:str|None=None):
    if actor and version.created_by and actor==version.created_by and actor!='system':
        raise ValueError('RULE_SELF_APPROVAL_NOT_ALLOWED')
    versions=list(db.scalars(select(RuleVersion).where(RuleVersion.rule_definition_id==definition.id)))
    for row in versions:
        if row.id!=version.id and row.status==RuleVersionStatus.ACTIVE.value: row.status=RuleVersionStatus.APPROVED.value
    version.status=RuleVersionStatus.ACTIVE.value; version.approved_by=actor; version.approved_at=utcnow(); definition.active_version=version.version; definition.enabled=1
    audit(db,event_type='RULE_VERSION_ACTIVATED',actor=actor,target_type='rule_version',target_id=version.id,detail={'rule_key':definition.rule_key,'version':version.version})


def active_compiled_rules(db:Session):
    stmt=select(RuleDefinition,RuleVersion).join(RuleVersion,RuleVersion.rule_definition_id==RuleDefinition.id).where(RuleDefinition.enabled==1,RuleVersion.status==RuleVersionStatus.ACTIVE.value,RuleVersion.version==RuleDefinition.active_version)
    rows=list(db.execute(stmt).all())
    if rows:
        return [compile_rule(v.content_json) for d,v in rows]
    # Fresh install fallback: use reviewed filesystem rules read-only until bootstrap persists them.
    root=Path(settings.rule_root)
    if not root.exists(): return []
    return [load_rule_yaml(path.read_text(encoding='utf-8')) for path in sorted(root.glob('*.yaml'))]


def bootstrap_rules(db:Session, *, actor:str='system', activate:bool=True):
    root=Path(settings.rule_root)
    count=0; items=[]
    if not root.exists(): return {'count':0,'items':[]}
    for path in sorted(root.glob('*.yaml')):
        compiled=load_rule_yaml(path.read_text(encoding='utf-8'))
        d,v=upsert_rule_version(db,compiled.source,actor=actor,change_note=f'bootstrap:{path.name}',activate=activate)
        count+=1; items.append({'key':d.rule_key,'version':v.version,'checksum':v.checksum})
    db.commit(); return {'count':count,'items':items}


def replay_rule(db:Session,*,case_id:str,rule_version_id:str,actor:str|None=None):
    version=db.get(RuleVersion,rule_version_id)
    if not version: raise ValueError('RULE_VERSION_NOT_FOUND')
    from app.diagnosis.snapshot import CaseEvidenceSnapshotBuilder
    snapshot=CaseEvidenceSnapshotBuilder().build(db,case_id)
    replay=RuleReplayRun(case_id=case_id,rule_version_id=version.id,status=RunStatus.RUNNING.value,input_fingerprint=snapshot['fingerprint'],created_by=actor)
    db.add(replay); db.flush()
    try:
        effects,matches,facts=RuleEngine().evaluate(snapshot,[compile_rule(version.content_json)])
        match=matches[0]
        replay.matched=1 if match.matched else 0; replay.result_json={'match':match.to_dict(),'facts':facts,'effects':_effects_json(effects)}; replay.status=RunStatus.SUCCESS.value; replay.finished_at=utcnow()
        audit(db,case_id=case_id,actor=actor,event_type='RULE_REPLAY_COMPLETED',target_type='rule_replay',target_id=replay.id,detail={'matched':bool(replay.matched),'rule_version_id':version.id})
    except Exception as exc:
        replay.status=RunStatus.FAILED.value; replay.result_json={'error':type(exc).__name__,'message':str(exc)}; replay.finished_at=utcnow(); raise
    finally: db.flush()
    return replay


def _effects_json(effects):
    return {
        'hypotheses':[x.to_dict() for x in effects.get('hypotheses',[])],
        'known':effects.get('known',[]),'unknown':effects.get('unknown',[]),'excluded':effects.get('excluded',[]),
        'plan':[x.to_dict() for x in effects.get('plan',[])],
    }


def merge_rule_effects(decision,effects,matches):
    by_code={h.code:h for h in decision.hypotheses}
    for h in effects.get('hypotheses',[]):
        old=by_code.get(h.code)
        if old:
            # Rule may strengthen deterministic proposal, but never auto-CONFIRMED.
            old.confidence=max(old.confidence,h.confidence)
            old.status=normalize_hypothesis_state(old.status).value
            if old.status in {HypothesisState.OPEN.value,HypothesisState.CONTRADICTED.value} and h.status==HypothesisState.SUPPORTED.value: old.status=HypothesisState.SUPPORTED.value
            old.confirmable=old.confirmable or h.confirmable
            old.confirm_rule=old.confirm_rule or h.confirm_rule
            old.evidence.extend(h.evidence)
            if h.rationale and h.rationale not in (old.rationale or ''): old.rationale=(old.rationale+'；' if old.rationale else '')+h.rationale
        else:
            decision.hypotheses.append(h); by_code[h.code]=h
    decision.known.extend(x for x in effects.get('known',[]) if x and x not in decision.known)
    decision.unknown.extend(x for x in effects.get('unknown',[]) if x and x not in decision.unknown)
    decision.excluded.extend(x for x in effects.get('excluded',[]) if x and x not in decision.excluded)
    decision.plan.extend(effects.get('plan',[]))
    decision.summary={**decision.summary,'rule_engine':{'version':RuleEngine.version,'evaluated':len(matches),'matched':sum(1 for m in matches if m.matched),'matches':[{'key':m.rule_key,'version':m.rule_version,'matched':m.matched} for m in matches if m.matched]}}
    # Recompute top hypotheses after rules.
    rank={HypothesisState.CONFIRMED.value:6,HypothesisState.STRONGLY_SUPPORTED.value:5,HypothesisState.SUPPORTED.value:4,HypothesisState.OPEN.value:3,HypothesisState.CONTRADICTED.value:1,HypothesisState.REJECTED.value:0}
    top=sorted(decision.hypotheses,key=lambda h:(rank.get(h.status,0),h.confidence),reverse=True)[:3]
    decision.summary['top_hypotheses']=[{'code':h.code,'title':h.title,'confidence':round(h.confidence,3),'status':h.status} for h in top]
    return decision
