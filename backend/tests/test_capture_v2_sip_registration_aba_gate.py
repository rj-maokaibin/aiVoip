from __future__ import annotations

import pytest

from app.capture_v2.errors import CaptureV2Error
from app.capture_v2.gate.sip_registration_aba import (
    FirewallRule,
    registrar_success_observed,
    select_healthy_registrar,
)


def _registration(*, status: str, ip: str = "192.0.2.10", port: int = 5060, code: int | None = 200):
    return {
        "status": status,
        "final_status_code": code,
        "ladder": [
            {
                "method": "REGISTER",
                "dst": f"{ip}:{port}",
            }
        ],
    }


def test_select_healthy_registrar_requires_exactly_one_endpoint():
    analysis = {"registrations": [_registration(status="SUCCESS")]}
    ip, port, row = select_healthy_registrar(analysis)
    assert (ip, port) == ("192.0.2.10", 5060)
    assert row["final_status_code"] == 200


@pytest.mark.parametrize(
    "registrations,error_code",
    [
        ([], "SIP_ABA_BASELINE_REGISTRAR_NOT_UNIQUE"),
        (
            [
                _registration(status="SUCCESS", ip="192.0.2.10"),
                _registration(status="SUCCESS", ip="192.0.2.11"),
            ],
            "SIP_ABA_BASELINE_REGISTRAR_NOT_UNIQUE",
        ),
    ],
)
def test_select_healthy_registrar_fails_closed(registrations, error_code):
    with pytest.raises(CaptureV2Error) as exc:
        select_healthy_registrar({"registrations": registrations})
    assert exc.value.code == error_code


def test_registration_with_multiple_register_destinations_is_ambiguous():
    row = _registration(status="SUCCESS")
    row["ladder"].append({"method": "REGISTER", "dst": "192.0.2.11:5060"})
    with pytest.raises(CaptureV2Error) as exc:
        select_healthy_registrar({"registrations": [row]})
    assert exc.value.code == "SIP_ABA_REGISTRAR_ENDPOINT_AMBIGUOUS"


def test_b_phase_success_detection_is_target_specific():
    analysis = {
        "registrations": [
            _registration(status="INCOMPLETE", ip="192.0.2.10", code=None),
            _registration(status="SUCCESS", ip="192.0.2.11"),
        ]
    }
    assert not registrar_success_observed(
        analysis,
        registrar_ip="192.0.2.10",
        registrar_port=5060,
    )
    assert registrar_success_observed(
        analysis,
        registrar_ip="192.0.2.11",
        registrar_port=5060,
    )


def test_firewall_rule_is_exact_non_persistent_output_drop():
    rule = FirewallRule(
        registrar_ip="192.0.2.10",
        registrar_port=5060,
        transport="udp",
        comment="AIVOIP_SIP_ABA_deadbeef",
    )
    insert = rule.insert_command()
    delete = rule.delete_command()
    check = rule.check_command()

    assert "iptables -w 5 -I OUTPUT 1" in insert
    assert "-p udp" in insert
    assert "-d 192.0.2.10" in insert
    assert "--dport 5060" in insert
    assert "-j DROP" in insert
    assert "AIVOIP_SIP_ABA_deadbeef" in insert
    assert "iptables -w 5 -D OUTPUT" in delete
    assert "iptables -w 5 -C OUTPUT" in check
    assert " -F " not in insert
    assert "iptables-save" not in insert
    assert "uci" not in insert


def test_firewall_rule_rejects_ipv6_and_bad_transport():
    with pytest.raises(CaptureV2Error) as exc:
        FirewallRule("2001:db8::1", 5060, "udp", "AIVOIP_SIP_ABA_x")
    assert exc.value.code == "SIP_ABA_IPV6_REGISTRAR_NOT_SUPPORTED"

    with pytest.raises(CaptureV2Error) as exc:
        FirewallRule("192.0.2.10", 5060, "sctp", "AIVOIP_SIP_ABA_x")
    assert exc.value.code == "SIP_ABA_TRANSPORT_INVALID"
