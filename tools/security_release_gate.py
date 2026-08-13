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
    checks["ec02_partial_has_no_autonomous_actions"] = not bool(platform.autonomous_reproduction_actions)
    if not checks["ec02_partial_has_no_autonomous_actions"]:
        errors.append("Partial EC-02 platform must not expose autonomous reproduction actions")

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
    # A safe F1 build must explicitly block production for known incomplete security/platform integrations.
    checks["production_auth_gap_explicit"] = keys.get("PRODUCTION_AUTH_PROVIDER", {}).get("status") == "BLOCKED"
    checks["mock_platform_gap_explicit"] = keys.get("REAL_REPRODUCTION_PLATFORM", {}).get("status") == "BLOCKED"
    checks["feishu_live_gap_explicit"] = keys.get("FEISHU_LIVE_TRANSPORT", {}).get("status") == "BLOCKED"
    for key in ("production_auth_gap_explicit", "mock_platform_gap_explicit", "feishu_live_gap_explicit"):
        if not checks[key]:
            errors.append(f"expected explicit release blocker missing: {key}")

    payload = {"status": "PASS" if not errors else "FAIL", "checks": checks, "errors": errors}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if not errors else 1


if __name__ == "__main__":
    raise SystemExit(main())
