from __future__ import annotations

from app.automation.adapters.pbx.profile import FusionPbxLabProfile
from app.automation.adapters.pbx.runtime_read import FreeSwitchRuntimeReadProbe


def _profile() -> FusionPbxLabProfile:
    return FusionPbxLabProfile.from_dict({
        "schema_version": "pbx-lab-profile-v1",
        "node_key": "test-pbx",
        "host": "127.0.0.1",
        "fusionpbx_root": "/tmp/fusionpbx",
        "internal_port": 5060,
        "extension_pool": {"start": 7900, "end": 7999},
        "protected_extensions": ["7102"],
        "source_fence": {"version": "test-v1", "files": {"x": "0" * 64}},
    })


def test_registration_contact_distinguishes_active_from_recovery_row() -> None:
    outputs = {
        "sofia_contact 7900.a@pbx.test": "sofia/internal/sip:7900.a@127.0.0.1:5090",
        "sofia_contact 7901.a@pbx.test": "error/user_not_registered",
    }
    def runner(argv, _timeout):
        return 0, outputs[argv[-1]]
    probe = FreeSwitchRuntimeReadProbe(_profile(), runner=runner)
    assert probe.registration_contact_visible("7900.a", domain_name="pbx.test") is True
    assert probe.registration_contact_visible("7901.a", domain_name="pbx.test") is False


def test_wait_registration_absent_is_bounded_and_fail_closed() -> None:
    calls = 0
    def runner(_argv, _timeout):
        nonlocal calls
        calls += 1
        if calls < 3:
            return 0, "sofia/internal/sip:7900.a@127.0.0.1:5090"
        return 0, "error/user_not_registered"
    probe = FreeSwitchRuntimeReadProbe(_profile(), runner=runner)
    assert probe.wait_registration_absent(
        "7900.a", domain_name="pbx.test", timeout_seconds=0.05, poll_seconds=0.001
    ) is True

    stuck = FreeSwitchRuntimeReadProbe(
        _profile(), runner=lambda _argv, _timeout: (0, "sofia/internal/sip:7900.a@127.0.0.1:5090")
    )
    assert stuck.wait_registration_absent(
        "7900.a", domain_name="pbx.test", timeout_seconds=0.003, poll_seconds=0.001
    ) is False
