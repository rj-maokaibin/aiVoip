from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.db.base import Base
from app.db.models import Case, FeishuCaseBinding
from app.integrations.feishu.case_resolver import resolve_case


def _engine():
    eng = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(eng)
    return eng


def _case(db: Session, case_no: str, status: str = "ANALYZING") -> Case:
    row = Case(case_no=case_no, summary=f"{case_no} 电流音", status=status)
    db.add(row)
    db.flush()
    return row


def _bind(db: Session, case: Case, *, tenant: str, chat: str, message: str) -> FeishuCaseBinding:
    row = FeishuCaseBinding(
        case_id=case.id, receive_id=chat, receive_id_type="chat_id",
        source_tenant_key=tenant, source_message_id=message,
        source_root_message_id=message, status="ACTIVE", card_version=0,
    )
    db.add(row)
    db.flush()
    return row


def test_resolver_uses_chat_active_case_as_default_context():
    with Session(_engine()) as db:
        case = _case(db, "CASE-A")
        _bind(db, case, tenant="tenant-a", chat="oc-1", message="m-a")

        resolved = resolve_case(
            db, tenant_key="tenant-a", chat_id="oc-1", case_ref=None,
            message_id="m-new", root_message_id="", parent_message_id="",
            device_refs=[], symptoms=["电流音"],
        )

        assert resolved.case_id == case.id
        assert resolved.reason == "CHAT_ACTIVE_CASE"


def test_explicit_case_reference_has_priority_and_bad_reference_fails_closed():
    with Session(_engine()) as db:
        active = _case(db, "CASE-A")
        explicit = _case(db, "CASE-B")
        _bind(db, active, tenant="tenant-a", chat="oc-1", message="m-a")
        _bind(db, explicit, tenant="tenant-a", chat="oc-2", message="m-b")

        resolved = resolve_case(
            db, tenant_key="tenant-a", chat_id="oc-1", case_ref="CASE-B",
            message_id="m-new", root_message_id="", parent_message_id="",
        )
        assert resolved.case_id == explicit.id
        assert resolved.reason == "EXPLICIT_CASE_REF"

        missing = resolve_case(
            db, tenant_key="tenant-a", chat_id="oc-1", case_ref="CASE-NOT-FOUND",
            message_id="m-new-2", root_message_id="", parent_message_id="",
        )
        assert missing.case is None
        assert missing.reason == "EXPLICIT_CASE_NOT_FOUND"


def test_thread_can_resolve_historical_case_before_current_active_case():
    with Session(_engine()) as db:
        old = _case(db, "CASE-OLD", status="RESOLVED")
        current = _case(db, "CASE-CURRENT")
        _bind(db, old, tenant="tenant-a", chat="oc-1", message="m-old")
        _bind(db, current, tenant="tenant-a", chat="oc-1", message="m-current")

        resolved = resolve_case(
            db, tenant_key="tenant-a", chat_id="oc-1", case_ref=None,
            message_id="m-reply", root_message_id="m-old", parent_message_id="m-old",
        )

        assert resolved.case_id == old.id
        assert resolved.reason == "THREAD"


def test_same_chat_id_is_isolated_by_tenant():
    with Session(_engine()) as db:
        tenant_a = _case(db, "CASE-TA")
        tenant_b = _case(db, "CASE-TB")
        _bind(db, tenant_a, tenant="tenant-a", chat="oc-shared", message="m-a")
        _bind(db, tenant_b, tenant="tenant-b", chat="oc-shared", message="m-b")

        resolved_a = resolve_case(
            db, tenant_key="tenant-a", chat_id="oc-shared", case_ref=None,
            message_id="m-x", root_message_id="", parent_message_id="",
        )
        resolved_b = resolve_case(
            db, tenant_key="tenant-b", chat_id="oc-shared", case_ref=None,
            message_id="m-y", root_message_id="", parent_message_id="",
        )

        assert resolved_a.case_id == tenant_a.id
        assert resolved_b.case_id == tenant_b.id
