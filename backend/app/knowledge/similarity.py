from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Iterable

ASCII_RE = re.compile(r'[A-Za-z0-9_\-\.]+')
CJK_RE = re.compile(r'[\u4e00-\u9fff]+')


def tokenize(text: str) -> set[str]:
    text = (text or '').lower()
    out = set(x.lower() for x in ASCII_RE.findall(text) if len(x) >= 2)
    for block in CJK_RE.findall(text):
        if len(block) == 1:
            out.add(block)
        else:
            out.update(block[i:i + 2] for i in range(len(block) - 1))
            if len(block) <= 6:
                out.add(block)
    return out


def jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a or b else 0.0


def cosine(a: list[float] | None, b: list[float] | None) -> float | None:
    if not a or not b or len(a) != len(b):
        return None
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if not na or not nb:
        return None
    # Embedding providers can produce small negative values. Similar-case scoring is
    # normalized into [0, 1] so lexical/structured features remain interpretable.
    return max(0.0, min(1.0, (dot / (na * nb) + 1.0) / 2.0))


@dataclass
class CaseSignature:
    case_id: str
    summary: str
    hypothesis_codes: set[str]
    fault_domains: set[str]
    version_tokens: set[str]
    symptom_codes: set[str] = field(default_factory=set)
    finding_tokens: set[str] = field(default_factory=set)
    evidence_types: set[str] = field(default_factory=set)
    product_tokens: set[str] = field(default_factory=set)
    embedding: list[float] | None = None


class CaseSimilarity:
    """Explainable hybrid retrieval/reranking for historical VOIP Cases.

    Stage 1 is a cheap lexical/structured coarse score. Stage 2 reranks up to 30
    candidates using confirmed hypothesis/fault-domain/symptom/finding/evidence
    features and an optional embedding similarity. Embeddings are optional so the
    deterministic offline path stays reproducible without an external vector service.
    """

    version = '2.0.0'

    def coarse_score(self, a: CaseSignature, b: CaseSignature) -> float:
        text = jaccard(tokenize(a.summary), tokenize(b.summary))
        domains = jaccard(a.fault_domains, b.fault_domains)
        symptoms = jaccard(a.symptom_codes, b.symptom_codes)
        versions = jaccard(a.version_tokens, b.version_tokens)
        return 0.60 * text + 0.20 * domains + 0.15 * symptoms + 0.05 * versions

    def score(self, a: CaseSignature, b: CaseSignature) -> tuple[float, dict]:
        text = jaccard(tokenize(a.summary), tokenize(b.summary))
        hypotheses = jaccard(a.hypothesis_codes, b.hypothesis_codes)
        domains = jaccard(a.fault_domains, b.fault_domains)
        versions = jaccard(a.version_tokens, b.version_tokens)
        symptoms = jaccard(a.symptom_codes, b.symptom_codes)
        findings = jaccard(a.finding_tokens, b.finding_tokens)
        evidence = jaccard(a.evidence_types, b.evidence_types)
        products = jaccard(a.product_tokens, b.product_tokens)
        embedding = cosine(a.embedding, b.embedding)

        components = {
            'text': text,
            'hypothesis': hypotheses,
            'domain': domains,
            'symptom': symptoms,
            'finding': findings,
            'version': versions,
            'evidence': evidence,
            'product': products,
        }
        weights = {
            'text': 0.25,
            'hypothesis': 0.28,
            'domain': 0.17,
            'symptom': 0.10,
            'finding': 0.08,
            'version': 0.04,
            'evidence': 0.04,
            'product': 0.04,
        }
        score = sum(weights[name] * value for name, value in components.items())
        if embedding is not None:
            # Blend rather than replace deterministic features. This keeps the
            # retrieval explainable and lets vector service failures degrade cleanly.
            score = 0.80 * score + 0.20 * embedding

        same = []
        different = []
        feature_pairs = {
            'hypothesis_codes': (a.hypothesis_codes, b.hypothesis_codes),
            'fault_domains': (a.fault_domains, b.fault_domains),
            'symptom_codes': (a.symptom_codes, b.symptom_codes),
            'finding_tokens': (a.finding_tokens, b.finding_tokens),
            'version_tokens': (a.version_tokens, b.version_tokens),
            'evidence_types': (a.evidence_types, b.evidence_types),
            'product_tokens': (a.product_tokens, b.product_tokens),
        }
        for name, (left, right) in feature_pairs.items():
            shared = sorted(left & right)
            if shared:
                same.append({'feature': name, 'values': shared})
            if left or right:
                only_left = sorted(left - right)
                only_right = sorted(right - left)
                if only_left or only_right:
                    different.append({
                        'feature': name,
                        'target_only': only_left,
                        'candidate_only': only_right,
                    })

        details = {
            'text_jaccard': round(text, 4),
            'hypothesis_jaccard': round(hypotheses, 4),
            'domain_jaccard': round(domains, 4),
            'symptom_jaccard': round(symptoms, 4),
            'finding_jaccard': round(findings, 4),
            'version_jaccard': round(versions, 4),
            'evidence_jaccard': round(evidence, 4),
            'product_jaccard': round(products, 4),
            'embedding_cosine_normalized': round(embedding, 4) if embedding is not None else None,
            'same_points': same,
            'different_points': different,
            'transferability': 'HIGH' if hypotheses >= 0.5 and domains >= 0.5 else (
                'MEDIUM' if score >= 0.35 else 'LOW'
            ),
            'algorithm_version': self.version,
        }
        return max(0.0, min(1.0, score)), details

    def rank(
        self,
        target: CaseSignature,
        candidates: Iterable[CaseSignature],
        *,
        coarse_limit: int = 30,
    ) -> list[tuple[CaseSignature, float, dict]]:
        coarse = sorted(
            ((candidate, self.coarse_score(target, candidate)) for candidate in candidates),
            key=lambda row: (-row[1], row[0].case_id),
        )[:coarse_limit]
        reranked = []
        for candidate, coarse_score in coarse:
            score, details = self.score(target, candidate)
            details = {**details, 'coarse_score': round(coarse_score, 4)}
            reranked.append((candidate, score, details))
        reranked.sort(key=lambda row: (-row[1], row[0].case_id))
        return reranked
