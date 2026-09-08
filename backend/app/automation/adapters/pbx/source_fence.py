from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from app.automation.adapters.pbx.profile import FusionPbxLabProfile


class FusionPbxSourceFenceError(RuntimeError):
    pass


@dataclass(frozen=True)
class FusionPbxSourceFenceResult:
    ok: bool
    version: str
    file_hashes: dict[str, str]
    contract_facts: dict[str, bool]
    mismatches: tuple[str, ...]

    def safe_dict(self) -> dict:
        return {
            "ok": self.ok,
            "version": self.version,
            "file_hashes": dict(self.file_hashes),
            "contract_facts": dict(self.contract_facts),
            "mismatches": list(self.mismatches),
            "mutation_executed": False,
            "secret_values_emitted": False,
        }


class FusionPbxSourceFence:
    def __init__(self, profile: FusionPbxLabProfile) -> None:
        self.profile = profile

    @staticmethod
    def _sha256(path: Path) -> str:
        h = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()

    def inspect(self) -> FusionPbxSourceFenceResult:
        root = Path(self.profile.fusionpbx_root)
        actual: dict[str, str] = {}
        mismatches: list[str] = []
        for rel, expected in sorted(self.profile.source_hashes.items()):
            path = root / rel
            if not path.is_file():
                mismatches.append(f"missing:{rel}")
                continue
            digest = self._sha256(path)
            actual[rel] = digest
            if digest != expected:
                mismatches.append(f"sha256:{rel}")

        ext_path = root / "app/extensions/resources/classes/extension.php"
        ext_text = ext_path.read_text(encoding="utf-8", errors="ignore") if ext_path.is_file() else ""
        facts = {
            "extension_exists_method": bool(re.search(r"public\s+function\s+exists\s*\(", ext_text)),
            "extension_delete_method": bool(re.search(r"public\s+function\s+delete\s*\(", ext_text)),
            "exists_checks_number_alias": "number_alias = :extension" in ext_text,
        }
        for key, ok in facts.items():
            if not ok:
                mismatches.append(f"contract:{key}")
        return FusionPbxSourceFenceResult(
            ok=not mismatches,
            version=self.profile.source_fence_version,
            file_hashes=actual,
            contract_facts=facts,
            mismatches=tuple(mismatches),
        )

    def verify(self) -> FusionPbxSourceFenceResult:
        result = self.inspect()
        if not result.ok:
            raise FusionPbxSourceFenceError("PBX_PROVIDER_SOURCE_FENCE_FAILED")
        return result
