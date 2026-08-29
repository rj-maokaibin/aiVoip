from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import datetime
import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

import yaml
from sqlalchemy import and_, select
from sqlalchemy.orm import Session

from app.db.knowledge_models import ProductFact


_ALLOWED_APPROVAL = {"DRAFT", "REVIEW", "APPROVED", "REJECTED", "SUPERSEDED"}


class ProductFactImportError(ValueError):
    pass


@dataclass(frozen=True)
class ProductFactImportResult:
    created: int
    updated: int
    skipped: int
    ids: list[str]
    errors: list[dict[str, Any]]


def _parse_dt(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value
    text = str(value).strip()
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception as exc:
        raise ProductFactImportError(f"INVALID_DATETIME:{text}") from exc


def _json_value(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if raw is None:
        return {}
    if isinstance(raw, str):
        text = raw.strip()
        if text.startswith("{"):
            parsed = json.loads(text)
            if not isinstance(parsed, dict):
                raise ProductFactImportError("VALUE_JSON_MUST_BE_OBJECT")
            return parsed
        return {"value": raw}
    return {"value": raw}


def normalize_product_fact(raw: dict[str, Any], *, default_approval: str = "DRAFT") -> dict[str, Any]:
    product_model = str(raw.get("product_model") or raw.get("model") or "").strip()
    feature_key = str(raw.get("feature_key") or raw.get("feature") or "").strip()
    source_document = str(raw.get("source_document") or raw.get("source") or "").strip()
    if not product_model:
        raise ProductFactImportError("PRODUCT_MODEL_REQUIRED")
    if not feature_key:
        raise ProductFactImportError("FEATURE_KEY_REQUIRED")
    if not source_document:
        raise ProductFactImportError("SOURCE_DOCUMENT_REQUIRED")
    approval = str(raw.get("approval_status") or default_approval).upper().strip()
    if approval not in _ALLOWED_APPROVAL:
        raise ProductFactImportError(f"INVALID_APPROVAL_STATUS:{approval}")
    authority = int(raw.get("authority_level") or 2)
    if not 0 <= authority <= 5:
        raise ProductFactImportError("AUTHORITY_LEVEL_OUT_OF_RANGE")

    value_json = raw.get("value_json")
    if value_json is None and "value" in raw:
        value_json = raw.get("value")
    value_json = _json_value(value_json)
    value_text = raw.get("value_text")
    if value_text is None and "value" in value_json and not isinstance(value_json.get("value"), (dict, list)):
        value_text = str(value_json.get("value"))

    return {
        "product_model": product_model,
        "feature_key": feature_key,
        "value_json": value_json,
        "value_text": str(value_text).strip() if value_text not in (None, "") else None,
        "unit": str(raw.get("unit") or "").strip() or None,
        "hw_scope": str(raw.get("hw_scope") or raw.get("hardware_revision") or "*").strip() or "*",
        "sw_version_scope": str(raw.get("sw_version_scope") or raw.get("software_version") or "*").strip() or "*",
        "region_scope": str(raw.get("region_scope") or raw.get("region") or "*").strip() or "*",
        "source_document": source_document,
        "source_section": str(raw.get("source_section") or raw.get("section") or "").strip() or None,
        "source_ref": str(raw.get("source_ref") or "").strip() or None,
        "authority_level": authority,
        "approval_status": approval,
        "supersedes_fact_id": str(raw.get("supersedes_fact_id") or "").strip() or None,
        "effective_from": _parse_dt(raw.get("effective_from")),
        "effective_to": _parse_dt(raw.get("effective_to")),
        "metadata_json": dict(raw.get("metadata_json") or raw.get("metadata") or {}),
        "created_by": str(raw.get("created_by") or "").strip() or None,
        "approved_by": str(raw.get("approved_by") or "").strip() or None,
    }


def _same_scope_query(data: dict[str, Any]):
    conditions = [
        ProductFact.product_model == data["product_model"],
        ProductFact.feature_key == data["feature_key"],
        ProductFact.hw_scope == data["hw_scope"],
        ProductFact.sw_version_scope == data["sw_version_scope"],
        ProductFact.region_scope == data["region_scope"],
    ]
    if data["effective_from"] is None:
        conditions.append(ProductFact.effective_from.is_(None))
    else:
        conditions.append(ProductFact.effective_from == data["effective_from"])
    return and_(*conditions)


def import_product_facts(
    db: Session,
    rows: Iterable[dict[str, Any]],
    *,
    actor: str = "knowledge-import",
    default_approval: str = "DRAFT",
    allow_update: bool = True,
) -> ProductFactImportResult:
    created = updated = skipped = 0
    ids: list[str] = []
    errors: list[dict[str, Any]] = []
    for index, raw in enumerate(rows, start=1):
        try:
            data = normalize_product_fact(dict(raw), default_approval=default_approval)
            data["created_by"] = data.get("created_by") or actor
            if data["approval_status"] == "APPROVED":
                data["approved_by"] = data.get("approved_by") or actor
            canonical = json.dumps(
                {
                    key: (value.isoformat() if isinstance(value, datetime) else value)
                    for key, value in data.items()
                    if key not in {"metadata_json", "created_by", "approved_by"}
                },
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
            metadata = dict(data.get("metadata_json") or {})
            metadata.setdefault("import_sha256", hashlib.sha256(canonical.encode("utf-8")).hexdigest())
            metadata.setdefault("import_actor", actor)
            data["metadata_json"] = metadata

            existing = db.scalar(select(ProductFact).where(_same_scope_query(data)).limit(1))
            if existing is None:
                row = ProductFact(**data)
                db.add(row)
                db.flush()
                created += 1
                ids.append(row.id)
                continue
            if not allow_update:
                skipped += 1
                ids.append(existing.id)
                continue
            changed = False
            for key, value in data.items():
                if getattr(existing, key) != value:
                    setattr(existing, key, value)
                    changed = True
            db.flush()
            ids.append(existing.id)
            if changed:
                updated += 1
            else:
                skipped += 1
        except Exception as exc:
            errors.append({"row": index, "error": f"{type(exc).__name__}:{exc}", "input": dict(raw)})
    return ProductFactImportResult(created, updated, skipped, ids, errors)


def load_fact_records(path: str | Path) -> list[dict[str, Any]]:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as fh:
            return [dict(row) for row in csv.DictReader(fh)]
    if suffix in {".yaml", ".yml"}:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    elif suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
    else:
        raise ProductFactImportError(f"UNSUPPORTED_FACT_FILE:{suffix}")
    if isinstance(data, dict):
        data = data.get("facts") or data.get("items") or [data]
    if not isinstance(data, list):
        raise ProductFactImportError("FACT_FILE_MUST_CONTAIN_LIST")
    return [dict(row) for row in data]


def import_product_fact_path(
    db: Session,
    path: str | Path,
    *,
    actor: str = "knowledge-import",
    default_approval: str = "DRAFT",
    allow_update: bool = True,
) -> ProductFactImportResult:
    root = Path(path)
    files = [root] if root.is_file() else sorted(
        p for p in root.rglob("*") if p.is_file() and p.suffix.lower() in {".json", ".yaml", ".yml", ".csv"}
    )
    aggregate = ProductFactImportResult(0, 0, 0, [], [])
    for file in files:
        try:
            records = load_fact_records(file)
        except Exception as exc:
            aggregate.errors.append({"file": str(file), "error": f"{type(exc).__name__}:{exc}"})
            continue
        result = import_product_facts(
            db,
            records,
            actor=actor,
            default_approval=default_approval,
            allow_update=allow_update,
        )
        aggregate = ProductFactImportResult(
            aggregate.created + result.created,
            aggregate.updated + result.updated,
            aggregate.skipped + result.skipped,
            aggregate.ids + result.ids,
            aggregate.errors + [{**item, "file": str(file)} for item in result.errors],
        )
    return aggregate
