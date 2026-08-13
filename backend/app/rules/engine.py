from __future__ import annotations

from typing import Any

from app.contracts.enums import RULE_CATEGORY_ORDER, HypothesisState
from app.diagnosis.policy import enforce_plan_action
from app.diagnosis.types import EvidenceRef, HypothesisProposal, PlanAction
from .facts import build_rule_facts
from .types import CompiledRule, RuleExpression, RuleMatch


def _get(data:dict,path:str):
    cur=data
    for part in path.split('.'):
        if not isinstance(cur,dict): return None
        cur=cur.get(part)
    return cur


def _compare(actual,op,expected):
    if op=='exists': return (actual is not None) == bool(expected)
    if op=='eq': return actual==expected
    if op=='ne': return actual!=expected
    if op=='in': return actual in expected if expected is not None else False
    try:
        if op=='gt': return actual>expected
        if op=='gte': return actual>=expected
        if op=='lt': return actual<expected
        if op=='lte': return actual<=expected
    except TypeError: return False
    return False


def _evaluate_expr(expr:RuleExpression,facts:dict[str,Any],observed:dict[str,Any]) -> bool:
    if expr.kind=='LEAF':
        actual=_get(facts,expr.path or '')
        observed[expr.path or '']=actual
        return _compare(actual,expr.op,expr.value)
    if expr.kind=='AND': return all(_evaluate_expr(x,facts,observed) for x in expr.children)
    if expr.kind=='OR': return any(_evaluate_expr(x,facts,observed) for x in expr.children)
    if expr.kind=='NOT': return not _evaluate_expr(expr.children[0],facts,observed)
    return False


class RuleEngine:
    version='2.0.0'

    def evaluate(self,snapshot:dict,rules:list[CompiledRule]):
        facts=build_rule_facts(snapshot)
        matches=[]; effects={'hypotheses':[],'known':[],'unknown':[],'excluded':[],'plan':[]}
        source_run=None
        for item in (snapshot.get('analyzers') or {}).values():
            if item.get('result'): source_run=item.get('run_id')
        ordered=sorted([r for r in rules if r.enabled], key=lambda x:(RULE_CATEGORY_ORDER[x.category],x.priority,x.key))
        for rule in ordered:
            observed={}
            ok=_evaluate_expr(rule.condition,facts,observed)
            output_dicts=[]
            if ok:
                for out in rule.outputs:
                    p=dict(out.payload); output_dicts.append({'action':out.action,'payload':p})
                    if out.action=='hypothesis':
                        effects['hypotheses'].append(HypothesisProposal(
                            code=str(p['code']),title=str(p['title']),fault_domain=str(p.get('fault_domain',rule.fault_domain)),
                            confidence=float(p.get('confidence',0.8)),status=str(p.get('status',HypothesisState.SUPPORTED.value)),
                            rationale=str(p.get('rationale',f'命中规则 {rule.key}@{rule.version}')),
                            confirmable=bool(p.get('confirmable',False)),confirm_rule=rule.key if p.get('confirmable') else None,
                            evidence=[EvidenceRef(
                                'ANALYZER_RUN' if source_run else 'RULE_MATCH',source_run or f'{rule.key}@{rule.version}',
                                str(p.get('evidence_level','L2')) if source_run else ('L2' if str(p.get('evidence_level','L2'))=='L1' else str(p.get('evidence_level','L2'))),
                                'SUPPORT',float(p.get('weight',0.8)),str(p.get('rationale','规则命中')),
                                {'rule_key':rule.key,'rule_version':rule.version,'rule_checksum':rule.checksum,'category':rule.category.value,'facts':observed}
                            )],
                        ))
                    elif out.action in {'known','unknown','excluded'}:
                        effects[out.action].append(str(p.get('text','')))
                    elif out.action=='plan':
                        pa=PlanAction(str(p['action_type']),str(p.get('reason','规则请求补采')),str(p.get('risk_level','USER')),bool(p.get('auto_execute',False)),dict(p.get('params') or {}),int(p.get('priority',100)))
                        effects['plan'].append(enforce_plan_action(pa))
            matches.append(RuleMatch(rule.key,rule.version,rule.checksum,ok,observed,output_dicts,'MATCHED' if ok else 'CONDITIONS_NOT_MET',rule.category.value))
        return effects,matches,facts
