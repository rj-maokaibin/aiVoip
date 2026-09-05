#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

WORKFLOW = Path('.github/workflows/golden-web-config-pr-live-v2.yml')

REQUIRED = (
    'issue_comment:',
    'github.event.issue.number == 151',
    'github.event.comment.user.login == github.repository_owner',
    "startsWith(github.event.comment.body, '/run-golden-web-config ')",
    'runs-on: [self-hosted, linux, x64, voip-controlled-linux]',
    'REAL_LIVE_MUTATION: EXPLICIT_ONLY',
    'read -r command requested extra <<< "$TRIGGER_BODY"',
    "test \"${lines[0]:-}\" = \"$requested\"",
    "test \"${lines[1]:-}\" = 'feat/generic-voip-automation-v1-pr-d-web-golden'",
    "test \"${lines[2]:-}\" = 'master'",
    "test \"${lines[3]:-}\" = 'open'",
    'LIVE_ENV_FILE: /home/github-runner/.config/voip-ai/.env',
    'WEB_USERNAME_RUNTIME_REQUIRED',
    'WEB_PASSWORD_RUNTIME_REQUIRED',
    'no-automatic-secret-source.yaml',
    '--username-env WEB_USERNAME',
    '--password-env WEB_PASSWORD',
    'tools/resolve_current_web_credential_env.py',
    'tools/run_golden_web_config.py',
    "--target-number '7900'",
    '--registration-timeout 60',
    '--allow-live-mutation',
    "web_password=os.environ.get('WEB_PASSWORD','')",
    'GOLDEN_WEB_SECRET_AUDIT=PASS',
    'rm -rf "$LIVE_RUNTIME_ROOT" "$LIVE_VENV"',
)

ORDERED = (
    'Authorize exact current PR-D head',
    'Materialize exact authorized PR-D SHA',
    'Prepare isolated runtime and validate secret provider',
    'Resolve production DB and exactly one eligible DUT',
    'Resolve existing DUT SSH credential',
    'Prove runtime WEB credential read-only and bind to exact DUT',
    'Execute real Golden-WEB-CONFIG-001 with mandatory cleanup',
    'Audit evidence for raw secret leakage',
    'Upload immutable safe Golden evidence',
    'Remove runtime secrets',
)

FORBIDDEN = (
    'pull_request_target:',
    'workflow_dispatch:',
    'secrets.',
    'HOST_SECRET_FILE:',
    'CREDENTIAL_TRANSFER_KEY',
    'web-golden-credential-envelope',
    'rsa_padding_mode:oaep',
    'USER_WEB_USERNAME',
    'USER_WEB_PASSWORD',
    'config set voipUserInfo',
    'git reset --hard origin/master',
    'ref: feat/generic-voip-automation-v1-pr-d-web-golden',
)


def main() -> int:
    text = WORKFLOW.read_text(encoding='utf-8')
    missing = [item for item in REQUIRED if item not in text]
    forbidden = [item for item in FORBIDDEN if item in text]
    positions = [text.find(item) for item in ORDERED]
    order_ok = all(pos >= 0 for pos in positions) and positions == sorted(positions)
    if missing or forbidden or not order_ok:
        raise SystemExit(
            'GOLDEN_WEB_LIVE_CONTROL_INVALID '
            f'missing={missing!r} forbidden={forbidden!r} order_ok={order_ok}'
        )
    print('GOLDEN_WEB_LIVE_CONTROL_CONTRACT=PASS')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
