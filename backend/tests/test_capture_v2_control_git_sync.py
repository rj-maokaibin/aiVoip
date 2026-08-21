import subprocess
from pathlib import Path

import pytest

from app.capture_v2.control.git_sync import GitControlSync, GitSyncError


def test_commit_sync_rejects_path_outside_repo(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    sync = GitControlSync(tmp_path, branch="main")
    outside = tmp_path.parent / "outside.txt"
    outside.write_text("x")
    with pytest.raises(GitSyncError, match="PATH_OUTSIDE_REPO"):
        sync.commit_and_push([outside], message="no")
