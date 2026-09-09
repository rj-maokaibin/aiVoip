#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import uuid

from app.automation.adapters.pbx.esl import FreeSwitchEventSocketClient
from app.automation.adapters.pbx.profile import load_fusionpbx_profile


def main() -> int:
    parser = argparse.ArgumentParser(description="FreeSWITCH ESL custom event round-trip probe")
    parser.add_argument("--profile", required=True)
    args = parser.parse_args()
    profile = load_fusionpbx_profile(args.profile)
    probe_id = uuid.uuid4().hex
    listener = FreeSwitchEventSocketClient(profile, timeout_seconds=5)
    sender = FreeSwitchEventSocketClient(profile, timeout_seconds=5)
    try:
        listener.connect()
        listener.subscribe(("CUSTOM", "aivoip::probe"))
        sender.connect()
        sender.send_custom_probe_event(probe_id)
        event = None
        for _ in range(20):
            candidate = listener.read_event()
            if candidate is not None and candidate.kind == "PROBE" and candidate.probe_id == probe_id:
                event = candidate
                break
        evidence = {
            "schema": "freeswitch-esl-event-probe-v1",
            "connected": True,
            "subscribed": True,
            "probe_event_received": event is not None,
            "event": event.safe_dict() if event is not None else None,
            "secret_values_emitted": False,
        }
        evidence["verdict"] = "PASS" if event is not None else "FAIL"
        print(json.dumps(evidence, sort_keys=True))
        return 0 if event is not None else 1
    finally:
        sender.close()
        listener.close()


if __name__ == "__main__":
    raise SystemExit(main())
