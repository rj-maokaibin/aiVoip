#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

SHA40 = re.compile(r"^[0-9a-f]{40}$")
SCHEMA = "voip-production-exact-tree-transport-v1"


def run(args: list[str], *, cwd: Path, input_text: str | None = None) -> str:
    cp = subprocess.run(args, cwd=cwd, input=input_text, text=True, capture_output=True, check=True)
    return cp.stdout.strip()


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def load_manifest(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("files")
    if not isinstance(rows, list) or not rows:
        raise ValueError("manifest files must be a non-empty list")
    paths: list[str] = []
    for row in rows:
        p = str(row.get("path") or "") if isinstance(row, dict) else ""
        pp = Path(p)
        if not p or pp.is_absolute() or ".." in pp.parts:
            raise ValueError(f"unsafe manifest path: {p!r}")
        paths.append(p.replace("\\", "/"))
    if len(paths) != len(set(paths)):
        raise ValueError("manifest contains duplicate paths")
    return payload


def build(args: argparse.Namespace) -> int:
    root = args.repo.resolve()
    revision = args.revision
    if not SHA40.fullmatch(revision):
        raise ValueError("revision must be a lowercase 40-char SHA")
    head = run(["git", "rev-parse", "HEAD"], cwd=root)
    if head != revision:
        raise ValueError(f"HEAD mismatch: {head} != {revision}")
    if run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=root):
        raise ValueError("tracked source is dirty")

    manifest = args.manifest.resolve()
    payload = load_manifest(manifest)
    subprocess.run(
        ["python3", "tools/source_manifest_gate.py", "--expected", str(manifest)],
        cwd=root,
        check=True,
    )
    selected = {row["path"].replace("\\", "/") for row in payload["files"]}

    object_ids = {revision, run(["git", "rev-parse", f"{revision}^{{tree}}"], cwd=root)}
    blobs: dict[str, str] = {}
    for line in run(["git", "ls-tree", "-r", "-t", "--full-tree", revision], cwd=root).splitlines():
        meta, path = line.split("\t", 1)
        mode, kind, oid = meta.split()
        path = path.replace("\\", "/")
        if kind == "tree":
            object_ids.add(oid)
        elif kind == "blob":
            blobs[path] = oid
    missing = sorted(selected - blobs.keys())
    if missing:
        raise ValueError(f"manifest paths missing from exact Git tree: {missing[:10]}")
    for path in selected:
        object_ids.add(blobs[path])

    args.pack.parent.mkdir(parents=True, exist_ok=True)
    oid_input = "\n".join(sorted(object_ids)) + "\n"
    with args.pack.open("wb") as f:
        subprocess.run(
            ["git", "pack-objects", "--stdout", "--compression=9"],
            cwd=root,
            input=oid_input.encode(),
            stdout=f,
            stderr=subprocess.PIPE,
            check=True,
        )
    args.sparse_paths.parent.mkdir(parents=True, exist_ok=True)
    args.sparse_paths.write_text("\n".join(sorted(selected)) + "\n", encoding="utf-8")
    identity = {
        "schema_version": SCHEMA,
        "revision": revision,
        "root_tree": run(["git", "rev-parse", f"{revision}^{{tree}}"], cwd=root),
        "source_manifest_aggregate_sha256": payload.get("aggregate_sha256"),
        "source_manifest_file_count": payload.get("file_count"),
        "transport_object_count": len(object_ids),
        "transport_file_count": len(selected),
        "pack_sha256": sha256(args.pack),
        "sparse_paths_sha256": sha256(args.sparse_paths),
        "transport_tool_sha256": sha256(root / "tools" / "exact_tree_transport.py"),
        "pack_bytes": args.pack.stat().st_size,
        "history_commits_transported": 0,
        "transport_mode": "EXACT_COMMIT_SPARSE_OBJECT_PACK",
    }
    args.identity.parent.mkdir(parents=True, exist_ok=True)
    args.identity.write_text(json.dumps(identity, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "PASS", **identity}, indent=2))
    return 0


