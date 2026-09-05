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
    "test \"${lines[1]:-}\" = 'feat/generic-voip-automation-v1-pr-d-web-golden'",
    "test \"${lines[2]:-}\" = 'master'",
    'Materialize exact authorized PR-D SHA',
    '"${api}/commits/${TARGET_SHA}"',
    '"${api}/tarball/${TARGET_SHA}"',
    'test "$actual" = "$TARGET_SHA"',
    'GOLDEN_WEB_EXACT_SOURCE_TRANSPORT=GITHUB_EXACT_SHA_ARCHIVE',
    'CREDENTIAL_TRANSFER_KEY:',
    'web-golden-credential-envelope.b64',
    'rsa_padding_mode:oaep',
    'rsa_oaep_md:sha256',
    'web-golden-user-credential-v1',
    'USER_WEB_USERNAME',
    'USER_WEB_PASSWORD',
    'no-automatic-secret-source.yaml',
    '--username-env USER_WEB_USERNAME',
    '--password-env USER_WEB_PASSWORD',
    'WEB_CREDENTIAL_USER_PROVIDED_BINDING=PASS',
    'tools/resolve_current_web_credential_env.py',
    'tools/run_golden_web_config.py',
    "--target-number '7900'",
    '--allow-live-mutation',
    "runtime/'user-web-credential.env'",
    'GOLDEN_WEB_SECRET_AUDIT=PASS',
    'rm -rf "$LIVE_RUNTIME_ROOT" "$LIVE_VENV"',
)

FORBIDDEN = (
    'pull_request_target:',
    'workflow_dispatch:',
    'secrets.',
    'HOST_SECRET_FILE:',
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
