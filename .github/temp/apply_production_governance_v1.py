from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def patch_backend_dockerfile() -> None:
    p = ROOT / "backend/Dockerfile"
    s = p.read_text(encoding="utf-8")
    if "org.opencontainers.image.revision" not in s:
        s = s.replace(
            "FROM python:3.12-slim\n",
            "FROM python:3.12-slim\n"
            "ARG BUILD_REVISION=unknown\n"
            "LABEL org.opencontainers.image.revision=$BUILD_REVISION\n",
            1,
        )
    p.write_text(s, encoding="utf-8")


def patch_frontend_dockerfile() -> None:
    p = ROOT / "frontend/Dockerfile"
    s = p.read_text(encoding="utf-8")
    if not s.startswith("ARG BUILD_REVISION=unknown\n"):
        s = "ARG BUILD_REVISION=unknown\n" + s
    if "LABEL org.opencontainers.image.revision=$BUILD_REVISION" not in s:
        s = s.replace(
            "FROM nginx:1.27-alpine\n",
            "FROM nginx:1.27-alpine\n"
            "ARG BUILD_REVISION\n"
            "LABEL org.opencontainers.image.revision=$BUILD_REVISION\n",
            1,
        )
    p.write_text(s, encoding="utf-8")


def patch_compose() -> None:
    p = ROOT / "docker-compose.yml"
    s = p.read_text(encoding="utf-8")
    backend_old = "build:\n      context: ./backend"
    frontend_old = "build:\n      context: ./frontend"
    backend_new = (
        "build:\n      context: ./backend\n      args:\n"
        "        BUILD_REVISION: ${BUILD_REVISION:?BUILD_REVISION is required}"
    )
    frontend_new = (
        "build:\n      context: ./frontend\n      args:\n"
        "        BUILD_REVISION: ${BUILD_REVISION:?BUILD_REVISION is required}"
    )
    if backend_old in s:
        s = s.replace(backend_old, backend_new)
    if frontend_old in s:
        s = s.replace(frontend_old, frontend_new)
    count = s.count("BUILD_REVISION: ${BUILD_REVISION:?BUILD_REVISION is required}")
    assert count >= 12, count
    p.write_text(s, encoding="utf-8")

    p = ROOT / "docker-compose.production.yml"
    s = p.read_text(encoding="utf-8")
    old = "  frontend:\n    restart: unless-stopped\n"
    new = (
        "  frontend:\n"
        "    restart: unless-stopped\n"
        "    environment:\n"
        "      BUILD_REVISION: ${BUILD_REVISION:?BUILD_REVISION is required}\n"
    )
    if "frontend:\n    restart: unless-stopped\n    environment:\n      BUILD_REVISION:" not in s:
        assert old in s
        s = s.replace(old, new, 1)
    p.write_text(s, encoding="utf-8")


def patch_source_manifest_contract() -> None:
    p = ROOT / "tools/source_manifest_gate.py"
    s = p.read_text(encoding="utf-8")
    needle = (
        '    "docker-compose.yml", "docker-compose.production.yml", '
        '"docker-compose.e2e.yml", "release/release_policy.yaml",\n'
    )
    replacement = needle + (
        '    ".github/workflows/production-deploy.yml", '
        '".github/workflows/source-manifest-gate.yml",\n'
    )
    if '".github/workflows/production-deploy.yml"' not in s:
        assert needle in s
        s = s.replace(needle, replacement, 1)
    p.write_text(s, encoding="utf-8")


def patch_deploy_cli() -> None:
    p = ROOT / "deploy/voip-ai"
    s = p.read_text(encoding="utf-8")
    marker = "host_preflight() {\n"
    if "source_binding_preflight() {" not in s:
        fn = """source_binding_preflight() {
  local expected head dirty
  expected="$(env_value BUILD_REVISION 2>/dev/null || true)"
  head="$(git rev-parse HEAD 2>/dev/null || true)"
  dirty="$(git status --porcelain --untracked-files=no 2>/dev/null || true)"
  if [[ ! "$expected" =~ ^[0-9a-f]{40}$ ]]; then
    echo "ERROR: BUILD_REVISION must be an immutable 40-char git SHA; observed=${expected:-missing}" >&2
    return 1
  fi
  if [[ "$head" != "$expected" ]]; then
    echo "ERROR: exact source binding failed before deploy: git_head=$head BUILD_REVISION=$expected" >&2
    return 1
  fi
  if [[ -n "$dirty" ]]; then
    echo "ERROR: tracked production source is dirty; refusing deployment" >&2
    printf '%s\\n' "$dirty" >&2
    return 1
  fi
  mkdir -p validation
  python3 tools/source_manifest_gate.py
  python3 deploy/exact_source_binding_gate.py \\
    --env-file "$ENV_FILE" \\
    --phase source \\
    --out validation/exact_source_binding_source_result.json
  echo "EXACT_SOURCE_PREDEPLOY_GATE=PASS revision=$expected"
}

"""
        assert marker in s
        s = s.replace(marker, fn + marker, 1)

    host_old = """host_preflight() {
  require_docker
  [[ -f "$ENV_FILE" ]] || { echo "ERROR: production env file missing: $ENV_FILE" >&2; exit 2; }
  mkdir -p validation
"""
    host_new = """host_preflight() {
  require_docker
  [[ -f "$ENV_FILE" ]] || { echo "ERROR: production env file missing: $ENV_FILE" >&2; exit 2; }
  source_binding_preflight
  mkdir -p validation
"""
    if host_new not in s:
        assert host_old in s
        s = s.replace(host_old, host_new, 1)

    s = s.replace(
        'compose build --pull "${services[@]}" 2>&1 | tee "$build_log"',
        'compose build --pull --build-arg "BUILD_REVISION=$(env_value BUILD_REVISION)" "${services[@]}" 2>&1 | tee "$build_log"',
    )
    s = s.replace(
        'compose build --pull=false "${services[@]}"',
        'compose build --pull=false --build-arg "BUILD_REVISION=$(env_value BUILD_REVISION)" "${services[@]}"',
    )

    verify_old = """  compose run --rm -e PRODUCTION_RUNTIME_EVIDENCE=/workspace/validation/production_runtime_result.json release-runner \\
    python deploy/production_runtime_verify.py
  echo "Runtime evidence: validation/production_runtime_result.json"
"""
    verify_new = """  compose run --rm -e PRODUCTION_RUNTIME_EVIDENCE=/workspace/validation/production_runtime_result.json release-runner \\
    python deploy/production_runtime_verify.py
  local backend_port
  backend_port="$(env_value VOIP_BACKEND_PORT 2>/dev/null || echo 8000)"
  python3 deploy/exact_source_binding_gate.py \\
    --env-file "$ENV_FILE" \\
    --phase runtime \\
    --project "$PROJECT" \\
    --backend-url "http://127.0.0.1:${backend_port}/health/ready" \\
    --runtime-evidence validation/production_runtime_result.json \\
    --feishu-evidence validation/feishu_long_connection_runtime.json \\
    --out validation/exact_source_binding_result.json
  echo "Runtime evidence: validation/production_runtime_result.json"
  echo "Exact source binding evidence: validation/exact_source_binding_result.json"
"""
    if "Exact source binding evidence: validation/exact_source_binding_result.json" not in s:
        assert verify_old in s
        s = s.replace(verify_old, verify_new, 1)
    p.write_text(s, encoding="utf-8")


def main() -> None:
    patch_backend_dockerfile()
    patch_frontend_dockerfile()
    patch_compose()
    patch_source_manifest_contract()
    patch_deploy_cli()


if __name__ == "__main__":
    main()
