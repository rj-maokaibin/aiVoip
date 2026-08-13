#!/usr/bin/env bash
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
OUT="${FRONTEND_BUILD_EVIDENCE:-$ROOT/validation/frontend_build_runtime.json}"

if [[ ! -f release/source_manifest.json ]]; then
  echo "ERROR: source manifest missing; run python tools/source_manifest_gate.py --update" >&2
  exit 2
fi
if [[ ! -f frontend/package-lock.json ]]; then
  echo "ERROR: frontend/package-lock.json is required for a reproducible release build" >&2
  exit 3
fi
if ! command -v npm >/dev/null 2>&1; then
  echo "ERROR: npm is required" >&2
  exit 127
fi

rm -rf frontend/node_modules frontend/dist
(
  cd frontend
  npm ci
  npm run build
)
[[ -s frontend/dist/index.html ]] || { echo "ERROR: frontend/dist/index.html missing after build" >&2; exit 4; }

python - "$OUT" <<'PY'
import hashlib,json,sys
from pathlib import Path
sys.path.insert(0, str(Path.cwd()/'tools'))
from release_evidence import evidence_envelope
out=Path(sys.argv[1])
index=Path('frontend/dist/index.html')
payload=evidence_envelope(evidence_type='FRONTEND_PRODUCTION_BUILD',payload={
    'passed': True,
    'lockfile': 'frontend/package-lock.json',
    'index_path': 'frontend/dist/index.html',
    'index_size_bytes': index.stat().st_size,
    'index_sha256': hashlib.sha256(index.read_bytes()).hexdigest(),
})
out.parent.mkdir(parents=True,exist_ok=True)
out.write_text(json.dumps(payload,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
print(json.dumps(payload,ensure_ascii=False,indent=2))
PY
