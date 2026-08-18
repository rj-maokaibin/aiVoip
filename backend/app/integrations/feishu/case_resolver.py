from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

from sqlalchemy import inspect, or_, select, text
from sqlalchemy.orm import Session

from app.db.models import Case, CaseDevice, FeishuCaseBinding


TERMINAL_CASE_STATES = {"RESOLVED", "CLOSED", "FAILED"}
_GENERIC_SYMPTOMS = {"故障", "异常", "问题"}
_EXPLICIT_NEW_CASE_PHRASES = (
    "新问题", "新的问题", "另一个问题", "另外一个问题", "新故障", "新的故障",
    "另一个故障", "另外一个故障", "新case", "new case", "another case", "另外一台",
)


@dataclass(frozen=True)
class CaseResolution:
    case: Case | None
    ambiguous_cases: list[Case] = field(default_factory=list)
    reason: str = "NO_SAFE_MATCH"
    binding_id: str | None = None

    @property
    def case_id(self) -> str | None:
        return self.case.id if self.case else None


@dataclass(frozen=True)
class ActiveChatBinding:
    binding_id: str
    case_id: str
    generation: int


def normalize_tenant_key(value: str | None) -> str:
    return str(value or "")


def lifecycle_columns_available(db: Session) -> bool:
    """Return whether migration 0021 is present on this database.

    IMPORTANT: inspect the Session's *current Connection*, not the Engine. Unit
    tests use a SQLite StaticPool; opening/closing a second Engine connection can
    reuse the same DBAPI connection and roll back the Session's uncommitted work.
    Keeping schema inspection on ``db.connection()`` makes introspection part of
    the current transaction and preserves message/idempotency/Case state.
    """
    try:
        columns = {
            row["name"]
            for row in inspect(db.connection()).get_columns("feishu_case_bindings")
        }
    except Exception:
        return False
    return {"binding_state", "binding_generation", "activated_at", "closed_at"}.issubset(columns)


def _tenant_filter(tenant_key: str):
    return (
        or_(FeishuCaseBinding.source_tenant_key == "", FeishuCaseBinding.source_tenant_key.is_(None))
        if not tenant_key else FeishuCaseBinding.source_tenant_key == tenant_key
    )


def _raw_active_binding(db: Session, *, tenant_key: str, chat_id: str) -> ActiveChatBinding | None:
    if not lifecycle_columns_available(db) or not chat_id:
        return None
    row = db.execute(
        text(
            """
            SELECT id, case_id, binding_generation
            FROM feishu_case_bindings
            WHERE source_tenant_key = :tenant_key
              AND receive_id = :chat_id
              AND receive_id_type = 'chat_id'
              AND binding_state = 'ACTIVE'
            ORDER BY activated_at DESC NULLS LAST, created_at DESC
            LIMIT 1
            """
        ),
        {"tenant_key": tenant_key, "chat_id": chat_id},
    ).mappings().first()
    if not row:
        return None
    return ActiveChatBinding(
        binding_id=str(row["id"]), case_id=str(row["case_id"]),
        generation=int(row["binding_generation"] or 1),
    )


def close_binding_lifecycle(db: Session, *, binding_id: str, reason: str) -> None:
    if not lifecycle_columns_available(db):
        return
    db.execute(
        text(
            """
            UPDATE feishu_case_bindings
            SET binding_state = 'CLOSED',
                closed_at = COALESCE(closed_at, CURRENT_TIMESTAMP),
                close_reason = COALESCE(:reason, close_reason)
            WHERE id = :binding_id AND binding_state = 'ACTIVE'
            """
        ),
        {"binding_id": binding_id, "reason": reason[:128]},
    )


