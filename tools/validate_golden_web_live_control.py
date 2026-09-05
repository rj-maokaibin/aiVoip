#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path

WORKFLOW = Path('.github/workflows/golden-web-config-pr-live-v2.yml')

# Keep this validator deliberately focused on hard security/governance invariants.
# Quoting/layout details are exercised by Actions itself and must not create false
# negatives in the control-plane contract gate.
REQUIRED = (
    'github.event.issue.number == 151',
    'github.event.comment.user.login == github.repository_owner',
    '/run-golden-web-config ',
    'read -r command requested extra',
    'feat/generic-voip-automation-v1-pr-d-web-golden',
    'runs-on: [self-hosted, linux, x64, voip-controlled-linux]',
    'REAL_LIVE_MUTATION: EXPLICIT_ONLY',
    '/commits/${TARGET_SHA}',
    '/tarball/${TARGET_SHA}',
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
    'GOLDEN_WEB_SECRET_AUDIT=PASS',
    'WEB_ONLY_MUTATION_CONTRACT_FAILED',
    'FROZEN_IDENTITY_PRESERVATION_FAILED',
    'AUTHORITY_RELEASE_LAST_FAILED',
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
    if missing or forbidden:
        raise SystemExit(
            f'GOLDEN_WEB_LIVE_CONTROL_INVALID missing={missing!r} forbidden={forbidden!r}'
        )
    print('GOLDEN_WEB_LIVE_CONTROL_CONTRACT=PASS')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
