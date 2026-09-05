#!/usr/bin/env python3
from __future__ import annotations

# Reuse the exact PR-D live runtime/credential/authority/evidence plumbing while
# replacing only the gate implementation that resolves HTTP mutation UNKNOWN by
# its mandatory read-only observation. Importing the sibling module is stable
# because GitHub invokes this file as `python tools/run_golden_web_config_observed.py`.
import run_golden_web_config as base

from app.automation.gates.golden_web_config_observed import ObservedGoldenWebConfigGate


base.GoldenWebConfigGate = ObservedGoldenWebConfigGate


if __name__ == "__main__":
    raise SystemExit(base.main())
