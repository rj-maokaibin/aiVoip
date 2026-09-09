from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from sqlalchemy import select

from app.automation.adapters.pbx.esl import FreeSwitchRuntimeEvent
from app.automation.adapters.pbx.resource_authority import (
    PbxExtensionLeaseManager,
    PbxLeaseToken,
)
from app.automation.pbx_models import PbxCallSession

_ACTIVE = {"CREATED", "ORIGINATING", "RINGING", "ANSWERED", "MEDIA"}
_TERMINAL = {"COMPLETED", "BUSY", "NO_ANSWER", "REJECTED", "FAILED", "TIMEOUT"}
_EVENT_STATE = {
    "CHANNEL_CREATE": "ORIGINATING",
    "RINGING": "RINGING",
    "EARLY_MEDIA": "RINGING",
    "ANSWERED": "ANSWERED",
    "DTMF": "MEDIA",
    "HANGUP": "COMPLETED",
}


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PbxCallSessionError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code

@dataclass(frozen=True)
class PbxCallSessionView:
    id: str
    run_id: str
    pbx_node_id: str
    state: str
    freeswitch_uuid: str | None
    caller_resource_id: str | None
    callee_resource_id: str | None
    hangup_cause: str | None

    @classmethod
    def from_row(cls, row: PbxCallSession) -> "PbxCallSessionView":
        return cls(
            id=row.id,
            run_id=row.run_id,
            pbx_node_id=row.pbx_node_id,
            state=row.state,
            freeswitch_uuid=row.freeswitch_uuid,
            caller_resource_id=row.caller_resource_id,
            callee_resource_id=row.callee_resource_id,
            hangup_cause=row.hangup_cause,
        )


