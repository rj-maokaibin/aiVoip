"""Preliminary Evidence Report V2 deterministic correctness layer.

V2 modules consume canonical analyzer facts and must not invent packet, call,
media, or root-cause facts. User-facing projection is intentionally kept out of
this package until the semantic validator and migration gates are complete.
"""

from .call_reconstruction import reconstruct_call_v2
from .timeline import build_timeline_v2

__all__ = ["reconstruct_call_v2", "build_timeline_v2"]
