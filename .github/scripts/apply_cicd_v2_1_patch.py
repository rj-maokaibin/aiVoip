#!/usr/bin/env python3
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[2]

compose_path = ROOT / 'docker-compose.yml'
compose = compose_path.read_text(encoding='utf-8')
backend_header = "  backend:\n    build:\n      context: ./backend\n      args:\n        BUILD_REVISION: ${BUILD_REVISION:?BUILD_REVISION is required}\n"
backend_repl = "  backend:\n    image: aivoip-backend:${BUILD_REVISION:?BUILD_REVISION is required}\n    build:\n      context: ./backend\n      args:\n        BUILD_REVISION: ${BUILD_REVISION:?BUILD_REVISION is required}\n"
if backend_header in compose:
    compose = compose.replace(backend_header, backend_repl, 1)
elif backend_repl not in compose:
    raise SystemExit('backend build block not found')

shared_services = [
    'collector-worker','packet-worker','pcm-worker','media-worker','diagnosis-worker',
    'feishu-long-connection','reproduction-worker','reproduction-control-high-worker',
    'reproduction-watch-worker','beat',
]
for svc in shared_services:
    pattern = re.compile(
        rf"(  {re.escape(svc)}:\n)    build:\n      context: \./backend\n      args:\n        BUILD_REVISION: \$\{{BUILD_REVISION:\?BUILD_REVISION is required\}}\n"
    )
    repl = rf"\1    image: aivoip-backend:${{BUILD_REVISION:?BUILD_REVISION is required}}\n"
    compose, count = pattern.subn(repl, compose, count=1)
    if count == 0 and f"  {svc}:\n    image: aivoip-backend:${{BUILD_REVISION:?BUILD_REVISION is required}}\n" not in compose:
        raise SystemExit(f'build block not found for {svc}')
compose_path.write_text(compose, encoding='utf-8')

prod_path = ROOT / 'docker-compose.production.yml'
prod = prod_path.read_text(encoding='utf-8')
release_build = "  release-runner:\n    build:\n      context: ./backend\n"
release_image = "  release-runner:\n    image: aivoip-backend:${BUILD_REVISION:?BUILD_REVISION is required}\n"
if release_build in prod:
    prod = prod.replace(release_build, release_image, 1)
elif release_image not in prod:
    raise SystemExit('release-runner build block not found')
prod_path.write_text(prod, encoding='utf-8')

deploy_path = ROOT / 'deploy/voip-ai'
deploy = deploy_path.read_text(encoding='utf-8')
old_services = """  local services=(
    backend collector-worker packet-worker pcm-worker media-worker diagnosis-worker
    feishu-long-connection
    reproduction-worker reproduction-control-high-worker reproduction-watch-worker beat
    frontend release-runner
  )
"""
new_services = "  local services=(backend frontend)\n"
if old_services in deploy:
    deploy = deploy.replace(old_services, new_services, 1)
elif new_services not in deploy:
    raise SystemExit('build service list not found')

start = deploy.find('  # Fast registry/mirror probe.')
end_marker = '  echo "REGISTRY_PREFLIGHT=PASS mode=ONLINE_PULL probe_image=$probe_image"\n'
end = deploy.find(end_marker, start)
if start < 0 or end < 0:
    if 'REGISTRY_PREFLIGHT=PASS mode=HTTP_CONNECTIVITY' not in deploy:
        raise SystemExit('registry probe block not found')
