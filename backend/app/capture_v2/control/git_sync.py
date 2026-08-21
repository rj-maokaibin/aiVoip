from __future__ import annotations

import subprocess
from pathlib import Path


class GitSyncError(RuntimeError):
    pass


class GitControlSync:
    """Small audited Git transport. It never stages the whole worktree."""

    def __init__(self, repo_root: Path, *, remote: str = "origin", branch: str):
        self.repo_root = repo_root.resolve()
        self.remote = remote
        self.branch = branch

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        cp = subprocess.run(["git", *args], cwd=self.repo_root, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        if check and cp.returncode != 0:
            raise GitSyncError(f"git {' '.join(args)} failed: {cp.stderr.strip()}")
        return cp

    def pull_ff_only(self) -> str:
        self._run("fetch", self.remote, self.branch)
        current = self._run("branch", "--show-current").stdout.strip()
        if current != self.branch:
            raise GitSyncError(f"CONTROL_BRANCH_MISMATCH:{current}:{self.branch}")
        self._run("merge", "--ff-only", f"{self.remote}/{self.branch}")
        return self._run("rev-parse", "HEAD").stdout.strip()

    def commit_and_push(self, paths: list[Path], *, message: str) -> str:
        rels: list[str] = []
        for path in paths:
            resolved = path.resolve()
            try:
                rel = resolved.relative_to(self.repo_root)
            except ValueError as exc:
                raise GitSyncError(f"PATH_OUTSIDE_REPO:{path}") from exc
            rels.append(str(rel))
        if not rels:
            raise GitSyncError("NO_RESULT_PATHS")
        self._run("add", "--", *rels)
        staged = self._run("diff", "--cached", "--name-only").stdout.splitlines()
        unexpected = sorted(set(staged) - set(rels))
        if unexpected:
            raise GitSyncError("UNEXPECTED_STAGED_PATHS:" + ",".join(unexpected))
        if not staged:
            return self._run("rev-parse", "HEAD").stdout.strip()
        self._run("commit", "-m", message)
        sha = self._run("rev-parse", "HEAD").stdout.strip()
        self._run("push", self.remote, f"HEAD:{self.branch}")
        return sha
