"""Shared infrastructure for Diagnose / Verify / Reproduce.

This package is facade-first: production implementations remain owned by their
existing modules (notably collectors.asyncssh_adapter and capture_v2).
"""

from app.infrastructure.action_route import (
    ActionBackend,
    ActionEntry,
    ActionPurpose,
    ActionRoute,
    ActionTransport,
    RunIntent,
)

__all__ = [
    "ActionBackend",
    "ActionEntry",
    "ActionPurpose",
    "ActionRoute",
    "ActionTransport",
    "RunIntent",
]
