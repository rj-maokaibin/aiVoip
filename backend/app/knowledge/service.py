from __future__ import annotations

import json
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.models import AnalyzerRun, Case, CaseDevice, CaseRelation, Evidence, Hypothesis, KnowledgeItem
from app.contracts.enums import HypothesisState, KnowledgeStatus
from app.diagnosis.types import EvidenceRef
from .similarity import CaseSignature, CaseSimilarity, tokenize


_SYMPTOM_TOKENS = {
    'REGISTER_FAILURE': ('注册', 'register'),
    'CALL_SETUP_FAILURE': ('呼叫失败', 'invite', 'call setup'),
    'ONE_WAY_AUDIO': ('单通', '单向', 'one way'),
    'AUDIO_STUTTER': ('卡顿', '断续', 'jitter', 'stutter'),
    'AUDIO_NOISE': ('电流音', '噪声', 'noise', 'hum'),
    'DTMF_LOSS': ('dtmf', '丢号', '按键'),
    'ECHO': ('回声', 'echo'),
}


def _symptom_codes(summary: str) -> set[str]:
    lowered = (summary or '').lower()
    return {
        code
        for code, tokens in _SYMPTOM_TOKENS.items()
        if any(token.lower() in lowered for token in tokens)
    }


def _signature(db: Session, case: Case) -> CaseSignature:
    hs = list(db.scalars(select(Hypothesis).where(
        Hypothesis.case_id == case.id,
        Hypothesis.status.in_([
            HypothesisState.SUPPORTED.value,
            HypothesisState.STRONGLY_SUPPORTED.value,
            HypothesisState.CONFIRMED.value,
        ]),
    )))
    evidences = list(db.scalars(select(Evidence).where(Evidence.case_id == case.id)))
    analyzer_runs = list(db.scalars(select(AnalyzerRun).where(AnalyzerRun.case_id == case.id)))
    devices = list(db.scalars(select(CaseDevice).where(CaseDevice.case_id == case.id)))

    version_source = [case.summary]
    product_source = []
    for device in devices:
        info = device.device_info or {}
        for key in ('version', 'software_version', 'firmware_version'):
            if info.get(key):
                version_source.append(str(info[key]))
        for key in ('product', 'model', 'platform', 'platform_id'):
            value = info.get(key) or (device.platform_id if key == 'platform_id' else None)
            if value:
                product_source.append(str(value))

    version_tokens = {
        token
        for token in tokenize(' '.join(version_source))
        if token.startswith(('r3', 'r4', 'v1', 'v2', 'release', 'reyeeos', 'ew_'))
    }
    finding_text = ' '.join(
        json.dumps(row.summary_json or {}, ensure_ascii=False, sort_keys=True)
        for row in analyzer_runs
        if row.summary_json
    )
    return CaseSignature(
        case_id=case.id,
        summary=case.summary,
        hypothesis_codes={h.code for h in hs},
        fault_domains={h.fault_domain for h in hs},
        version_tokens=version_tokens,
        symptom_codes=_symptom_codes(case.summary),
        finding_tokens=tokenize(finding_text),
        evidence_types={str(row.type) for row in evidences},
        product_tokens=tokenize(' '.join(product_source)),
    )


def find_similar_cases(db: Session, case_id: str, *, limit: int = 5, min_score: float = 0.18):
    case = db.get(Case, case_id)
    if not case:
        return []
    target = _signature(db, case)
    algo = CaseSimilarity()
    candidates = list(db.scalars(
        select(Case)
        .where(Case.id != case_id, Case.status.in_(['ROOT_CAUSE_CONFIRMED', 'RESOLVED', 'CLOSED']))
        .order_by(Case.updated_at.desc())
        .limit(300)
    ))
    case_by_id = {row.id: row for row in candidates}
    signatures = [_signature(db, row) for row in candidates]
    ranked = algo.rank(target, signatures, coarse_limit=30)

    found = []
    for signature, score, details in ranked:
        if score < min_score:
            continue
        other = case_by_id[signature.case_id]
        hs = list(db.scalars(select(Hypothesis).where(
            Hypothesis.case_id == other.id,
            Hypothesis.status.in_([
                HypothesisState.SUPPORTED.value,
                HypothesisState.STRONGLY_SUPPORTED.value,
                HypothesisState.CONFIRMED.value,
            ]),
        ).order_by(Hypothesis.confidence.desc())))
        found.append({
            'case_id': other.id,
            'case_no': other.case_no,
            'summary': other.summary,
            'status': other.status,
            'score': round(score, 4),
            'details': details,
            'why_similar': {
                'same_points': details.get('same_points') or [],
                'different_points': details.get('different_points') or [],
                'transferability': details.get('transferability'),
            },
            'hypotheses': [
                {'code': h.code, 'title': h.title, 'status': h.status, 'confidence': h.confidence / 10000.0}
                for h in hs[:5]
            ],
        })
    return found[:limit]


