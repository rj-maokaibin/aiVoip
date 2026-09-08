#!/usr/bin/env python3
from __future__ import annotations

# Reuse the exact PR-D live runtime/credential/authority/evidence plumbing while
# replacing only the gate implementation that resolves HTTP mutation UNKNOWN by
# its mandatory read-only observation. Importing the sibling module is stable
# because GitHub invokes this file as `python tools/run_golden_web_config_observed.py`.
from collections.abc import Mapping
from typing import Any

import run_golden_web_config as base

from app.automation.gates.golden_web_config import config_payload_from_web_module
from app.automation.gates.golden_web_config_observed import ObservedGoldenWebConfigGate
from app.infrastructure.config_framework.executor import ConfigFrameworkExecutor


class DiagnosticObservedGoldenWebConfigGate(ObservedGoldenWebConfigGate):
    """Persist a secret-free outcome for the SSH read-only UNKNOWN observation.

    The previous live run proved that WEB observation remained unavailable, but
    the evidence could not distinguish an SSH/config read failure from a valid
    readback that simply did not match the probe.  This override keeps the same
    read-only behavior and records only booleans plus an exception class name;
    no config payload, credential, cookie, token, or response body is persisted.
    """

    async def _observe_unknown_target_via_config(self) -> dict[str, Any] | None:
        diagnostic: dict[str, Any] = {
            "attempt": 1,
            "phase": "ssh_config_read_only",
            "success": False,
            "payload_match": False,
            "error": None,
        }
        self.runtime["sanitized_config_observation_diagnostics"] = [diagnostic]

        probe = self.runtime.get("probe")
        if not isinstance(probe, Mapping):
            diagnostic["error"] = "PROBE_MISSING"
            return None
        probe_user_info = probe.get("voipUserInfo")
        if probe_user_info is None:
            diagnostic["error"] = "PROBE_USER_INFO_MISSING"
            return None

        try:
            expected = config_payload_from_web_module(probe_user_info)
            current = await self.config.get("voipUserInfo")
        except Exception as exc:
            diagnostic["error"] = type(exc).__name__
            return None

        diagnostic["success"] = bool(current.success)
        matched = ConfigFrameworkExecutor.payload_matches_readback(expected, current)
        diagnostic["payload_match"] = bool(matched)
        if not matched:
            diagnostic["error"] = "CONFIG_READBACK_NOT_TARGET"
            return None

        rows = expected.get("data")
        if not isinstance(rows, list) or not rows or not isinstance(rows[0], Mapping):
            diagnostic["error"] = "EXPECTED_DATA_INVALID"
            return None
        row = rows[0]
        target = str(self.target_number)
        if str(row.get("number")) != target or str(row.get("disName")) != target:
            diagnostic["error"] = "EXPECTED_IDENTITY_NOT_TARGET"
            return None

        diagnostic["success"] = True
        diagnostic["payload_match"] = True
        diagnostic["error"] = None
        return {
            "number": row.get("number"),
            "disName": row.get("disName"),
            "authId": row.get("authId"),
        }


_original_safe_transport_diagnostics = base._safe_transport_diagnostics


def _safe_transport_diagnostics(gate):
    result = _original_safe_transport_diagnostics(gate)
    allowed = ("attempt", "phase", "success", "payload_match", "error")
    items = gate.runtime.get("sanitized_config_observation_diagnostics") or ()
    config_observation = []
    if isinstance(items, (list, tuple)):
        for item in items:
            if isinstance(item, dict):
                config_observation.append({key: item.get(key) for key in allowed})
    result["config_observation"] = config_observation
    return result


base._safe_transport_diagnostics = _safe_transport_diagnostics
base.GoldenWebConfigGate = DiagnosticObservedGoldenWebConfigGate


if __name__ == "__main__":
    raise SystemExit(base.main())
