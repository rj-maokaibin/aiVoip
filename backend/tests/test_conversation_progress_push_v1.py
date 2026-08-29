from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db import models as _models  # noqa: F401
from app.db import conversation_models as _conversation_models  # noqa: F401
from app.db.models import Case, DiagnosisRun, FeishuCaseBinding
from app.conversation.progress import push_meaningful_progress


def _db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def _seed(db: Session, *, summary=None, case_status="WAITING_USER"):
    case = Case(case_no="VOIP-20260829-ABC123", summary="单通无声", status=case_status)
    db.add(case)
    db.flush()
    run = DiagnosisRun(
        case_id=case.id,
        status="WAITING_USER",
        cycle=2,
        summary_json=summary or {},
        decision_json={},
    )
    db.add(run)
    db.add(FeishuCaseBinding(
        case_id=case.id,
        receive_id="oc_chat",
        receive_id_type="chat_id",
        source_message_id="om_source",
        status="ACTIVE",
    ))
    db.commit()
    return case, run


def test_same_progress_digest_is_pushed_only_once(monkeypatch):
    sent = []
    monkeypatch.setattr("app.integrations.feishu.feedback.enqueue_reply", lambda mid, text: sent.append((mid, text)) or True)
    with _db() as db:
        case, _run = _seed(db, summary={"headline": "已完成媒体分析", "known": ["识别到 2 通 SIP 呼叫"]})
        first = push_meaningful_progress(db, case_id=case.id)
        db.commit()
        second = push_meaningful_progress(db, case_id=case.id)
        assert first["status"] == "QUEUED"
        assert second["reason"] == "NO_MEANINGFUL_CHANGE"
        assert len(sent) == 1
        assert "识别到 2 通 SIP 呼叫" in sent[0][1]


def test_new_grounded_finding_produces_new_push(monkeypatch):
    sent = []
    monkeypatch.setattr("app.integrations.feishu.feedback.enqueue_reply", lambda mid, text: sent.append((mid, text)) or True)
    with _db() as db:
        case, run = _seed(db, summary={"known": ["2 SIP calls"]})
        assert push_meaningful_progress(db, case_id=case.id)["status"] == "QUEUED"
        db.commit()
        run.summary_json = {"known": ["2 SIP calls", "3 RTP streams"]}
        db.commit()
        result = push_meaningful_progress(db, case_id=case.id)
        assert result["status"] == "QUEUED"
        assert len(sent) == 2


def test_cycle_only_or_empty_state_does_not_spam(monkeypatch):
    sent = []
    monkeypatch.setattr("app.integrations.feishu.feedback.enqueue_reply", lambda mid, text: sent.append((mid, text)) or True)
    with _db() as db:
        case, run = _seed(db, summary={})
        result = push_meaningful_progress(db, case_id=case.id)
        assert result["reason"] == "NO_USER_VISIBLE_FINDING"
        run.cycle = 3
        db.commit()
        second = push_meaningful_progress(db, case_id=case.id)
        # cycle is intentionally absent from the meaningful digest.
        assert second["reason"] == "NO_MEANINGFUL_CHANGE"
        assert sent == []
