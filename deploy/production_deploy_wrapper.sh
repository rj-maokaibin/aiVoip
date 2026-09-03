#!/usr/bin/env bash
set -Eeuo pipefail
umask 077

TARGET="${1:-}"
REPO="/home/github-runner/actions-runner/_work/aiVoip/aiVoip"
ENV_FILE="/etc/voip-ai/production.env"

[[ "$TARGET" =~ ^[0-9a-f]{40}$ ]] || {
    echo "ERROR: target must be a 40-char git SHA" >&2
    exit 2
}

[[ -d "$REPO/.git" ]] || {
    echo "ERROR: runner checkout missing: $REPO" >&2
    exit 2
}

git_safe() {
    git -c "safe.directory=$REPO" -C "$REPO" "$@"
}

HEAD="$(git_safe rev-parse HEAD)"
MASTER="$(git_safe rev-parse refs/remotes/origin/master)"

echo "TARGET=$TARGET"
echo "HEAD=$HEAD"
echo "ORIGIN_MASTER=$MASTER"

[[ "$HEAD" == "$TARGET" ]] || {
    echo "ERROR: checkout SHA mismatch" >&2
    exit 3
}
[[ "$MASTER" == "$TARGET" ]] || {
    echo "ERROR: target is not current origin/master" >&2
    exit 3
}
[[ -z "$(git_safe status --porcelain --untracked-files=no)" ]] || {
    echo "ERROR: tracked working tree is dirty" >&2
    exit 3
}

cd "$REPO"
python3 tools/source_manifest_gate.py

cleanup() {
    if [[ -d "$REPO/validation" ]]; then
        chown -R github-runner:github-runner "$REPO/validation" || true
    fi
}
trap cleanup EXIT

echo "PRODUCTION_WRAPPER_VERSION=source-controlled-v2"
./deploy/voip-ai --env "$ENV_FILE" --revision "$TARGET" deploy
./deploy/voip-ai --env "$ENV_FILE" --revision "$TARGET" verify
echo "PRODUCTION_DEPLOY_WRAPPER=PASS"