def activate_binding_lifecycle(
    db: Session, *, binding_id: str, tenant_key: str, chat_id: str,
    created_by_open_id: str | None = None,
) -> int:
    """Activate a binding and assign its monotonic generation for this chat."""
    if not lifecycle_columns_available(db):
        return 1
    tenant_key = normalize_tenant_key(tenant_key)
    generation = int(db.execute(
        text(
            """
            SELECT COALESCE(MAX(binding_generation), 0) + 1
            FROM feishu_case_bindings
            WHERE source_tenant_key = :tenant_key
              AND receive_id = :chat_id
              AND receive_id_type = 'chat_id'
              AND id <> :binding_id
            """
        ),
        {"tenant_key": tenant_key, "chat_id": chat_id, "binding_id": binding_id},
    ).scalar_one())
    db.execute(
        text(
            """
            UPDATE feishu_case_bindings
            SET source_tenant_key = :tenant_key,
                binding_state = 'ACTIVE',
                binding_generation = :generation,
                activated_at = COALESCE(activated_at, CURRENT_TIMESTAMP),
                closed_at = NULL,
                close_reason = NULL,
                created_by_open_id = COALESCE(created_by_open_id, :created_by_open_id)
            WHERE id = :binding_id
            """
        ),
        {
            "tenant_key": tenant_key, "generation": generation,
            "created_by_open_id": created_by_open_id, "binding_id": binding_id,
        },
    )
    return generation


def active_case_for_chat(db: Session, *, tenant_key: str | None, chat_id: str) -> tuple[Case | None, str | None]:
    """Resolve the one ACTIVE Case for a tenant-bound Feishu chat.

    G1's business key is ``tenant_key + chat_id``. Empty-tenant bindings are
    legacy/default-delivery records and deliberately do not opt into Active-Case
    routing; they retain the older thread/fingerprint behavior.
    """
    tenant = normalize_tenant_key(tenant_key)
    if not tenant or not chat_id:
        return None, None

    active = _raw_active_binding(db, tenant_key=tenant, chat_id=chat_id)
    if active:
        case = db.get(Case, active.case_id)
        if case is None or case.status in TERMINAL_CASE_STATES:
            close_binding_lifecycle(
                db, binding_id=active.binding_id,
                reason="AUTO_CASE_TERMINAL" if case else "AUTO_CASE_MISSING",
            )
            return None, None
        return case, active.binding_id

    # Legacy developer/test databases may not have migration 0021 yet, but a real
    # tenant-bound source binding can still be resolved conservatively by ORM.
    if not lifecycle_columns_available(db):
        row = db.scalar(
            select(FeishuCaseBinding)
            .join(Case, Case.id == FeishuCaseBinding.case_id)
            .where(
                FeishuCaseBinding.receive_id == chat_id,
                FeishuCaseBinding.receive_id_type == "chat_id",
                FeishuCaseBinding.source_tenant_key == tenant,
                Case.status.not_in(sorted(TERMINAL_CASE_STATES)),
            )
            .order_by(Case.created_at.desc())
            .limit(1)
        )
        if row:
            return db.get(Case, row.case_id), row.id
    return None, None


def _thread_case(
    db: Session, *, tenant_key: str, chat_id: str,
    message_id: str, root_message_id: str, parent_message_id: str,
) -> tuple[Case | None, str | None]:
    keys = {value for value in (message_id, root_message_id, parent_message_id) if value}
    if not chat_id or not keys:
        return None, None
    binding = db.scalar(
        select(FeishuCaseBinding)
        .where(
            FeishuCaseBinding.receive_id == chat_id,
            FeishuCaseBinding.receive_id_type == "chat_id",
            _tenant_filter(tenant_key),
            or_(
                FeishuCaseBinding.source_message_id.in_(keys),
                FeishuCaseBinding.source_root_message_id.in_(keys),
                FeishuCaseBinding.source_parent_message_id.in_(keys),
            ),
        )
        .order_by(FeishuCaseBinding.created_at.desc())
        .limit(1)
    )
    if not binding:
        return None, None
    return db.get(Case, binding.case_id), binding.id


