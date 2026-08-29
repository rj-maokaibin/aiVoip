from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.knowledge.answering import answer_verified_question
from app.knowledge.product_facts import lookup_product_fact


def _render_fact(fact: dict[str, Any]) -> str:
    value_text = str(fact.get("value_text") or "").strip()
    if not value_text:
        value = fact.get("value")
        if isinstance(value, dict) and len(value) == 1:
            value_text = str(next(iter(value.values())))
        else:
            value_text = str(value)
    unit = str(fact.get("unit") or "").strip()
    value_rendered = f"{value_text}{unit}" if unit and unit not in value_text else value_text
    source = str(fact.get("source_document") or "受控产品事实")
    section = str(fact.get("source_section") or "").strip()
    source_rendered = f"{source} / {section}" if section else source
    return f"根据当前已审核产品事实，结论是：{value_rendered}。来源：{source_rendered}。"


def answer_grounded_knowledge(
    db: Session,
    query: str,
    *,
    entities: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Answer knowledge questions with strict-fact-first authority.

    Exact product facts are used only when the conversation layer already resolved
    both product_model and feature_key. The function deliberately does not infer a
    strict feature key from free text. If no exact ProductFact authority is
    available it falls back to the existing reviewer-verified KnowledgeItem path.
    """
    entities = entities or {}
    product_model = str(entities.get("product_model") or "").strip()
    feature_key = str(entities.get("feature_key") or "").strip()
    if product_model and feature_key:
        result = lookup_product_fact(
            db,
            product_model=product_model,
            feature_key=feature_key,
            hw_revision=str(entities.get("hardware_revision") or "").strip() or None,
            sw_version=str(entities.get("software_version") or "").strip() or None,
            region=str(entities.get("region") or "").strip() or None,
        )
        if result.status == "FOUND" and result.fact:
            return {
                "answered": True,
                "answer_type": "PRODUCT_FACT",
                "text": _render_fact(result.fact),
                "citations": [{
                    "product_fact_id": result.fact["id"],
                    "title": result.fact["source_document"],
                    "source_ref": result.fact.get("source_ref"),
                    "authority_level": result.fact.get("authority_level"),
                }],
                "fact": result.fact,
            }
        if result.status == "CONFLICT":
            sources = "、".join(sorted({str(x.get("source_document") or "未命名来源") for x in result.candidates}))
            return {
                "answered": False,
                "answer_type": "PRODUCT_FACT_CONFLICT",
                "text": f"当前受控产品事实存在冲突（{sources}），我不能替你猜一个结论。需要先按产品/硬件/软件版本确认适用范围或完成知识审核。",
                "citations": [{
                    "product_fact_id": item["id"],
                    "title": item["source_document"],
                    "source_ref": item.get("source_ref"),
                    "authority_level": item.get("authority_level"),
                } for item in result.candidates],
                "conflict": True,
            }

    fallback = answer_verified_question(db, query)
    return {
        **fallback,
        "answer_type": "VERIFIED_KNOWLEDGE_ITEM" if fallback.get("answered") else "NOT_FOUND",
    }
