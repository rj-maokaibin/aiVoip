#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

command -v docker >/dev/null 2>&1 || {
  echo "ERROR: docker CLI is required for production Compose config validation" >&2
  exit 127
}
docker compose version >/dev/null 2>&1 || {
  echo "ERROR: docker compose plugin is required for production Compose config validation" >&2
  exit 127
}

TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
APP_ENV="$TMP_DIR/app.env"
: > "$APP_ENV"

for name in auth_gateway_hmac minio_access_key minio_secret_key credential_api_token feishu_app_secret feishu_verification_token; do
  printf 'ci-placeholder\n' > "$TMP_DIR/$name"
done

export BUILD_REVISION="${BUILD_REVISION:-0000000000000000000000000000000000000000}"
export POSTGRES_PASSWORD="${POSTGRES_PASSWORD:-ci-postgres}"
export MINIO_ROOT_USER="${MINIO_ROOT_USER:-ci-minio}"
export MINIO_ROOT_PASSWORD="${MINIO_ROOT_PASSWORD:-ci-minio-secret}"
export VOIP_APP_ENV_FILE="$APP_ENV"
export AUTH_GATEWAY_HMAC_SECRET_HOST_FILE="$TMP_DIR/auth_gateway_hmac"
export MINIO_ACCESS_KEY_SECRET_HOST_FILE="$TMP_DIR/minio_access_key"
export MINIO_SECRET_KEY_SECRET_HOST_FILE="$TMP_DIR/minio_secret_key"
export CREDENTIAL_API_TOKEN_SECRET_HOST_FILE="$TMP_DIR/credential_api_token"
export FEISHU_APP_SECRET_HOST_FILE="$TMP_DIR/feishu_app_secret"
export FEISHU_VERIFICATION_TOKEN_HOST_FILE="$TMP_DIR/feishu_verification_token"

docker compose \
  -f docker-compose.yml \
  -f docker-compose.production.yml \
  config >/dev/null

echo "PRODUCTION_COMPOSE_CONFIG_GATE=PASS"
