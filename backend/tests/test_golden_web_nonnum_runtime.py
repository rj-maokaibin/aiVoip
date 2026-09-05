from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from app.automation.adapters.pbx.base import (
    PbxIdentityRequest,
    PbxProvisioningReceipt,
    PbxResourceKind,
    PbxVerification,
)
from app.automation.contracts import TestContractStatus, parse_test_case
from app.automation.gates.golden_web_nonnum import GoldenWebNonnumGate
from app.automation.orchestrator import AutomationRunContext
from app.automation.product_contracts.extension_identifier import load_extension_identifier_contract
from app.automation.registry import TestDefinition


ROOT = Path(__file__).resolve().parents[2]
CONTRACT_PATH = ROOT / "profiles/product_contracts/apf3260m_extension_identifier_v1.yaml"
CASE_PATH = ROOT / "profiles/tests/golden_web_nonnum_001.yaml"


class FakePbx:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.receipt = PbxProvisioningReceipt(
            provider="fake-pbx",
            resource_id="resource-1",
            kind=PbxResourceKind.ALIAS,
            extension="79+00.a",
            auth_id="Auth+01",
            evidence_refs=("pbx://provision",),
        )

    async def provision_identity(self, request, *, run_id: str):
        self.calls.append(("provision", run_id))
        assert request.extension == "79+00.a"
        return self.receipt

    async def verify_identity(self, receipt):
        self.calls.append(("verify", receipt.resource_id))
        return PbxVerification(
            matched=True,
            details={"state": "ready"},
            evidence_refs=("pbx://verify",),
        )

    async def delete_identity(self, receipt, *, run_id: str):
        self.calls.append(("delete", run_id))
        return PbxVerification(matched=True, details={"deleted": True})

    async def verify_deleted(self, receipt):
        self.calls.append(("verify_deleted", receipt.resource_id))
        return PbxVerification(matched=True, details={"absent": True})


def _definition(*, executable: bool = True) -> TestDefinition:
    raw = yaml.safe_load(CASE_PATH.read_text(encoding="utf-8"))
    case = parse_test_case(raw)
    if executable:
        case = replace(case, contract_status=TestContractStatus.ACTIVE)
    return TestDefinition(case=case, checksum="test", source_path=str(CASE_PATH))


def _request() -> PbxIdentityRequest:
    return PbxIdentityRequest(
        kind=PbxResourceKind.ALIAS,
        extension="79+00.a",
        auth_id="Auth+01",
        display_name=".79A0a",
        alias_target="existing-registration-identity",
    )


def _gate(
    *,
    capability_present: bool = True,
    pbx: FakePbx | None = None,
    executable: bool = True,
) -> GoldenWebNonnumGate:
    contract, _ = load_extension_identifier_contract(CONTRACT_PATH)
    return GoldenWebNonnumGate(
        definition=_definition(executable=executable),
        run_id="run-nonnum-1",
        device_id="dut-1",
        worker_id="worker-1",
        target_number="79+00.a",
        capability_present=capability_present,
        extension_contract=contract,
        pbx_request=_request(),
        pbx=pbx or FakePbx(),
        web=SimpleNamespace(),
        registration_probe=SimpleNamespace(),
        authority=SimpleNamespace(),
        session_factory=lambda: None,
    )


@pytest.mark.asyncio
async def test_nonnum_runtime_blocks_reserved_contract_before_reserve_or_pbx_provision() -> None:
    pbx = FakePbx()
    gate = _gate(pbx=pbx, executable=False)
    context = AutomationRunContext(
        run_id=gate.run_id,
        case=gate.case,
        worker_id=gate.worker_id,
    )
    result = await gate._precheck(context)
    assert result.ok is False
    assert result.reason == "WEB_NONNUM_CONTRACT_NOT_EXECUTABLE:RESERVED"
    assert pbx.calls == []


@pytest.mark.asyncio
async def test_nonnum_runtime_blocks_legacy_before_reserve_or_pbx_provision() -> None:
    pbx = FakePbx()
    gate = _gate(capability_present=False, pbx=pbx)
    context = AutomationRunContext(
        run_id=gate.run_id,
        case=gate.case,
        worker_id=gate.worker_id,
    )
    result = await gate._precheck(context)
    assert result.ok is False
    assert result.reason == "NONNUM_EXTENSION_CAPABILITY_REQUIRED"
    assert pbx.calls == []


@pytest.mark.asyncio
async def test_nonnum_runtime_provisions_and_verifies_pbx_in_provision_phase() -> None:
    pbx = FakePbx()
    gate = _gate(pbx=pbx)
    context = AutomationRunContext(
        run_id=gate.run_id,
        case=gate.case,
        worker_id=gate.worker_id,
    )
    precheck = await gate._precheck(context)
    assert precheck.ok is True

    await gate._provision(context)

    assert pbx.calls == [("provision", "run-nonnum-1"), ("verify", "resource-1")]
    assert gate.runtime["pbx_receipt"] == pbx.receipt
    evidence = context.evidence.get("pbx")
    assert evidence is not None
    assert evidence.data["provisioned"] is True
    assert evidence.data["extension"] == "79+00.a"
    assert evidence.evidence_refs == ("pbx://provision", "pbx://verify")


@pytest.mark.asyncio
async def test_nonnum_runtime_deletes_and_reverse_verifies_pbx_identity() -> None:
    pbx = FakePbx()
    gate = _gate(pbx=pbx)
    gate.runtime["pbx_receipt"] = pbx.receipt

    deleted = await gate._delete_pbx_action()
    verified, details = await gate._delete_pbx_verify()

    assert deleted["pbx_identity_created"] is True
    assert verified is True
    assert details["pbx_identity_deleted"] is True
    assert pbx.calls == [("delete", "run-nonnum-1"), ("verify_deleted", "resource-1")]


def test_nonnum_runtime_source_has_no_ssh_mutation_fallback() -> None:
    source = (ROOT / "backend/app/automation/gates/golden_web_nonnum.py").read_text(encoding="utf-8")
    assert "ConfigFrameworkExecutor" not in source
    assert ".config.set" not in source
    assert "config.set(" not in source
    assert "WEB_CONFIG_ROUTE" in source
