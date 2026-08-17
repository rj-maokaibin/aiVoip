from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.core.config import settings
engine=create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal=sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)

# Every Case-owned state change committed through the application SessionLocal
# schedules a deterministic Golden Candidate refresh in a follow-up transaction.
# The listener is scoped to this factory, so isolated SQLAlchemy test sessions are
# unaffected.
from app.golden.auto import install_golden_candidate_session_hooks
install_golden_candidate_session_hooks(SessionLocal)
