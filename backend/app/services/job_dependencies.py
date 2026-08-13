from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.contracts.enums import DependencyPolicy, JobStatus
from app.core.errors import AppError
from app.db.models import Job, JobDependency
from app.services.audit import audit


TERMINAL = {
    JobStatus.PARTIAL_SUCCESS,
    JobStatus.SUCCESS,
    JobStatus.FAILED,
    JobStatus.TIMEOUT,
    JobStatus.CANCELLED,
}


@dataclass(frozen=True)
class DependencyCheck:
    ready: bool
    blocking_job_ids: tuple[str, ...]
    failed_job_ids: tuple[str, ...]


def check_job_dependencies(db: Session, job_id: str) -> DependencyCheck:
    """Evaluate the frozen EC-01 dependency policies without mutating the job."""
    deps = list(db.scalars(select(JobDependency).where(JobDependency.job_id == job_id)))
    blocking: list[str] = []
    failed: list[str] = []
    for dep in deps:
        upstream = db.get(Job, dep.depends_on_job_id)
        if upstream is None:
            blocking.append(dep.depends_on_job_id)
            continue
        status = JobStatus(upstream.status)
        policy = DependencyPolicy(dep.policy)
        if policy == DependencyPolicy.OPTIONAL:
            continue
        if policy == DependencyPolicy.WAIT_ALL_SUCCESS:
            if status != JobStatus.SUCCESS:
                blocking.append(upstream.id)
                if status in {JobStatus.FAILED, JobStatus.TIMEOUT, JobStatus.CANCELLED, JobStatus.PARTIAL_SUCCESS}:
                    failed.append(upstream.id)
        elif policy == DependencyPolicy.WAIT_TERMINAL_ALLOW_PARTIAL:
            if status not in {JobStatus.SUCCESS, JobStatus.PARTIAL_SUCCESS}:
                blocking.append(upstream.id)
                if status in {JobStatus.FAILED, JobStatus.TIMEOUT, JobStatus.CANCELLED}:
                    failed.append(upstream.id)
    return DependencyCheck(not blocking, tuple(blocking), tuple(failed))


def _would_create_cycle(db: Session, job_id: str, depends_on_job_id: str) -> bool:
    # Edge is job -> depends_on. A cycle exists when upstream already (transitively) depends on job.
    frontier = [depends_on_job_id]
    seen: set[str] = set()
    while frontier:
        node = frontier.pop()
        if node == job_id:
            return True
        if node in seen:
            continue
        seen.add(node)
        frontier.extend(db.scalars(select(JobDependency.depends_on_job_id).where(JobDependency.job_id == node)).all())
    return False


def create_job_dependency(
    db: Session,
    *,
    job_id: str,
    depends_on_job_id: str,
    policy: DependencyPolicy | str,
    actor: str | None = None,
) -> JobDependency:
    if job_id == depends_on_job_id:
        raise AppError('JOB_DEPENDENCY_CYCLE', details={'job_id': job_id})
    job = db.get(Job, job_id)
    upstream = db.get(Job, depends_on_job_id)
    if not job or not upstream:
        raise AppError('JOB_NOT_FOUND', details={'job_id': job_id, 'depends_on_job_id': depends_on_job_id})
    if job.case_id != upstream.case_id:
        raise AppError('JOB_DEPENDENCY_CROSS_CASE', details={'job_id': job_id, 'depends_on_job_id': depends_on_job_id})
    if JobStatus(job.status) != JobStatus.PENDING:
        raise AppError('JOB_DEPENDENCY_JOB_NOT_PENDING', details={'job_id': job_id, 'status': job.status})
    if _would_create_cycle(db, job_id, depends_on_job_id):
        raise AppError('JOB_DEPENDENCY_CYCLE', details={'job_id': job_id, 'depends_on_job_id': depends_on_job_id})
    existing = db.scalar(select(JobDependency).where(JobDependency.job_id == job_id, JobDependency.depends_on_job_id == depends_on_job_id))
    if existing:
        if existing.policy != DependencyPolicy(policy).value:
            raise AppError('JOB_DEPENDENCY_CONFLICT', details={'existing_policy': existing.policy, 'requested_policy': DependencyPolicy(policy).value})
        return existing
    row = JobDependency(job_id=job_id, depends_on_job_id=depends_on_job_id, policy=DependencyPolicy(policy).value)
    db.add(row); db.flush()
    audit(
        db, case_id=job.case_id, actor=actor, event_type='JOB_DEPENDENCY_CREATED',
        target_type='job_dependency', target_id=row.id,
        detail={'job_id': job_id, 'depends_on_job_id': depends_on_job_id, 'policy': row.policy},
    )
    return row
