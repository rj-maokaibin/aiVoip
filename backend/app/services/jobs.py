from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.contracts.enums import JobStatus
from app.core.errors import AppError
from app.db.models import Job, JobStateHistory
from app.services.audit import audit
from app.services.job_dependencies import check_job_dependencies

_ALLOWED = {
    JobStatus.PENDING: {JobStatus.RUNNING, JobStatus.CANCEL_REQUESTED, JobStatus.CANCELLED, JobStatus.FAILED, JobStatus.TIMEOUT, JobStatus.WAITING_EVIDENCE, JobStatus.WAITING_USER},
    JobStatus.RUNNING: {JobStatus.PARTIAL_SUCCESS, JobStatus.SUCCESS, JobStatus.FAILED, JobStatus.TIMEOUT, JobStatus.CANCEL_REQUESTED, JobStatus.WAITING_EVIDENCE, JobStatus.WAITING_USER},
    JobStatus.WAITING_EVIDENCE: {JobStatus.RUNNING, JobStatus.WAITING_USER, JobStatus.CANCEL_REQUESTED, JobStatus.FAILED, JobStatus.TIMEOUT},
    JobStatus.WAITING_USER: {JobStatus.RUNNING, JobStatus.CANCEL_REQUESTED, JobStatus.FAILED, JobStatus.TIMEOUT},
    JobStatus.CANCEL_REQUESTED: {JobStatus.CANCELLED, JobStatus.FAILED},
    JobStatus.PARTIAL_SUCCESS: set(), JobStatus.SUCCESS: set(), JobStatus.FAILED: set(), JobStatus.TIMEOUT: set(), JobStatus.CANCELLED: set(),
}


def transition_job(
    db: Session,
    job: Job,
    target: JobStatus | str,
    *,
    reason: str,
    actor: str | None = None,
    cleanup_verified: bool = False,
    detail: dict | None = None,
) -> Job:
    target = JobStatus(target)
    current = JobStatus(job.status)
    if current == target:
        return job
    if target not in _ALLOWED.get(current, set()):
        raise AppError("JOB_TRANSITION_NOT_ALLOWED", details={"from": current.value, "to": target.value})
    if target == JobStatus.CANCELLED and not cleanup_verified:
        raise AppError("CANCEL_CLEANUP_REQUIRED")
    if target == JobStatus.RUNNING:
        dep = check_job_dependencies(db, job.id)
        if not dep.ready:
            raise AppError(
                "DEPENDENCY_NOT_SATISFIED",
                details={"job_id": job.id, "blocking_job_ids": list(dep.blocking_job_ids), "failed_job_ids": list(dep.failed_job_ids)},
            )
    now = datetime.now(timezone.utc)
    old = current.value
    job.status = target.value
    if target == JobStatus.RUNNING:
        job.started_at = job.started_at or now
    if target in {JobStatus.PARTIAL_SUCCESS, JobStatus.SUCCESS, JobStatus.FAILED, JobStatus.TIMEOUT, JobStatus.CANCELLED}:
        job.finished_at = now
    db.add(JobStateHistory(
        job_id=job.id,
        case_id=job.case_id,
        from_status=old,
        to_status=target.value,
        actor=actor,
        reason=reason,
        detail_json=detail or {},
    ))
    audit(
        db,
        case_id=job.case_id,
        actor=actor,
        event_type="JOB_STATE_CHANGED",
        target_type="job",
        target_id=job.id,
        before={"status": old},
        after={"status": target.value},
        reason=reason,
        detail={"from": old, "to": target.value, "reason": reason, **(detail or {})},
    )
    db.flush()
    return job
