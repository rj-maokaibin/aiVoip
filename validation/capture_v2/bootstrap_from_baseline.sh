#!/usr/bin/env bash
set -euo pipefail

BASE_SHA="a805e2dfefdc8ca62fae90bc403166bfeea61827"
BASELINE_SHA="5728424bbeebb6a666935c467b3ed556fdb0833282121d159a5072b9737c3b01"
ZIP="${1:-}"

if [[ -z "$ZIP" || ! -f "$ZIP" ]]; then
  echo "usage: $0 /path/to/Capture_Engine_V2.1.1_A-F_Software_Baseline.zip" >&2
  exit 2
fi

repo="$(git rev-parse --show-toplevel)"
cd "$repo"
if ! git merge-base --is-ancestor "$BASE_SHA" HEAD; then
  echo "ERROR: this validation branch expects master base $BASE_SHA" >&2
  exit 3
fi

actual="$(sha256sum "$ZIP" | awk '{print $1}')"
if [[ "$actual" != "$BASELINE_SHA" ]]; then
  echo "ERROR: baseline SHA mismatch: expected $BASELINE_SHA got $actual" >&2
  exit 4
fi

work="$(mktemp -d)"
trap 'rm -rf "$work"' EXIT
unzip -q "$ZIP" -d "$work"
src="$work/Capture_Engine_V2.1.1_A-F_Software_Baseline"

mkdir -p backend/app backend/tests backend/migrations/versions profiles/capture/v2.1 profiles/platforms
cp -a "$src/backend/app/capture_v2" backend/app/
cp -a "$src/backend/tests"/test_capture_v2_*.py backend/tests/
cp -a "$src/backend/migrations/versions"/0027_capture_v2_foundation.py backend/migrations/versions/
cp -a "$src/backend/migrations/versions"/0028_capture_v2_reliable_segments.py backend/migrations/versions/
cp -a "$src/backend/migrations/versions"/0029_capture_v2_readiness_fxs.py backend/migrations/versions/
cp -a "$src/backend/migrations/versions"/0030_capture_v2_coverage.py backend/migrations/versions/
cp -a "$src/backend/migrations/versions"/0031_capture_v2_quality_report.py backend/migrations/versions/
cp -a "$src/profiles/capture/v2.1/standard.yaml" profiles/capture/v2.1/
cp -a "$src/profiles/platforms"/capture_v2_*.yaml profiles/platforms/

for p in \
  0001-config-capture-v2.patch \
  0002-alembic-metadata-capture-v2.patch \
  0003-v1-v2-authority-guard.patch \
  0004-asyncssh-sftp-get.patch \
  0005-config-v2-cutover.patch; do
  git apply --check "$src/patches/$p"
  git apply "$src/patches/$p"
done

echo "Baseline materialized. Gate tooling already lives on this branch."
echo "Keep CAPTURE_ENGINE_VERSION=V1 and CAPTURE_V2_PRODUCTION_ENABLED=false."
echo "Run: cd backend && PYTHONPATH=. pytest -q tests/test_capture_v2_*.py"
