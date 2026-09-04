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

recover_evidence_v2_acceptance() {
    local started_at="$1"
    local out="$REPO/validation/evidence_v2_production_acceptance.json"
    local in_container="/tmp/evidence-v2-production-acceptance.json"
    local cid rev mtime
    local -a candidates=()

    # Never manufacture acceptance evidence. Recover only a result written by
    # the current deploy attempt from an exact-revision backend container.
    mapfile -t backend_ids < <(docker ps -q \
        --filter 'label=com.docker.compose.service=backend')
    for cid in "${backend_ids[@]}"; do
        rev="$(docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$cid" 2>/dev/null \
            | awk -F= '$1=="BUILD_REVISION" {print substr($0,index($0,"=")+1); exit}')"
        [[ "$rev" == "$TARGET" ]] && candidates+=("$cid")
    done
    if [[ "${#candidates[@]}" -ne 1 ]]; then
        echo "EVIDENCE_V2_ACCEPTANCE_ARTIFACT_RECOVERY=SKIP reason=EXACT_BACKEND_COUNT count=${#candidates[@]}" >&2
        return 0
    fi

    cid="${candidates[0]}"
    mtime="$(docker exec "$cid" stat -c '%Y' "$in_container" 2>/dev/null || true)"
    if [[ ! "$mtime" =~ ^[0-9]+$ ]] || (( mtime < started_at )); then
        echo "EVIDENCE_V2_ACCEPTANCE_ARTIFACT_RECOVERY=SKIP reason=NO_CURRENT_ATTEMPT_RESULT" >&2
        return 0
    fi

    mkdir -p "$REPO/validation"
    rm -f "$out"
    docker cp "$cid:$in_container" "$out" >/dev/null 2>&1 || {
        echo "EVIDENCE_V2_ACCEPTANCE_ARTIFACT_RECOVERY=SKIP reason=DOCKER_CP_FAILED" >&2
        rm -f "$out"
        return 0
    }

    if ! python3 - "$out" "$TARGET" <<'PY'
import json
import sys
from pathlib import Path
path, target = sys.argv[1:]
payload = json.loads(Path(path).read_text(encoding="utf-8"))
assert payload.get("contract") == "evidence-v2-production-rollout-acceptance-v1", payload
assert payload.get("source_revision") == target, payload
assert payload.get("status") in {"PASS", "FAIL"}, payload
assert payload.get("stage") in {"SHADOW", "CANARY", "DEFAULT"}, payload
PY
    then
        echo "EVIDENCE_V2_ACCEPTANCE_ARTIFACT_RECOVERY=SKIP reason=EVIDENCE_BINDING_INVALID" >&2
        rm -f "$out"
        return 0
    fi

    echo "EVIDENCE_V2_ACCEPTANCE_ARTIFACT_RECOVERY=PASS revision=$TARGET"
}

echo "PRODUCTION_WRAPPER_VERSION=source-controlled-v2"
deploy_started_at="$(date +%s)"
set +e
./deploy/voip-ai --env "$ENV_FILE" --revision "$TARGET" deploy
deploy_rc="$?"
set -e
if [[ "$deploy_rc" -ne 0 ]]; then
    recover_evidence_v2_acceptance "$deploy_started_at"
    exit "$deploy_rc"
fi
echo "PRODUCTION_DEPLOY_WRAPPER=PASS verify_source=DEPLOY_RUNTIME_VERIFY"
