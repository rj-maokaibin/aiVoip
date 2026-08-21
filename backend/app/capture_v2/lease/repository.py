from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.capture_v2.db_models import CaptureLease


class CaptureLeaseRepository:
    def __init__(self, db: Session):
        self.db = db

    def get_for_update(self, device_id: str) -> CaptureLease | None:
        return self.db.execute(
            select(CaptureLease).where(CaptureLease.device_id == device_id).with_for_update()
        ).scalar_one_or_none()
