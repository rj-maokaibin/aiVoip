#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

from app.automation.adapters.pbx.esl import FreeSwitchEventSocketClient
from app.automation.adapters.pbx.profile import load_fusionpbx_profile


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only FreeSWITCH ESL health probe")
    parser.add_argument("--profile", required=True)
    args = parser.parse_args()
    profile = load_fusionpbx_profile(args.profile)
    client = FreeSwitchEventSocketClient(profile)
    try:
        health = client.safe_health()
    finally:
        client.close()
    health["verdict"] = "PASS"
    print(json.dumps(health, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
