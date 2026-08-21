from __future__ import annotations

import subprocess
from pathlib import Path


class GitSyncError(RuntimeError):
    pass


class GitControlSync:
    """Small audited Git transport. It never stages the whole worktree.

    A control Runner is allowed to repair only divergence created by its own
    validation/control result commits. Product/source commits are never rebased
    automatically. Every Git subprocess has a bounded timeout so a live systemd
    process cannot hang forever inside fetch/push.
    """

    CONTROL_PREFIX = "validation/control/"

    def __init__(self, repo_root: Path, *, remote: str = "origin", branch: str,
                 command_timeout_seconds: float = 30.0):
        self.repo_root = repo_root.resolve()
        self.remote = remote
        self.branch = branch
        self.command_timeout_seconds = float(command_timeout_seconds)
        if self.command_timeout_seconds <= 0:
            raise ValueError("GIT_COMMAND_TIMEOUT_INVALID")

    def _run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            cp = subprocess.run(
                ["git", *args],
                cwd=self.repo_root,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=self.command_timeout_seconds,
            )
        except subprocess.TimeoutExpired as exc:
            raise GitSyncError(
                f"GIT_COMMAND_TIMEOUT:{' '.join(args)}:{self.command_timeout_seconds:g}s"
            ) from exc
        if check and cp.returncode != 0:
            raise GitSyncError(f"git {' '.join(args)} failed: {cp.stderr.strip()}")
        return cp

    def _control_only_local_commits(self, remote_ref: str) -> list[str]:
        commits = [
            line.strip()
            for line in self._run("log", "--format=%H", f"{remote_ref}..HEAD").stdout.splitlines()
            if line.strip()
        ]
        for commit in commits:
            paths = [
                line.strip()
                for line in self._run(
                    "diff-tree", "--no-commit-id", "--name-only", "-r", commit
                ).stdout.splitlines()
                if line.strip()
            ]
            bad = [path for path in paths if not path.startswith(self.CONTROL_PREFIX)]
            if bad:
                raise GitSyncError(
                    "CONTROL_DIVERGENCE_TOUCHED_PRODUCT_CODE:" + ",".join(bad[:20])
                )
        return commits

    def _discard_republish_status_noise(self) -> None:
        """Drop only the post-push diagnostic status edit from an old Runner.

        `_terminal()` commits result.json + status.json before pushing. If that
        push failed, `_maybe_push()` may then append `git_sync_error` to the
        working-tree status.json. The durable result remains in the local commit;
        this uncommitted status-only diagnostic can be regenerated after rebase.
        No other dirty path is discarded automatically.
        """
        dirty = [line for line in self._run("status", "--porcelain", "--untracked-files=all").stdout.splitlines() if line]
        if not dirty:
            return
        paths = []
        for line in dirty:
            path = line[3:].strip().strip('"') if len(line) >= 4 else ""
            paths.append(path)
        allowed = {"validation/control/status.json"}
        if any(path not in allowed for path in paths):
            raise GitSyncError("CONTROL_REBASE_DIRTY_WORKTREE:" + ",".join(paths[:20]))
        self._run("restore", "--worktree", "--", "validation/control/status.json")

    def _rebase_control_results_and_push(self, remote_ref: str) -> str:
        local_commits = self._control_only_local_commits(remote_ref)
        if not local_commits:
            raise GitSyncError("CONTROL_NON_FF_WITHOUT_LOCAL_RESULT_COMMITS")
        self._discard_republish_status_noise()
        rebase = self._run("rebase", remote_ref, check=False)
        if rebase.returncode != 0:
            self._run("rebase", "--abort", check=False)
            raise GitSyncError(f"CONTROL_RESULT_REBASE_FAILED:{rebase.stderr.strip()}")
        push = self._run("push", self.remote, f"HEAD:{self.branch}", check=False)
        if push.returncode != 0:
            raise GitSyncError(f"CONTROL_RESULT_REPUBLISH_FAILED:{push.stderr.strip()}")
        return self._run("rev-parse", "HEAD").stdout.strip()

    def pull_ff_only(self) -> str:
        self._run("fetch", self.remote, self.branch)
        current = self._run("branch", "--show-current").stdout.strip()
        if current != self.branch:
            raise GitSyncError(f"CONTROL_BRANCH_MISMATCH:{current}:{self.branch}")
        remote_ref = f"{self.remote}/{self.branch}"
        merge = self._run("merge", "--ff-only", remote_ref, check=False)
        if merge.returncode != 0:
            # The only auto-repairable split is: Runner has local result commits,
            # controller has newer validation/control action commits. Rebase and
            # republish those exact result commits. Any product path fails closed.
            self._rebase_control_results_and_push(remote_ref)
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
        push = self._run("push", self.remote, f"HEAD:{self.branch}", check=False)
        if push.returncode == 0:
            return sha

        # A controller may have advanced only validation/control/next_action.json
        # between our fetch and result push. Fetch, prove every local-only commit
        # is control-only, rebase, and retry once. Never rebase product code here.
        self._run("fetch", self.remote, self.branch)
        return self._rebase_control_results_and_push(f"{self.remote}/{self.branch}")