def materialize(args: argparse.Namespace) -> int:
    revision = args.revision
    if not SHA40.fullmatch(revision):
        raise ValueError("revision must be a lowercase 40-char SHA")
    manifest = args.manifest.resolve()
    payload = load_manifest(manifest)
    identity = json.loads(args.identity.read_text(encoding="utf-8"))
    if identity.get("schema_version") != SCHEMA:
        raise ValueError("transport identity schema mismatch")
    if identity.get("revision") != revision:
        raise ValueError("transport revision mismatch")
    if identity.get("source_manifest_aggregate_sha256") != payload.get("aggregate_sha256"):
        raise ValueError("transport manifest aggregate mismatch")
    if identity.get("source_manifest_file_count") != payload.get("file_count"):
        raise ValueError("transport manifest file_count mismatch")
    if identity.get("pack_sha256") != sha256(args.pack):
        raise ValueError("transport pack digest mismatch")
    if identity.get("sparse_paths_sha256") != sha256(args.sparse_paths):
        raise ValueError("transport sparse path digest mismatch")
    if identity.get("transport_tool_sha256") != sha256(Path(__file__).resolve()):
        raise ValueError("transport tool digest mismatch")

    selected = [line.strip() for line in args.sparse_paths.read_text(encoding="utf-8").splitlines() if line.strip()]
    expected_selected = sorted(row["path"].replace("\\", "/") for row in payload["files"])
    if sorted(selected) != expected_selected:
        raise ValueError("transport sparse paths do not exactly match manifest files")

    dest = args.dest.resolve()
    if dest.exists() and any(dest.iterdir()):
        raise ValueError(f"destination is not empty: {dest}")
    dest.mkdir(parents=True, exist_ok=True)
    run(["git", "init", "-q"], cwd=dest)
    with args.pack.open("rb") as f:
        subprocess.run(["git", "index-pack", "--stdin"], cwd=dest, stdin=f, check=True, stdout=subprocess.DEVNULL)
    run(["git", "update-ref", "refs/heads/exact-source", revision], cwd=dest)
    run(["git", "symbolic-ref", "HEAD", "refs/heads/exact-source"], cwd=dest)
    run(["git", "config", "core.sparseCheckout", "true"], cwd=dest)
    run(["git", "config", "core.sparseCheckoutCone", "false"], cwd=dest)
    info = dest / ".git" / "info"
    info.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(args.sparse_paths, info / "sparse-checkout")
    run(["git", "read-tree", "-mu", "HEAD"], cwd=dest)
    run(["git", "update-ref", "refs/remotes/origin/master", revision], cwd=dest)
    if run(["git", "rev-parse", "HEAD"], cwd=dest) != revision:
        raise ValueError("materialized HEAD mismatch")
    if run(["git", "rev-parse", "refs/remotes/origin/master"], cwd=dest) != revision:
        raise ValueError("materialized origin/master mismatch")
    if run(["git", "status", "--porcelain", "--untracked-files=no"], cwd=dest):
        raise ValueError("materialized tracked source is dirty")
    for path in expected_selected:
        if not (dest / path).exists() and not (dest / path).is_symlink():
            raise ValueError(f"materialized manifest file missing: {path}")
    if sha256(dest / "tools" / "exact_tree_transport.py") != identity.get("transport_tool_sha256"):
        raise ValueError("materialized transport tool digest mismatch")
    subprocess.run(
        ["python3", "tools/source_manifest_gate.py", "--expected", str(manifest)],
        cwd=dest,
        check=True,
    )
    print(json.dumps({
        "status": "PASS",
        "schema_version": SCHEMA,
        "revision": revision,
        "transport_mode": identity.get("transport_mode"),
        "pack_bytes": identity.get("pack_bytes"),
        "materialized_file_count": len(expected_selected),
        "source_manifest_aggregate_sha256": payload.get("aggregate_sha256"),
    }, indent=2))
    return 0


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Build/materialize history-free exact Git source transport bound to derived manifest")
    sub = p.add_subparsers(dest="cmd", required=True)
    b = sub.add_parser("build")
    b.add_argument("--repo", type=Path, default=Path.cwd())
    b.add_argument("--revision", required=True)
    b.add_argument("--manifest", type=Path, required=True)
    b.add_argument("--pack", type=Path, required=True)
    b.add_argument("--sparse-paths", type=Path, required=True)
    b.add_argument("--identity", type=Path, required=True)
    b.set_defaults(func=build)
    m = sub.add_parser("materialize")
    m.add_argument("--revision", required=True)
    m.add_argument("--manifest", type=Path, required=True)
    m.add_argument("--pack", type=Path, required=True)
    m.add_argument("--sparse-paths", type=Path, required=True)
    m.add_argument("--identity", type=Path, required=True)
    m.add_argument("--dest", type=Path, required=True)
    m.set_defaults(func=materialize)
    return p


def main() -> int:
    args = parser().parse_args()
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
