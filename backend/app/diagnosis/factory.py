from app.core.config import settings
from .reasoner import DeterministicDiagnosisReasoner
from .hybrid import HybridDiagnosisReasoner

def get_diagnosis_reasoner():
    mode=(settings.diagnosis_reasoner or 'deterministic').lower()
    # Shadow evaluation is intentionally a side channel. While it is enabled,
    # the formal DiagnosisDecision must remain deterministic.
    if settings.ai_shadow_enabled: return DeterministicDiagnosisReasoner()
    if mode=='hybrid': return HybridDiagnosisReasoner()
    return DeterministicDiagnosisReasoner()
