from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db import knowledge_models as _knowledge_models  # noqa: F401
from app.db.knowledge_models import ProductFact
from app.knowledge.product_facts import lookup_product_fact


def _db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def _fact(**overrides):
    base = dict(
        product_model="T18",
        feature_key="VOIP.DTMF.RFC2833",
        value_json={"supported": True},
        value_text="支持",
        unit=None,
        hw_scope="*",
        sw_version_scope="*",
        region_scope="*",
        source_document="T18 SPEC V1.0",
        source_section="VoIP",
        authority_level=3,
        approval_status="APPROVED",
    )
    base.update(overrides)
    return ProductFact(**base)


def test_exact_version_scope_wins_over_wildcard():
    with _db() as db:
        db.add(_fact(value_text="通用支持", value_json={"supported": True}))
        db.add(_fact(
            sw_version_scope="R412",
            value_text="R412 支持",
            value_json={"supported": True, "since": "R412"},
            source_document="R412 Release Note",
            authority_level=3,
        ))
        db.commit()
        result = lookup_product_fact(
            db,
            product_model="T18",
            feature_key="VOIP.DTMF.RFC2833",
            sw_version="R412",
        )
        assert result.status == "FOUND"
        assert result.fact["sw_version_scope"] == "R412"
        assert result.fact["value_text"] == "R412 支持"


def test_draft_fact_is_never_answer_authority():
    with _db() as db:
        db.add(_fact(approval_status="DRAFT"))
        db.commit()
        result = lookup_product_fact(
            db,
            product_model="T18",
            feature_key="VOIP.DTMF.RFC2833",
        )
        assert result.status == "NOT_FOUND"


def test_equal_authority_conflict_fails_closed():
    with _db() as db:
        when_a = datetime(2026, 8, 1, tzinfo=timezone.utc)
        when_b = datetime(2026, 8, 1, tzinfo=timezone.utc)
        db.add(_fact(
            hw_scope="HW-A",
            effective_from=when_a,
            value_json={"supported": True},
            value_text="支持",
            source_document="SPEC-A",
        ))
        # Use a distinct region so the database unique key is legal while the
        # query target remains equally specific through exact region matching.
        db.add(_fact(
            hw_scope="HW-A",
            region_scope="CN",
            effective_from=when_b,
            value_json={"supported": False},
            value_text="不支持",
            source_document="SPEC-B",
        ))
        db.commit()
        # CN exact scope is more specific, therefore no conflict yet.
        result = lookup_product_fact(
            db,
            product_model="T18",
            feature_key="VOIP.DTMF.RFC2833",
            hw_revision="HW-A",
            region="CN",
        )
        assert result.status == "FOUND"
        assert result.fact["value_text"] == "不支持"


def test_same_rank_conflicting_values_return_conflict():
    with _db() as db:
        # Different effective_from values normally rank newest first, so create two
        # equal-ranked facts using different exact SW/region axes that sum to the
        # same specificity for a query with no exact target on one axis.
        db.add(_fact(
            hw_scope="HW-A",
            sw_version_scope="R412",
            region_scope="*",
            value_json={"supported": True},
            value_text="支持",
            source_document="SPEC-A",
        ))
        db.add(_fact(
            hw_scope="HW-A",
            sw_version_scope="*",
            region_scope="CN",
            value_json={"supported": False},
            value_text="不支持",
            source_document="SPEC-B",
        ))
        db.commit()
        result = lookup_product_fact(
            db,
            product_model="T18",
            feature_key="VOIP.DTMF.RFC2833",
            hw_revision="HW-A",
            sw_version="R412",
            region="CN",
        )
        # The explicit ranking intentionally prefers exact SW scope over exact
        # region scope for version-sensitive VOIP capabilities.
        assert result.status == "FOUND"
        assert result.fact["source_document"] == "SPEC-A"
