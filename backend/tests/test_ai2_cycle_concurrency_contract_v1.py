from __future__ import annotations

import pytest

from app.core.config import settings
from app.diagnosis.ai_cycle import AIDiagnosticCycleService
from app.diagnosis.ai_runtime import AIPromotionStage, AIRuntimePolicy


class _StopAfterFirstScalar(RuntimeError):
    pass


class _RecordingSession:
    def __init__(self):
        self.first_stmt = None

    def scalar(self, stmt):
        self.first_stmt = stmt
        raise _StopAfterFirstScalar()


def test_cycle_creation_acquires_case_row_lock_before_snapshot(monkeypatch):
    monkeypatch.setattr(settings, "ai_diagnostic_loop_enabled", True)
    db = _RecordingSession()
    runtime = AIRuntimePolicy(stage=AIPromotionStage.SHADOW)
    service = AIDiagnosticCycleService(runtime=runtime, snapshot_builder=object())

    with pytest.raises(_StopAfterFirstScalar):
        service.run_next(db, case_id="case-concurrency")

    assert db.first_stmt is not None
    assert getattr(db.first_stmt, "_for_update_arg", None) is not None
    sql = str(db.first_stmt.compile(compile_kwargs={"literal_binds": True}))
    assert "cases" in sql.lower()
