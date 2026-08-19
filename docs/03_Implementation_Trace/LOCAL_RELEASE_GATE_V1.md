# VOIP AI Local Software Release Gate V1

GitHub-hosted CI is no longer a mandatory acceptance dependency for the current V1 development flow. Automatic PR/push execution is disabled; the workflow remains available only through `workflow_dispatch` as an optional manual path.

The authoritative software acceptance command is:

```bash
bash tools/voip_ai_release_gate.sh
```

The gate runs on a controlled Linux host and performs, as applicable to the current branch:

1. Python compile.
2. AI contract coverage.
3. AI E1-E6 regression.
4. AI1 Semantic Router focused gate.
5. AI3 Case Copilot focused gate.
6. AI2 Diagnostic Loop SHADOW/SUGGEST focused gate.
7. M7 acceptance contract.
8. Clean PostgreSQL migration to Alembic head.
9. Full backend regression.
10. Preliminary Evidence Report software gate.
11. Frontend dependency audit and production build.

The script creates an isolated Python virtual environment under `.venv-release-gate`, starts ephemeral PostgreSQL 16 and Redis 7 containers with random localhost ports, and removes those containers on exit.

Required host prerequisites are Python 3 with `venv`, Docker, npm/Node, curl, git and network access to the Python/npm/Docker registries needed for dependency installation. Project-specific system dependencies such as ffmpeg and tesseract-ocr should be preinstalled on the controlled host.

A release may be labeled `SOFTWARE GATE PASS` only when this command exits zero on the exact commit being accepted. Static review alone is not PASS. GitHub Actions status is informational and is no longer a mandatory prerequisite.

External production acceptance remains separate: live Feishu tenant, real DUT end-to-end, and real semantic/Golden Dataset validation are not converted into software PASS by this local gate.
