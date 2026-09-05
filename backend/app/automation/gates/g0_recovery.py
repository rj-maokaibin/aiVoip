from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


class G0RecoveryMarkerStore:
    """Runner-private minimal recovery state for Golden-CFG-CONFIG-001.

    G0 mutates only voipUserInfo.disName, so persisting the whole module would
    unnecessarily retain passwd/authId and violate the evidence contract. This
    store keeps only the original disName needed to reverse the test mutation.
    Files are never artifacts and are removed immediately after reverse verify.
    """

    schema = "g0-recovery-marker-v1"

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)

    def _path(self, run_id: str) -> Path:
        if not run_id or "/" in run_id or ".." in run_id:
            raise ValueError("G0_RECOVERY_RUN_ID_INVALID")
        return self.root / f"{run_id}.json"

    def write(
        self,
        *,
        run_id: str,
        device_id: str,
        original_disname: str,
    ) -> None:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        path = self._path(run_id)
        temporary = path.with_suffix(".tmp")
        payload: dict[str, Any] = {
            "schema": self.schema,
            "run_id": run_id,
            "device_id": device_id,
            "module": "voipUserInfo",
            "field": "disName",
            "original_disname": str(original_disname),
        }
        fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, path)
            os.chmod(path, 0o600)
        finally:
            if temporary.exists():
                temporary.unlink()

    def remove(self, *, run_id: str) -> None:
        path = self._path(run_id)
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    def retained(self, *, run_id: str) -> bool:
        return self._path(run_id).exists()

    def read_for_recovery(self, *, run_id: str, device_id: str) -> str:
        path = self._path(run_id)
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("schema") != self.schema:
            raise RuntimeError("G0_RECOVERY_SCHEMA_INVALID")
        if payload.get("run_id") != run_id or payload.get("device_id") != device_id:
            raise RuntimeError("G0_RECOVERY_IDENTITY_MISMATCH")
        if payload.get("module") != "voipUserInfo" or payload.get("field") != "disName":
            raise RuntimeError("G0_RECOVERY_SCOPE_INVALID")
        return str(payload.get("original_disname") or "")
