#!/usr/bin/env bash
# Daily PostgreSQL backup for the VOIP AI local stack.
# Data lives under ${VOIP_DATA_ROOT:-/home/dev/.local/share/aiVoip} (see .env).
# Keeps the last 14 daily dumps plus today's dump under BACKUP_DIR.
set -euo pipefail

PROJECT_DIR="/home/dev/workspace/aiVoip"
DATA_ROOT="${VOIP_DATA_ROOT:-/home/dev/.local/share/aiVoip}"
BACKUP_DIR="${VOIP_BACKUP_DIR:-${DATA_ROOT}/backups}"
KEEP_DAYS="${VOIP_BACKUP_KEEP_DAYS:-14}"

DB_USER="${VOIP_DB_USER:-voip}"
DB_NAME="${VOIP_DB_NAME:-voip}"
STAMP="$(date +%Y%m%d-%H%M%S)"
OUT_FILE="${BACKUP_DIR}/voip-${STAMP}.sql.gz"

mkdir -p "${BACKUP_DIR}"

# Dump through the running postgres container. 'exec -T' avoids TTY allocation so
# the pipeline works under cron. Docker group access is handled by sg docker.
cd "${PROJECT_DIR}"
sg docker -c "docker compose --env-file .env exec -T postgres pg_dump -U ${DB_USER} ${DB_NAME}" \
  | gzip -9 > "${OUT_FILE}"

# Prune dumps older than KEEP_DAYS.
find "${BACKUP_DIR}" -name 'voip-*.sql.gz' -mtime "+${KEEP_DAYS}" -delete

echo "backup written: ${OUT_FILE} ($(du -h "${OUT_FILE}" | cut -f1))"
echo "remaining backups: $(find "${BACKUP_DIR}" -name 'voip-*.sql.gz' | wc -l)"
