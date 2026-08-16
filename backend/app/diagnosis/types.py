from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any

from app.contracts.enums import EvidenceLevel, HypothesisState, normalize_hypothesis_state

HYPOTHESIS_STATES={x.value for x in HypothesisState}
EVIDENCE_LEVELS={x.value for x in EvidenceLevel}
PLAN_ACTION_TYPES={'RUN_MEDIA_ANALYSIS','RUN_PACKET_ANALYSIS','RUN_PCM_ANALYSIS','RUN_FIELD_AUDIO_ANALYSIS','RUN_IMAGE_METADATA_ANALYSIS','RUN_FIELD_MEDIA_ALIGNMENT','COLLECT_PROFILE','REQUEST_USER_EVIDENCE','REQUEST_MULTI_POINT_PCAP'}


@dataclass
class EvidenceRef:
    ref_type: str
    ref_id: str
    level: str
    direction: str = 'SUPPORT'  # SUPPORT / CONTRADICT / CONTEXT
    weight: float = 1.0
    rationale: str = ''
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        self.level=EvidenceLevel(self.level).value
        self.direction=str(self.direction).upper()
        if self.direction not in {'SUPPORT','CONTRADICT','CONTEXT'}:
            raise ValueError(f'INVALID_EVIDENCE_DIRECTION:{self.direction}')
        self.weight=max(0.0,min(1.0,float(self.weight)))

    def to_dict(self): return asdict(self)


@dataclass
class HypothesisProposal:
    code: str
    title: str
    fault_domain: str
    confidence: float
    status: str = HypothesisState.OPEN.value
    rationale: str = ''
    confirmable: bool = False
    confirm_rule: str | None = None
    evidence: list[EvidenceRef] = field(default_factory=list)

    def __post_init__(self):
        self.status=normalize_hypothesis_state(self.status).value
        self.confidence=max(0.0,min(1.0,float(self.confidence)))

    def to_dict(self):
        data=asdict(self)
        data['confidence']=round(self.confidence,4)
        return data


@dataclass
class PlanAction:
    action_type: str
    reason: str
    risk_level: str
    auto_execute: bool
    params: dict[str, Any] = field(default_factory=dict)
    priority: int = 100

    def to_dict(self): return asdict(self)


@dataclass
class DiagnosisDecision:
    hypotheses: list[HypothesisProposal]
    plan: list[PlanAction]
    conclusion_state: str
    summary: dict[str, Any]
    known: list[str] = field(default_factory=list)
    unknown: list[str] = field(default_factory=list)
    excluded: list[str] = field(default_factory=list)

    def to_dict(self):
        return {
            'hypotheses':[x.to_dict() for x in self.hypotheses],
            'plan':[x.to_dict() for x in sorted(self.plan,key=lambda x:x.priority)],
            'conclusion_state':self.conclusion_state,
            'summary':self.summary,
            'known':self.known,
            'unknown':self.unknown,
            'excluded':self.excluded,
        }
