from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db import knowledge_models as _knowledge_models  # noqa: F401
from app.db.knowledge_models import ProductFact
from app.knowledge.importer import import_product_facts
from app.knowledge.product_facts import lookup_product_fact


def _db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def _row(**extra):
    row = {
        "product_model": "T18",
        "feature_key": "VOIP.DTMF.RFC2833",
        "value": {"supported": True},
        "value_text": "支持",
        "source_document": "T18 SPEC V1.0",
        "source_section": "VoIP / DTMF",
        "authority_level": 3,
    }
    row.update(extra)
    return row


def test_default_import_is_draft_and_not_answer_authority():
    with _db() as db:
        result = import_product_facts(db, [_row()])
        db.commit()
        assert result.created == 1
        fact = db.scalar(select(ProductFact))
        assert fact.approval_status == "DRAFT"
        assert (fact.metadata_json or {}).get("import_sha256")
        lookup = lookup_product_fact(db, product_model="T18", feature_key="VOIP.DTMF.RFC2833")
        assert lookup.status == "NOT_FOUND"


def test_explicit_approved_import_becomes_strict_authority():
    with _db() as db:
        result = import_product_facts(db, [_row(approval_status="APPROVED")], actor="reviewer-a")
        db.commit()
        assert result.created == 1
        fact = db.scalar(select(ProductFact))
        assert fact.approved_by == "reviewer-a"
        lookup = lookup_product_fact(db, product_model="T18", feature_key="VOIP.DTMF.RFC2833")
        assert lookup.status == "FOUND"
        assert lookup.fact["value_text"] == "支持"


def test_same_scope_reimport_updates_instead_of_duplicating():
    with _db() as db:
        first = import_product_facts(db, [_row()])
        second = import_product_facts(db, [_row(value_text="支持 RFC2833")])
        db.commit()
        assert first.created == 1
        assert second.updated == 1
        assert db.query(ProductFact).count() == 1
        assert db.scalar(select(ProductFact)).value_text == "支持 RFC2833"


def test_invalid_record_is_reported_without_inventing_fact():
    with _db() as db:
        result = import_product_facts(db, [{"product_model": "T18"}])
        assert result.created == 0
        assert len(result.errors) == 1
        assert "FEATURE_KEY_REQUIRED" in result.errors[0]["error"]
