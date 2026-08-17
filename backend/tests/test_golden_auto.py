from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db.base import Base
from app.db.golden_models import GoldenCandidateAssessment  # noqa: F401
from app.db.models import Case
from app.golden.auto import install_golden_candidate_session_hooks


def test_application_scoped_hook_materializes_assessment_after_commit():
    engine = create_engine(
        'sqlite+pysqlite:///:memory:',
        poolclass=StaticPool,
        connect_args={'check_same_thread': False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    install_golden_candidate_session_hooks(factory)

    with factory() as db:
        case = Case(case_no='AUTO-GC-1', summary='新建Case尚未上传证据', status='NEW')
        db.add(case)
        db.commit()
        case_id = case.id

    # The original commit has already returned; the hook has persisted the sidecar
    # in its own transaction without mutating/failing the business transaction.
    with factory() as db:
        row = db.scalar(select(GoldenCandidateAssessment).where(
            GoldenCandidateAssessment.case_id == case_id
        ))
        assert row is not None
        assert row.status == 'NOT_ELIGIBLE'
        assert 'NO_CASE_EVIDENCE' in (row.gap_codes or [])
