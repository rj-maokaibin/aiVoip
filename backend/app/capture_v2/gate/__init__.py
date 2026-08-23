"""Real-environment Gate tooling for Capture Engine V2.1.1.

The gate package is intentionally orchestration-only. It consumes the existing
Capture V2 ledgers/bridges and never relaxes production cutover guards.
"""

from app.capture_v2.gate.models import GateCaseResult, GateVerdict

__all__ = ["GateCaseResult", "GateVerdict"]
