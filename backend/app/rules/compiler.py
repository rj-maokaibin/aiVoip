from __future__ import annotations

import hashlib
import json
from typing import Any

import yaml

from app.contracts.enums import RuleCategory
from .types import ALLOWED_ACTIONS, ALLOWED_OPS, CompiledRule, RuleExpression, RuleOutput


class RuleCompileError(ValueError): pass

ALLOWED_PATH_PREFIXES=('symptoms.','anomaly_counts.','cross_event_counts.','pcm.','sip.','rtp.','media.','capture.','case.')


def _canonical(data:dict)->str:
    return json.dumps(data,sort_keys=True,separators=(',',':'),ensure_ascii=False)


def _leaf(raw:dict[str,Any]) -> RuleExpression:
    path=str(raw.get('path',''))
    op=str(raw.get('op',''))
    if not any(path.startswith(p) for p in ALLOWED_PATH_PREFIXES):
        raise RuleCompileError(f'RULE_PATH_NOT_ALLOWED:{path}')
    if op not in ALLOWED_OPS:
        raise RuleCompileError(f'RULE_OP_NOT_ALLOWED:{op}')
    if op == 'exists':
        return RuleExpression('LEAF',path=path,op=op,value=bool(raw.get('value',True)))
    if op == 'in' and not isinstance(raw.get('value'),(list,tuple,set)):
        raise RuleCompileError('RULE_IN_VALUE_MUST_BE_COLLECTION')
    return RuleExpression('LEAF',path=path,op=op,value=raw.get('value'))


def _expr(raw:Any) -> RuleExpression:
    if not isinstance(raw,dict):
        raise RuleCompileError('RULE_EXPRESSION_MUST_BE_OBJECT')
    if 'and' in raw:
        items=raw.get('and')
        if not isinstance(items,list) or not items: raise RuleCompileError('RULE_AND_REQUIRES_ITEMS')
        return RuleExpression('AND',children=tuple(_expr(x) for x in items))
    if 'or' in raw:
        items=raw.get('or')
        if not isinstance(items,list) or not items: raise RuleCompileError('RULE_OR_REQUIRES_ITEMS')
        return RuleExpression('OR',children=tuple(_expr(x) for x in items))
    if 'not' in raw:
        return RuleExpression('NOT',children=(_expr(raw.get('not')),))
    return _leaf(raw)


def _translate_legacy_when(raw_when:dict[str,Any]) -> dict[str,Any]:
    """Read-only compatibility for already persisted v1 rules.

    New rule sources are emitted as DSL v2. This translator prevents an upgrade from
    making historical active versions unreadable; it does not expand the frozen v2
    operator set.
    """
    items=raw_when.get('all')
    if not isinstance(items,list) or not items:
        raise RuleCompileError('RULE_CONDITION_REQUIRED')
    translated=[]
    for item in items:
        item=dict(item)
        op=item.get('op')
        if op=='truthy':
            item['op']='eq'; item['value']=True
        elif op=='falsy':
            item['op']='eq'; item['value']=False
        elif op=='contains':
            raise RuleCompileError('RULE_LEGACY_CONTAINS_NOT_SUPPORTED')
        translated.append(item)
    return {'and':translated}


def compile_rule(data:dict[str,Any]) -> CompiledRule:
    if not isinstance(data,dict): raise RuleCompileError('RULE_MUST_BE_OBJECT')
    key=str(data.get('key','')).strip(); version=str(data.get('version','')).strip()
    if not key or not version: raise RuleCompileError('RULE_KEY_VERSION_REQUIRED')
    dsl_version=int(data.get('dsl_version',1))
    raw_when=data.get('when') or {}
    if dsl_version == 1:
        raw_condition=_translate_legacy_when(raw_when)
    elif dsl_version == 2:
        raw_condition=raw_when
    else:
        raise RuleCompileError(f'RULE_DSL_VERSION_UNSUPPORTED:{dsl_version}')
    condition=_expr(raw_condition)
    outputs=[]
    for raw in data.get('then',[]) or []:
        action=str(raw.get('action',''))
        if action not in ALLOWED_ACTIONS: raise RuleCompileError(f'RULE_ACTION_NOT_ALLOWED:{action}')
        payload=raw.get('payload') or {}
        if not isinstance(payload,dict): raise RuleCompileError('RULE_OUTPUT_PAYLOAD_MUST_BE_OBJECT')
        outputs.append(RuleOutput(action,payload))
    if not outputs: raise RuleCompileError('RULE_OUTPUT_REQUIRED')
    try: category=RuleCategory(str(data.get('category','SUPPORT')).upper())
    except ValueError as exc: raise RuleCompileError(f'RULE_CATEGORY_INVALID:{data.get("category")}') from exc
    checksum=hashlib.sha256(_canonical(data).encode()).hexdigest()
    return CompiledRule(
        key=key,version=version,name=str(data.get('name',key)),fault_domain=str(data.get('fault_domain','Other')),
        category=category,priority=int(data.get('priority',100)),enabled=bool(data.get('enabled',True)),condition=condition,
        outputs=outputs,source=data,checksum=checksum,dsl_version=dsl_version,
    )


def load_rule_yaml(text:str) -> CompiledRule:
    data=yaml.safe_load(text)
    return compile_rule(data)
