from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.models import KnowledgeItem
from app.knowledge.retrieval import hybrid_search_verified_knowledge


def _db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def test_verified_relevant_item_ranks_first():
    with _db() as db:
        db.add(KnowledgeItem(
            type="SPEC",
            title="T18 DTMF RFC2833 规格",
            summary="T18 支持 RFC2833 telephone-event DTMF 方式。",
            tags_json=["T18", "DTMF", "RFC2833"],
            source_ref="spec:t18:dtmf",
            verified=1,
            verified_by="reviewer",
        ))
        db.add(KnowledgeItem(
            type="GUIDE",
            title="SIP 注册排障",
            summary="介绍 REGISTER 401 403 常见排查步骤。",
            tags_json=["SIP", "REGISTER"],
            source_ref="guide:sip-register",
            verified=1,
            verified_by="reviewer",
        ))
        db.commit()
        rows = hybrid_search_verified_knowledge(db, "T18 RFC2833 DTMF", limit=5)
        assert rows
        assert rows[0]["source_ref"] == "spec:t18:dtmf"
        assert rows[0]["retrieval"]["bm25"] > 0
        assert rows[0]["retrieval"]["vector"] > 0


def test_unverified_item_never_enters_retrieval_authority():
    with _db() as db:
        db.add(KnowledgeItem(
            type="SPEC",
            title="T18 RFC2833",
            summary="未经审核的测试内容。",
            tags_json=["T18", "RFC2833"],
            source_ref="draft:t18",
            verified=0,
        ))
        db.commit()
        assert hybrid_search_verified_knowledge(db, "T18 RFC2833") == []


def test_irrelevant_query_fails_closed():
    with _db() as db:
        db.add(KnowledgeItem(
            type="GUIDE",
            title="SIP 注册排障",
            summary="REGISTER 401 403 排查。",
            tags_json=["SIP", "REGISTER"],
            source_ref="guide:sip-register",
            verified=1,
        ))
        db.commit()
        assert hybrid_search_verified_knowledge(db, "FXS REN 振铃负载", min_score=0.06) == []
