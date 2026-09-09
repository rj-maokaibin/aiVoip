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

    def verify_mutation_contract(self) -> FusionPbxSourceFenceResult:
        result = self.verify()
        root = Path(self.profile.fusionpbx_root)
        required = {
            "resources/classes/database.php",
            "resources/classes/permissions.php",
            "resources/classes/event_socket.php",
            "resources/classes/cache.php",
            "app/extensions/resources/classes/extension.php",
            "app/extensions/extension_copy.php",
            "app/switch/resources/scripts/resources/functions/config.lua",
            "app/switch/resources/scripts/resources/functions/cache.lua",
            "app/switch/resources/scripts/resources/functions/xml.lua",
            "app/switch/resources/scripts/app/xml_handler/resources/scripts/directory/directory.lua",
        }
        if not required.issubset(self.profile.source_hashes):
            raise FusionPbxSourceFenceError("PBX_PROVIDER_SOURCE_FENCE_FAILED")
        database_text = (root / "resources/classes/database.php").read_text(encoding="utf-8", errors="ignore")
        permissions_text = (root / "resources/classes/permissions.php").read_text(encoding="utf-8", errors="ignore")
        event_socket_text = (root / "resources/classes/event_socket.php").read_text(encoding="utf-8", errors="ignore")
        cache_text = (root / "resources/classes/cache.php").read_text(encoding="utf-8", errors="ignore")
        lua_config_text = (
            root / "app/switch/resources/scripts/resources/functions/config.lua"
        ).read_text(encoding="utf-8", errors="ignore")
        lua_cache_text = (
            root / "app/switch/resources/scripts/resources/functions/cache.lua"
        ).read_text(encoding="utf-8", errors="ignore")
        lua_xml_text = (
            root / "app/switch/resources/scripts/resources/functions/xml.lua"
        ).read_text(encoding="utf-8", errors="ignore")
        directory_lua_text = (
            root / "app/switch/resources/scripts/app/xml_handler/resources/scripts/directory/directory.lua"
        ).read_text(encoding="utf-8", errors="ignore")
        facts = {
            "database_save_method": bool(re.search(r"public\s+function\s+save\s*\(", database_text)),
            "database_delete_method": bool(re.search(r"public\s+function\s+delete\s*\(", database_text)),
            "permissions_add_method": bool(re.search(r"public\s+function\s+add\s*\(", permissions_text)),
            "permissions_delete_method": bool(re.search(r"public\s+function\s+delete\s*\(", permissions_text)),
            "event_socket_api_method": bool(re.search(r"public\s+static\s+function\s+api\s*\(", event_socket_text)),
            "cache_delete_method": bool(re.search(r"public\s+function\s+delete\s*\(", cache_text)),
            "lua_cache_method_from_config": 'if (k == "cache.method")' in lua_config_text,
            "lua_cache_file_key_mapping": "key = key2file(key)" in lua_cache_text,
            "lua_cache_file_delete": "File.remove(key)" in lua_cache_text,
            "lua_xml_sanitize_strips_dollar": '["$"] = ""' in lua_xml_text,
            "lua_xml_sanitize_escapes_apostrophe": '["\'"] = "&apos;"' in lua_xml_text,
            "directory_cache_key_uses_domain_name": (
                '"directory:" .. (from_user or user) .. "@" .. domain_name' in directory_lua_text
            ),
        }
        if not all(facts.values()):
            raise FusionPbxSourceFenceError("PBX_PROVIDER_SOURCE_FENCE_FAILED")
        return FusionPbxSourceFenceResult(
            ok=True,
            version=result.version,
            file_hashes=result.file_hashes,
            contract_facts={**result.contract_facts, **facts},
            mismatches=(),
        )
