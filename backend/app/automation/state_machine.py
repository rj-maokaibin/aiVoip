from __future__ import annotations

from enum import Enum


class AutomationStateError(RuntimeError):
    pass


class AutomationRunState(str, Enum):
    CREATED = "CREATED"
    PRECHECK = "PRECHECK"
    RESERVE = "RESERVE"
    SNAPSHOT = "SNAPSHOT"
    PROVISION = "PROVISION"
    ARM = "ARM"
    EXECUTE = "EXECUTE"
    ASSERT = "ASSERT"
    CLEANUP = "CLEANUP"
    VERIFY_CLEANUP = "VERIFY_CLEANUP"
    REPORT = "REPORT"
    PASSED = "PASSED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    INCONCLUSIVE = "INCONCLUSIVE"


TERMINAL_STATES = frozenset({
    AutomationRunState.PASSED,
    AutomationRunState.FAILED,
    AutomationRunState.BLOCKED,
    AutomationRunState.INCONCLUSIVE,
})

_NORMAL_NEXT = {
    AutomationRunState.CREATED: AutomationRunState.PRECHECK,
    AutomationRunState.PRECHECK: AutomationRunState.RESERVE,
    AutomationRunState.RESERVE: AutomationRunState.SNAPSHOT,
    AutomationRunState.SNAPSHOT: AutomationRunState.PROVISION,
    AutomationRunState.PROVISION: AutomationRunState.ARM,
    AutomationRunState.ARM: AutomationRunState.EXECUTE,
    AutomationRunState.EXECUTE: AutomationRunState.ASSERT,
    AutomationRunState.ASSERT: AutomationRunState.CLEANUP,
    AutomationRunState.CLEANUP: AutomationRunState.VERIFY_CLEANUP,
    AutomationRunState.VERIFY_CLEANUP: AutomationRunState.REPORT,
}


class AutomationStateMachine:
    """Enforces cleanup before every terminal outcome."""

    def __init__(self) -> None:
        self.state = AutomationRunState.CREATED
        self.history: list[AutomationRunState] = [self.state]

    def transition(self, target: AutomationRunState) -> AutomationRunState:
        if self.state in TERMINAL_STATES:
            raise AutomationStateError(f"TERMINAL_STATE:{self.state.value}")
        if self.state == AutomationRunState.REPORT:
            if target not in TERMINAL_STATES:
                raise AutomationStateError(f"REPORT_REQUIRES_TERMINAL:{target.value}")
        else:
            expected = _NORMAL_NEXT.get(self.state)
            if target != expected:
                raise AutomationStateError(
                    f"INVALID_TRANSITION:{self.state.value}->{target.value}"
                )
        self.state = target
        self.history.append(target)
        return target

    def advance(self) -> AutomationRunState:
        if self.state == AutomationRunState.REPORT:
            raise AutomationStateError("REPORT_REQUIRES_EXPLICIT_TERMINAL")
        target = _NORMAL_NEXT.get(self.state)
        if target is None:
            raise AutomationStateError(f"NO_NORMAL_NEXT:{self.state.value}")
        return self.transition(target)

    def to_cleanup(self) -> AutomationRunState:
        if self.state in TERMINAL_STATES:
            raise AutomationStateError(f"TERMINAL_STATE:{self.state.value}")
        if self.state in {
            AutomationRunState.CLEANUP,
            AutomationRunState.VERIFY_CLEANUP,
            AutomationRunState.REPORT,
        }:
            return self.state
        self.state = AutomationRunState.CLEANUP
        self.history.append(self.state)
        return self.state

    def finish(self, terminal: AutomationRunState) -> AutomationRunState:
        if terminal not in TERMINAL_STATES:
            raise AutomationStateError(f"NOT_TERMINAL:{terminal.value}")
        return self.transition(terminal)
