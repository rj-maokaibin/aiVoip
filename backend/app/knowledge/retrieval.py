from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import math
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.contracts.enums import KnowledgeStatus
from app.db.models import KnowledgeItem
from app.knowledge.similarity import tokenize


@dataclass(frozen=True)
class RetrievalCandidate:
    id: str
    title: str
    summary: str
    source_ref: str | None
    tags: list[str]
    bm25_score: float
    vector_score: float
    metadata_score: float
    final_score: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "KNOWLEDGE_ITEM",
            "title": self.title,
            "summary": self.summary,
            "source_ref": self.source_ref,
            "tags": self.tags,
            "score": round(self.final_score, 4),
            "retrieval": {
                "bm25": round(self.bm25_score, 4),
                "vector": round(self.vector_score, 4),
                "metadata": round(self.metadata_score, 4),
            },
        }


def _doc_tokens(row: KnowledgeItem) -> list[str]:
    tags = " ".join(str(tag) for tag in (row.tags_json or []))
    title_tokens = list(tokenize(row.title))
    # Title and tags are intentionally repeated to make controlled metadata more
    # influential than prose length without introducing an LLM ranking authority.
    return title_tokens * 2 + list(tokenize(row.summary)) + list(tokenize(tags)) * 2


def _bm25(query: set[str], docs: list[list[str]]) -> list[float]:
    if not docs or not query:
        return [0.0] * len(docs)
    n = len(docs)
    avgdl = sum(len(doc) for doc in docs) / max(1, n)
    df = Counter()
    for doc in docs:
        for token in set(doc):
            df[token] += 1
    k1 = 1.5
    b = 0.75
    scores: list[float] = []
    for doc in docs:
        tf = Counter(doc)
        dl = len(doc)
        score = 0.0
        for token in query:
            if token not in tf:
                continue
            idf = math.log(1.0 + (n - df[token] + 0.5) / (df[token] + 0.5))
            freq = tf[token]
            denom = freq + k1 * (1.0 - b + b * dl / max(avgdl, 1e-9))
            score += idf * (freq * (k1 + 1.0)) / max(denom, 1e-9)
        scores.append(score)
    max_score = max(scores) if scores else 0.0
    return [score / max_score if max_score > 0 else 0.0 for score in scores]


def _tfidf_cosine(query: set[str], docs: list[list[str]]) -> list[float]:
    """Offline vector channel used when no external embedding service is enabled.

    This is deliberately deterministic and auditable. A future embedding provider
    can replace this channel without changing fusion/authority contracts.
    """
    if not docs or not query:
        return [0.0] * len(docs)
    n = len(docs)
    df = Counter()
    for doc in docs:
        for token in set(doc):
            df[token] += 1
    vocab = set(query)
    for doc in docs:
        vocab.update(doc)
    idf = {token: math.log((n + 1) / (df[token] + 1)) + 1.0 for token in vocab}
    qvec = {token: idf[token] for token in query}
    qnorm = math.sqrt(sum(value * value for value in qvec.values())) or 1.0
    scores: list[float] = []
    for doc in docs:
        tf = Counter(doc)
        dvec = {token: tf[token] * idf[token] for token in tf}
        dot = sum(qvec.get(token, 0.0) * value for token, value in dvec.items())
        dnorm = math.sqrt(sum(value * value for value in dvec.values())) or 1.0
        scores.append(dot / (qnorm * dnorm))
    return scores


def _metadata_score(query: set[str], row: KnowledgeItem) -> float:
    if not query:
        return 0.0
    title = tokenize(row.title)
    tags = tokenize(" ".join(str(tag) for tag in (row.tags_json or [])))
    title_overlap = len(query & title) / max(1, len(query))
    tag_overlap = len(query & tags) / max(1, len(query))
    exact_phrase = 1.0 if " ".join(query).lower() in row.title.lower() else 0.0
    return min(1.0, title_overlap * 0.55 + tag_overlap * 0.35 + exact_phrase * 0.10)


def hybrid_search_verified_knowledge(
    db: Session,
    query: str,
    *,
    limit: int = 10,
    min_score: float = 0.06,
) -> list[dict[str, Any]]:
    """BM25 + deterministic TF-IDF vector + metadata fusion over verified KB.

    Only ACTIVE + reviewer-verified KnowledgeItems enter the corpus. Retrieval
    ranking therefore cannot promote unreviewed material into an answer authority.
    """
    qtokens = tokenize(query)
    if not qtokens:
        return []
    rows = list(db.scalars(
        select(KnowledgeItem).where(
            KnowledgeItem.status == KnowledgeStatus.ACTIVE.value,
            KnowledgeItem.verified == 1,
        ).order_by(KnowledgeItem.updated_at.desc()).limit(1000)
    ))
    if not rows:
        return []
    docs = [_doc_tokens(row) for row in rows]
    bm25_scores = _bm25(qtokens, docs)
    vector_scores = _tfidf_cosine(qtokens, docs)
    candidates: list[RetrievalCandidate] = []
    for row, bm25_score, vector_score in zip(rows, bm25_scores, vector_scores):
        metadata_score = _metadata_score(qtokens, row)
        final = bm25_score * 0.45 + vector_score * 0.35 + metadata_score * 0.20
        if final < min_score:
            continue
        candidates.append(RetrievalCandidate(
            id=row.id,
            title=row.title,
            summary=row.summary,
            source_ref=row.source_ref,
            tags=list(row.tags_json or []),
            bm25_score=bm25_score,
            vector_score=vector_score,
            metadata_score=metadata_score,
            final_score=final,
        ))
    candidates.sort(
        key=lambda item: (item.final_score, item.metadata_score, item.bm25_score, item.id),
        reverse=True,
    )
    return [item.to_dict() for item in candidates[:max(1, limit)]]
