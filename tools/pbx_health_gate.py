#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
from pathlib import Path

from app.automation.adapters.pbx.health import PbxHealthGate
from app.automation.adapters.pbx.profile import load_fusionpbx_profile


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only FusionPBX/FreeSWITCH PBX_READY gate"
    )
    parser.add_argument("--profile", default="profiles/pbx/fusionpbx_srv_v1.json")
    parser.add_argument("--output")
    args = parser.parse_args()
    profile = load_fusionpbx_profile(args.profile)
    result = PbxHealthGate(profile).check()
    payload = result.safe_dict()
    rendered = json.dumps(payload, indent=2, sort_keys=True)
    if args.output:
        path = Path(args.output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(rendered + "\n", encoding="utf-8")
    print(rendered)
    return 0 if result.ready else 2


if __name__ == "__main__":
    raise SystemExit(main())
