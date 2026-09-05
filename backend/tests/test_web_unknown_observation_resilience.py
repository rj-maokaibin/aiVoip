from __future__ import annotations

import asyncio
import inspect
import json

from app.automation.adapters.entries import web as web_module
from app.automation.adapters.entries.web import WebEntryAdapter
from app.automation.adapters.web_auth.legacy_luci import LegacyLuciAuthError
from app.automation.adapters.web_profiles.schema import WebApiProfile
from app.automation.gates.golden_web_config_observed import ObservedGoldenWebConfigGate
from app.infrastructure.transport.http import (
    HttpEvidence,
    HttpMutationResultUnknown,
    HttpResponse,
)


def run(coro):
    return asyncio.run(coro)


def _profile() -> WebApiProfile:
    return WebApiProfile.from_mapping({
        "id": "unknown-observation-resilience",
        "auth_provider": "fake",
        "operations": {
            "voip.account.configure": {
                "endpoint": "/cmd",
                "method": "POST",
                "rpc_method": "voip.set",
                "mutation": True,
                "readback_operation": "voip.account.read",
            },
            "voip.account.read": {
                "endpoint": "/cmd",
                "method": "POST",
                "rpc_method": "voip.get",
                "mutation": False,
            },
        },
    })


def _response(body: dict, request_id: str = "read-1") -> HttpResponse:
    evidence = HttpEvidence(
        request_id=request_id,
        attempt=1,
        method="POST",
        path="/cmd",
        request={},
        response={"status_code": 200},
        elapsed_ms=1.0,
    )
    return HttpResponse(
        status_code=200,
        headers={},
        json_body=body,
        text=json.dumps(body),
        request_id=request_id,
        elapsed_ms=1.0,
        evidence=evidence,
    )


class RecoveringSession:
    def __init__(self, *, auth_failures: int):
        self.auth_failures = auth_failures
        self.auth_calls = 0
        self.invalidations = 0
        self.requests = []

    def invalidate(self):
        self.invalidations += 1

    async def ensure_session(self, *, force: bool = False):
        assert force is True
        self.auth_calls += 1
        if self.auth_calls <= self.auth_failures:
            raise LegacyLuciAuthError("LEGACY_LUCI_AUTH_PROTOCOL_REJECTED")
        return object()

    async def request(self, request, *, retry_policy=None):
        self.requests.append(request)
        if request.mutation:
            evidence = HttpEvidence(
                request_id="mut-1",
                attempt=1,
                method="POST",
                path="/cmd",
                request={},
                response=None,
                elapsed_ms=1.0,
                error="ReadTimeout",
            )
            raise HttpMutationResultUnknown(
                request_id="mut-1",
                evidence=evidence,
                cause=TimeoutError("unknown"),
            )
        return _response({"number": "7900", "disName": "7900"})


def test_unknown_save_retries_only_reauth_and_readback(monkeypatch) -> None:
    monkeypatch.setattr(web_module, "_UNKNOWN_OBSERVE_BACKOFF_SECONDS", (0.0, 0.0, 0.0))
    session = RecoveringSession(auth_failures=2)
    adapter = WebEntryAdapter(profile=_profile(), session_manager=session)  # type: ignore[arg-type]

    result = run(adapter.execute("voip.account.configure", {"number": "7900"}))

    assert result.unknown_result is True
    assert result.readback == {"number": "7900", "disName": "7900"}
    assert session.auth_calls == 3
    assert sum(1 for request in session.requests if request.mutation) == 1
    assert sum(1 for request in session.requests if not request.mutation) == 1
    assert session.invalidations >= 3


def test_unknown_save_auth_outage_degrades_to_unknown_without_second_mutation(monkeypatch) -> None:
    monkeypatch.setattr(web_module, "_UNKNOWN_OBSERVE_BACKOFF_SECONDS", (0.0, 0.0, 0.0))
    session = RecoveringSession(auth_failures=99)
    adapter = WebEntryAdapter(profile=_profile(), session_manager=session)  # type: ignore[arg-type]

    result = run(adapter.execute("voip.account.configure", {"number": "7900"}))

    assert result.unknown_result is True
    assert result.readback is None
    assert result.error == "HTTP_MUTATION_RESULT_UNKNOWN_OBSERVE_UNAVAILABLE"
    assert session.auth_calls == 3
    assert sum(1 for request in session.requests if request.mutation) == 1
    assert sum(1 for request in session.requests if not request.mutation) == 0


def test_observed_gate_polls_read_only_and_has_one_configure_call() -> None:
    configure_source = inspect.getsource(ObservedGoldenWebConfigGate._configure)
    observe_source = inspect.getsource(ObservedGoldenWebConfigGate._observe_unknown_target)

    assert configure_source.count("configure_voip_bundle") == 1
    assert '"retry_executed": False' in configure_source
    assert "configure_voip_bundle" not in observe_source
    assert "WEB_READ_ACTION" in observe_source
    assert "self.web.execute" in observe_source
