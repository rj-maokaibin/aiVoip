import fcntl

from app.capture_v2.control.runner import RemoteValidationRunner


def test_duplicate_runner_skips_poll_when_host_lock_is_held(monkeypatch, tmp_path):
    runner = RemoteValidationRunner(repo_root=tmp_path, git_sync=False, runner_id="test:1")
    calls = []
    monkeypatch.setattr(runner, "_process_once_locked", lambda: calls.append("ran") or "SENTINEL")

    with runner.process_lock_path.open("a+", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert runner.process_once() is None
        assert calls == []
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_UN)

    assert runner.process_once() == "SENTINEL"
    assert calls == ["ran"]
