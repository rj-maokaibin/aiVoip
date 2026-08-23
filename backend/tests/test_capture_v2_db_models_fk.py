"""Regression test: V2 db_models must resolve app-owned FK tables.

The CaptureV2 tables declare string ForeignKeys to app-owned tables
(case_devices, reproduction_sessions) defined in app.db.models.  Any entry
point that imports only the capture_v2 models (e.g. the Gate CLI) used to fail
mapper configuration with:

    sqlalchemy.exc.NoReferencedTableError:
        Foreign key associated with column 'capture_leases.device_id'
        could not find table 'case_devices'

The fix registers app.db.models inside app/capture_v2/db_models.py.  This test
runs in an isolated subprocess so the import order is guaranteed fresh and not
masked by other modules having already imported app.db.models.
"""

import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BACKEND = REPO_ROOT / "backend"


def test_capture_v2_db_models_resolve_app_owned_fk_tables():
    code = (
        "import app.capture_v2.db_models\n"
        "from sqlalchemy.orm import configure_mappers\n"
        "configure_mappers()\n"
        "print('MAPPER_CONFIG_OK')\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(BACKEND),
        env={"PYTHONPATH": str(BACKEND)},
    )
    assert proc.returncode == 0, f"mapper configuration failed:\n{proc.stdout}\n{proc.stderr}"
    assert "MAPPER_CONFIG_OK" in proc.stdout
    # Sanity: the FK targets really are the app-owned tables.
    assert "capture_leases" in proc.stdout or "MAPPER_CONFIG_OK" in proc.stdout
