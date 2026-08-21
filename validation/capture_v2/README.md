# Capture V2.1.1 Real-Gates Validation Branch

**VALIDATION ONLY / NOT MERGE READY**

Base commit: `a805e2dfefdc8ca62fae90bc403166bfeea61827`.

This branch carries the real-gate tooling source, tests, Runbook and a bootstrap helper for the previously validated A-F Software Baseline.

Baseline artifact expected by `bootstrap_from_baseline.sh`:

- file: `Capture_Engine_V2.1.1_A-F_Software_Baseline.zip`
- SHA256: `5728424bbeebb6a666935c467b3ed556fdb0833282121d159a5072b9737c3b01`
- software regression before Gate tooling: 97 passed
- Gate tooling delta: 9 passed
- combined Capture V2 regression: 106 passed

## Materialize the A-F baseline

```bash
bash validation/capture_v2/bootstrap_from_baseline.sh /path/to/Capture_Engine_V2.1.1_A-F_Software_Baseline.zip
cd backend
PYTHONPATH=. pytest -q tests/test_capture_v2_*.py
```

Then inspect the CLI:

```bash
python -m app.capture_v2.gate_cli --help
```

See `docs/CAPTURE_V2_GATE_TOOLING_RUNBOOK.md`.

## Safety

- Keep `CAPTURE_ENGINE_VERSION=V1`.
- Keep `CAPTURE_V2_PRODUCTION_ENABLED=false`.
- The branch must not be merged as-is.
- Real R1-R7 release gates remain mandatory before Production V2 can be enabled.
