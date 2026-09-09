from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def run(*args: str, cwd: Path = ROOT) -> str:
    cp = subprocess.run(args, cwd=cwd, text=True, capture_output=True, check=True)
    return cp.stdout.strip()


def test_production_exact_tree_transport_round_trip(tmp_path: Path) -> None:
    revision = run("git", "rev-parse", "HEAD")
    manifest = tmp_path / "source_manifest_expected.json"
    pack = tmp_path / "source.pack"
    sparse = tmp_path / "source.paths"
    identity = tmp_path / "source.identity.json"
    dest = tmp_path / "materialized"

    run("python3", "tools/source_manifest_gate.py", "--write", str(manifest))
    manifest_payload = json.loads(manifest.read_text(encoding="utf-8"))
    manifest_paths = {row["path"] for row in manifest_payload["files"]}
    assert "frontend/index.html" in manifest_paths
    assert "frontend/evidence-report.html" in manifest_paths

    run(
        "python3", "tools/exact_tree_transport.py", "build",
        "--repo", str(ROOT),
        "--revision", revision,
        "--manifest", str(manifest),
        "--pack", str(pack),
        "--sparse-paths", str(sparse),
        "--identity", str(identity),
    )
    payload = json.loads(identity.read_text(encoding="utf-8"))
    assert payload["revision"] == revision
    assert payload["history_commits_transported"] == 0
    assert payload["transport_mode"] == "EXACT_COMMIT_SPARSE_OBJECT_PACK"
    assert payload["transport_file_count"] == manifest_payload["file_count"]

    run(
        "python3", str(ROOT / "tools" / "exact_tree_transport.py"), "materialize",
        "--revision", revision,
        "--manifest", str(manifest),
        "--pack", str(pack),
        "--sparse-paths", str(sparse),
        "--identity", str(identity),
        "--dest", str(dest),
    )
    assert run("git", "rev-parse", "HEAD", cwd=dest) == revision
    assert run("git", "rev-parse", "refs/remotes/origin/master", cwd=dest) == revision
    assert run("git", "status", "--porcelain", "--untracked-files=no", cwd=dest) == ""
    assert (dest / "frontend" / "index.html").is_file()
    assert (dest / "frontend" / "evidence-report.html").is_file()
