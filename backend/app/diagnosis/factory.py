from app.core.config import settings
from .reasoner import DeterministicDiagnosisReasoner
from .ai_runtime import AIRuntimePolicy


def get_diagnosis_reasoner():
    """Return the only formal DiagnosisDecision authority.

    Older builds could select ``HybridDiagnosisReasoner`` through
    ``DIAGNOSIS_REASONER=hybrid``.  That path allowed model output to be merged into
    formal hypothesis/plan objects and made promotion difficult to reason about.

    AI now runs only through the proposal/workbench sidecar governed by
    ``AIRuntimePolicy``.  The legacy setting is intentionally ignored for formal
    reasoning so upgrading an environment cannot accidentally re-enable model-owned
    DiagnosisDecision state.
    """
    _ = settings.diagnosis_reasoner  # retained for backwards-compatible config parsing
    _ = AIRuntimePolicy.from_settings(settings)
    return DeterministicDiagnosisReasoner()