def persist_case_relations(db: Session, case_id: str, similar: list[dict]):
    for item in similar:
        row = db.scalar(select(CaseRelation).where(
            CaseRelation.case_id == case_id,
            CaseRelation.related_case_id == item['case_id'],
            CaseRelation.relation_type == 'SIMILAR',
        ))
        if not row:
            row = CaseRelation(case_id=case_id, related_case_id=item['case_id'], relation_type='SIMILAR')
            db.add(row)
        row.score = int(round(item['score'] * 10000))
        row.details_json = item['details']
    db.flush()


def enrich_decision_with_history(decision, similar: list[dict]):
    if not similar:
        return decision
    decision.summary = {**decision.summary, 'similar_cases': similar[:5]}
    current = {h.code: h for h in decision.hypotheses}
    for item in similar:
        score = float(item['score'])
        for hist in item.get('hypotheses', []):
            h = current.get(hist['code'])
            if not h:
                continue
            # Historical cases are L4 only: modest confidence boost, never change to CONFIRMED.
            boost = min(0.08, score * 0.08)
            h.confidence = min(0.98, h.confidence + boost)
            h.evidence.append(EvidenceRef(
                'HISTORICAL_CASE', item['case_id'], 'L4', 'SUPPORT', min(0.35, score),
                f'历史Case {item["case_no"]} 存在相同假设，仅作为经验佐证。',
                {
                    'similarity': score,
                    'historical_status': hist['status'],
                    'transferability': (item.get('why_similar') or {}).get('transferability'),
                    'same_points': (item.get('why_similar') or {}).get('same_points') or [],
                    'different_points': (item.get('why_similar') or {}).get('different_points') or [],
                },
            ))
    decision.known.append(
        f'发现 {len(similar)} 个达到阈值的历史相似Case；仅作为L4经验佐证，不替代当前Case直接证据。'
    )
    return decision


def search_knowledge_items(db: Session, query: str, *, limit: int = 10):
    qtokens = tokenize(query)
    rows = list(db.scalars(select(KnowledgeItem).where(
        KnowledgeItem.status == KnowledgeStatus.ACTIVE.value,
        KnowledgeItem.verified == 1,
    ).order_by(KnowledgeItem.updated_at.desc()).limit(500)))
    scored = []
    for row in rows:
        tags = ' '.join(str(tag) for tag in (row.tags_json or []))
        tokens = tokenize(row.title + ' ' + row.summary + ' ' + tags)
        overlap = len(qtokens & tokens) / max(1, len(qtokens | tokens))
        if overlap > 0:
            scored.append((overlap, row))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [
        {
            'id': r.id,
            'type': r.type,
            'title': r.title,
            'summary': r.summary,
            'verified': bool(r.verified),
            'score': round(s, 4),
            'source_ref': r.source_ref,
            'tags': r.tags_json or [],
        }
        for s, r in scored[:limit]
    ]


def bootstrap_knowledge(db: Session, *, root=None, actor: str = 'system'):
    from pathlib import Path
    import yaml
    from app.core.config import settings
    root = Path(root or settings.knowledge_root)
    count = 0
    ids = []
    if not root.exists():
        return {'count': 0, 'ids': []}
    for path in sorted(root.glob('*.yaml')):
        data = yaml.safe_load(path.read_text(encoding='utf-8')) or {}
        for raw in data.get('items', []) or []:
            source_ref = f'bootstrap:{path.name}:{raw.get("title")}'
            existing = db.scalar(select(KnowledgeItem).where(KnowledgeItem.source_ref == source_ref))
            if existing:
                continue
            row = KnowledgeItem(
                type=str(raw.get('type', 'GUIDE')),
                title=str(raw['title']),
                summary=str(raw['summary']),
                content_json=raw.get('content_json'),
                tags_json=list(raw.get('tags') or []),
                source_ref=source_ref,
                verified=1 if raw.get('verified') else 0,
                verified_by=actor if raw.get('verified') else None,
                verified_at=__import__('datetime').datetime.now(__import__('datetime').timezone.utc) if raw.get('verified') else None,
                created_by=actor,
            )
            db.add(row)
            db.flush()
            count += 1
            ids.append(row.id)
    db.commit()
    return {'count': count, 'ids': ids}
