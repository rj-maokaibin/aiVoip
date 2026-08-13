from app.core.config import settings
from .reasoner import DeterministicDiagnosisReasoner
from .hybrid import HybridDiagnosisReasoner

def get_diagnosis_reasoner():
    mode=(settings.diagnosis_reasoner or 'deterministic').lower()
    if mode=='hybrid': return HybridDiagnosisReasoner()
    return DeterministicDiagnosisReasoner()
