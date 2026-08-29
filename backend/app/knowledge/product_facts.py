from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db.knowledge_models import ProductFact


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass(frozen=True)
class ProductFactLookupResult:
    status: str
    fact: dict[str, Any] | None
    candidates: list[dict[str, Any]]
    reason: str | None = None


def _serialize(row: ProductFact) -> dict[str, Any]:
    return {
        "id": row.id,
        "product_model": row.product_model,
        "feature_key": row.feature_key,
        "value": row.value_json,
        "value_text": row.value_text,
        "unit": row.unit,
        "hw_scope": row.hw_scope,
        "sw_version_scope": row.sw_version_scope,
        "region_scope": row.region_scope,
        "source_document": row.source_document,
        "source_section": row.source_section,
        "source_ref": row.source_ref,
        "authority_level": row.authority_level,
        "approval_status": row.approval_status,
        "effective_from": row.effective_from.isoformat() if row.effective_from else None,
        "effective_to": row.effective_to.isoformat() if row.effective_to else None,
    }


def _scope_rank(value: str, target: str | None) -> int:
    if target and value == target:
        return 2
    if value == "*":
        return 1
    return 0


def lookup_product_fact(
    db: Session,
    *,
    product_model: str,
    feature_key: str,
    hw_revision: str | None = None,
    sw_version: str | None = None,
    region: str | None = None,
    at: datetime | None = None,
) -> ProductFactLookupResult:
    """Return an approved strict fact or an explicit conflict/not-found state.

    The function never chooses between equal-authority conflicting values.  This
    is deliberate: product/spec questions such as support flags and limits must
    fail closed rather than asking an LLM to reconcile incompatible documents.
    """
    at = at or utcnow()
    rows = list(db.scalars(
        select(ProductFact).where(
            ProductFact.product_model == product_model,
            ProductFact.feature_key == feature_key,
            ProductFact.approval_status == "APPROVED",
            or_(ProductFact.effective_from.is_(None), ProductFact.effective_from <= at),
            or_(ProductFact.effective_to.is_(None), ProductFact.effective_to > at),
        )
    ))
    scoped: list[tuple[tuple[int, int, int, int, float], ProductFact]] = []
    for row in rows:
        hw_rank = _scope_rank(row.hw_scope, hw_revision)
        sw_rank = _scope_rank(row.sw_version_scope, sw_version)
        region_rank = _scope_rank(row.region_scope, region)
        if not hw_rank or not sw_rank or not region_rank:
            continue
        effective = row.effective_from.timestamp() if row.effective_from else 0.0
        scoped.append(((row.authority_level, hw_rank + sw_rank + region_rank, sw_rank, region_rank, effective), row))
    if not scoped:
        return ProductFactLookupResult("NOT_FOUND", None, [], "NO_APPROVED_MATCH")
    scoped.sort(key=lambda item: item[0], reverse=True)
    best_rank = scoped[0][0]
    best = [row for rank, row in scoped if rank == best_rank]
    normalized_values = {
        json.dumps({"value": row.value_json, "text": row.value_text, "unit": row.unit}, sort_keys=True, ensure_ascii=False, default=str)
        for row in best
    }
    candidates = [_serialize(row) for row in best]
    if len(normalized_values) > 1:
        return ProductFactLookupResult("CONFLICT", None, candidates, "EQUAL_AUTHORITY_CONFLICT")
    return ProductFactLookupResult("FOUND", candidates[0], candidates)
