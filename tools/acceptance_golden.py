#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import tempfile
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = Path(os.environ.get("VOIP_ACCEPTANCE_ROOT", "/opt/voip-acceptance"))
DEFAULT_MANIFEST = REPO_ROOT / "golden_registry/real_offline_001/manifest.json"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_manifest(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") != 1:
        raise RuntimeError("GOLDEN_MANIFEST_SCHEMA_UNSUPPORTED")
    expected = str(data.get("artifacts", {}).get("pcap", {}).get("sha256") or "")
    if len(expected) != 64:
        raise RuntimeError("GOLDEN_MANIFEST_SHA256_INVALID")
    return data


def cache_path(root: Path, manifest: dict) -> Path:
    pcap = manifest["artifacts"]["pcap"]
    return root / "golden-cache" / manifest["golden_id"] / manifest["version"] / pcap["cache_name"]


def verify(path: Path, expected_sha: str) -> tuple[bool, str]:
    if not path.is_file():
        return False, "MISSING"
    actual = sha256_file(path)
    return actual == expected_sha, actual


def _copy_source(source: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as temp:
        temp_path = Path(temp.name)
    try:
        if source.startswith(("https://", "http://")):
            with urllib.request.urlopen(source, timeout=60) as response, temp_path.open("wb") as out:
                shutil.copyfileobj(response, out)
        else:
            src = Path(source).expanduser()
            if not src.is_file():
                raise RuntimeError(f"GOLDEN_SOURCE_NOT_FOUND:{src}")
            shutil.copyfile(src, temp_path)
        os.chmod(temp_path, 0o444)
        os.replace(temp_path, destination)
    finally:
        temp_path.unlink(missing_ok=True)


def ensure(root: Path, manifest_path: Path, source: str | None) -> dict:
    manifest = load_manifest(manifest_path)
    expected = manifest["artifacts"]["pcap"]["sha256"]
    target = cache_path(root, manifest)
    ok, actual = verify(target, expected)
    if ok:
        return {"status": "PASS", "cache_hit": True, "golden_id": manifest["golden_id"], "version": manifest["version"], "path": str(target), "sha256": actual}
    if target.exists():
        quarantine = target.with_name(target.name + ".corrupt")
        target.replace(quarantine)
    resolved_source = source or os.environ.get("VOIP_GOLDEN_001_SOURCE")
    if not resolved_source:
        raise RuntimeError("GOLDEN_CACHE_MISS_NO_REGISTRY_SOURCE")
    _copy_source(resolved_source, target)
    ok, actual = verify(target, expected)
    if not ok:
        target.unlink(missing_ok=True)
        raise RuntimeError(f"GOLDEN_SHA256_MISMATCH:{actual}")
    return {"status": "PASS", "cache_hit": False, "golden_id": manifest["golden_id"], "version": manifest["version"], "path": str(target), "sha256": actual}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["ensure", "verify", "path"])
    parser.add_argument("--root", default=str(DEFAULT_ROOT))
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--source")
    args = parser.parse_args()
    root = Path(args.root)
    manifest_path = Path(args.manifest)
    try:
        manifest = load_manifest(manifest_path)
        target = cache_path(root, manifest)
        if args.command == "path":
            print(target)
            return 0
        if args.command == "verify":
            ok, actual = verify(target, manifest["artifacts"]["pcap"]["sha256"])
            print(json.dumps({"status": "PASS" if ok else "FAIL", "path": str(target), "sha256": actual}, ensure_ascii=False))
            return 0 if ok else 2
        print(json.dumps(ensure(root, manifest_path, args.source), ensure_ascii=False))
        return 0
    except Exception as exc:
        print(json.dumps({"status": "FAIL", "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
