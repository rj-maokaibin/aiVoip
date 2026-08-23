# Capture Engine V2.1.1 — Final Apply Guide

## 1. Baseline

Apply only on:

```text
rj-maokaibin/aiVoip
a805e2dfefdc8ca62fae90bc403166bfeea61827
```

If master has moved, rebase/re-audit before applying.

## 2. Copy new files

Copy:

```text
backend/app/capture_v2/
backend/migrations/versions/0027_capture_v2_foundation.py
backend/migrations/versions/0028_capture_v2_reliable_segments.py
backend/migrations/versions/0029_capture_v2_readiness_fxs.py
backend/migrations/versions/0030_capture_v2_coverage.py
backend/migrations/versions/0031_capture_v2_quality_report.py
backend/tests/test_capture_v2_*.py
profiles/capture/v2.1/standard.yaml
profiles/platforms/capture_v2_mt7621.yaml
profiles/platforms/capture_v2_mt7981.yaml
```

## 3. Apply integration patches in order

```bash
git apply patches/0001-config-capture-v2.patch
git apply patches/0002-alembic-metadata-capture-v2.patch
git apply patches/0003-v1-v2-authority-guard.patch
git apply patches/0004-asyncssh-sftp-get.patch
git apply patches/0005-config-v2-cutover.patch
```

Then inspect diff before commit.

## 4. Production safety default

Keep:

```text
CAPTURE_ENGINE_VERSION=V1
capture_v2_production_enabled=false
```

Do **not** enable V2 from software completion alone.

## 5. Software validation

```bash
cd backend
pytest -q tests/test_capture_v2_*.py
python -m compileall -q app/capture_v2 tests/test_capture_v2_*.py
alembic upgrade head
```

Expected Capture V2 regression baseline:

```text
97 passed
```

## 6. Release artifact

Use `RELEASE_GATE_TEMPLATE.json` as the machine-readable template. All real gates initially remain false.

## 7. Real Gate order

Follow `DEFERRED_REAL_GATES.md` and the existing B/C Gate runbooks. Do not change Production V2 enable flags until R1~R7 are passed and approved.
