from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app.automation.adapters.pbx.esl import FreeSwitchRuntimeEvent
from app.automation.adapters.pbx.registration_esl import FreeSwitchRegistrationObserver
from app.automation.gates.golden_web_config import SipRegistrationEvidence


class FakeClient:
    def __init__(self, events):
        self.events = list(events)
        self.connected = False
        self.subscribed = False

    def connect(self):
        self.connected = True

    def subscribe(self):
        self.subscribed = True

    def read_event(self):
        if self.events:
            return self.events.pop(0)
        raise TimeoutError()

    def close(self):
        self.connected = False


class FakeFallback:
    def __init__(self, registered=False):
        self.registered = registered
        self.calls = 0

    def observe_registered_once(self, *, number: str):
        self.calls += 1
        return SipRegistrationEvidence(
            registered=self.registered,
            number=number,
            evidence_refs=(f"fallback://{number}",),
            details={"secret_values_emitted": False},
        )


def _profile():
    return SimpleNamespace(fs_cli_bin="fs_cli")


def test_esl_registration_observer_accepts_non_numeric_identity_event() -> None:
    client = FakeClient([
        FreeSwitchRuntimeEvent(
            kind="REGISTERED", raw_event_name="CUSTOM", subclass="sofia::register",
            identity="7900.a", domain="pbx.test",
        )
    ])
    observer = FreeSwitchRegistrationObserver(
        _profile(), client=client, fallback=FakeFallback(False)
    )
    result = asyncio.run(observer.wait_registered(
        number="7900.a", timeout_seconds=0.1, require_esl_event=True
    ))
    assert result.registered is True
    assert result.number == "7900.a"
    assert result.details["event_observed"] is True
    assert result.details["secret_values_emitted"] is False


def test_esl_registration_observer_allows_plus_identity_and_fallback_reconcile() -> None:
    observer = FreeSwitchRegistrationObserver(
        _profile(), client=FakeClient([]), fallback=FakeFallback(True)
    )
    result = asyncio.run(observer.wait_registered(number="+7900", timeout_seconds=0.1))
    assert result.registered is True
    assert result.details["event_observed"] is False
    assert result.details["fallback_observed"] is True
