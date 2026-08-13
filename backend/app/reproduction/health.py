from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.contracts.enums import CaptureChannel, ChannelHealth
from app.db.models import CaptureChannelHealth, ReproductionSession
from app.reproduction.profile import ReproductionProfileDefinition


def _utcnow(): return datetime.now(timezone.utc)


@dataclass(frozen=True)
class CaptureHealthDecision:
    healthy: bool
    failed_required_channels: tuple[str, ...]
    observed: dict


class CaptureHealthMonitor:
    @classmethod
    def persist(cls, db: Session, *, session: ReproductionSession, profile: ReproductionProfileDefinition, observed: dict) -> CaptureHealthDecision:
        stage=next(x for x in profile.stages if x.stage.value==session.capture_stage)
        now=_utcnow(); failed=[]
        for channel in CaptureChannel:
            data=observed.get(channel.value) or {}
            row=db.scalar(select(CaptureChannelHealth).where(
                CaptureChannelHealth.session_id==session.id,CaptureChannelHealth.channel==channel.value))
            if not row:
                row=CaptureChannelHealth(session_id=session.id,channel=channel.value); db.add(row)
            row.status=str(data.get('status') or ChannelHealth.UNKNOWN.value)
            row.packet_count=int(data.get('packet_count',0) or 0)
            row.last_observed_at=now if data else row.last_observed_at
            row.health_json=data
            if channel in stage.required_channels and row.status!=ChannelHealth.HEALTHY.value:
                failed.append(channel.value)
        db.flush()
        return CaptureHealthDecision(not failed,tuple(sorted(failed)),observed)
