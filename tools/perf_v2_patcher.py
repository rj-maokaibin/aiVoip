#!/usr/bin/env python3
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def replace_once(text: str, old: str, new: str, label: str) -> str:
    if old not in text:
        raise SystemExit(f"patch anchor missing: {label}")
    return text.replace(old, new, 1)


def patch_deploy_cli() -> None:
    path = ROOT / "deploy" / "voip-ai"
    text = path.read_text(encoding="utf-8")
    if "CICD_PERFORMANCE_V2_EVIDENCE" in text:
        return

    text = replace_once(
        text,
        '  VOIP_APP_ENV_FILE="$ENV_FILE" docker compose \\\n',
        '  DOCKER_BUILDKIT="${DOCKER_BUILDKIT:-1}" COMPOSE_DOCKER_CLI_BUILD="${COMPOSE_DOCKER_CLI_BUILD:-1}" \\\n'
        '    VOIP_APP_ENV_FILE="$ENV_FILE" docker compose \\\n',
        "compose-buildkit",
    )

    anchor = """if [[ "$PROJECT_FROM_CLI" != "1" ]]; then
  PROJECT="$(env_value VOIP_PROJECT_NAME 2>/dev/null || printf '%s' "$PROJECT")"
fi
"""
    helpers = """if [[ "$PROJECT_FROM_CLI" != "1" ]]; then
  PROJECT="$(env_value VOIP_PROJECT_NAME 2>/dev/null || printf '%s' "$PROJECT")"
fi

# CICD_PERFORMANCE_V2_EVIDENCE: timing is evidence-only; every existing gate remains fail-closed.
PERF_TIMING_LOG=""
PERF_EVIDENCE="${VOIP_CICD_PERF_EVIDENCE:-validation/cicd_performance_v2.json}"

perf_reset() {
  mkdir -p validation
  PERF_TIMING_LOG="$(mktemp)"
  : > "$PERF_TIMING_LOG"
}

perf_write_timing() {
  local revision
  revision="$(env_value BUILD_REVISION 2>/dev/null || echo unknown)"
  python3 - "$PERF_TIMING_LOG" "$PERF_EVIDENCE" "$revision" <<'PYTIMING'
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

log_path = Path(sys.argv[1])
out_path = Path(sys.argv[2])
revision = sys.argv[3]
phases = []
for raw in log_path.read_text(encoding="utf-8").splitlines():
    if not raw.strip():
        continue
    name, status, duration_ms = raw.split("\\t", 2)
    phases.append({"phase": name, "status": status, "duration_ms": int(duration_ms)})
payload = {
    "schema_version": "cicd-performance-v2",
    "created_at": datetime.now(timezone.utc).isoformat(),
    "build_revision": revision,
    "status": "PASS" if phases and all(x["status"] == "PASS" for x in phases) else "FAIL",
    "total_duration_ms": sum(x["duration_ms"] for x in phases),
    "phases": phases,
}
out_path.parent.mkdir(parents=True, exist_ok=True)
out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\\n", encoding="utf-8")
print(f"CICD_PERFORMANCE_V2_EVIDENCE={out_path} total_ms={payload['total_duration_ms']} status={payload['status']}")
PYTIMING
}

perf_phase() {
  local name="$1" start_ns end_ns duration_ms rc status
  shift
  start_ns="$(date +%s%N)"
  if "$@"; then
    rc=0
    status=PASS
  else
    rc=$?
    status=FAIL
  fi
  end_ns="$(date +%s%N)"
  duration_ms="$(( (end_ns - start_ns) / 1000000 ))"
  printf '%s\\t%s\\t%s\\n' "$name" "$status" "$duration_ms" >> "$PERF_TIMING_LOG"
  perf_write_timing
  echo "PERF_PHASE name=$name status=$status duration_ms=$duration_ms"
  return "$rc"
}
"""
    text = replace_once(text, anchor, helpers, "timing-helpers")

    text = replace_once(
        text,
        """build_images() {
  host_preflight
""",
        """build_images() {
  if [[ "${1:-}" != "--preflight-done" ]]; then
    host_preflight
  fi
""",
        "build-preflight-dedup",
    )

    registry_anchor = """  audit_file="${VOIP_OFFLINE_BUILD_AUDIT:-$release_root/offline-build-fallback.json}"
  mkdir -p "$release_root"

  # Online pull is always preferred. Offline fallback is allowed only for a
"""
    registry_block = """  audit_file="${VOIP_OFFLINE_BUILD_AUDIT:-$release_root/offline-build-fallback.json}"
  mkdir -p "$release_root"

  # Fast registry/mirror probe. Offline mode is allowed only by the audited
  # local-image inventory guard; auth/config/semantic failures remain BLOCKED.
  local probe_log probe_image probe_timeout probe_rc
  probe_log="$release_root/registry-probe.log"
  probe_image="${VOIP_REGISTRY_PROBE_IMAGE:-python:3.12-slim}"
  probe_timeout="${VOIP_REGISTRY_PROBE_TIMEOUT_SECONDS:-12}"
  set +e
  timeout "${probe_timeout}s" docker pull "$probe_image" 2>&1 | tee "$probe_log"
  probe_rc="${PIPESTATUS[0]}"
  set -e
  if [[ "$probe_rc" != "0" ]]; then
    if [[ "$probe_rc" == "124" ]]; then
      echo "registry probe timeout image=$probe_image timeout_seconds=$probe_timeout" | tee -a "$probe_log"
    else
      echo "registry probe failed image=$probe_image exit_code=$probe_rc" | tee -a "$probe_log"
    fi
    echo "REGISTRY_PREFLIGHT=FAIL; evaluating audited offline fallback immediately..."
    if python3 deploy/offline_build_fallback.py \\
        --build-log "$probe_log" \\
        --online-exit-code "$probe_rc" \\
        --audit "$audit_file"; then
      echo "OFFLINE_IMAGE_FALLBACK=AUTHORIZED source=REGISTRY_PREFLIGHT audit=$audit_file"
      BUILDKIT_PROGRESS="${BUILDKIT_PROGRESS:-plain}" compose build --pull=false --build-arg "BUILD_REVISION=$(env_value BUILD_REVISION)" "${services[@]}"
      echo "PRODUCTION_IMAGE_BUILD=PASS mode=OFFLINE_LOCAL_INVENTORY audit=$audit_file"
      return 0
    fi
    echo "PRODUCTION_IMAGE_BUILD=BLOCKED reason=REGISTRY_PREFLIGHT online_exit_code=$probe_rc audit=$audit_file" >&2
    return "$probe_rc"
  fi
  echo "REGISTRY_PREFLIGHT=PASS mode=ONLINE_PULL probe_image=$probe_image"

  # Online pull is always preferred. Offline fallback is allowed only for a
"""
    text = replace_once(text, registry_anchor, registry_block, "registry-probe")

    text = replace_once(
        text,
        '  compose build --pull --build-arg "BUILD_REVISION=$(env_value BUILD_REVISION)" "${services[@]}" 2>&1 | tee "$build_log"',
        '  BUILDKIT_PROGRESS="${BUILDKIT_PROGRESS:-plain}" compose build --pull --build-arg "BUILD_REVISION=$(env_value BUILD_REVISION)" "${services[@]}" 2>&1 | tee "$build_log"',
        "online-buildkit",
    )
    text = replace_once(
        text,
        '  compose build --pull=false --build-arg "BUILD_REVISION=$(env_value BUILD_REVISION)" "${services[@]}"\n  echo "PRODUCTION_IMAGE_BUILD=PASS mode=OFFLINE_LOCAL_INVENTORY audit=$audit_file"\n}',
        '  BUILDKIT_PROGRESS="${BUILDKIT_PROGRESS:-plain}" compose build --pull=false --build-arg "BUILD_REVISION=$(env_value BUILD_REVISION)" "${services[@]}"\n  echo "PRODUCTION_IMAGE_BUILD=PASS mode=OFFLINE_LOCAL_INVENTORY audit=$audit_file"\n}',
        "offline-buildkit",
    )

    old_deploy = """deploy_stack() {
  host_preflight
  prepare_host
  if [[ "$SKIP_BACKUP" != "1" ]]; then backup_db; fi
  build_images

  echo "Starting PostgreSQL, Redis and MinIO..."
  compose up -d postgres redis minio
  echo "Applying Alembic migration explicitly before application promotion..."
  compose run --rm backend alembic upgrade head
  # Keep this stable marker because Capture V2's frozen deploy contract parses the
  # promotion block after it. Feishu is now part of that same managed promotion.
  echo "Starting backend, workers, scheduler and frontend..."
  compose up -d \\
    backend collector-worker packet-worker pcm-worker media-worker diagnosis-worker \\
    feishu-long-connection \\
    reproduction-worker reproduction-control-high-worker reproduction-watch-worker beat frontend

  local timeout backend_port frontend_port
  timeout="$(env_value VOIP_HEALTH_TIMEOUT_SECONDS 2>/dev/null || echo 180)"
  backend_port="$(env_value VOIP_BACKEND_PORT 2>/dev/null || echo 8000)"
  frontend_port="$(env_value VOIP_FRONTEND_PORT 2>/dev/null || echo 8088)"
  wait_http "http://127.0.0.1:${backend_port}/health/ready" "$timeout"
  wait_http "http://127.0.0.1:${frontend_port}/" "$timeout"
  wait_feishu_long_connection "$timeout"
  verify_stack
}
"""
    new_deploy = """deploy_stack() {
  perf_reset
  perf_phase preflight host_preflight
  perf_phase prepare_host prepare_host
  if [[ "$SKIP_BACKUP" != "1" ]]; then perf_phase backup_db backup_db; fi
  perf_phase image_build build_images --preflight-done

  echo "Starting PostgreSQL, Redis and MinIO..."
  perf_phase data_services_start compose up -d postgres redis minio
  echo "Applying Alembic migration explicitly before application promotion..."
  perf_phase migration compose run --rm backend alembic upgrade head
  # Keep this stable marker because Capture V2's frozen deploy contract parses the
  # promotion block after it. Feishu is now part of that same managed promotion.
  echo "Starting backend, workers, scheduler and frontend..."
  perf_phase application_promotion compose up -d \\
    backend collector-worker packet-worker pcm-worker media-worker diagnosis-worker \\
    feishu-long-connection \\
    reproduction-worker reproduction-control-high-worker reproduction-watch-worker beat frontend

  local timeout backend_port frontend_port
  timeout="$(env_value VOIP_HEALTH_TIMEOUT_SECONDS 2>/dev/null || echo 180)"
  backend_port="$(env_value VOIP_BACKEND_PORT 2>/dev/null || echo 8000)"
  frontend_port="$(env_value VOIP_FRONTEND_PORT 2>/dev/null || echo 8088)"
  perf_phase backend_ready wait_http "http://127.0.0.1:${backend_port}/health/ready" "$timeout"
  perf_phase frontend_ready wait_http "http://127.0.0.1:${frontend_port}/" "$timeout"
  perf_phase feishu_ready wait_feishu_long_connection "$timeout"
  perf_phase runtime_verify verify_stack
}
"""
    text = replace_once(text, old_deploy, new_deploy, "deploy-phase-timing")
    path.write_text(text, encoding="utf-8")


