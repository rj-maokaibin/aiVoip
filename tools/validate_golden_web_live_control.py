#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

WORKFLOW = Path('.github/workflows/golden-web-config-pr-live.yml')

REQUIRED = (
    'issue_comment:',
    'github.event.issue.number == 151',
    'github.event.comment.user.login == github.repository_owner',
    "startsWith(github.event.comment.body, '/run-golden-web-config ')",
    'runs-on: [self-hosted, linux, x64, voip-controlled-linux]',
    'REAL_LIVE_MUTATION: EXPLICIT_ONLY',
    "test \"$head_ref\" = 'feat/generic-voip-automation-v1-pr-d-web-golden'",
    "test \"$base_ref\" = 'master'",
    'tools/resolve_current_web_credential_env.py',
    'tools/run_golden_web_config.py',
    "--target-number '7900'",
    '--allow-live-mutation',
    'GOLDEN_WEB_SECRET_AUDIT=PASS',
    'rm -rf "$LIVE_RUNTIME_ROOT" "$LIVE_VENV"',
)

FORBIDDEN = (
    'pull_request_target:',
    'workflow_dispatch:',
    'secrets.',
    'config set voipUserInfo',
    'devConfig.set',
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
