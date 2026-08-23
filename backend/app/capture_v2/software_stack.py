from __future__ import annotations

from app.capture_v2.d_bridge import CaptureV2DSession
from app.capture_v2.e_bridge import CaptureV2ECoverageFinalizer
from app.capture_v2.f_bridge import CaptureV2FQualityReporter


class CaptureV2SoftwareStack:
    """A-F deterministic software services after C ownership/transfer bootstrap.

    This class intentionally has no device-specific live loop. Real-gate wiring is
    deferred while all pure/stateful software remains executable in CI.
    """

    def __init__(self, *, session_factory, capture_session_id: str, effective_profile: dict):
        self.capture_session_id = capture_session_id
        self.d = CaptureV2DSession(
            capture_session_id=capture_session_id,
            session_factory=session_factory,
            effective_profile=effective_profile,
        )
        self.e = CaptureV2ECoverageFinalizer(session_factory)
        self.f = CaptureV2FQualityReporter(session_factory)