def patch_workflow() -> None:
    path = ROOT / ".github" / "workflows" / "production-deploy.yml"
    text = path.read_text(encoding="utf-8")
    if "validation/cicd_performance_v2.json" in text:
        return
    text = replace_once(
        text,
        """            validation/exact_source_binding_result.json
          if-no-files-found: warn
""",
        """            validation/exact_source_binding_result.json
            validation/cicd_performance_v2.json
          if-no-files-found: warn
""",
        "performance-artifact",
    )
    path.write_text(text, encoding="utf-8")


def write_tests() -> None:
    path = ROOT / "backend" / "tests" / "test_cicd_performance_v2.py"
    path.write_text(
        '''from pathlib import Path\n\nROOT = Path(__file__).resolve().parents[2]\n\n\ndef test_backend_revision_label_does_not_invalidate_dependency_layers():\n    text = (ROOT / "backend/Dockerfile").read_text(encoding="utf-8")\n    assert text.index("pip install -r requirements.txt") < text.index("ARG BUILD_REVISION=unknown")\n    assert "--mount=type=cache,target=/root/.cache/pip" in text\n\n\ndef test_frontend_npm_install_isolated_from_source_copy():\n    text = (ROOT / "frontend/Dockerfile").read_text(encoding="utf-8")\n    assert text.index("npm ci --no-audit --no-fund") < text.index("COPY src ./src")\n    assert "--mount=type=cache,target=/root/.npm" in text\n\n\ndef test_production_deploy_records_timing_and_keeps_governance():\n    text = (ROOT / "deploy/voip-ai").read_text(encoding="utf-8")\n    assert "CICD_PERFORMANCE_V2_EVIDENCE" in text\n    assert "perf_phase runtime_verify verify_stack" in text\n    assert "source_binding_preflight" in text\n    assert "python3 tools/source_manifest_gate.py" in text\n    assert "compose config >/dev/null" in text\n\n\ndef test_registry_probe_fails_closed_or_uses_audited_fallback():\n    text = (ROOT / "deploy/voip-ai").read_text(encoding="utf-8")\n    probe = text.index("REGISTRY_PREFLIGHT=FAIL")\n    guard = text.index("offline_build_fallback.py", probe)\n    offline = text.index("compose build --pull=false", guard)\n    assert probe < guard < offline\n    assert "VOIP_REGISTRY_PROBE_TIMEOUT_SECONDS" in text\n''',
        encoding="utf-8",
    )


if __name__ == "__main__":
    patch_deploy_cli()
    patch_workflow()
    write_tests()
