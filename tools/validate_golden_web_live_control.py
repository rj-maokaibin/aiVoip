#!/usr/bin/env python3
from __future__ import annotations

import re
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
    "test \"${lines[1]:-}\" = 'feat/generic-voip-automation-v1-pr-d-web-golden'",
    "test \"${lines[2]:-}\" = 'master'",
    "test \"${lines[3]:-}\" = 'open'",
    'Materialize exact authorized PR-D SHA',
    '/commits/${TARGET_SHA}',
    '/tarball/${TARGET_SHA}',
    'test "$actual" = "$TARGET_SHA"',
    'GOLDEN_WEB_EXACT_SOURCE_TRANSPORT=GITHUB_EXACT_SHA_ARCHIVE',
    'credential_transport=RUNNER_RUNTIME_ENV',
    'LIVE_ENV_FILE: /home/github-runner/.config/voip-ai/.env',
    'WEB_USERNAME_RUNTIME_REQUIRED',
    'WEB_PASSWORD_RUNTIME_REQUIRED',
    'no-automatic-secret-source.yaml',
    '--username-env WEB_USERNAME',
    '--password-env WEB_PASSWORD',
    'WEB_CREDENTIAL_RUNTIME_BINDING=PASS',
    'tools/resolve_current_web_credential_env.py',
    'tools/run_golden_web_config.py',
    "--target-number '7900'",
    '--registration-timeout 60',
    '--allow-live-mutation',
    "web_password=os.environ.get('WEB_PASSWORD','')",
    'GOLDEN_WEB_SECRET_AUDIT=PASS',
    'rm -rf "$LIVE_RUNTIME_ROOT" "$LIVE_VENV"',
)

REQUIRED_PATTERNS = (
    re.compile(r"test\s+\"\$\(stat -c ['\"]%a['\"] \"\$LIVE_ENV_FILE\"\)\"\s*=\s*['\"]600['\"]"),
    re.compile(r"printf\s+'%s'\s+\"\$requested\"\s*\|\s*grep -Eq '\^\[0-9a-f\]\{40\}\$'"),
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
    missing_patterns = [pattern.pattern for pattern in REQUIRED_PATTERNS if not pattern.search(text)]
    forbidden = [item for item in FORBIDDEN if item in text]
    if missing or missing_patterns or forbidden:
        raise SystemExit(
            'GOLDEN_WEB_LIVE_CONTROL_INVALID '
            f'missing={missing!r} missing_patterns={missing_patterns!r} forbidden={forbidden!r}'
        )
    print('GOLDEN_WEB_LIVE_CONTROL_CONTRACT=PASS')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