def _fingerprint_case(
    db: Session, *, tenant_key: str, chat_id: str,
    device_refs: list[dict] | None, symptoms: list[str] | None,
) -> CaseResolution:
    specific = {
        str(item).lower() for item in (symptoms or [])
        if str(item).lower() not in _GENERIC_SYMPTOMS
    }
    refs = device_refs or []
    if not chat_id or not refs or not specific:
        return CaseResolution(None, reason="NO_SAFE_MATCH")

    since = datetime.now(timezone.utc) - timedelta(hours=24)
    candidates = list(db.scalars(
        select(Case)
        .join(FeishuCaseBinding, FeishuCaseBinding.case_id == Case.id)
        .where(
            FeishuCaseBinding.receive_id == chat_id,
            FeishuCaseBinding.receive_id_type == "chat_id",
            _tenant_filter(tenant_key),
            Case.created_at >= since,
            Case.status.not_in(sorted(TERMINAL_CASE_STATES)),
        )
        .order_by(Case.created_at.desc())
    ))

    scored: list[tuple[int, Case]] = []
    for candidate in candidates:
        devices = list(db.scalars(select(CaseDevice).where(CaseDevice.case_id == candidate.id)))
        device_score = 0
        for ref in refs:
            for device in devices:
                info = device.device_info or {}
                if ref.get("sn") and str(ref["sn"]).lower() == str(device.sn).lower():
                    device_score = max(device_score, 5)
                elif ref.get("ssh_ip") and str(ref["ssh_ip"]) == str(device.ip):
                    device_score = max(device_score, 4)
                elif ref.get("mac") and str(ref["mac"]).lower() == str(info.get("mac") or "").lower():
                    device_score = max(device_score, 4)
        symptom_score = 2 * sum(1 for token in specific if token in candidate.summary.lower())
        if device_score >= 4 and symptom_score >= 2:
            scored.append((device_score + symptom_score, candidate))

    if not scored:
        return CaseResolution(None, reason="NO_SAFE_MATCH")
    scored.sort(key=lambda item: (item[0], item[1].created_at), reverse=True)
    top_score = scored[0][0]
    top = [case for score, case in scored if score == top_score]
    if len(top) > 1:
        return CaseResolution(None, ambiguous_cases=top, reason="AMBIGUOUS_FINGERPRINT")
    return CaseResolution(top[0], reason="DEVICE_SYMPTOM_TIME_WINDOW")


def resolve_case(
    db: Session, *, tenant_key: str | None, chat_id: str,
    case_ref: str | None, message_id: str, root_message_id: str,
    parent_message_id: str, device_refs: list[dict] | None = None,
    symptoms: list[str] | None = None,
) -> CaseResolution:
    """Resolve a Feishu message to a Case using the frozen G1 priority order."""
    tenant = normalize_tenant_key(tenant_key)

    # P1: explicit Case reference is authoritative. A bad explicit reference
    # fails closed and MUST NOT silently fall back to another Case in the chat.
    if case_ref:
        row = db.scalar(select(Case).where(Case.case_no == case_ref).limit(1))
        return CaseResolution(row, reason="EXPLICIT_CASE_REF" if row else "EXPLICIT_CASE_NOT_FOUND")

    # P2: reply/thread anchor can intentionally address a historical Case even
    # after the group has moved to a newer Active Case.
    thread_case, binding_id = _thread_case(
        db, tenant_key=tenant, chat_id=chat_id, message_id=message_id,
        root_message_id=root_message_id, parent_message_id=parent_message_id,
    )
    if thread_case:
        return CaseResolution(thread_case, reason="THREAD", binding_id=binding_id)

    # P3 is intentionally tenant-bound. Empty-tenant legacy events keep the old
    # conservative fingerprint path instead of being reinterpreted as governed
    # Active-Case conversations.
    if tenant:
        active_case, binding_id = active_case_for_chat(db, tenant_key=tenant, chat_id=chat_id)
        if active_case:
            return CaseResolution(active_case, reason="CHAT_ACTIVE_CASE", binding_id=binding_id)

    # P4/P5: conservative cross-thread fingerprint; ambiguity is surfaced.
    return _fingerprint_case(
        db, tenant_key=tenant, chat_id=chat_id,
        device_refs=device_refs, symptoms=symptoms,
    )


def is_explicit_new_fault(text_value: str) -> bool:
    lowered = str(text_value or "").lower()
    return any(phrase in lowered for phrase in _EXPLICIT_NEW_CASE_PHRASES)
