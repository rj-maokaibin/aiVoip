from types import SimpleNamespace

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.api.v1 import feishu_document_acl as api
from app.auth.providers import AuthIdentity
from app.contracts.enums import UserRole
from app.db.base import Base
from app.db.evidence_report_models import FeishuEvidenceDocumentBinding
from app.db.feishu_governance_models import FeishuDocumentAclBinding
from app.db.models import Case, IdempotencyRecord


def _db():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    return Session(engine)


def _admin():
    return AuthIdentity("admin-acl", UserRole.ADMIN, True, "test")


def _case_with_doc(db: Session):
    case = Case(case_no="CASE-DOC-ACL-API", summary="电流音", status="ANALYZING")
    db.add(case)
    db.flush()
    db.add(FeishuEvidenceDocumentBinding(
        case_id=case.id,
        document_id="docx-1",
        document_url="https://example.invalid/docx-1",
        status="SYNCED",
        projection_version=1,
    ))
    db.commit()
    return case


def test_document_acl_status_reports_document_bound_but_not_configured():
    with _db() as db:
        case = _case_with_doc(db)
        result = api.get_document_acl_status(case.id, db=db, _identity=_admin())
        assert result["case_id"] == case.id
        assert result["document_id"] == "docx-1"
        assert result["status"] == "NOT_CONFIGURED"
        assert result["capability"] == "MANAGE_DOCUMENT_ACL"


def test_manual_sync_is_idempotent_and_enqueues_once(monkeypatch):
    calls = []
    monkeypatch.setattr(
        api.sync_document_acl,
        "delay",
        lambda case_id, document_id: calls.append((case_id, document_id)) or SimpleNamespace(id="task-1"),
    )
    with _db() as db:
        case = _case_with_doc(db)
        request = api.ManualAclSyncRequest(force_revision=False)
        first = api.request_document_acl_sync(
            case.id, request, db=db, idempotency_key="idem-doc-acl-1", identity=_admin(),
        )
        second = api.request_document_acl_sync(
            case.id, request, db=db, idempotency_key="idem-doc-acl-1", identity=_admin(),
        )
        assert first == second
        assert first["status"] == "QUEUED"
        assert first["task_id"] == "task-1"
        assert calls == [(case.id, "docx-1")]
        record = db.scalar(select(IdempotencyRecord).where(
            IdempotencyRecord.scope == f"POST:/api/v1/cases/{case.id}/feishu-document-acl/sync"
        ))
        assert record is not None
        assert record.status == "COMPLETED"


def test_force_sync_bumps_revision_and_resets_failure(monkeypatch):
    monkeypatch.setattr(
        api.sync_document_acl,
        "delay",
        lambda case_id, document_id: SimpleNamespace(id="task-force"),
    )
    with _db() as db:
        case = _case_with_doc(db)
        row = FeishuDocumentAclBinding(
            case_id=case.id,
            document_id="docx-1",
            tenant_key="tenant-a",
            chat_id="oc-chat-a",
            sync_mode="CHAT_SCOPE",
            desired_permission="view",
            desired_revision=3,
            applied_revision=2,
            status="FAILED",
            retry_count=2,
            last_error="FeishuTransportError:boom",
        )
        db.add(row)
        db.commit()
        result = api.request_document_acl_sync(
            case.id,
            api.ManualAclSyncRequest(force_revision=True),
            db=db,
            idempotency_key="idem-doc-acl-force",
            identity=_admin(),
        )
        db.refresh(row)
        assert result["desired_revision"] == 4
        assert row.desired_revision == 4
        assert row.status == "PENDING"
        assert row.last_error is None


def test_status_returns_applied_and_desired_state():
    with _db() as db:
        case = _case_with_doc(db)
        row = FeishuDocumentAclBinding(
            case_id=case.id,
            document_id="docx-1",
            tenant_key="tenant-a",
            chat_id="oc-chat-a",
            sync_mode="AUTO",
            effective_mode="CHAT_SCOPE",
            desired_permission="view",
            desired_revision=2,
            applied_revision=2,
            status="SYNCED",
            metadata_json={"member_count": 1},
        )
        db.add(row)
        db.commit()
        result = api.get_document_acl_status(case.id, db=db, _identity=_admin())
        assert result["status"] == "SYNCED"
        assert result["sync_mode"] == "AUTO"
        assert result["effective_mode"] == "CHAT_SCOPE"
        assert result["desired_revision"] == result["applied_revision"] == 2
        assert result["metadata"]["member_count"] == 1
