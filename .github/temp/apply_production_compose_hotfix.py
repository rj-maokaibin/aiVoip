from pathlib import Path

compose = Path('docker-compose.yml')
text = compose.read_text(encoding='utf-8')
old = """  frontend:\n    build:\n      context: ./frontend\n      args:\n        BUILD_REVISION: ${BUILD_REVISION:?BUILD_REVISION is required}\n      args:\n        VITE_API_BASE_URL: ${VITE_API_BASE_URL:-/api/v1}\n"""
new = """  frontend:\n    build:\n      context: ./frontend\n      args:\n        BUILD_REVISION: ${BUILD_REVISION:?BUILD_REVISION is required}\n        VITE_API_BASE_URL: ${VITE_API_BASE_URL:-/api/v1}\n"""
if old not in text:
    raise SystemExit('expected duplicate frontend build.args block not found')
compose.write_text(text.replace(old, new, 1), encoding='utf-8')

workflow = Path('.github/workflows/source-manifest-gate.yml')
w = workflow.read_text(encoding='utf-8')
marker = """          python3 tools/source_manifest_gate.py\n          echo \"SOURCE_MANIFEST_PR_GATE=PASS\"\n"""
replacement = marker + """\n      - name: Verify production Compose configuration\n        shell: bash\n        run: |\n          set -euo pipefail\n          bash tools/production_compose_config_gate.sh\n"""
if marker not in w:
    raise SystemExit('source manifest gate insertion marker not found')
workflow.write_text(w.replace(marker, replacement, 1), encoding='utf-8')

Path('tools/production_compose_config_gate.sh').write_text("""#!/usr/bin/env bash
set -Eeuo pipefail

ROOT=\"$(cd \"$(dirname \"${BASH_SOURCE[0]}\")/..\" && pwd)\"
cd \"$ROOT\"

command -v docker >/dev/null 2>&1 || {
  echo \"ERROR: docker CLI is required for production Compose config validation\" >&2
  exit 127
}
docker compose version >/dev/null 2>&1 || {
  echo \"ERROR: docker compose plugin is required for production Compose config validation\" >&2
  exit 127
}

TMP_DIR=\"$(mktemp -d)\"
trap 'rm -rf \"$TMP_DIR\"' EXIT
APP_ENV=\"$TMP_DIR/app.env\"
: > \"$APP_ENV\"

for name in auth_gateway_hmac minio_access_key minio_secret_key credential_api_token feishu_app_secret feishu_verification_token; do
  printf 'ci-placeholder\\n' > \"$TMP_DIR/$name\"
done

export BUILD_REVISION=\"${BUILD_REVISION:-0000000000000000000000000000000000000000}\"
export POSTGRES_PASSWORD=\"${POSTGRES_PASSWORD:-ci-postgres}\"
export MINIO_ROOT_USER=\"${MINIO_ROOT_USER:-ci-minio}\"
export MINIO_ROOT_PASSWORD=\"${MINIO_ROOT_PASSWORD:-ci-minio-secret}\"
export VOIP_APP_ENV_FILE=\"$APP_ENV\"
export AUTH_GATEWAY_HMAC_SECRET_HOST_FILE=\"$TMP_DIR/auth_gateway_hmac\"
export MINIO_ACCESS_KEY_SECRET_HOST_FILE=\"$TMP_DIR/minio_access_key\"
export MINIO_SECRET_KEY_SECRET_HOST_FILE=\"$TMP_DIR/minio_secret_key\"
export CREDENTIAL_API_TOKEN_SECRET_HOST_FILE=\"$TMP_DIR/credential_api_token\"
export FEISHU_APP_SECRET_HOST_FILE=\"$TMP_DIR/feishu_app_secret\"
export FEISHU_VERIFICATION_TOKEN_HOST_FILE=\"$TMP_DIR/feishu_verification_token\"

docker compose \\
  -f docker-compose.yml \\
  -f docker-compose.production.yml \\
  config >/dev/null

echo \"PRODUCTION_COMPOSE_CONFIG_GATE=PASS\"
""", encoding='utf-8')

Path('backend/tests/test_production_compose_config_gate_v1.py').write_text("""from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_frontend_build_args_are_single_mapping():
    text = (ROOT / 'docker-compose.yml').read_text(encoding='utf-8')
    frontend = text.split('\\n  frontend:\\n', 1)[1]
    build = frontend.split('\\n    ports:\\n', 1)[0]
    assert build.count('\\n      args:\\n') == 1
    assert 'BUILD_REVISION:' in build
    assert 'VITE_API_BASE_URL:' in build


def test_source_manifest_gate_runs_production_compose_config_gate():
    workflow = (ROOT / '.github/workflows/source-manifest-gate.yml').read_text(encoding='utf-8')
    assert 'bash tools/production_compose_config_gate.sh' in workflow


def test_compose_config_gate_checks_production_overlay():
    script = (ROOT / 'tools/production_compose_config_gate.sh').read_text(encoding='utf-8')
    assert '-f docker-compose.yml' in script
    assert '-f docker-compose.production.yml' in script
    assert 'config >/dev/null' in script
""", encoding='utf-8')
