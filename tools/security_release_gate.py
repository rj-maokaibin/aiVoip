#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from app.actions.registry import ActionRegistry, RegistryError  # noqa: E402
from app.core.config import settings  # noqa: E402
from app.platforms.registry import PlatformProfileRegistry  # noqa: E402
from app.release_readiness import runtime_release_readiness  # noqa: E402


def main() -> int:
    errors: list[str] = []
    checks: dict[str, bool] = {}

    registry = ActionRegistry(ROOT / "profiles")
    try:
        registry.action("rm -rf /")
        checks["unknown_action_rejected"] = False
        errors.append("ActionRegistry accepted an arbitrary shell string")
    except RegistryError:
        checks["unknown_action_rejected"] = True

    platform = PlatformProfileRegistry(ROOT / "profiles").get("RUIJIE_VOIP_AIM_V1").definition
    checks["ec02_autonomous_contract_verified"] = bool(
        platform.autonomous_reproduction_actions
        and platform.production_ready_for("AUTONOMOUS_REPRODUCTION")
    )
    if not checks["ec02_autonomous_contract_verified"]:
        errors.append("EC-02 autonomous reproduction actions must have a production-ready platform contract")

    # High-level API/orchestration code must not bypass ActionRegistry/Adapter with direct shell execution.
    forbidden_hits: list[str] = []
    for base in [ROOT / "backend" / "app" / "api", ROOT / "backend" / "app" / "reproduction", ROOT / "backend" / "app" / "experiments"]:
        for path in base.rglob("*.py"):
            text = path.read_text(encoding="utf-8")
            for token in ("subprocess.run(", "os.system(", "create_subprocess_shell("):
                if token in text:
                    forbidden_hits.append(f"{path.relative_to(ROOT)}:{token}")
    checks["no_direct_shell_in_high_level_orchestration"] = not forbidden_hits
    if forbidden_hits:
        errors.append("high-level direct shell execution found: " + ", ".join(forbidden_hits))

    readiness = runtime_release_readiness(profile_root=ROOT / "profiles")
    keys = {x["key"]: x for x in readiness["items"]}
    # Implementation gates and runtime-configuration gates are separate: a
    # capability may be implemented while the current environment remains
    # intentionally unconfigured.  The readiness report must describe that
    # state consistently instead of preserving the old Phase-D1 blockers.
    checks["production_auth_gap_explicit"] = keys.get("PRODUCTION_AUTH_PROVIDER", {}).get("status") == "BLOCKED"
    platform_mode = str(settings.reproduction_platform_mode).lower()
    expected_platform_status = "BLOCKED" if platform_mode == "mock" else "PASS"
    checks["reproduction_platform_configuration_consistent"] = (
        keys.get("EC02_PLATFORM_PRODUCTION_READY", {}).get("status") == "PASS"
        and keys.get("REAL_REPRODUCTION_PLATFORM", {}).get("status") == expected_platform_status
    )
    checks["feishu_transport_implementation_verified"] = (
        keys.get("FEISHU_TRANSPORT_IMPLEMENTATION", {}).get("status") == "PASS"
        and keys.get("FEISHU_LIVE_TRANSPORT", {}).get("status") in {"PASS", "BLOCKED"}
    )
    for key in (
        "production_auth_gap_explicit",
        "reproduction_platform_configuration_consistent",
        "feishu_transport_implementation_verified",
    ):
        if not checks[key]:
            errors.append(f"security/readiness contract failed: {key}")

    payload = {"status": "PASS" if not errors else "FAIL", "checks": checks, "errors": errors}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