class PbxCallSessionManager:
    """Persisted call lifecycle bound to active PBX extension authority."""

    def __init__(self, session_factory, *, authority: PbxExtensionLeaseManager) -> None:
        self.session_factory = session_factory
        self.authority = authority
    def _validate_token(self, token: PbxLeaseToken | None) -> None:
        if token is not None and not self.authority.validate(token):
            raise PbxCallSessionError("PBX_CALL_LEASE_FENCED")

    def create(
        self,
        *,
        run_id: str,
        callee: PbxLeaseToken,
        caller: PbxLeaseToken | None = None,
        direction: str = "OUTBOUND",
    ) -> PbxCallSessionView:
        self._validate_token(callee)
        self._validate_token(caller)
        if caller is not None and caller.pbx_node_id != callee.pbx_node_id:
            raise PbxCallSessionError("PBX_CALL_NODE_MISMATCH")
        value = str(direction or "").upper()
        if value not in {"OUTBOUND", "INBOUND", "PEER_TO_DUT", "DUT_TO_PEER"}:
            raise PbxCallSessionError("PBX_CALL_DIRECTION_INVALID")
        with self.session_factory() as session:
            row = PbxCallSession(
                run_id=str(run_id),
                pbx_node_id=callee.pbx_node_id,
                caller_resource_id=(caller.resource_id if caller else None),
                callee_resource_id=callee.resource_id,
                direction=value,
                state="CREATED",
                sip_evidence_refs_json=[],
                rtp_evidence_refs_json=[],
                dtmf_evidence_refs_json=[],
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return PbxCallSessionView.from_row(row)
    def bind_uuid(self, session_id: str, call_uuid: str) -> PbxCallSessionView:
        value = str(call_uuid or "").strip()
        if len(value) != 36:
            raise PbxCallSessionError("PBX_CALL_UUID_INVALID")
        with self.session_factory() as session:
            row = session.get(PbxCallSession, session_id)
            if row is None:
                raise PbxCallSessionError("PBX_CALL_SESSION_NOT_FOUND")
            if row.state not in _ACTIVE:
                raise PbxCallSessionError("PBX_CALL_SESSION_TERMINAL")
            if row.freeswitch_uuid and row.freeswitch_uuid != value:
                raise PbxCallSessionError("PBX_CALL_UUID_MISMATCH")
            row.freeswitch_uuid = value
            if row.state == "CREATED":
                row.state = "ORIGINATING"
            session.commit()
            return PbxCallSessionView.from_row(row)

    def apply_event(self, session_id: str, event: FreeSwitchRuntimeEvent) -> PbxCallSessionView:
        state = _EVENT_STATE.get(event.kind)
        if state is None:
            raise PbxCallSessionError("PBX_CALL_EVENT_UNSUPPORTED")
        with self.session_factory() as session:
            row = session.get(PbxCallSession, session_id)
            if row is None:
                raise PbxCallSessionError("PBX_CALL_SESSION_NOT_FOUND")
            if row.state in _TERMINAL:
                return PbxCallSessionView.from_row(row)
            if row.freeswitch_uuid and event.uuid and row.freeswitch_uuid != event.uuid:
                raise PbxCallSessionError("PBX_CALL_EVENT_UUID_MISMATCH")
            if not row.freeswitch_uuid and event.uuid:
                row.freeswitch_uuid = event.uuid
            row.state = state
            refs = list(row.sip_evidence_refs_json or [])
            refs.append({"kind": event.kind, "observed_at": event.observed_at})
            row.sip_evidence_refs_json = refs
            now = utcnow()
            if state == "RINGING" and row.ringing_at is None:
                row.ringing_at = now
            elif state == "ANSWERED" and row.answered_at is None:
                row.answered_at = now
            elif event.kind == "DTMF":
                dtmf = list(row.dtmf_evidence_refs_json or [])
                dtmf.append({"digit": event.dtmf_digit, "observed_at": event.observed_at})
                row.dtmf_evidence_refs_json = dtmf
            elif event.kind == "HANGUP":
                row.hangup_at = now
                row.hangup_cause = event.hangup_cause
            session.commit()
            return PbxCallSessionView.from_row(row)

    def record_rtp(self, session_id: str, stats: dict[str, int | bool]) -> PbxCallSessionView:
        safe = {
            str(key): int(value)
            for key, value in stats.items()
            if key != "secret_values_emitted" and isinstance(value, int) and not isinstance(value, bool)
        }
        with self.session_factory() as session:
            row = session.get(PbxCallSession, session_id)
            if row is None:
                raise PbxCallSessionError("PBX_CALL_SESSION_NOT_FOUND")
            refs = list(row.rtp_evidence_refs_json or [])
            refs.append(safe)
            row.rtp_evidence_refs_json = refs
            if row.state in {"ANSWERED", "MEDIA"} and safe:
                row.state = "MEDIA"
            session.commit()
            return PbxCallSessionView.from_row(row)

    def fail(self, session_id: str, *, state: str, cause: str) -> PbxCallSessionView:
        value = str(state or "").upper()
        if value not in _TERMINAL - {"COMPLETED"}:
            raise PbxCallSessionError("PBX_CALL_TERMINAL_STATE_INVALID")
        with self.session_factory() as session:
            row = session.get(PbxCallSession, session_id)
            if row is None:
                raise PbxCallSessionError("PBX_CALL_SESSION_NOT_FOUND")
            row.state = value
            row.hangup_at = utcnow()
            row.hangup_cause = str(cause or value)[:128]
            session.commit()
            return PbxCallSessionView.from_row(row)

    def active_for_resource(self, resource_id: str) -> tuple[PbxCallSessionView, ...]:
        with self.session_factory() as session:
            rows = session.execute(
                select(PbxCallSession).where(
                    (
                        (PbxCallSession.caller_resource_id == resource_id)
                        | (PbxCallSession.callee_resource_id == resource_id)
                    ),
                    PbxCallSession.state.in_(_ACTIVE),
                )
            ).scalars().all()
            return tuple(PbxCallSessionView.from_row(row) for row in rows)
