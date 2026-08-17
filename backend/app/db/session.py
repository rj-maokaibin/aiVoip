from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.core.config import settings
engine=create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal=sessionmaker(bind=engine, autocommit=False, autoflush=False, expire_on_commit=False)

# Install one global SQLAlchemy hook so every Case-owned state change (API, worker,
# reproduction, experiment, fix verification) automatically refreshes the persisted
# Golden Candidate assessment in the same transaction.  The hook is deterministic
# and explicitly excludes its own Audit/assessment writes to avoid recursion.
from app.golden.auto import install_golden_candidate_session_hooks
install_golden_candidate_session_hooks()