else:
    end += len(end_marker)
    new_probe = '''  # V2.1: prove registry DNS/TCP/HTTP transport quickly. A full docker pull is
  # intentionally not used as a preflight because the corporate Nexus mirror can
  # legitimately take tens of seconds before layer transfer begins. The actual
  # online freshness check remains compose build --pull below.
  local probe_log probe_json probe_timeout probe_rc
  probe_log="$release_root/registry-probe.log"
  probe_json="${VOIP_REGISTRY_PROBE_EVIDENCE:-validation/registry_connectivity_v2_1.json}"
  probe_timeout="${VOIP_REGISTRY_PROBE_TIMEOUT_SECONDS:-3}"
  set +e
  python3 deploy/registry_connectivity_probe.py \
    --timeout-seconds "$probe_timeout" \
    --out "$probe_json" >"$probe_log" 2>&1
  probe_rc="$?"
  set -e
  cat "$probe_log"
  if [[ "$probe_rc" != "0" ]]; then
    echo "REGISTRY_PREFLIGHT=FAIL; evaluating audited offline fallback immediately..."
    if python3 deploy/offline_build_fallback.py \
        --build-log "$probe_log" \
        --online-exit-code "$probe_rc" \
        --audit "$audit_file"; then
      echo "OFFLINE_IMAGE_FALLBACK=AUTHORIZED source=REGISTRY_PREFLIGHT audit=$audit_file"
      local offline_rc
      set +e
      BUILDKIT_PROGRESS="${BUILDKIT_PROGRESS:-plain}" compose build --pull=false --build-arg "BUILD_REVISION=$(env_value BUILD_REVISION)" "${services[@]}"
      offline_rc="$?"
      set -e
      if [[ "$offline_rc" != "0" ]]; then
        echo "PRODUCTION_IMAGE_BUILD=FAIL mode=OFFLINE_LOCAL_INVENTORY source=REGISTRY_PREFLIGHT exit_code=$offline_rc audit=$audit_file" >&2
        return "$offline_rc"
      fi
      echo "PRODUCTION_IMAGE_BUILD=PASS mode=OFFLINE_LOCAL_INVENTORY audit=$audit_file"
      return 0
    fi
    echo "PRODUCTION_IMAGE_BUILD=BLOCKED reason=REGISTRY_PREFLIGHT online_exit_code=$probe_rc audit=$audit_file" >&2
    return "$probe_rc"
  fi
  echo "REGISTRY_PREFLIGHT=PASS mode=HTTP_CONNECTIVITY evidence=$probe_json"
'''
    deploy = deploy[:start] + new_probe + deploy[end:]
deploy_path.write_text(deploy, encoding='utf-8')

test_path = ROOT / 'backend/tests/test_cicd_performance_v2.py'
tests = test_path.read_text(encoding='utf-8')
append = '''

def test_v2_1_registry_probe_is_transport_only_and_not_full_pull():
    deploy = (ROOT / "deploy/voip-ai").read_text(encoding="utf-8")
    probe = (ROOT / "deploy/registry_connectivity_probe.py").read_text(encoding="utf-8")
    assert "registry_connectivity_probe.py" in deploy
    assert 'REGISTRY_PREFLIGHT=PASS mode=HTTP_CONNECTIVITY' in deploy
    assert 'docker pull "$probe_image"' not in deploy
    assert "HTTPError" in probe
    assert "REGISTRY_CONNECTIVITY=PASS" in probe


def test_v2_1_backend_runtime_services_share_one_built_image():
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    prod = (ROOT / "docker-compose.production.yml").read_text(encoding="utf-8")
    deploy = (ROOT / "deploy/voip-ai").read_text(encoding="utf-8")
    shared = 'image: aivoip-backend:${BUILD_REVISION:?BUILD_REVISION is required}'
    assert compose.count(shared) >= 11
    assert shared in prod
    assert "local services=(backend frontend)" in deploy


def test_v2_1_production_workflow_restores_workspace_ownership():
    text = (ROOT / ".github/workflows/production-deploy.yml").read_text(encoding="utf-8")
    assert "Restore runner workspace ownership" in text
    assert "PRODUCTION_RUNNER_WORKSPACE_RESTORE=PASS" in text
    assert "validation/registry_connectivity_v2_1.json" in text
'''
if 'test_v2_1_registry_probe_is_transport_only_and_not_full_pull' not in tests:
    tests += append
test_path.write_text(tests, encoding='utf-8')

print('CICD_V2_1_PATCH=PASS')
