from __future__ import annotations

import copy
from datetime import datetime, timezone
from sqlalchemy import select

from app.automation.models import AutomationTestRun
from app.automation.registry import TestDefinition
from app.capture_v2.db_models import CaptureSession
from app.capture_v2.enums import CaptureHealth, CaptureSessionState
from app.capture_v2.repository.core import CaptureEventRepository
from app.contracts.enums import CaptureStage, CleanupStatus, EvidenceCompleteness, EvidenceSufficiency, ReproductionState
from app.core.ids import new_id
from app.db.models import ReproductionSession
from app.infrastructure.action_route import RunIntent


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AutomationAuthorityBindingError(RuntimeError):
    pass


def prepare_authority_bound_run(*, session_factory, device_id: str, worker_id: str, definition: TestDefinition) -> tuple[str, str]:
    """Create one TestRun plus one existing-schema CaptureSession sharing an id.

    This is the PR-A compatibility mapping for CaptureLeaseManager. It creates no
    producer, capture epoch, parallel lease table, or alternate fencing token.
    """
    run_id = new_id()
    with session_factory() as db:
        with db.begin():
            reproduction_template = db.scalar(
                select(ReproductionSession)
                .where(ReproductionSession.device_id == device_id)
                .order_by(ReproductionSession.created_at.desc())
                .limit(1)
            )
            if reproduction_template is None:
                raise AutomationAuthorityBindingError("AUTHORITY_REPRODUCTION_TEMPLATE_MISSING")
            capture_template = db.scalar(
                select(CaptureSession)
                .where(CaptureSession.device_id == device_id)
                .order_by(CaptureSession.created_at.desc())
                .limit(1)
            )
            if capture_template is None:
                raise AutomationAuthorityBindingError("AUTHORITY_CAPTURE_PROFILE_TEMPLATE_MISSING")

            reproduction = ReproductionSession(
                case_id=reproduction_template.case_id,
                device_id=device_id,
                profile_key=reproduction_template.profile_key,
                profile_version=reproduction_template.profile_version,
                profile_checksum=reproduction_template.profile_checksum,
                effective_profile_snapshot=copy.deepcopy(reproduction_template.effective_profile_snapshot),
                platform_profile_id=reproduction_template.platform_profile_id,
                platform_profile_version=reproduction_template.platform_profile_version,
                state=ReproductionState.CREATED.value,
                capture_stage=CaptureStage.BASE.value,
                cleanup_required=False,
                cleanup_status=CleanupStatus.NOT_REQUIRED.value,
                capture_completeness=EvidenceCompleteness.NOT_REQUIRED.value,
                evidence_sufficiency=EvidenceSufficiency.NOT_EVALUATED.value,
            )
            db.add(reproduction)
            db.flush()

            db.add(CaptureSession(
                id=run_id,
                reproduction_session_id=str(reproduction.id),
                device_id=device_id,
                state=CaptureSessionState.CREATED.value,
                health_status=CaptureHealth.HEALTHY.value,
                capture_profile_id=capture_template.capture_profile_id,
                capture_profile_version=capture_template.capture_profile_version,
                platform_profile_id=capture_template.platform_profile_id,
                platform_profile_version=capture_template.platform_profile_version,
                effective_profile=copy.deepcopy(capture_template.effective_profile),
                cleanup_status="AUTHORITY_ONLY",
            ))
            db.flush()
            CaptureEventRepository(db).append(
                capture_session_id=run_id,
                event_type="AUTOMATION_AUTHORITY_SESSION_CREATED",
                entity_type="AUTOMATION_DEVICE_AUTHORITY",
                entity_id=run_id,
                source_ts=utcnow(),
                payload={"authority": "CaptureLeaseManager", "producer_started": False},
            )

            db.add(AutomationTestRun(
                id=run_id,
                case_id=reproduction_template.case_id,
                suite_id=definition.case.suite_id,
                case_key=definition.case.case_id,
                case_version=definition.case.version,
                case_checksum=definition.checksum,
                intent=RunIntent.VERIFY.value,
                status="CREATED",
                environment_profile=definition.case.environment_profile,
                effective_plan_json={"case": definition.case.case_id, "authority": "capture_lease", "producer_started": False},
                worker_id=worker_id,
            ))
        return run_id, str(reproduction.id)


def record_authority_ref(*, session_factory, run_id: str, token) -> None:
    with session_factory() as db:
        with db.begin():
            row = db.get(AutomationTestRun, run_id)
            if row is None:
                raise AutomationAuthorityBindingError("AUTOMATION_TEST_RUN_NOT_FOUND")
            row.authority_ref = {
                "type": "capture_lease",
                "capture_session_id": token.capture_session_id,
                "device_id": token.device_id,
                "owner_worker_id": token.owner_worker_id,
                "lease_epoch": token.lease_epoch,
            }


def finalize_authority_bound_run(*, session_factory, run_id: str, reproduction_session_id: str, passed: bool) -> None:
    with session_factory() as db:
        with db.begin():
            capture = db.get(CaptureSession, run_id)
            if capture is not None:
                capture.state = CaptureSessionState.COMPLETED.value if passed else CaptureSessionState.FAILED.value
                capture.cleanup_status = "VERIFIED" if passed else "FAILED"
                capture.ended_at = utcnow()
            reproduction = db.get(ReproductionSession, reproduction_session_id)
            if reproduction is not None:
                reproduction.state = ReproductionState.COMPLETED.value if passed else ReproductionState.FAILED.value
