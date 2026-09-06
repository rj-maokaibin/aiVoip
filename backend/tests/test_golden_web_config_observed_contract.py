from __future__ import annotations

import inspect

from app.automation.adapters.entries.web import WebEntryAdapter
from app.automation.gates.golden_web_config_observed import (
    ObservedGoldenWebConfigGate,
    _http_evidence_summary,
    observed_unknown_target,
)
from app.infrastructure.transport.http import HttpEvidence


def _readback(number: str, dis_name: str) -> dict:
    return {
        "code": 0,
        "error": None,
        "modules": {
            "voice_vlan": {"mode": "current"},
            "voipServInfo": {"server": "current"},
            "voipUserInfo": {
                "data": [{
                    "number": number,
                    "disName": dis_name,
                    "authId": "auth-separate",
                    "passwd": "***",
                }]
            },
            "voipFxsTbl": {"fxs": "current"},
            "voipRegState": {"state": "unknown"},
            "voipZoneInfo": {"zone": "current"},
            "voipAdvanced": {"advanced": "current"},
        },
    }


def test_unknown_readback_can_prove_exact_target_identity_without_secret_values() -> None:
    account = observed_unknown_target(_readback("7900", "7900"), target_number="7900")

    assert account == {
        "number": "7900",
        "disName": "7900",
        "authId": "auth-separate",
    }


def test_unknown_readback_does_not_guess_success_from_original_or_partial_identity() -> None:
    assert observed_unknown_target(_readback("7102", "7102"), target_number="7900") is None
    assert observed_unknown_target(_readback("7900", "7102"), target_number="7900") is None
    assert observed_unknown_target({}, target_number="7900") is None


def test_observed_unknown_gate_never_retries_web_mutation() -> None:
    configure_source = inspect.getsource(ObservedGoldenWebConfigGate._configure)
    observe_source = inspect.getsource(ObservedGoldenWebConfigGate._observe_unknown_target)

    # There is exactly one mutation call in the handler. UNKNOWN is resolved by
    # bounded read-only observation or remains INCONCLUSIVE; a second Save is forbidden.
    assert configure_source.count("configure_voip_bundle") == 1
    assert '"retry_executed": False' in configure_source
    assert "_observe_unknown_target" in configure_source
    assert "observed_unknown_target" in observe_source
    assert "WEB_READ_ACTION" in observe_source
    assert "asyncio.wait_for" in observe_source
    assert "_UNKNOWN_TARGET_OBSERVE_ATTEMPT_TIMEOUT_SECONDS" in observe_source
    assert "configure_voip_bundle" not in observe_source


def test_unknown_readback_forces_fresh_web_session_without_retrying_mutation() -> None:
    source = inspect.getsource(WebEntryAdapter._readback_after_unknown)

    assert "invalidate" in source
    assert "_request_operation(readback_op" in source
    assert "asyncio.wait_for" in source
    assert "_UNKNOWN_OBSERVE_ATTEMPT_TIMEOUT_SECONDS" in source
    assert "configure_voip_bundle" not in source


def test_observed_unknown_gate_continues_registration_only_after_target_observation() -> None:
    source = inspect.getsource(ObservedGoldenWebConfigGate._configure)
    finish_source = inspect.getsource(ObservedGoldenWebConfigGate._finish_from_account)

    assert "if account is None" in source
    assert "unknown_result=True" in source
    assert "wait_registered" in finish_source
    assert "mutation_effect_observed" in finish_source


def test_cleanup_restore_unknown_is_observed_without_mutation_retry() -> None:
    restore_source = inspect.getsource(ObservedGoldenWebConfigGate._restore_action)
    cleanup_read_source = inspect.getsource(ObservedGoldenWebConfigGate._cleanup_read)

    # Cleanup may Save the original snapshot at most once. If that Save is
    # UNKNOWN, only bounded read-only observation is allowed afterwards.
    assert restore_source.count("configure_voip_bundle") == 1
    assert "if restored.unknown_result" in restore_source
    assert "_cleanup_read" in restore_source
    assert "web_restore_effect_observed" in restore_source
    assert "configure_voip_bundle" not in cleanup_read_source
    assert "WEB_READ_ACTION" in cleanup_read_source
    assert "asyncio.wait_for" in cleanup_read_source
    assert "invalidate" in cleanup_read_source


def test_unknown_transport_evidence_summary_is_sanitized_and_actionable() -> None:
    evidence = HttpEvidence(
        request_id="req-123",
        attempt=1,
        method="POST",
        path="/cgi-bin/luci/api/xqsystem/login",
        request={"headers": {"Authorization": "***"}},
        response=None,
        elapsed_ms=15001.23456,
        error="ReadTimeout",
    )

    summary = _http_evidence_summary(evidence)

    assert summary == {
        "request_id": "req-123",
        "attempt": 1,
        "method": "POST",
        "path": "/cgi-bin/luci/api/xqsystem/login",
        "elapsed_ms": 15001.235,
        "status_code": None,
        "error": "ReadTimeout",
    }
    assert "request" not in summary
    assert "response" not in summary
