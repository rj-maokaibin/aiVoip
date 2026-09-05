#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path


WORKFLOW = Path('.github/workflows/golden-web-config-pr-live-v3.yml')


def main() -> int:
    text = WORKFLOW.read_text(encoding='utf-8')
    required = (
        "github.event.issue.number == 151",
        "github.event.comment.user.login == github.repository_owner",
        "'/run-golden-web-config-v3 '",
        "cancel-in-progress: true",
        "REAL_LIVE_MUTATION: EXPLICIT_ONLY",
        "test \"${lines[3]:-}\" = 'open'",
        "tools/run_golden_web_config_observed.py",
        "ObservedGoldenWebConfigGate",
        "retry_executed\": False",
        "WEB_V3_CREDENTIAL_AND_DUT_BINDING=PASS",
        "SIP_ABA_SSH_PASSWORD",
        "WEB_PASSWORD",
        "RAW_PASSWORD_LEAK",
        "ssh_fallback",
        "release_last",
        "Remove runtime secrets",
        # The live workflow must retain its own exact-source scan for forbidden
        # alternate mutation implementations. Requiring those probe strings here
        # prevents this validator from falsely flagging the probes themselves.
        "TemporaryExtensionProvider|database->save|self\\.config\\.set\\(|devConfig\\.set",
    )
    missing = [item for item in required if item not in text]
    # Only patterns whose mere presence is forbidden belong here. Strings used by
    # runtime negative checks must not be listed as static forbidden tokens.
    forbidden = (
        '${{ secrets.',
        '/run-golden-web-config ',
    )
    found_forbidden = [item for item in forbidden if item in text]
    if missing or found_forbidden:
        print(f'GOLDEN_WEB_LIVE_CONTROL_V3_CONTRACT=FAIL missing={missing} forbidden={found_forbidden}')
        return 1
    print('GOLDEN_WEB_LIVE_CONTROL_V3_CONTRACT=PASS')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
