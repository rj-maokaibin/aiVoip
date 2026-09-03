#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import os
import re
import stat
import tempfile
from pathlib import Path

LEGACY_WRAPPER_SHA256 = {
    "77b2b30e448b1600a56e476dae9c359617d87706e1bf48e549ac4d4d35635edb",
}
BUILD_REVISION_RE = re.compile(rb"^[ \t]*(?:export[ \t]+)?BUILD_REVISION[ \t]*=", re.ASCII)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def normalize_persistent_env(path: Path) -> int:
    st = path.stat()
    mode = stat.S_IMODE(st.st_mode)
    if mode & 0o077:
        raise RuntimeError(f"persistent production env is not private: {path} mode={mode:04o}")
    raw = path.read_bytes()
    lines = raw.splitlines(keepends=True)
    kept = [line for line in lines if not BUILD_REVISION_RE.match(line)]
    removed = len(lines) - len(kept)
    if removed == 0:
        return 0

    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(b"".join(kept))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.chown(tmp, st.st_uid, st.st_gid)
        os.replace(tmp, path)
        dir_fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if tmp.exists():
            tmp.unlink()
    return removed


def install_wrapper(source: Path, target: Path) -> None:
    data = source.read_bytes()
    fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, 0o755)
        if os.geteuid() == 0:
            os.chown(tmp, 0, 0)
        os.replace(tmp, target)
        dir_fd = os.open(target.parent, os.O_DIRECTORY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if tmp.exists():
            tmp.unlink()


def sync(source: Path, target: Path, env_file: Path) -> tuple[str, int, str]:
    source_hash = digest(source)
    current_hash = digest(target) if target.exists() else "missing"

    if current_hash == source_hash:
        removed = normalize_persistent_env(env_file)
        mode = "CURRENT_NORMALIZED" if removed else "CURRENT"
        return mode, removed, source_hash

    if current_hash != "missing" and current_hash not in LEGACY_WRAPPER_SHA256:
        raise RuntimeError(
            "refusing to replace unknown privileged production wrapper: "
            f"observed_sha256={current_hash} expected_current={source_hash} "
            f"allowed_legacy={sorted(LEGACY_WRAPPER_SHA256)}"
        )

    removed = normalize_persistent_env(env_file)
    install_wrapper(source, target)
    if digest(target) != source_hash:
        raise RuntimeError("privileged production wrapper post-install digest mismatch")
    return "MIGRATED", removed, source_hash


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", required=True, type=Path)
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--env-file", required=True, type=Path)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise SystemExit("ERROR: production wrapper synchronization requires root")
    try:
        mode, removed, source_hash = sync(
            args.source.resolve(), args.target.resolve(), args.env_file.resolve()
        )
    except Exception as exc:
        print(f"PRODUCTION_WRAPPER_SYNC=FAIL reason={exc}", file=__import__("sys").stderr)
        return 2
    print(
        "PRODUCTION_WRAPPER_SYNC=PASS "
        f"mode={mode} source_sha256={source_hash} "
        f"persistent_build_revision_removed={removed}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
