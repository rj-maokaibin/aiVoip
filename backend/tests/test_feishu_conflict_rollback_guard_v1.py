import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import Case, FeishuCaseBinding
from app.integrations.feishu.service import FeishuActiveCaseConflict, bind_case_to_chat


def _case(db: Session, case_no: str) -> Case:
    row = Case(case_no=case_no, summary="电流音", status="ANALYZING")
    db.add(row)
    db.flush()
    return row


def _context(message_id: str) -> dict:
    return {
        "tenant_key": "tenant-a",
        "message_id": message_id,
        "sender_open_id": "ou-engineer",
        "chat_type": "group",
    }


def test_swallowed_active_case_conflict_cannot_commit_orphan_case():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)

    with Session(engine) as db:
        winner = _case(db, "CASE-WINNER")
        bind_case_to_chat(
            db, case_id=winner.id, chat_id="oc-1", chat_type="group",
            source_context=_context("m-1"),
        )
        db.commit()

        loser = _case(db, "CASE-LOSER")
        loser_id = loser.id
        try:
            bind_case_to_chat(
                db, case_id=loser.id, chat_id="oc-1", chat_type="group",
                source_context=_context("m-2"),
            )
        except FeishuActiveCaseConflict:
            # Simulates the legacy best-effort caller swallowing a bind error.
            pass

        with pytest.raises(FeishuActiveCaseConflict):
            db.commit()

        db.rollback()
        assert db.get(Case, loser_id) is None
        bindings = list(db.scalars(select(FeishuCaseBinding)))
        assert len(bindings) == 1
        assert bindings[0].case_id == winner.id
