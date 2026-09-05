from __future__ import annotations

import copy
from typing import Any, Mapping

from app.automation.orchestrator import RuntimeBlocked
from app.automation.product_contracts.extension_identifier import (
    ExtensionIdentifierContract,
    effective_contract,
)
from app.automation.gates.golden_web_config import WEB_WRITABLE_MODULES


GOLDEN_WEB_NONNUM_CASE_ID = "Golden-WEB-NONNUM-001"
NONNUM_CLEANUP_ORDER = (
    "restore_web_voip_bundle",
    "delete_pbx_identity",
    "release_device_authority",
)


def _account_rows(value: Any) -> list[dict[str, Any]]:
    current = value
    for _ in range(3):
        if isinstance(current, list):
            if current and isinstance(current[0], dict):
                return current
            break
        if isinstance(current, Mapping) and "data" in current:
            current = current["data"]
            continue
        break
    raise RuntimeBlocked("WEB_VOIP_USER_ACCOUNT_REQUIRED")


def build_nonnum_probe(
    snapshot: Mapping[str, Any],
    target_number: str,
    *,
    contract: ExtensionIdentifierContract,
    capability_present: bool,
) -> dict[str, Any]:
    target = str(target_number)
    if not target:
        raise RuntimeBlocked("WEB_NONNUM_TARGET_REQUIRED")
    if target.isascii() and target.isdigit():
        raise RuntimeBlocked("WEB_NONNUM_TARGET_REQUIRED")
    if not capability_present:
        # Old DUTs remain digits-only. Do not attempt a speculative WEB mutation.
        raise RuntimeBlocked("NONNUM_EXTENSION_CAPABILITY_REQUIRED")

    resolved = effective_contract(contract, capability_present=True)
    validation = resolved.validate(target)
    if not validation.accepted:
        raise RuntimeBlocked(f"WEB_NONNUM_TARGET_INVALID:{validation.reason}")

    probe = copy.deepcopy(dict(snapshot))
    missing = [module for module in WEB_WRITABLE_MODULES if module not in probe]
    if missing:
        raise RuntimeBlocked(f"WEB_WRITABLE_SNAPSHOT_INCOMPLETE:{','.join(missing)}")

    rows = _account_rows(probe["voipUserInfo"])
    rows[0]["number"] = target
    rows[0]["disName"] = target
    return probe


def expected_nonnum_cleanup_order() -> tuple[str, ...]:
    """DUT state first, PBX temporary resource second, shared authority last."""

    return NONNUM_CLEANUP_ORDER
