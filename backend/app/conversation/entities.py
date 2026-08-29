from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.knowledge_models import ProductFact


_RELEASE_RE = re.compile(r"(?i)\bR\d{2,4}(?:\.\d+)?\b")


def resolve_catalog_entities(
    db: Session,
    text: str,
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve only entities backed by the structured ProductFact catalog.

    The resolver is intentionally conservative. It may carry forward previously
    resolved context, replace an explicitly mentioned software release, and match
    exact known product/feature aliases. It never fabricates a product model or a
    strict feature key from general language.
    """
    entities = dict(existing or {})
    lowered = (text or "").lower()
    rows = list(db.scalars(select(ProductFact).limit(2000)))

    products = sorted({str(row.product_model) for row in rows if row.product_model}, key=len, reverse=True)
    for model in products:
        if model.lower() in lowered:
            entities["product_model"] = model
            break

    features = sorted({str(row.feature_key) for row in rows if row.feature_key}, key=len, reverse=True)
    for feature_key in features:
        leaf = feature_key.split(".")[-1]
        aliases = {
            feature_key.lower(),
            leaf.lower(),
            leaf.replace("_", " ").lower(),
            feature_key.replace("_", " ").lower(),
        }
        if any(len(alias) >= 4 and alias in lowered for alias in aliases):
            entities["feature_key"] = feature_key
            break

    releases = _RELEASE_RE.findall(text or "")
    if releases:
        entities["software_version"] = releases[-1].upper()
    return entities
