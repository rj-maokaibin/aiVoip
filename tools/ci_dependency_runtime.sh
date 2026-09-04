#!/usr/bin/env bash
# Shared CI dependency preparation for authoritative acceptance gates.
# This file is sourced by gate scripts; callers keep set -Eeuo pipefail.

CI_REPO_ROOT="${CI_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"

ci_now_ms() {
  python3 - <<'PY'
import time
print(time.time_ns() // 1_000_000)
PY
}

ci_perf_out() {
  local configured="${CICD_PERFORMANCE_V3_EVIDENCE:-validation/cicd_performance_v3.json}"
  if [[ "$configured" = /* ]]; then printf '%s\n' "$configured"; else printf '%s/%s\n' "$CI_REPO_ROOT" "$configured"; fi
}

ci_record_perf() {
  local phase="$1" status="$2" duration_ms="$3"
  shift 3
  local args=(--out "$(ci_perf_out)" record --phase "$phase" --status "$status" --duration-ms "$duration_ms")
  local item
  for item in "$@"; do args+=(--meta "$item"); done
  python3 "$CI_REPO_ROOT/tools/cicd_performance_v3.py" "${args[@]}"
}

ci_prepare_python_runtime() {
  local venv_dir="$1" requirements="$2"
  local requirements_path="$requirements"
  [[ "$requirements_path" = /* ]] || requirements_path="$CI_REPO_ROOT/$requirements_path"
  local start end duration key marker status cache_state
  start="$(ci_now_ms)"
  marker="$venv_dir/.voip-ai-dependency-key"
  key="$(python3 - "$requirements_path" <<'PY'
import hashlib, platform, sys
from pathlib import Path
p=Path(sys.argv[1])
data=p.read_bytes()
print(hashlib.sha256(data + platform.python_version().encode()).hexdigest())
PY
)"
  if [[ -x "$venv_dir/bin/python" && -f "$marker" && "$(cat "$marker")" == "$key" ]]; then
    # shellcheck disable=SC1090
    source "$venv_dir/bin/activate"
    cache_state=HIT
    status=PASS
  else
    rm -rf "$venv_dir"
    python3 -m venv "$venv_dir"
    # shellcheck disable=SC1090
    source "$venv_dir/bin/activate"
    cache_state=MISS
    local -a pip_args=(
      --disable-pip-version-check
      --retries "${VOIP_PIP_RETRIES:-1}"
      --timeout "${VOIP_PIP_TIMEOUT_SECONDS:-10}"
      -r "$requirements_path"
    )
    if [[ -n "${VOIP_PIP_PRIMARY_INDEX:-}" ]]; then
      pip_args=(--index-url "$VOIP_PIP_PRIMARY_INDEX" "${pip_args[@]}")
    fi
    if ! python -m pip install "${pip_args[@]}"; then
      local fallback="${VOIP_PIP_FALLBACK_INDEX:-}"
      if [[ -z "$fallback" || "$fallback" == "${VOIP_PIP_PRIMARY_INDEX:-}" ]]; then
        end="$(ci_now_ms)"; duration="$((end-start))"
        ci_record_perf python_dependency_prepare FAIL "$duration" "cache=$cache_state" "fallback=none" || true
        return 1
      fi
      python -m pip install --disable-pip-version-check --retries 1 --timeout "${VOIP_PIP_FALLBACK_TIMEOUT_SECONDS:-15}" --index-url "$fallback" -r "$requirements_path" || {
        end="$(ci_now_ms)"; duration="$((end-start))"
        ci_record_perf python_dependency_prepare FAIL "$duration" "cache=$cache_state" "fallback=$fallback" || true
        return 1
      }
    fi
    printf '%s\n' "$key" > "$marker"
    status=PASS
  fi
  end="$(ci_now_ms)"; duration="$((end-start))"
  ci_record_perf python_dependency_prepare "$status" "$duration" "cache=$cache_state" "dependency_key=$key"
}

ci_run_timed() {
  local phase="$1"
  shift
  local start end duration rc status
  start="$(ci_now_ms)"
  if "$@"; then rc=0; status=PASS; else rc=$?; status=FAIL; fi
  end="$(ci_now_ms)"; duration="$((end-start))"
  ci_record_perf "$phase" "$status" "$duration" || true
  return "$rc"
}

ci_npm_registry() {
  local configured="${VOIP_NPM_PRIMARY_REGISTRY:-}"
  if [[ -n "$configured" ]]; then printf '%s\n' "$configured"; return 0; fi
  npm config get registry
}

ci_npm_probe() {
  local registry="$1"
  local timeout_seconds="${VOIP_NPM_PROBE_TIMEOUT_SECONDS:-5}"
  curl --fail --silent --show-error --location --max-time "$timeout_seconds" "${registry%/}/-/ping" >/dev/null
}

ci_npm_ci() {
  local registry start end duration rc status
  registry="$(ci_npm_registry)"
  start="$(ci_now_ms)"
  if ! ci_npm_probe "$registry"; then
    end="$(ci_now_ms)"; duration="$((end-start))"
    ci_record_perf npm_registry_probe FAIL "$duration" "registry=$registry" || true
    return 1
  fi
  end="$(ci_now_ms)"; duration="$((end-start))"
  ci_record_perf npm_registry_probe PASS "$duration" "registry=$registry"

  start="$(ci_now_ms)"
  set +e
  timeout --signal=TERM --kill-after=5s "${VOIP_NPM_CI_TIMEOUT_SECONDS:-30}s" \
    npm ci --prefer-offline --no-audit --no-fund --registry "$registry"
  rc=$?
  set -e
  [[ "$rc" -eq 0 ]] && status=PASS || status=FAIL
  end="$(ci_now_ms)"; duration="$((end-start))"
  ci_record_perf npm_ci "$status" "$duration" "registry=$registry" "timeout_s=${VOIP_NPM_CI_TIMEOUT_SECONDS:-30}" || true
  return "$rc"
}

ci_npm_audit() {
  local registry start end duration rc status
  registry="$(ci_npm_registry)"
  start="$(ci_now_ms)"
  set +e
  timeout --signal=TERM --kill-after=5s "${VOIP_NPM_AUDIT_TIMEOUT_SECONDS:-30}s" \
    npm audit --audit-level=low --registry "$registry"
  rc=$?
  set -e
  [[ "$rc" -eq 0 ]] && status=PASS || status=FAIL
  end="$(ci_now_ms)"; duration="$((end-start))"
  ci_record_perf npm_audit "$status" "$duration" "registry=$registry" "timeout_s=${VOIP_NPM_AUDIT_TIMEOUT_SECONDS:-30}" || true
  return "$rc"
}
