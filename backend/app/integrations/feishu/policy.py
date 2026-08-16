from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NotificationDecision:
    update_card: bool
    notify: bool
    priority: str
    reason: str


class FeishuNotificationPolicy:
    """Frozen SPEC-29 notification behavior, independent of transport credentials."""

    ALWAYS_UPDATE = {
        'CASE_STATE_CHANGED', 'DIAGNOSIS_UPDATED', 'REPRODUCTION_STATE_CHANGED',
        'REPRODUCTION_ARM_VALIDATED', 'REPRODUCTION_ATTEMPT_CHANGED', 'REPRODUCTION_CALL_CHANGED',
        'FXS_MONITOR_READY', 'FXS_MONITOR_FAILED',
        'TARGET_CONFIRMED', 'CLEANUP_ALERT', 'DIAGNOSTIC_QUESTION_CHANGED', 'EXPERIMENT_RUN_CHANGED',
        'CAUSAL_ASSESSMENT_UPDATED', 'ROOT_CAUSE_CAUSALLY_CONFIRMED', 'FIX_VERIFICATION_UPDATED',
        'REPORT_READY',
    }

    def decide(self, event_type: str, payload: dict[str, Any] | None = None) -> NotificationDecision:
        payload = payload or {}
        update = event_type in self.ALWAYS_UPDATE
        if event_type == 'TARGET_CONFIRMED':
            return NotificationDecision(update, True, 'HIGH', 'target_fault_captured')
        if event_type == 'CLEANUP_ALERT':
            return NotificationDecision(update, True, 'CRITICAL', 'diagnostic_cleanup_alert')
        if event_type == 'FXS_MONITOR_READY':
            return NotificationDecision(update, True, 'NORMAL', 'fxs_monitor_ready')
        if event_type == 'FXS_MONITOR_FAILED':
            return NotificationDecision(update, True, 'CRITICAL', 'fxs_monitor_failed')
        if event_type == 'ROOT_CAUSE_CAUSALLY_CONFIRMED':
            return NotificationDecision(update, True, 'HIGH', 'root_cause_confirmed')
        if event_type == 'REPRODUCTION_ARM_VALIDATED' and str(payload.get('status', '')).upper() in {'PASSED','ARMED','READY'}:
            return NotificationDecision(update, True, 'NORMAL', 'reproduction_armed')
        if event_type == 'REPRODUCTION_STATE_CHANGED':
            state = str(payload.get('state') or payload.get('to_state') or '').upper()
            if state in {'FAILED','ARM_FAILED','CLEANUP_FAILED'}:
                return NotificationDecision(update, True, 'HIGH', f'reproduction_{state.lower()}')
            if state in {'EXHAUSTED'}:
                return NotificationDecision(update, True, 'NORMAL', 'reproduction_exhausted')
            return NotificationDecision(update, False, 'SILENT', 'routine_reproduction_state')
        if event_type == 'FIX_VERIFICATION_UPDATED':
            state = str(payload.get('status') or '').upper()
            if state in {'FIX_VERIFIED','FIX_FAILED','FIX_REGRESSION'}:
                return NotificationDecision(update, True, 'HIGH' if state!='FIX_VERIFIED' else 'NORMAL', state.lower())
        # Ordinary Call/Attempt updates are deliberately silent to prevent message storms.
        return NotificationDecision(update, False, 'SILENT', 'card_update_only' if update else 'not_relevant')
