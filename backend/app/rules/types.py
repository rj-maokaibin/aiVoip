from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.contracts.enums import RuleCategory

# Frozen EC-07 leaf operator set. Boolean composition is represented by AND/OR/NOT nodes.
ALLOWED_OPS={'eq','ne','gt','gte','lt','lte','in','exists'}
ALLOWED_ACTIONS={'hypothesis','known','unknown','excluded','plan'}


@dataclass(frozen=True)
class RuleExpression:
    kind: str  # LEAF / AND / OR / NOT
    path: str | None = None
    op: str | None = None
    value: Any = None
    children: tuple['RuleExpression', ...] = ()


@dataclass
class RuleOutput:
    action: str
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class CompiledRule:
    key: str
    version: str
    name: str
    fault_domain: str
    category: RuleCategory
    priority: int
    enabled: bool
    condition: RuleExpression
    outputs: list[RuleOutput]
    source: dict[str, Any]
    checksum: str
    dsl_version: int = 2


@dataclass
class RuleMatch:
    rule_key: str
    rule_version: str
    checksum: str
    matched: bool
    facts: dict[str, Any]
    outputs: list[dict[str, Any]]
    reason: str = ''
    category: str = RuleCategory.SUPPORT.value

    def to_dict(self):
        return {
            'rule_key':self.rule_key,'rule_version':self.rule_version,'checksum':self.checksum,
            'matched':self.matched,'facts':self.facts,'outputs':self.outputs,'reason':self.reason,
            'category':self.category,
        }
