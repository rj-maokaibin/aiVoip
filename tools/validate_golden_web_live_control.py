#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

WORKFLOW = Path('.github/workflows/golden-web-config-pr-live-v2.yml')

REQUIRED = (
    'github.event.issue.number == 151',
    'github.event.comment.user.login == github.repository_owner',
    'read -r command requested extra',
    'feat/generic-voip-automation-v1-pr-d-web-golden',
    'LIVE_ENV_FILE: /home/github-runner/.config/voip-ai/.env',
    'WEB_PASSWORD_RUNTIME_REQUIRED',
    "--target-number '7900'",
    'AUTHORITY_RELEASE_LAST_FAILED',
)

FORBIDDEN = (
    '${{ secrets.',
    'pull_request_target:',
    'config set voipUserInfo',
    'git reset --hard origin/master',
)


def main() -> int:
    text = WORKFLOW.read_text(encoding='utf-8')
    missing = [item for item in REQUIRED if item not in text]
    forbidden = [item for item in FORBIDDEN if item in text]
    if missing or forbidden:
        raise SystemExit(
            f'GOLDEN_WEB_LIVE_CONTROL_INVALID missing={missing!r} forbidden={forbidden!r}'
        )
    print('GOLDEN_WEB_LIVE_CONTROL_CONTRACT=PASS')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
